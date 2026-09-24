"""The bot's own memory: open positions, what it already decided, today's spend. Same file format as the
Node bot (state.<mode>.json, decisions.<mode>.jsonl) — the two implementations can take over each other's
data directory."""
from __future__ import annotations

import json
import os
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


class BotState:
    def __init__(self, directory: str, mode: str) -> None:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        self.file = d / f"state.{mode}.json"
        self.decisions_file = d / f"decisions.{mode}.jsonl"
        if self.file.exists():
            self.data: dict[str, Any] = json.loads(self.file.read_text("utf8"))
        else:
            self.data = {"version": 1, "positions": {}, "processed": [], "handledTx": [], "spend": {"day": "", "usdc": "0"}}
        self.data.setdefault("handledTx", [])
        self._processed = set(self.data["processed"])
        self._handled_tx = set(self.data["handledTx"])

    def save(self) -> None:
        for name, index in (("processed", self._processed), ("handledTx", self._handled_tx)):
            lst = self.data[name]
            if len(lst) > MAX_REMEMBERED:
                drop = lst[: len(lst) - MAX_REMEMBERED]
                del lst[: len(lst) - MAX_REMEMBERED]
                for x in drop:
                    index.discard(x)
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

    def position(self, target: str, token_id: str) -> Optional[dict[str, Any]]:
        return self.data["positions"].get(pos_key(target, token_id))

    def positions(self) -> list[dict[str, Any]]:
        return list(self.data["positions"].values())

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
