import asyncio
import re
import itertools
import json
from datetime import datetime, timezone

import pytest

from pmwallets import FillMeta
from pmwallets_copytrade.config import TargetConfig, build_config
from pmwallets_copytrade.engine import CopyEngine
from pmwallets_copytrade.polymarket import Book, Level, Market, OrderOutcome, Token, TradeFill
from pmwallets_copytrade.state import BotState, InstanceLock
from pmwallets_copytrade.units import to_micro

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
T1 = "0x" + "1" * 40
T2 = "0x" + "2" * 40
WS = FillMeta("ws")
REPLAY = FillMeta("replay")


class Silent:
    def info(self, *a, **k): ...
    def warn(self, *a, **k): ...
    def error(self, *a, **k): ...


_n = itertools.count(1)


def fill(**o):
    n = next(_n)
    f = {
        "eventId": f"137:{n}:0xh:0xtx{n}:1", "chain": 137, "entityId": T1, "wallet": T1, "ts": "2026-09-24 11:59:50",
        "block": 1000 + n, "blockHash": "0xh", "txHash": f"0xtx{n}", "logIndex": 1, "exchange": "pm_ctf_v2",
        "side": "BUY", "role": "taker", "tokenId": "TOK", "price": "0.500000", "shares": "100000000", "usdc": "50000000", "fee": "0",
    }
    f.update(o)
    return f


