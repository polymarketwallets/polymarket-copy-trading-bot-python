"""The bot's own memory: open positions, what it already decided, orders and exits still in flight, today's spend.
Same file format as the Node bot (state.<mode>.json, decisions.<mode>.jsonl, lock.<mode>) — the two implementations
can take over each other's data directory, and exclude each other while running."""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MAX_REMEMBERED = 20_000


def pos_key(target: str, token_id: str) -> str:
    return f"{target}|{token_id}"


def _iso(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _trim(lst: list[str], index: set[str]) -> None:
    if len(lst) <= MAX_REMEMBERED:
        return
    cut = len(lst) - MAX_REMEMBERED
    for x in lst[:cut]:
        index.discard(x)
    del lst[:cut]


class BotState:
    """Positions (keyed per target: target A's SELL never sells what we bought following target B; amounts are
    micro-unit strings); pending orders — written BEFORE an order is sent (orderId None until answered) and removed
    only once its fill or absence is established; pending exits — a target left and we still have to."""

    def __init__(self, directory: str, mode: str) -> None:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        self.file = d / f"state.{mode}.json"
        self.decisions_file = d / f"decisions.{mode}.jsonl"
        raw: dict[str, Any] = json.loads(self.file.read_text("utf8")) if self.file.exists() else {}
        self.data: dict[str, Any] = {
            "version": 1,
            "positions": raw.get("positions") or {},
            "processed": raw.get("processed") or [],
            "handledTx": raw.get("handledTx") or [],
            "spend": raw.get("spend") or {"day": "", "usdc": "0"},
            "pendingOrders": raw.get("pendingOrders") or [],
            "pendingExits": raw.get("pendingExits") or [],
            "bookedOrderIds": raw.get("bookedOrderIds") or [],
        }
        self._processed = set(self.data["processed"])
        self._handled_tx = set(self.data["handledTx"])
        self._booked = set(self.data["bookedOrderIds"])

    def save(self) -> None:
        _trim(self.data["processed"], self._processed)
        _trim(self.data["handledTx"], self._handled_tx)
        _trim(self.data["bookedOrderIds"], self._booked)
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.file)

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str) -> None:
        if event_id in self._processed:
            return
        self._processed.add(event_id)
        self.data["processed"].append(event_id)

    def is_handled_tx(self, key: str) -> bool:
        return key in self._handled_tx

    def mark_handled_tx(self, key: str) -> None:
        if key in self._handled_tx:
            return
        self._handled_tx.add(key)
        self.data["handledTx"].append(key)

    def is_booked(self, order_id: str) -> bool:
        return order_id.lower() in self._booked

    def mark_booked(self, order_id: str) -> None:
        oid = (order_id or "").lower()
        if not oid or oid in self._booked:
            return
        self._booked.add(oid)
        self.data["bookedOrderIds"].append(oid)

    def position(self, target: str, token_id: str) -> Optional[dict[str, Any]]:
        return self.data["positions"].get(pos_key(target, token_id))

    def positions(self) -> list[dict[str, Any]]:
        return list(self.data["positions"].values())

    def shares_held_by_others(self, target: str, token_id: str) -> int:
        """shares of this token booked to targets other than `target`"""
        return sum(int(p["shares"]) for p in self.positions() if p["tokenId"] == token_id and p["target"] != target)

    def add_buy(self, p: dict[str, Any], shares: int, usdc: int) -> dict[str, Any]:
        key = pos_key(p["target"], p["tokenId"])
        cur = self.data["positions"].get(key)
        if cur:
            nxt = {**cur, "shares": str(int(cur["shares"]) + shares), "costUsdc": str(int(cur["costUsdc"]) + usdc), "buyCount": cur["buyCount"] + 1}
        else:
            nxt = {**{k: v for k, v in p.items() if v is not None}, "shares": str(shares), "costUsdc": str(usdc), "buyCount": 1, "openedAt": _iso()}
        self.data["positions"][key] = nxt
        return nxt

    def reduce(self, target: str, token_id: str, shares: int) -> None:
        """Reduce by what was sold; the position is dropped once nothing sellable is left."""
        key = pos_key(target, token_id)
        cur = self.data["positions"].get(key)
        if not cur:
            return
        held = int(cur["shares"])
        left = held - shares
        if left <= 100:  # < 1e-4 share: dust
            del self.data["positions"][key]
            return
        cost = int(cur["costUsdc"]) * left // held  # the remaining position keeps its average price
        self.data["positions"][key] = {**cur, "shares": str(left), "costUsdc": str(cost)}

    def drop(self, target: str, token_id: str) -> None:
        self.data["positions"].pop(pos_key(target, token_id), None)

    # ── orders whose result is not known yet
    def pending_orders(self) -> list[dict[str, Any]]:
        return self.data["pendingOrders"]

    def add_pending_order(self, p: dict[str, Any]) -> None:
        self.data["pendingOrders"] = [x for x in self.data["pendingOrders"] if x["key"] != p["key"]] + [p]

    def update_pending_order(self, key: str, **patch: Any) -> None:
        self.data["pendingOrders"] = [{**x, **patch} if x["key"] == key else x for x in self.data["pendingOrders"]]

    def remove_pending_order(self, key: str) -> None:
        self.data["pendingOrders"] = [x for x in self.data["pendingOrders"] if x["key"] != key]

    # ── exits still to do
    def pending_exits(self) -> list[dict[str, Any]]:
        return self.data["pendingExits"]

    def add_pending_exit(self, e: dict[str, Any]) -> None:
        """one exit per (target, token) — a second SELL while one is queued changes nothing"""
        if any(x["target"] == e["target"] and x["tokenId"] == e["tokenId"] for x in self.data["pendingExits"]):
            return
        self.data["pendingExits"].append(e)

    def update_pending_exit(self, target: str, token_id: str, **patch: Any) -> None:
        self.data["pendingExits"] = [{**x, **patch} if x["target"] == target and x["tokenId"] == token_id else x for x in self.data["pendingExits"]]

    def remove_pending_exit(self, target: str, token_id: str) -> None:
        self.data["pendingExits"] = [x for x in self.data["pendingExits"] if not (x["target"] == target and x["tokenId"] == token_id)]

    def spent_today(self, now: Optional[datetime] = None) -> int:
        s = self.data["spend"]
        return int(s["usdc"]) if s["day"] == _day(now) else 0

    def add_spend(self, usdc: int, now: Optional[datetime] = None) -> None:
        day = _day(now)
        s = self.data["spend"]
        base = int(s["usdc"]) if s["day"] == day else 0
        self.data["spend"] = {"day": day, "usdc": str(base + usdc)}

    def log_decision(self, entry: dict[str, Any]) -> None:
        """Append-only audit trail: one line per decision, including every skip and its reason."""
        with open(self.decisions_file, "a", encoding="utf8") as f:
            f.write(json.dumps({"at": _iso(), **entry}, default=str) + "\n")


