"""Turns the fills of the traders you follow into your own orders (port of node/copytrade/src/engine.ts).

- Every fill is decided at most once: the eventId — and, for an order, a pending-order record — is written to the
  state file BEFORE the order is sent. A crash then costs at most a missed copy, never a second order, and the
  pending record lets the next run find out whether it filled.
- An order whose result is not known (killed-but-maybe-filled, or no answer) is looked up again from `tick()` until
  its fill or its absence is established — the record survives restarts.
- A target's SELL becomes a persisted exit, retried until the position is gone.
- All state changes run one at a time through a single lock.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol, TypeVar

from .config import Config, TargetConfig
from .filters import book_gate, market_gate, slippage_gate
from .log import Logger
from .polymarket import Book, Market, OrderMatch, OrderOutcome, TradeFill
from .state import BotState
from .units import UNIT, clamp_limit, fmt_usd, from_micro, js_num, parse_fill_ts, round_buy_shares, to_micro

T = TypeVar("T")

# an unconfirmed order that stays unmatched after this many lookups (and 5 minutes) had no fill
RECHECK_ATTEMPTS = 5
RECHECK_MIN_AGE_MS = 5 * 60_000
# a balance too low to sell must hold for this many readings and this long before the exit gives up
BALANCE_CONFIRM_ATTEMPTS = 5
BALANCE_CONFIRM_MS = 10 * 60_000
# an order that cannot be looked up for a day is surrendered to the operator
RECHECK_GIVE_UP_MS = 24 * 3600_000


class Exchange(Protocol):
    """The slice of PolymarketGateway the engine uses — a fake in tests, the real one in production."""

    @property
    def can_trade(self) -> bool: ...
    async def condition_id_for(self, token_id: str) -> str: ...
    async def market(self, condition_id: str, max_age_ms: float = 30_000) -> Market: ...
    async def orderbook(self, token_id: str) -> Book: ...
    async def buy_fok(self, token_id: str, condition_id: str, limit: int, shares: int) -> OrderOutcome: ...
    async def sell_fak(self, token_id: str, condition_id: str, limit: int, shares: int) -> OrderOutcome: ...
    async def fills_of(self, order_id: Optional[str], condition_id: str, since_ms: float, match: Optional[OrderMatch] = None) -> TradeFill: ...
    async def token_balance(self, token_id: str) -> int: ...


def validate_reconcile(p: dict[str, Any], r: tuple[int, int]) -> None:
    """What an operator may enter for an order: never more shares than were sent, never a negative amount, and a price
    the order could actually have filled at (a BUY at its limit or better, a SELL at its limit or better), one cent of
    rounding either way. A wrong entry must not move the books — or the caps they feed."""
    shares, usdc = r
    if shares <= 0:
        raise ValueError("filled shares must be above 0 (use --none for no fill)")
    if usdc < 0:
        raise ValueError("usdc cannot be negative")
    if p.get("shares") and shares > int(p["shares"]):
        raise ValueError(f"the order was for {js_num(from_micro(int(p['shares'])))} shares; {js_num(from_micro(shares))} cannot have filled")
    if p.get("limit"):
        at_limit = shares * int(p["limit"]) // UNIT
        cent = 10_000
        if p["side"] == "buy" and usdc > at_limit + cent:
            raise ValueError(f"{fmt_usd(usdc)} for {js_num(from_micro(shares))} shares is above the BUY limit ({fmt_usd(at_limit)} max)")
        if p["side"] == "sell" and usdc + cent < at_limit:
            raise ValueError(f"{fmt_usd(usdc)} for {js_num(from_micro(shares))} shares is below the SELL limit ({fmt_usd(at_limit)} min)")


def _short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def _avg(r: OrderOutcome) -> Optional[float]:
    return (r.usdc * UNIT // r.shares) / 1e6 if r.shares > 0 else None


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


class CopyEngine:
    def __init__(
        self,
        cfg: Config,
        exchange: Exchange,
        state: BotState,
        log: Logger,
        targets: Optional[dict[str, TargetConfig]],  # address → settings; None = every subscribed entity
        now: Optional[Callable[[], float]] = None,  # epoch ms
        recheck_ms: float = 30_000,  # first delay before an unconfirmed order is looked up again (doubling)
        exit_retry_ms: float = 30_000,  # first delay before a failed exit is retried (doubling, capped at 5 min)
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.state = state
        self.log = log
        self.targets = targets
        self.now = now or (lambda: time.time() * 1000)
        self.recheck_ms = recheck_ms
        self.exit_retry_ms = exit_retry_ms
        self._lock = asyncio.Lock()

    def _dt(self) -> datetime:
        return datetime.fromtimestamp(self.now() / 1000, timezone.utc)

    async def _serial(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            return await fn()

    async def on_fill(self, fill: dict[str, Any], meta: Any) -> None:
        """The FillStream handler. Never raises: a raise would make the stream redeliver, forever."""
        source = getattr(meta, "source", None) or (meta.get("source") if isinstance(meta, dict) else "ws")

        async def run() -> None:
            try:
                await self._handle(fill, source)
            except Exception as e:  # only reached by a bug: every expected failure is a decision, a pending order or exit
                self.log.error("fill handling failed", {"eventId": fill.get("eventId"), "error": str(e)})
                self.state.mark_processed(fill["eventId"])
                self.state.log_decision({"eventId": fill["eventId"], "decision": "error", "reason": str(e)})
                self.state.save()

        await self._serial(run)

    # ── bookkeeping
    def _decide(self, base: dict[str, Any], decision: str, **extra: Any) -> None:
        self.state.mark_processed(base["eventId"])
        self.state.log_decision({**base, "decision": decision, **extra})
        self.state.save()
        picked = {k: base[k] for k in ("side", "role", "price") if base.get(k) is not None}
        self.log.info(decision, {"target": _short(base["target"]), **picked, **extra})

    def _record(self, entry: dict[str, Any], level: str = "info") -> None:
        self.state.log_decision(entry)
        self.state.save()
        fields = {**entry, "target": _short(entry["target"]) if isinstance(entry.get("target"), str) else None}
        getattr(self.log, level)(str(entry.get("decision")), fields)

    def _commit_order(self, event_id: Optional[str], pending: dict[str, Any], entry: dict[str, Any]) -> None:
        """Persist "decided" plus a pending-order record BEFORE the order leaves: the at-most-once guarantee, and the
        record is how a fill whose answer was lost still gets booked."""
        if event_id:
            self.state.mark_processed(event_id)
        self.state.add_pending_order(pending)
        self.state.log_decision(entry)
        self.state.save()

    def _recheck_delay(self, attempts: int) -> float:
        return min(self.recheck_ms * 2**attempts, 10 * 60_000)

    def _exit_delay(self, attempts: int) -> float:
        return min(self.exit_retry_ms * 2**attempts, 5 * 60_000)

    # ── a fill arrives
    async def _handle(self, fill: dict[str, Any], source: str) -> None:
        cfg, state = self.cfg, self.state
        if state.is_processed(fill["eventId"]):
            return
        target = fill["entityId"].lower()
        base: dict[str, Any] = {
            "eventId": fill["eventId"], "target": target, "wallet": fill["wallet"], "side": fill["side"], "role": fill["role"],
            "tokenId": fill["tokenId"], "price": fill["price"], "usdc": from_micro(int(fill["usdc"])), "tx": fill["txHash"], "source": source,
        }
        tcfg = self.targets.get(target) if self.targets is not None else None
        if self.targets is not None and tcfg is None:
            return self._decide(base, "skipped_not_a_target")
        if fill["role"] not in cfg.copy.roles:
            return self._decide(base, "skipped_role")
        # one copy per target transaction: a taker order that walks several makers, or a maker order hit several
        # times in one tx, is still one decision by the trader
        tx_key = f"{target}|{fill['txHash']}|{fill['tokenId']}|{fill['side']}"
        if state.is_handled_tx(tx_key):
            return self._decide(base, "skipped_same_tx")

        if fill["side"] == "BUY":
            # freshness is an ENTRY rule: a replay after downtime must not buy history. An exit is not subject to
            # it — if the target left while we were down, we still want out.
            age = self.now() - parse_fill_ts(fill["ts"])
            if not (age <= cfg.copy.maxFillAgeSec * 1000):  # NaN counts as stale
                return self._decide(base, "skipped_stale", ageSec=None if math.isnan(age) else _js_round(age / 1000))
            state.mark_handled_tx(tx_key)
            return await self._buy(fill, base, target, tcfg)
        state.mark_handled_tx(tx_key)
        if cfg.copy.sellMode == "none":
            return self._decide(base, "skipped_sell_mode_none")
        # a BUY still being confirmed may turn into a position: queue the exit anyway, it waits for the BUY
        if not state.position(target, fill["tokenId"]) and not self._pending_buy(target, fill["tokenId"]):
            return self._decide(base, "skipped_no_position")
        now = self.now()
        state.add_pending_exit({"eventId": fill["eventId"], "target": target, "tokenId": fill["tokenId"], "firstAt": now, "attempts": 0, "nextAt": now})
        self._decide(base, "exit_queued")
        exit_ = next(e for e in state.pending_exits() if e["target"] == target and e["tokenId"] == fill["tokenId"])
        await self._attempt_exit(exit_)

    async def _buy(self, fill: dict[str, Any], base: dict[str, Any], target: str, tcfg: Optional[TargetConfig]) -> None:
        cfg, state, ex = self.cfg, self.state, self.exchange
        if from_micro(int(fill["usdc"])) < cfg.copy.minTargetNotionalUsdc:
            return self._decide(base, "skipped_small_target_trade")

        held = state.position(target, fill["tokenId"])
        max_buys = tcfg.maxBuysPerOutcome if tcfg and tcfg.maxBuysPerOutcome is not None else cfg.copy.maxBuysPerOutcome
        if held and held["buyCount"] >= max_buys:
            return self._decide(base, "skipped_max_buys_per_outcome", buyCount=held["buyCount"])
        if state.has_unknown_reservation():
            return self._decide(base, "skipped_reconcile_required")
        # an order still being confirmed on this outcome counts as open: buying again could double up
        if any(p["target"] == target and p["tokenId"] == fill["tokenId"] and p["side"] == "buy" for p in state.pending_orders()):
            return self._decide(base, "skipped_order_unconfirmed")
        # the caps count unconfirmed BUYs too: each may turn out filled, and all of them together must still fit
        # inside the limits the user set
        if not held:
            open_ = state.open_outcomes()
            if len([p for p in open_ if p["target"] == target]) >= cfg.copy.maxOpenPositionsPerTarget:
                return self._decide(base, "skipped_target_position_cap")
            if len(open_) >= cfg.copy.maxOpenPositions:
                return self._decide(base, "skipped_position_cap")
        budget = to_micro(tcfg.orderSizeUsdc if tcfg and tcfg.orderSizeUsdc is not None else cfg.copy.orderSizeUsdc)
        committed = state.spent_today(self._dt()) + state.reserved_usdc()
        if cfg.risk.maxDailySpendUsdc > 0 and committed + budget > to_micro(cfg.risk.maxDailySpendUsdc):
            return self._decide(base, "skipped_daily_spend_cap", spentToday=fmt_usd(state.spent_today(self._dt())), reserved=fmt_usd(state.reserved_usdc()))

        try:
            condition_id = held["conditionId"] if held else await ex.condition_id_for(fill["tokenId"])
            market = await ex.market(condition_id)
        except Exception as e:
            return self._decide(base, "skipped_market_lookup_failed", reason=str(e)[:200])
        mg = market_gate(market, "buy", cfg.copy, self.now())
        if not mg["ok"]:
            return self._decide(base, "skipped_market", reason=mg["reason"])
        outcome = next((t for t in market.tokens if t.tokenId == fill["tokenId"]), None)
        if outcome is None:
            return self._decide(base, "skipped_token_not_in_market")
        try:
            book = await ex.orderbook(fill["tokenId"])
        except Exception as e:
            return self._decide(base, "skipped_book_failed", reason=str(e)[:200])
        bg = book_gate(book, "buy", cfg.copy)
        if not bg["ok"]:
            return self._decide(base, "skipped_book", reason=bg["reason"])
        ask = book.asks[0].price
        sg = slippage_gate(ask, to_micro(fill["price"]), cfg.copy)
        if not sg["ok"]:
            return self._decide(base, "skipped_slippage", reason=sg["reason"])

        limit = clamp_limit(ask, book.tickSize)
        shares = round_buy_shares(budget * UNIT // ask, limit)
        if shares <= 0 or (book.minOrderSize and shares < book.minOrderSize):
            return self._decide(base, "skipped_below_min_order", shares=from_micro(shares), min=from_micro(book.minOrderSize) if book.minOrderSize else None)
        pos = {"target": target, "tokenId": fill["tokenId"], "conditionId": condition_id, "question": market.question, "outcome": outcome.outcome}

        if cfg.mode == "dry-run":
            usdc = shares * ask // UNIT
            state.add_buy(pos, shares, usdc)
            state.add_spend(usdc, self._dt())
            return self._decide(base, "dry_run_buy", shares=from_micro(shares), at=from_micro(ask), cost=fmt_usd(usdc), market=market.question, outcome=outcome.outcome)

        key = f"buy|{fill['eventId']}"
        now = self.now()
        reserve = shares * limit // UNIT
        self._commit_order(fill["eventId"], {"key": key, "side": "buy", "orderId": None, **pos, "shares": str(shares), "limit": str(limit),
                                             "reserveUsdc": str(reserve), "sentAt": now, "attempts": 0, "nextAt": now + self._recheck_delay(0)},
                           {**base, "decision": "buy_submitted", "limit": from_micro(limit), "shares": from_micro(shares)})
        r = await ex.buy_fok(fill["tokenId"], condition_id, limit, shares)
        self._after_order(key, r, base, {"market": market.question, "outcome": outcome.outcome})

    def _after_order(self, key: str, r: OrderOutcome, base: dict[str, Any], extra: dict[str, Any]) -> None:
        """Book what an order did, or leave its pending record for tick() to resolve."""
        state = self.state
        p = next((x for x in state.pending_orders() if x["key"] == key), None)
        if p is None:
            return
        if r.status == "filled":
            state.remove_pending_order(key)
            self._book(p, TradeFill(shares=r.shares, usdc=r.usdc, feeUsdc=r.feeUsdc, feeShares=r.shares - r.netShares, orderIds=[r.orderId]))
            self._record({**base, "decision": "bought" if p["side"] == "buy" else "sold", "orderId": r.orderId,
                          "shares": from_micro(r.netShares if p["side"] == "buy" else r.shares), "usdc": fmt_usd(r.usdc),
                          "fee": fmt_usd(r.feeUsdc), "avg": _avg(r), **extra})
            return
        if r.status == "failed":
            state.remove_pending_order(key)
            self._record({**base, "decision": "buy_rejected" if p["side"] == "buy" else "sell_rejected", "reason": r.reason})
            return
        # none / unknown: it may still have filled — keep the record, tick() will find out
        state.update_pending_order(key, orderId=r.orderId or None)
        self._record({**base, "decision": "buy_unconfirmed" if p["side"] == "buy" else "sell_unconfirmed", "orderId": r.orderId or None, "reason": r.reason},
                     "warn" if r.status == "unknown" else "info")

    def _pending_buy(self, target: str, token_id: str) -> bool:
        return any(p["side"] == "buy" and p["target"] == target and p["tokenId"] == token_id for p in self.state.pending_orders())

    def _needs_reconcile(self, p: dict[str, Any], reason: str) -> None:
        """Hand an order to a human. It stays pending — reservation and all — until reconcile()."""
        self.state.update_pending_order(p["key"], needsReconcile=reason)
        self._record({"eventId": f"recheck:{p['key']}", "target": p["target"], "decision": "order_needs_reconcile", "side": p["side"],
                      "orderId": p["orderId"], "tokenId": p["tokenId"], "key": p["key"], "reason": reason}, "error")

    async def reconcile(self, key: str, result: Optional[tuple[int, int]]) -> None:
        """The operator's verdict on an order the bot could not settle: None = it did not fill; otherwise (shares, usdc)
        it filled for (read them off polymarket.com). Releases the reservation."""

        async def run() -> None:
            p = next((x for x in self.state.pending_orders() if x["key"] == key), None)
            if p is None:
                raise RuntimeError(f"no pending order {key}")
            if result:
                validate_reconcile(p, result)
            self.state.remove_pending_order(key)
            if result and result[0] > 0:
                self._book(p, TradeFill(shares=result[0], usdc=result[1], orderIds=[p["orderId"]] if p.get("orderId") else []))
            self._record({"eventId": f"reconcile:{key}", "target": p["target"], "decision": "reconciled", "side": p["side"], "tokenId": p["tokenId"],
                          "filled": from_micro(result[0]) if result else 0, "usdc": fmt_usd(result[1]) if result else "$0.00"})

        await self._serial(run)

    def _book(self, p: dict[str, Any], f: TradeFill) -> None:
        """Apply a fill to the books. BUY fees are taken in shares, so the position is what actually landed."""
        # inventory on this outcome just changed: earlier "nothing there" readings no longer count
        self.state.update_pending_exit(p["target"], p["tokenId"], lowBalanceReads=0, lowBalanceSince=None)
        for oid in f.orderIds:
            if oid:
                self.state.mark_booked(oid)
        if p["side"] == "buy":
            self.state.add_buy({"target": p["target"], "tokenId": p["tokenId"], "conditionId": p["conditionId"],
                                "question": p.get("question"), "outcome": p.get("outcome")}, f.shares - f.feeShares, f.usdc)
            self.state.add_spend(f.usdc, self._dt())
        else:
            self.state.reduce(p["target"], p["tokenId"], f.shares)

    async def _attempt_exit(self, exit_: dict[str, Any]) -> None:
        """Sell what this target led us into. Called right away when the target sells, and again from tick() until the
        position is gone. Sells at most min(our position, balance − what other targets hold in the same token): the
        wallet's balance is shared, the books are per target."""
        cfg, state, ex, log = self.cfg, self.state, self.exchange, self.log
        target, token_id = exit_["target"], exit_["tokenId"]
        base = {"eventId": exit_["eventId"], "target": target, "tokenId": token_id, "side": "SELL"}

        # every retry except a low-balance reading breaks the run of low readings
        def retry(decision: str, low_run: Optional[tuple[int, float]] = None, **extra: Any) -> None:
            state.update_pending_exit(target, token_id, attempts=exit_["attempts"] + 1, nextAt=self.now() + self._exit_delay(exit_["attempts"]),
                                      lowBalanceReads=low_run[0] if low_run else 0, lowBalanceSince=low_run[1] if low_run else None)
            self._record({**base, "decision": decision, "attempt": exit_["attempts"] + 1, **extra}, "warn")

        def done(decision: str, level: str = "info", **extra: Any) -> None:
            state.remove_pending_exit(target, token_id)
            self._record({**base, "decision": decision, **extra}, level)

        held = state.position(target, token_id)
        if not held:
            # the BUY that would give us the position is still being confirmed (or awaits reconcile): wait for its
            # verdict — filled means sell it, not filled means there is nothing to exit
            if self._pending_buy(target, token_id):
                state.update_pending_exit(target, token_id, nextAt=self.now() + self._exit_delay(0))
                state.save()
                return
            return done("exit_done")
        # an exit order still being confirmed: wait for it rather than sell the same shares twice
        if any(p["target"] == target and p["tokenId"] == token_id and p["side"] == "sell" for p in state.pending_orders()):
            state.update_pending_exit(target, token_id, nextAt=self.now() + self._exit_delay(0), lowBalanceReads=0, lowBalanceSince=None)
            state.save()
            return

        try:
            market = await ex.market(held["conditionId"], 0)
            book = await ex.orderbook(token_id)
        except Exception as e:
            return retry("exit_retry_lookup_failed", reason=str(e)[:200])
        # resolved: the settlement sweep takes it from here. Paused (inactive / not accepting orders) is temporary —
        # keep the exit and try again, or a pause during the target's SELL strands us in it.
        if market.closed:
            return done("exit_dropped_market_closed")
        mg = market_gate(market, "sell", cfg.copy, self.now())
        if not mg["ok"]:
            return retry("exit_retry_market_paused", reason=mg["reason"])
        bg = book_gate(book, "sell", cfg.copy)
        if not bg["ok"]:
            return retry("exit_retry_no_bids")
        bid = book.bids[0].price
        shares = int(held["shares"])

        if cfg.mode == "dry-run":
            usdc = shares * bid // UNIT
            state.reduce(target, token_id, shares)
            return done("dry_run_sell", shares=from_micro(shares), at=from_micro(bid), proceeds=fmt_usd(usdc), pnl=fmt_usd(usdc - int(held["costUsdc"])))

        try:
            balance = await ex.token_balance(token_id)
        except Exception as e:
            return retry("exit_retry_balance_failed", reason=str(e)[:200])
        others = state.shares_held_by_others(target, token_id)
        available = balance - others
        if available < shares:
            shares = available if available > 0 else 0
        if shares <= 0:
            # The balance endpoint can lag a fill we have just booked (a late BUY, a fresh reconcile). One reading of
            # "not there" is not proof: retry, and only give up once it has held for a while.
            if self._pending_buy(target, token_id):
                # inventory on this outcome may still change: this reading proves nothing, start over after
                return retry("exit_retry_balance_short_buy_pending", balance=from_micro(balance), bookedToOthers=from_micro(others))
            since = exit_.get("lowBalanceSince")
            run = ((exit_.get("lowBalanceReads") or 0) + 1, since if since is not None else self.now())
            settled = run[0] >= BALANCE_CONFIRM_ATTEMPTS and self.now() - run[1] >= BALANCE_CONFIRM_MS
            if not settled:
                return retry("exit_retry_zero_balance" if balance == 0 else "exit_retry_balance_short", run,
                             balance=from_micro(balance), bookedToOthers=from_micro(others), lowReads=run[0])
            if balance == 0:
                state.drop(target, token_id)
                return done("exit_no_balance")
            log.error("the wallet holds less of this outcome than the books say; not selling shares booked to other targets — reconcile by hand",
                      {"target": _short(target), "tokenId": token_id[:16], "balance": from_micro(balance), "bookedToOthers": from_micro(others)})
            return done("exit_blocked_reconcile", "error", balance=from_micro(balance), bookedToOthers=from_micro(others))

        # shares ARE there: whatever run of low readings came before is over
        state.update_pending_exit(target, token_id, lowBalanceReads=0, lowBalanceSince=None)
        key = f"sell|{exit_['eventId']}|{exit_['attempts']}"
        now = self.now()
        self._commit_order(None, {"key": key, "side": "sell", "orderId": None, "target": target, "tokenId": token_id, "conditionId": held["conditionId"],
                                  **{k: held[k] for k in ("question", "outcome") if held.get(k) is not None},
                                  "shares": str(shares), "limit": str(clamp_limit(bid, book.tickSize)), "reserveUsdc": "0",
                                  "sentAt": now, "attempts": 0, "nextAt": now + self._recheck_delay(0)},
                           {**base, "decision": "sell_submitted", "limit": from_micro(bid), "shares": from_micro(shares)})
        r = await ex.sell_fak(token_id, held["conditionId"], bid, shares)
        cost_of_sold = int(held["costUsdc"]) * r.shares // int(held["shares"])
        self._after_order(key, r, base, {"pnl": fmt_usd(r.usdc - r.feeUsdc - cost_of_sold)} if r.status == "filled" else {})
        # done only when nothing is left AND no BUY on this outcome can still turn into shares (a DCA add being
        # confirmed): otherwise its late fill would reopen a position with no exit behind it
        if r.status == "filled" and not state.position(target, token_id) and not self._pending_buy(target, token_id):
            return done("exit_done")
        # partial, unfilled, unconfirmed or rejected: try again later
        state.update_pending_exit(target, token_id, attempts=exit_["attempts"] + 1, nextAt=self.now() + self._exit_delay(exit_["attempts"]))
        state.save()

    async def tick(self) -> None:
        """Periodic work: resolve orders whose outcome is not known yet, retry pending exits. Everything it needs is in
        the state file, so it picks up exactly where a previous run stopped."""

        async def run() -> None:
            state, ex = self.state, self.exchange
            now = self.now()
            for p in list(state.pending_orders()):
                if p.get("needsReconcile") or p["nextAt"] > now:
                    continue
                try:
                    since = p["sentAt"] - 30_000 if p["orderId"] else p["sentAt"] - 5_000
                    f = await ex.fills_of(p["orderId"], p["conditionId"], since,
                                          OrderMatch(p["tokenId"], p["side"], int(p.get("shares") or 0), int(p.get("limit") or 0), state.is_booked))
                except Exception as e:
                    if now - p["sentAt"] > RECHECK_GIVE_UP_MS:
                        self._needs_reconcile(p, f"could not look the order up for a day: {str(e)[:160]}")
                    else:
                        state.update_pending_order(p["key"], attempts=p["attempts"] + 1, nextAt=now + self._recheck_delay(p["attempts"] + 1))
                        state.save()
                    continue
                if f.candidates:
                    # the answer to our post was lost and something that looks like it filled: it may be ours, a manual
                    # trade, or another target's order — booking it to this target could sell the wrong position later.
                    # Keep the reservation and let the operator decide.
                    listed = "; ".join(f"{c['orderId']} {js_num(from_micro(c['shares']))} sh / {fmt_usd(c['usdc'])}" for c in f.candidates)
                    self._needs_reconcile(p, f"the order id never came back; possible fill(s): {listed}")
                    continue
                if f.shares > 0:
                    state.remove_pending_order(p["key"])
                    self._book(p, f)
                    self._record({"eventId": f"recheck:{p['key']}", "target": p["target"], "decision": "late_fill", "side": p["side"],
                                  "orderId": p["orderId"] or ",".join(f.orderIds),
                                  "shares": from_micro(f.shares - f.feeShares if p["side"] == "buy" else f.shares), "usdc": fmt_usd(f.usdc)}, "warn")
                    continue
                if p["attempts"] + 1 >= RECHECK_ATTEMPTS and now - p["sentAt"] >= RECHECK_MIN_AGE_MS:
                    state.remove_pending_order(p["key"])
                    self._record({"eventId": f"recheck:{p['key']}", "target": p["target"], "decision": "confirmed_no_fill", "side": p["side"], "orderId": p["orderId"]})
                else:
                    state.update_pending_order(p["key"], attempts=p["attempts"] + 1, nextAt=now + self._recheck_delay(p["attempts"] + 1))
                    state.save()
            for e in list(state.pending_exits()):
                if e["nextAt"] > now:
                    continue
                await self._attempt_exit(e)

        await self._serial(run)

    async def sweep_settled(self) -> None:
        """Drop positions whose market has resolved (redeemed on Polymarket); they must stop counting against caps."""

        async def run() -> None:
            for p in self.state.positions():
                try:
                    m = await self.exchange.market(p["conditionId"], 0)
                except Exception:
                    continue
                if not m.closed:
                    continue
                tok = next((t for t in m.tokens if t.tokenId == p["tokenId"]), None)
                winner = bool(tok and (tok.winner is True or (tok.price is not None and tok.price >= 0.99)))
                value = int(p["shares"]) if winner else 0
                pnl = fmt_usd(value - int(p["costUsdc"]))
                self.state.drop(p["target"], p["tokenId"])
                self.state.remove_pending_exit(p["target"], p["tokenId"])
                self.state.log_decision({"eventId": f"settle:{p['conditionId']}:{p['tokenId']}:{p['target']}", "target": p["target"], "decision": "settled",
                                         "market": m.question, "outcome": p.get("outcome"), "won": winner, "payout": fmt_usd(value), "pnl": pnl})
                self.log.info("settled", {"target": _short(p["target"]), "market": m.question, "outcome": p.get("outcome"), "won": winner, "pnl": pnl})
            self.state.save()

        await self._serial(run)

    def positions(self) -> list[dict[str, Any]]:
        return self.state.positions()
