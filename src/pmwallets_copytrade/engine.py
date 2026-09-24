"""Turns the fills of the traders you follow into your own orders (port of node/copytrade/src/engine.ts).

Every fill is decided exactly once: the eventId is written to the state file BEFORE any order is sent, so a
crash between "sent" and "recorded" can at worst miss a copy — never place it twice. All state changes run
one at a time through a single lock.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol, TypeVar

from .config import Config, TargetConfig
from .filters import book_gate, market_gate, slippage_gate
from .log import Logger
from .polymarket import Book, Market, OrderOutcome, TradeFill
from .state import BotState
from .units import UNIT, clamp_limit, fmt_usd, from_micro, parse_fill_ts, round_buy_shares, to_micro

T = TypeVar("T")


class Exchange(Protocol):
    """The slice of PolymarketGateway the engine uses — a fake in tests, the real one in production."""

    @property
    def can_trade(self) -> bool: ...
    async def condition_id_for(self, token_id: str) -> str: ...
    async def market(self, condition_id: str, max_age_ms: float = 30_000) -> Market: ...
    async def orderbook(self, token_id: str) -> Book: ...
    async def buy_fok(self, token_id: str, condition_id: str, limit: int, shares: int) -> OrderOutcome: ...
    async def sell_fak(self, token_id: str, condition_id: str, limit: int, shares: int) -> OrderOutcome: ...
    async def fills_of(self, order_id: str, condition_id: str, since_ms: float) -> TradeFill: ...
    async def token_balance(self, token_id: str) -> int: ...


def _short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def _avg(r: OrderOutcome) -> Optional[float]:
    return (r.usdc * UNIT // r.shares) / 1e6 if r.shares > 0 else None


class CopyEngine:
    def __init__(
        self,
        cfg: Config,
        exchange: Exchange,
        state: BotState,
        log: Logger,
        targets: Optional[dict[str, TargetConfig]],  # address → settings; None = every subscribed entity
        now: Optional[Callable[[], float]] = None,  # epoch ms
        recheck_s: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.state = state
        self.log = log
        self.targets = targets
        self.now = now or (lambda: time.time() * 1000)
        self.recheck_s = recheck_s
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

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
            except Exception as e:
                self.log.error("fill handling failed", {"eventId": fill.get("eventId"), "error": str(e)})
                self.state.mark_processed(fill["eventId"])
                self.state.log_decision({"eventId": fill["eventId"], "decision": "error", "reason": str(e)})
                self.state.save()

        await self._serial(run)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()

    def _decide(self, base: dict[str, Any], decision: str, **extra: Any) -> None:
        self.state.mark_processed(base["eventId"])
        self.state.log_decision({**base, "decision": decision, **extra})
        self.state.save()
        picked = {k: base[k] for k in ("side", "role", "price") if base.get(k) is not None}
        self.log.info(decision, {"target": _short(base["target"]), **picked, **extra})

    def _commit(self, base: dict[str, Any], decision: str, **extra: Any) -> None:
        """Persist "this fill is decided" BEFORE an order leaves: a crash while the order is in flight then
        costs at most a missed copy on restart — never a second order."""
        self.state.mark_processed(base["eventId"])
        self.state.log_decision({**base, "decision": decision, **extra})
        self.state.save()

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
        age = self.now() - parse_fill_ts(fill["ts"])
        if not (age <= cfg.copy.maxFillAgeSec * 1000):  # NaN counts as stale
            return self._decide(base, "skipped_stale", ageSec=None if math.isnan(age) else math.floor(age / 1000 + 0.5))
        # one copy per target transaction: a taker order that walks several makers, or a maker order hit
        # several times in one tx, is still one decision by the trader
        tx_key = f"{target}|{fill['txHash']}|{fill['tokenId']}|{fill['side']}"
        if state.is_handled_tx(tx_key):
            return self._decide(base, "skipped_same_tx")
        state.mark_handled_tx(tx_key)

        if fill["side"] == "BUY":
            await self._buy(fill, base, target, tcfg)
        else:
            await self._sell(fill, base, target)

    async def _buy(self, fill: dict[str, Any], base: dict[str, Any], target: str, tcfg: Optional[TargetConfig]) -> None:
        cfg, state, ex = self.cfg, self.state, self.exchange
        if from_micro(int(fill["usdc"])) < cfg.copy.minTargetNotionalUsdc:
            return self._decide(base, "skipped_small_target_trade")

        held = state.position(target, fill["tokenId"])
        max_buys = (tcfg.maxBuysPerOutcome if tcfg and tcfg.maxBuysPerOutcome is not None else None) or cfg.copy.maxBuysPerOutcome
        if held and held["buyCount"] >= max_buys:
            return self._decide(base, "skipped_max_buys_per_outcome", buyCount=held["buyCount"])
        if not held:
            open_ = state.positions()
            if len([p for p in open_ if p["target"] == target]) >= cfg.copy.maxOpenPositionsPerTarget:
                return self._decide(base, "skipped_target_position_cap")
            if len(open_) >= cfg.copy.maxOpenPositions:
                return self._decide(base, "skipped_position_cap")
        budget = to_micro(tcfg.orderSizeUsdc if tcfg and tcfg.orderSizeUsdc is not None else cfg.copy.orderSizeUsdc)
        if cfg.risk.maxDailySpendUsdc > 0 and state.spent_today(self._dt()) + budget > to_micro(cfg.risk.maxDailySpendUsdc):
            return self._decide(base, "skipped_daily_spend_cap", spentToday=fmt_usd(state.spent_today(self._dt())))

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

        self._commit(base, "buy_submitted", limit=from_micro(limit), shares=from_micro(shares))
        r = await ex.buy_fok(fill["tokenId"], condition_id, limit, shares)
        if r.status == "filled":
            state.add_buy(pos, r.netShares, r.usdc)
            state.add_spend(r.usdc, self._dt())
            return self._decide(base, "bought", orderId=r.orderId, shares=from_micro(r.netShares), cost=fmt_usd(r.usdc), fee=fmt_usd(r.feeUsdc),
                                avg=_avg(r), market=market.question, outcome=outcome.outcome)
        self._decide(base, "buy_not_filled", orderId=r.orderId or None, reason=r.reason)
        if r.recheck and r.orderId:
            self._recheck("buy", r.orderId, pos, market.question)

    async def _sell(self, fill: dict[str, Any], base: dict[str, Any], target: str) -> None:
        cfg, state, ex = self.cfg, self.state, self.exchange
        if cfg.copy.sellMode == "none":
            return self._decide(base, "skipped_sell_mode_none")
        held = state.position(target, fill["tokenId"])
        if not held:
            return self._decide(base, "skipped_no_position")

        try:
            market = await ex.market(held["conditionId"], 0)
            book = await ex.orderbook(fill["tokenId"])
        except Exception as e:
            return self._decide(base, "sell_lookup_failed", reason=str(e)[:200])
        mg = market_gate(market, "sell", cfg.copy, self.now())
        if not mg["ok"]:
            return self._decide(base, "skipped_market", reason=mg["reason"])
        bg = book_gate(book, "sell", cfg.copy)
        if not bg["ok"]:
            return self._decide(base, "sell_no_bids", reason=bg["reason"])
        bid = book.bids[0].price

        shares = int(held["shares"])
        if cfg.mode == "dry-run":
            usdc = shares * bid // UNIT
            state.reduce(target, fill["tokenId"], shares)
            return self._decide(base, "dry_run_sell", shares=from_micro(shares), at=from_micro(bid), proceeds=fmt_usd(usdc), pnl=fmt_usd(usdc - int(held["costUsdc"])))

        # never try to sell more than the account holds: fees, a manual trade or a redeem can leave less
        balance = await ex.token_balance(fill["tokenId"])
        if balance < shares:
            shares = balance
        if shares <= 0:
            state.drop(target, fill["tokenId"])
            return self._decide(base, "skipped_no_balance")
        self._commit(base, "sell_submitted", limit=from_micro(bid), shares=from_micro(shares))
        r = await ex.sell_fak(fill["tokenId"], held["conditionId"], bid, shares)
        if r.status == "filled":
            cost = int(held["costUsdc"]) * r.shares // int(held["shares"])
            state.reduce(target, fill["tokenId"], r.shares)
            return self._decide(base, "sold", orderId=r.orderId, shares=from_micro(r.shares), proceeds=fmt_usd(r.usdc), pnl=fmt_usd(r.usdc - r.feeUsdc - cost), avg=_avg(r))
        self.log.warn("target exited but our SELL did not fill — the position is still open", {"target": _short(target), "tokenId": fill["tokenId"][:16], "reason": r.reason})
        self._decide(base, "sell_not_filled", orderId=r.orderId or None, reason=r.reason)
        if r.recheck and r.orderId:
            self._recheck("sell", r.orderId, {"target": target, "tokenId": fill["tokenId"], "conditionId": held["conditionId"]}, market.question)

    def _recheck(self, side: str, order_id: str, pos: dict[str, Any], question: str) -> None:
        """An order reported as not filled is looked up once more after the trade indexer caught up. If shares
        did land they are booked — otherwise the wallet holds a position the bot does not know about."""
        since = self.now() - 120_000

        async def later() -> None:
            await asyncio.sleep(self.recheck_s)

            async def run() -> None:
                try:
                    f = await self.exchange.fills_of(order_id, pos["conditionId"], since)
                    if f.shares == 0:
                        return
                    if side == "buy":
                        self.state.add_buy({**pos, "question": question}, f.shares - f.feeShares, f.usdc)
                        self.state.add_spend(f.usdc, self._dt())
                    else:
                        self.state.reduce(pos["target"], pos["tokenId"], f.shares)
                    self.state.log_decision({"eventId": f"recheck:{order_id}", "target": pos["target"], "decision": "late_fill", "side": side,
                                             "orderId": order_id, "shares": from_micro(f.shares), "usdc": fmt_usd(f.usdc)})
                    self.state.save()
                    self.log.warn("an order reported as not filled did fill; position updated", {"side": side, "orderId": order_id, "shares": from_micro(f.shares)})
                except Exception as e:
                    self.log.error("could not re-check an unfilled order — verify it on polymarket.com", {"side": side, "orderId": order_id, "error": str(e)})

            await self._serial(run)

        task = asyncio.create_task(later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
                self.state.log_decision({"eventId": f"settle:{p['conditionId']}:{p['tokenId']}:{p['target']}", "target": p["target"], "decision": "settled",
                                         "market": m.question, "outcome": p.get("outcome"), "won": winner, "payout": fmt_usd(value), "pnl": pnl})
                self.log.info("settled", {"target": _short(p["target"]), "market": m.question, "outcome": p.get("outcome"), "won": winner, "pnl": pnl})
            self.state.save()

        await self._serial(run)

    def positions(self) -> list[dict[str, Any]]:
        return self.state.positions()
