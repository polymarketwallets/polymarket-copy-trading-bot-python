import asyncio
import itertools
import json
from datetime import datetime, timezone

import pytest

from pmwallets import FillMeta
from pmwallets_copytrade.config import TargetConfig, build_config
from pmwallets_copytrade.engine import CopyEngine
from pmwallets_copytrade.polymarket import Book, Level, Market, OrderOutcome, Token, TradeFill
from pmwallets_copytrade.state import BotState
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

    async def condition_id_for(self, token_id):
        return "CID"

    async def market(self, condition_id, max_age_ms=30_000):
        return self.market_

    async def orderbook(self, token_id):
        return self.book

    async def buy_fok(self, token_id, condition_id, limit, shares):
        self.buys.append((limit, shares))
        return self.buy_result(shares, limit)

    async def sell_fak(self, token_id, condition_id, limit, shares):
        self.sells.append((limit, shares))
        return self.sell_result(shares, limit)

    async def fills_of(self, order_id, condition_id, since_ms):
        return self.late_fill

    async def token_balance(self, token_id):
        return self.balance


LIVE = {"mode": "live", "polymarket": {"privateKey": "ab" * 32, "signatureType": 0}}


class H:
    def __init__(self, tmp_path, raw=None, targets=None):
        self.dir = str(tmp_path)
        self.cfg = build_config({"pmwallets": {"apiKey": "pmw_a_b"}, "dataDir": self.dir, **(raw or {})})
        self.ex = FakeExchange()
        self.state = BotState(self.dir, self.cfg.mode)
        self.engine = CopyEngine(self.cfg, self.ex, self.state, Silent(), {t: TargetConfig(t) for t in targets} if targets else None,
                                 now=lambda: NOW, recheck_s=0.005)

    def decisions(self):
        return [json.loads(l) for l in self.state.decisions_file.read_text().strip().split("\n")]

    def last(self):
        return self.decisions()[-1]


async def test_dry_run_buy(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    assert h.last()["decision"] == "dry_run_buy" and h.last()["at"] == 0.51
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


async def test_late_fill_is_booked(tmp_path):
    h = H(tmp_path, LIVE)
    h.ex.buy_result = lambda s, l: OrderOutcome("o9", "none", reason="order couldn't be fully filled", recheck=True)
    h.ex.late_fill = TradeFill(12_000_000, 6_120_000, 0, 0)
    await h.engine.on_fill(fill(), WS)
    assert h.state.position(T1, "TOK") is None
    await asyncio.sleep(0.05)
    assert h.state.position(T1, "TOK")["shares"] == "12000000"
    assert h.last()["decision"] == "late_fill" and h.last()["orderId"] == "o9"
    await h.engine.stop()


async def test_sell_capped_at_balance(tmp_path):
    h = H(tmp_path, LIVE)
    await h.engine.on_fill(fill(), WS)
    h.ex.balance = 10_000_000
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.ex.sells == [(to_micro("0.49"), 10_000_000)]
    assert h.state.position(T1, "TOK")["shares"] == "9000000"
    assert h.last()["decision"] == "sold"


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
    assert h.last()["decision"] == "dry_run_sell" and h.last()["at"] == 0.49
    assert h.state.positions() == []


async def test_sell_mode_none(tmp_path):
    h = H(tmp_path, {"copy": {"sellMode": "none"}})
    await h.engine.on_fill(fill(), WS)
    await h.engine.on_fill(fill(side="SELL"), WS)
    assert h.last()["decision"] == "skipped_sell_mode_none"


async def test_settlement_sweep(tmp_path):
    h = H(tmp_path)
    await h.engine.on_fill(fill(), WS)
    h.ex.market_ = Market("CID", "Will it?", True, True, True, "2026-12-31T00:00:00Z", [Token("TOK", "Yes", winner=True)])
    await h.engine.sweep_settled()
    assert h.state.positions() == []
    last = h.last()
    assert (last["decision"], last["won"], last["payout"]) == ("settled", True, "$19.00")