def _alive(pid: int) -> bool:
    """Is `pid` a running process on this machine? On Windows os.kill(pid, 0) would TERMINATE the process, so an
    existing lock is treated as held there (delete the file by hand after a crash)."""
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class InstanceLock:
    """One bot per data directory and mode. Two instances on the same state would each think a fill is new and each
    send an order; the lock makes the second refuse to start. A lock left by a crashed process on this machine is
    taken over; one held by a live process, or by another host, is not. Same file as the Node bot's lock."""

    def __init__(self, directory: str, mode: str) -> None:
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.file = Path(directory) / f"lock.{mode}"
        self._held = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as f:
                    f.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "startedAt": _iso()}))
                self._held = True
                return
            owner: dict[str, Any] = {}
            try:
                owner = json.loads(self.file.read_text("utf8"))
            except (OSError, ValueError):
                pass  # unreadable: treat as held
            pid = owner.get("pid")
            if owner.get("host") == socket.gethostname() and isinstance(pid, int) and not isinstance(pid, bool) and not _alive(pid):
                try:
                    self.file.unlink()
                except OSError:
                    pass  # raced with another starter; the retry decides
                continue
            raise RuntimeError(f"another pmwallets-copytrade is running on this data directory ({self.file}: pid {pid if pid is not None else '?'} "
                               f"on {owner.get('host') or '?'}). Stop it first; if it is really gone, delete the lock file.")
        raise RuntimeError(f"could not take {self.file}")

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.file.unlink()
        except OSError:
            pass