def filled(order_id):
    return lambda shares, limit: OrderOutcome(order_id, "filled", shares, shares * limit // 1_000_000, 0, shares)


class FakeExchange:
    can_trade = True

    def __init__(self):
        self.market_ = Market("CID", "Will it?", False, True, True, "2026-12-31T00:00:00Z", [Token("TOK", "Yes")])
        self.book = Book("TOK", bids=[Level(to_micro("0.49"), to_micro("1000"))], asks=[Level(to_micro("0.51"), to_micro("1000"))],
                         tickSize=to_micro("0.01"), minOrderSize=to_micro("5"))
        self.buys, self.sells = [], []
        self.buy_result = filled("o1")
        self.sell_result = filled("s1")
        self.balance = 10**12
        self.late_fill = TradeFill()
        self.fills_calls = []
        self.balance_fails = 0

    async def condition_id_for(self, token_id):
        return "CID"

    async def market(self, condition_id, max_age_ms=30_000, with_end_date=False):
        return self.market_

    async def orderbook(self, token_id):
        return self.book

    async def buy_fok(self, token_id, condition_id, limit, shares):
        self.buys.append((limit, shares))
        return self.buy_result(shares, limit)

    async def sell_fak(self, token_id, condition_id, limit, shares):
        self.sells.append((limit, shares))
        return self.sell_result(shares, limit)

    async def fills_of(self, order_id, condition_id, since_ms, match=None):
        self.fills_calls.append(order_id)
        return self.late_fill

    async def token_balance(self, token_id):
        if self.balance_fails > 0:
            self.balance_fails -= 1
            raise RuntimeError("timeout")
        return self.balance


LIVE = {"mode": "live", "polymarket": {"privateKey": "ab" * 32, "signatureType": 0}}


class H:
    def __init__(self, tmp_path, raw=None, targets=None):
        self.dir = str(tmp_path)
        self.cfg = build_config({"pmwallets": {"apiKey": "pmw_a_b"}, "dataDir": self.dir, **(raw or {})})
        self.ex = FakeExchange()
        self.clock = {"t": NOW}
        self._targets = targets
        self.state = BotState(self.dir, self.cfg.mode)
        self.engine = self.make(self.state)

    def make(self, state, exchange=None):
        return CopyEngine(self.cfg, exchange or self.ex, state, Silent(),
                          {t: TargetConfig(t) for t in self._targets} if self._targets else None,
                          now=lambda: self.clock["t"], recheck_ms=1000, exit_retry_ms=1000)

    def restart(self, exchange=None):
        """a fresh engine on the same data directory — what a restart looks like"""
        return self.make(BotState(self.dir, self.cfg.mode), exchange)

    def decisions(self):
        return [json.loads(l) for l in self.state.decisions_file.read_text().strip().split("\n")]

    def kinds(self):
        return [d["decision"] for d in self.decisions()]

    def last(self):
        return self.decisions()[-1]


async def test_dry_run_buy(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    assert h.last()["decision"] == "dry_run_buy" and h.last()["fillPrice"] == 0.51
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", h.last()["at"]), "the decision log line keeps its timestamp"
    p = h.state.position(T1, "TOK")
    assert p["shares"] == "19000000"  # $10 / 0.51 = 19.6, but at 0.51 only whole shares land on the cent grid
    assert p["buyCount"] == 1
    assert h.ex.buys == []


async def test_live_buy_net_shares(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = lambda shares, limit: OrderOutcome("o1", "filled", shares, shares * limit // 1_000_000, 10_000, shares - 20_000)
    await h.engine.on_fill(fill(), WS)
    assert h.ex.buys == [(to_micro("0.51"), 19_000_000)]
    assert h.state.position(T1, "TOK")["shares"] == str(19_000_000 - 20_000)
    assert h.last()["decision"] == "bought" and h.last()["orderId"] == "o1"
    assert h.state.spent_today(datetime.fromtimestamp(NOW / 1000, timezone.utc)) == 19_000_000 * 510_000 // 1_000_000


async def test_once_per_fill_and_per_tx(tmp_path):
    h = H(tmp_path)
    f = fill()
    await h.engine.on_fill(f, WS)
    await h.engine.on_fill(f, REPLAY)
    await h.engine.on_fill({**f, "eventId": f["eventId"][:-2] + ":2", "logIndex": 2}, WS)
    assert [d["decision"] for d in h.decisions()] == ["dry_run_buy", "skipped_same_tx"]


async def test_remembers_across_restart(tmp_path):
    h = H(tmp_path)
    f = fill()
    await h.engine.on_fill(f, WS)
    again = CopyEngine(h.cfg, h.ex, BotState(h.dir, h.cfg.mode), Silent(), None, now=lambda: NOW)
    await again.on_fill(f, REPLAY)
    assert len(h.decisions()) == 1


async def test_decided_on_disk_before_the_order_leaves(tmp_path):
    for side in ("BUY", "SELL"):
        h = H(tmp_path / side, LIVE)
        await h.engine.on_fill(fill(), WS)
        f = fill(side=side)
        seen = {}

        def see():
            seen["onDisk"] = f["eventId"] in json.loads(h.state.file.read_text())["processed"]

        h.ex.buy_result = lambda shares, limit: (see(), OrderOutcome("o", "filled", shares, shares * limit // 1_000_000, 0, shares))[1]
        h.ex.sell_result = lambda shares, limit: (see(), OrderOutcome("s", "filled", shares, shares * limit // 1_000_000, 0, shares))[1]
        await h.engine.on_fill(f, WS)
        assert seen["onDisk"] is True


async def test_stale(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(ts="2026-09-24 11:55:00"), REPLAY)
    assert h.last()["decision"] == "skipped_stale" and h.last()["ageSec"] == 300


@pytest.mark.parametrize("decision,f,raw", [
    ("skipped_small_target_trade", {"usdc": "5000000"}, {}),
    ("skipped_slippage", {"price": "0.450000"}, {}),
    ("skipped_role", {"role": "maker"}, {"copy": {"roles": ["taker"]}}),
])
async def test_skips(tmp_path, decision, f, raw):
    h = H(tmp_path, raw)
    await h.engine.on_fill(fill(**f), WS)
    assert h.last()["decision"] == decision


async def test_only_targets(tmp_path):
    h = H(tmp_path, targets=[T2])
    await h.engine.on_fill(fill(), WS)
    assert h.last()["decision"] == "skipped_not_a_target"


async def test_thin_book(tmp_path):
    h = H(tmp_path)
    h.ex.book.asks = [Level(to_micro("0.51"), to_micro("20"))]
    await h.engine.on_fill(fill(), WS)
    assert h.last()["decision"] == "skipped_book" and "depth" in h.last()["reason"]


async def test_dca_and_position_caps(tmp_path):
    h = H(tmp_path, {"copy": {"maxBuysPerOutcome": 2, "maxOpenPositionsPerTarget": 1}})
    for _ in range(3):
        await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(tokenId="OTHER"), WS)
    assert [d["decision"] for d in h.decisions()] == ["dry_run_buy", "dry_run_buy", "skipped_max_buys_per_outcome", "skipped_target_position_cap"]


async def test_daily_cap(tmp_path):
    h = H(tmp_path, {"risk": {"maxDailySpendUsdc": 15}, "copy": {"maxBuysPerOutcome": 5}})
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(), WS)
    assert [d["decision"] for d in h.decisions()] == ["dry_run_buy", "skipped_daily_spend_cap"]


def killed(order_id="o9", reason="killed"):
    return lambda s, l: OrderOutcome(order_id, "none", reason=reason, recheck=True)


async def test_unconfirmed_buy_blocks_a_second_buy(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = killed()
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(), WS)
    assert h.kinds() == ["buy_submitted", "buy_unconfirmed", "skipped_order_unconfirmed"]


def unknown_post():
    return lambda s, l: OrderOutcome("", "unknown", reason="post_error", recheck=True)


async def test_unconfirmed_buys_hold_budget_and_position_slot(tmp_path):
    h = H(tmp_path / "a", {**LIVE, "risk": {"maxDailySpendUsdc": 15}})
    h.ex.buy_result = unknown_post()
    await h.engine.on_fill(fill(tokenId="TOK"), WS)
    await h.engine.on_fill(fill(tokenId="TOK2"), WS)
    assert h.kinds()[-1] == "skipped_daily_spend_cap"
    assert len(h.ex.buys) == 1
    g = H(tmp_path / "b", {**LIVE, "copy": {"maxOpenPositions": 1}})
    g.ex.buy_result = unknown_post()
    await g.engine.on_fill(fill(tokenId="TOK"), WS)
    await g.engine.on_fill(fill(tokenId="TOK2"), WS)
    assert g.kinds()[-1] == "skipped_position_cap"


def ts_at(ms):
    """a feed timestamp a few seconds before `ms`"""
    return datetime.fromtimestamp((ms - 5_000) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def test_order_handed_to_the_operator_keeps_its_reservation(tmp_path):
    h = H(tmp_path, {**LIVE, "risk": {"maxDailySpendUsdc": 15}})
    h.ex.buy_result = unknown_post()
    await h.engine.on_fill(fill(), WS)
    h.ex.late_fill = TradeFill(candidates=[{"orderId": "0xa", "shares": 19_000_000, "usdc": 9_690_000}, {"orderId": "0xb", "shares": 5_000_000, "usdc": 2_550_000}])
    for _ in range(20):
        h.clock["t"] += 60_000
        await h.engine.tick()
    assert h.state.positions() == []
    assert [d["decision"] for d in h.decisions()].count("order_needs_reconcile") == 1
    [p] = h.state.pending_orders()
    assert re.search(r"0xa .*0xb", p["needsReconcile"])
    # the $9.69 it may have spent still counts: another $10 BUY would break the $15 cap
    h.ex.buy_result = filled("o2")
    await h.engine.on_fill(fill(tokenId="TOK2", ts=ts_at(h.clock["t"])), WS)
    assert h.last()["decision"] == "skipped_daily_spend_cap"
    # the operator checks polymarket.com: it did fill
    await h.engine.reconcile(p["key"], (19_000_000, 9_690_000))
    assert h.state.pending_orders() == []
    assert h.state.position(T1, "TOK")["shares"] == "19000000"
    assert h.last()["decision"] == "reconciled" and h.last()["filled"] == 19


async def test_pending_order_from_an_older_build_fails_closed(tmp_path):
    h = H(tmp_path, LIVE)
    h.state.file.write_text(json.dumps({"version": 1, "positions": {}, "processed": [], "handledTx": [], "spend": {"day": "", "usdc": "0"},
                                         "pendingOrders": [{"key": "buy|old", "side": "buy", "orderId": None, "target": T1, "tokenId": "TOK",
                                                            "conditionId": "CID", "sentAt": NOW, "attempts": 0, "nextAt": NOW}],
                                         "pendingExits": [], "bookedOrderIds": []}))
    again = h.restart()
    st = BotState(h.dir, "live")
    assert "older build" in st.pending_orders()[0]["needsReconcile"]
    await again.on_fill(fill(tokenId="TOK2"), WS)
    assert h.last()["decision"] == "skipped_reconcile_required"
    h.clock["t"] += 60 * 60_000
    await again.tick()
    assert h.ex.fills_calls == []  # never looked up with a made-up size, never dropped
    await again.reconcile("buy|old", None)
    await again.on_fill(fill(ts=ts_at(h.clock["t"])), WS)
    assert h.last()["decision"] == "bought"


async def test_killed_order_that_filled_is_booked_by_tick_even_after_restart(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = killed(reason="order couldn't be fully filled")
    await h.engine.on_fill(fill(), WS)
    assert h.state.position(T1, "TOK") is None
    assert [(p["orderId"], p["side"]) for p in h.state.pending_orders()] == [("o9", "buy")]
    again = h.restart()  # the process restarts before the re-check is due
    h.ex.late_fill = TradeFill(12_000_000, 6_120_000, 0, 0, ["o9"])
    await again.tick()  # not due yet
    assert h.ex.fills_calls == []
    h.clock["t"] += 1_000
    await again.tick()
    assert h.ex.fills_calls == ["o9"]
    st = BotState(h.dir, "live")
    assert st.position(T1, "TOK")["shares"] == "12000000"
    assert st.pending_orders() == []
    assert st.is_booked("o9")
    assert h.last()["decision"] == "late_fill"


async def test_order_whose_id_never_came_back_goes_to_the_operator(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = lambda s, l: OrderOutcome("", "unknown", reason="post_error: socket hang up", recheck=True)
    await h.engine.on_fill(fill(), WS)
    assert [p["orderId"] for p in h.state.pending_orders()] == [None]
    h.ex.late_fill = TradeFill(candidates=[{"orderId": "0xabc", "shares": 19_000_000, "usdc": 9_690_000}])
    h.clock["t"] += 1_000
    await h.engine.tick()
    assert h.ex.fills_calls == [None]
    assert h.state.positions() == []
    assert re.search(r"0xabc 19 sh", h.state.pending_orders()[0]["needsReconcile"])
    assert h.state.pending_orders()[0]["needsReconcile"] == "the order id never came back; possible fill(s): 0xabc 19 sh / $9.69"


async def test_two_targets_two_unanswered_orders_only_second_filled(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = unknown_post()
    await h.engine.on_fill(fill(entityId=T1), WS)
    await h.engine.on_fill(fill(entityId=T2), WS)
    assert len(h.state.pending_orders()) == 2
    h.ex.late_fill = TradeFill(candidates=[{"orderId": "0xsecond", "shares": 19_000_000, "usdc": 9_690_000}])
    h.clock["t"] += 1_000
    await h.engine.tick()
    assert h.state.positions() == []
    assert all(p.get("needsReconcile") for p in h.state.pending_orders())
    second = next(p for p in h.state.pending_orders() if p["target"] == T2)
    await h.engine.reconcile(second["key"], (19_000_000, 9_690_000))
    await h.engine.reconcile(h.state.pending_orders()[0]["key"], None)
    assert h.state.position(T2, "TOK")["shares"] == "19000000"
    assert h.state.position(T1, "TOK") is None


async def test_gives_up_only_after_repeated_empty_lookups_over_5_minutes(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = killed()
    await h.engine.on_fill(fill(), WS)
    for _ in range(20):
        if not h.state.pending_orders():
            break
        h.clock["t"] += 60_000
        await h.engine.tick()
    assert h.state.pending_orders() == []
    assert h.last()["decision"] == "confirmed_no_fill"
    assert h.clock["t"] - NOW >= 5 * 60_000


async def test_sell_capped_at_balance(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.balance = 10_000_000  # less than the 19 we think we hold
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.ex.sells == [(to_micro("0.49"), 10_000_000)]
    assert h.state.position(T1, "TOK")["shares"] == "9000000"
    assert h.kinds()[-3:] == ["exit_queued", "sell_submitted", "sold"]
    assert len(h.state.pending_exits()) == 1  # 9 shares still booked: keep trying


async def test_never_sells_shares_booked_to_another_target(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(entityId=T1), WS)
    await h.engine.on_fill(fill(entityId=T2), WS)
    h.ex.balance = 25_000_000  # books say 19 + 19; someone sold 13 by hand
    await h.engine.on_fill(fill(entityId=T1, side="SELL"), WS)
    assert h.ex.sells == [(to_micro("0.49"), 6_000_000)]  # 25 − T2's 19
    assert h.state.position(T2, "TOK")["shares"] == "19000000"


async def test_blocks_and_asks_for_reconcile_when_all_left_is_others(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(entityId=T1), WS)
    await h.engine.on_fill(fill(entityId=T2), WS)
    h.ex.balance = 19_000_000
    await h.engine.on_fill(fill(entityId=T1, side="SELL"), WS)
    assert h.last()["decision"] == "exit_retry_balance_short"  # could be a lagging balance
    for _ in range(10):
        if not h.state.pending_exits():
            break
        h.clock["t"] += 5 * 60_000
        await h.engine.tick()
    assert h.ex.sells == []
    assert h.last()["decision"] == "exit_blocked_reconcile"


async def test_zero_balance_right_after_a_late_fill_is_not_nothing_to_sell(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = killed()
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL"), WS)
    h.ex.late_fill = TradeFill(19_000_000, 9_690_000, 0, 0, ["o9"])
    h.ex.balance = 0  # the balance endpoint has not caught up
    h.clock["t"] += 1_000
    await h.engine.tick()  # books the late BUY, exit sees 0
    assert h.state.position(T1, "TOK")["shares"] == "19000000"
    assert len(h.state.pending_exits()) == 1
    again = h.restart()  # …and it survives a restart
    h.ex.balance = 19_000_000
    h.clock["t"] += 5_000
    await again.tick()
    assert h.ex.sells == [(to_micro("0.49"), 19_000_000)]
    assert BotState(h.dir, "live").positions() == []


async def test_zero_balance_for_10_minutes_finally_closes_the_books(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.balance = 0
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert len(h.state.positions()) == 1
    for _ in range(10):
        if not h.state.pending_exits():
            break
        h.clock["t"] += 5 * 60_000
        await h.engine.tick()
    assert h.state.positions() == []
    assert h.last()["decision"] == "exit_no_balance"


async def test_other_retries_do_not_count_as_low_balance_readings(tmp_path):
    from dataclasses import replace
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.market_ = replace(h.ex.market_, acceptingOrders=False)
    await h.engine.on_fill(fill(side="SELL"), WS)
    for _ in range(6):  # 30 min, 7 retries
        h.clock["t"] += 5 * 60_000
        await h.engine.tick()
    h.ex.market_ = replace(h.ex.market_, acceptingOrders=True)
    h.ex.balance = 0  # a single stale 0
    h.clock["t"] += 5 * 60_000
    await h.engine.tick()
    assert len(h.state.positions()) == 1
    assert h.last()["decision"] == "exit_retry_zero_balance" and h.last()["lowReads"] == 1


async def test_seeing_shares_ends_a_run_of_low_readings_even_if_the_sell_fails(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.balance = 0
    await h.engine.on_fill(fill(side="SELL"), WS)
    for _ in range(3):  # 4 low readings, 15 min
        h.clock["t"] += 5 * 60_000
        await h.engine.tick()
    assert h.state.pending_exits()[0]["lowBalanceReads"] == 4
    h.ex.balance = 19_000_000  # shares show up
    h.ex.sell_result = lambda s, l: OrderOutcome("", "failed", reason="rejected")
    h.clock["t"] += 5 * 60_000
    await h.engine.tick()
    assert h.state.pending_exits()[0]["lowBalanceReads"] == 0
    h.ex.balance = 0  # one stale 0 again
    h.clock["t"] += 5 * 60_000
    await h.engine.tick()
    assert len(h.state.positions()) == 1
    assert h.last()["decision"] == "exit_retry_zero_balance" and h.last()["lowReads"] == 1


async def test_low_readings_while_a_buy_is_pending_never_accumulate(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)  # 19 booked
    h.ex.buy_result = killed("o2")
    await h.engine.on_fill(fill(), WS)  # DCA add pending
    h.ex.balance = 0
    await h.engine.on_fill(fill(side="SELL"), WS)
    for _ in range(6):
        h.clock["t"] += 5 * 60_000
        await h.engine.tick()
    # the add resolved as "no fill" somewhere in there; the run of low readings only started afterwards
    assert len(h.state.positions()) == 1
    assert "exit_retry_balance_short_buy_pending" in h.kinds()


async def test_transient_failure_is_retried_until_the_exit_happens(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.balance_fails = 2
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.ex.sells == []
    assert h.last()["decision"] == "exit_retry_balance_failed"
    h.clock["t"] += 1_000
    await h.engine.tick()
    h.clock["t"] += 2_000
    await h.engine.tick()
    assert len(h.ex.sells) == 1
    assert h.state.positions() == []
    assert h.state.pending_exits() == []
    assert h.last()["decision"] == "exit_done"


async def test_pending_exit_survives_a_restart(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.balance_fails = 1
    await h.engine.on_fill(fill(side="SELL"), WS)
    again = h.restart()
    h.clock["t"] += 1_000
    await again.tick()
    assert len(h.ex.sells) == 1


async def test_paused_market_keeps_the_exit(tmp_path):
    from dataclasses import replace
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.market_ = replace(h.ex.market_, acceptingOrders=False)
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.last()["decision"] == "exit_retry_market_paused"
    h.ex.market_ = replace(h.ex.market_, acceptingOrders=True)
    h.clock["t"] += 1_000
    await h.engine.tick()
    assert len(h.ex.sells) == 1
    assert h.state.positions() == []


async def test_old_sell_still_exits(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL", ts="2026-09-24 09:00:00"), REPLAY)
    assert h.state.positions() == []


async def test_unconfirmed_exit_order_is_waited_for_not_doubled(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.sell_result = killed("s9", "no orders found")
    await h.engine.on_fill(fill(side="SELL"), WS)
    h.clock["t"] += 1_000
    h.ex.late_fill = TradeFill(19_000_000, 9_310_000, 0, 0, ["s9"])
    await h.engine.tick()  # books the late sell first, then the exit finds nothing left
    assert len(h.ex.sells) == 1
    assert h.state.positions() == []


async def test_sell_during_unconfirmed_buy_is_kept_and_executed_once_filled(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = killed()
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.last()["decision"] == "exit_queued"
    assert h.ex.sells == []
    # restart, then the BUY is found filled
    again = h.restart()
    h.ex.late_fill = TradeFill(19_000_000, 9_690_000, 0, 0, ["o9"])
    h.clock["t"] += 1_000
    await again.tick()
    h.clock["t"] += 1_000
    await again.tick()
    assert h.ex.sells == [(to_micro("0.49"), 19_000_000)]
    assert BotState(h.dir, "live").positions() == []


async def test_exit_outlives_the_position_while_a_dca_add_is_unconfirmed(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)  # first BUY: 19 shares booked
    h.ex.buy_result = killed("o2")
    await h.engine.on_fill(fill(), WS)  # the add is unconfirmed
    await h.engine.on_fill(fill(side="SELL"), WS)  # target exits: the 19 are sold
    assert h.ex.sells == [(to_micro("0.49"), 19_000_000)]
    assert len(h.state.pending_exits()) == 1  # …but the exit stays
    h.ex.late_fill = TradeFill(19_000_000, 9_690_000, 0, 0, ["o2"])
    h.clock["t"] += 1_000
    await h.engine.tick()  # the add did fill
    h.clock["t"] += 1_000
    await h.engine.tick()
    assert len(h.ex.sells) == 2
    assert h.state.positions() == []
    assert h.state.pending_exits() == []


async def test_sell_during_unconfirmed_buy_is_dropped_once_reconciled_as_none(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = killed()
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL"), WS)
    await h.engine.reconcile(h.state.pending_orders()[0]["key"], None)
    h.clock["t"] += 60_000
    await h.engine.tick()
    assert h.state.pending_exits() == []
    assert h.ex.sells == []
    assert h.last()["decision"] == "exit_done"


async def test_never_sells_another_targets_position(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(entityId=T2, side="SELL"), WS)
    assert h.last()["decision"] == "skipped_no_position"
    assert h.state.position(T1, "TOK") is not None


async def test_dry_run_sell(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.last()["decision"] == "dry_run_sell" and h.last()["fillPrice"] == 0.49
    assert h.state.positions() == []


async def test_sell_mode_none(tmp_path):
    h = H(tmp_path, {"copy": {"sellMode": "none"}})
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.last()["decision"] == "skipped_sell_mode_none"


async def test_reconcile_refuses_amounts_the_order_could_not_have_produced(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = unknown_post()
    await h.engine.on_fill(fill(), WS)  # 19 shares at limit 0.51 → at most $9.69
    key = h.state.pending_orders()[0]["key"]
    with pytest.raises(ValueError, match="negative"):
        await h.engine.reconcile(key, (19_000_000, -100_000_000))
    with pytest.raises(ValueError, match="was for 19 shares; 20 cannot"):
        await h.engine.reconcile(key, (20_000_000, 9_000_000))
    with pytest.raises(ValueError, match=r"\$12\.00 for 19 shares is above the BUY limit \(\$9\.69 max\)"):
        await h.engine.reconcile(key, (19_000_000, 12_000_000))
    with pytest.raises(ValueError, match="above 0"):
        await h.engine.reconcile(key, (0, 0))
    assert len(h.state.pending_orders()) == 1
    assert h.state.reserved_usdc() == 9_690_000
    await h.engine.reconcile(key, (19_000_000, 9_690_000))
    assert h.state.pending_orders() == []


async def test_settlement_sweep(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    h.ex.market_ = Market("CID", "Will it?", True, True, True, "2026-12-31T00:00:00Z", [Token("TOK", "Yes", winner=True)])
    await h.engine.sweep_settled()
    assert h.state.positions() == []
    last = h.last()
    assert (last["decision"], last["won"], last["payout"]) == ("settled", True, "$19.00")


def test_instance_lock(tmp_path):
    import socket
    a = InstanceLock(str(tmp_path), "live")
    a.acquire()
    with pytest.raises(RuntimeError, match="another pmwallets-copytrade"):
        InstanceLock(str(tmp_path), "live").acquire()
    InstanceLock(str(tmp_path), "dry-run").acquire()
    a.release()
    (tmp_path / "lock.live").write_text(json.dumps({"pid": 2**22 + 12345, "host": socket.gethostname()}))
    InstanceLock(str(tmp_path), "live").acquire()


def test_instance_lock_reads_a_node_lock_held_by_a_live_process(tmp_path):
    import os
    import socket
    (tmp_path / "lock.live").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "startedAt": "2026-09-24T12:00:00.000Z"}))
    with pytest.raises(RuntimeError, match="another pmwallets-copytrade"):
        InstanceLock(str(tmp_path), "live").acquire()
