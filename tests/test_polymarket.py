import json
from pathlib import Path

import httpx
import pytest
from py_clob_client_v2.exceptions import PolyApiException

from pmwallets_copytrade.config import PolymarketConfig
from pmwallets_copytrade.polymarket import OrderMatch, PolymarketGateway, attribute_fills, classify_post
from pmwallets_copytrade.units import to_micro

CASES = json.loads((Path(__file__).resolve().parents[1] / "testdata" / "post-classification.json").read_text())["cases"]


class Silent:
    def info(self, *a, **k): ...
    def warn(self, *a, **k): ...
    def error(self, *a, **k): ...


class FakeClob:
    """py-clob-client-v2 as it really behaves: a 2xx is the returned dict; an HTTP error raises
    PolyApiException(response); no response at all raises a transport error."""

    def __init__(self, http, body):
        self.http, self.body = http, body

    def create_order(self, args):
        return object()

    def post_order(self, signed, order_type):
        if self.http is None:
            raise httpx.ConnectError("socket hang up")
        if 200 <= self.http < 300:
            return self.body
        content = b"" if self.body is None else (self.body.encode() if isinstance(self.body, str) else json.dumps(self.body).encode())
        raise PolyApiException(httpx.Response(self.http, content=content))

    def get_trades(self, params, only_first_page=False):
        return []


# what each classification must turn into at the gateway's output
OUTCOME = {"answer": {"filled", "none"}, "rejected": {"failed"}, "unknown": {"unknown"}}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_post_classification_contract_through_the_gateway(case):
    gw = PolymarketGateway(PolymarketConfig(), Silent())
    gw.clob = FakeClob(case["http"], case["body"])
    r = await gw.buy_fok("TOK", "CID", to_micro("0.5"), to_micro(10))
    assert r.status in OUTCOME[case["expect"]], (case["name"], r)


def test_classify_post_rule():
    for c in CASES:
        # the pair the gateway hands to classify_post: (status, parsed body) — a PolyApiException carries resp.json() or the text
        body = c["body"] if isinstance(c["body"], dict) else (c["body"] or "")
        assert classify_post(c["http"], body if c["http"] is not None else None) == c["expect"], c["name"]


def T(**o):
    t = {"taker_order_id": "0xa", "trader_side": "TAKER", "asset_id": "TOK", "side": "BUY", "size": "10", "price": "0.50",
         "fee_rate_bps": "0", "match_time": "1790000000"}
    t.update(o)
    return t


def match(shares=None, limit=None, booked=()):
    return OrderMatch("TOK", "buy", shares if shares is not None else to_micro(10), limit if limit is not None else to_micro("0.51"),
                      lambda i: i in booked)


SINCE = 1_790_000_000_000 - 1000


def test_known_id_every_trade_of_that_order():
    f = attribute_fills([T(), T(taker_order_id="0xb"), T(size="5")], "0xA", 0)
    assert f.shares == to_micro(15) and f.orderIds == ["0xa"]


def test_unknown_id_nothing_attributed_only_candidates():
    f = attribute_fills([T(), T(taker_order_id="0xz", asset_id="OTHER")], None, SINCE, match())
    assert f.shares == 0
    assert f.candidates == [{"orderId": "0xa", "shares": to_micro(10), "usdc": to_micro(5)}]


def test_not_even_a_candidate():
    for t in (T(size="11"), T(price="0.52"), T(trader_side="MAKER"), T(match_time="1789999000")):
        assert attribute_fills([t], None, SINCE, match()).candidates == []
    assert attribute_fills([T()], None, SINCE, match(booked=("0xa",))).candidates == []


class BalanceClob:
    def __init__(self, update, balance="0"):
        self.update, self.balance = update, balance

    def update_balance_allowance(self, params):
        if isinstance(self.update, Exception):
            raise self.update
        return self.update

    def get_balance_allowance(self, params):
        return {"balance": self.balance}


async def test_token_balance_failed_refresh_fails_the_read():
    def gw(update, balance="0"):
        g = PolymarketGateway(PolymarketConfig(clobUrl="http://x", signatureType=0), Silent())
        g.clob = BalanceClob(update, balance)
        return g

    with pytest.raises(RuntimeError, match="refresh failed"):
        await gw({"error": "internal"}).token_balance("T")
    # py-clob-client-v2 raises on HTTP errors: read like the TS client's returned error body, same message
    with pytest.raises(RuntimeError, match="balance refresh failed: internal"):
        await gw(PolyApiException(httpx.Response(500, content=b'{"error":"internal"}'))).token_balance("T")
    with pytest.raises(httpx.ConnectError, match="ECONNRESET"):
        await gw(httpx.ConnectError("ECONNRESET")).token_balance("T")
    assert await gw("", "19000000").token_balance("T") == 19_000_000



async def test_balance_http_error_reads_like_the_node_bot():
    class Clob:
        def get_balance_allowance(self, params):
            raise PolyApiException(httpx.Response(404, content=b'{"error":"no deposit wallet found for owner"}'))

    g = PolymarketGateway(PolymarketConfig(clobUrl="http://x", signatureType=3), Silent())
    g.clob = Clob()
    with pytest.raises(RuntimeError) as e:
        await g.collateral_balance()
    assert str(e.value) == "no deposit wallet found for owner"



async def test_collateral_approvals():
    """reads spender → amount, and treats a missing or malformed map as none known"""
    class Clob:
        def __init__(self, resp):
            self.resp = resp

        def update_balance_allowance(self, params):
            return ""

        def get_balance_allowance(self, params):
            return self.resp

    def gw(resp):
        g = PolymarketGateway(PolymarketConfig(clobUrl="http://x", signatureType=0), Silent())
        g.clob = Clob(resp)
        return g

    assert await gw({"balance": "5000000", "allowances": {"0xA": "0", "0xB": "1000"}}).collateral() == (5_000_000, {"0xA": 0, "0xB": 1000})
    assert (await gw({"balance": "5000000"}).collateral())[1] == {}
    assert (await gw({"balance": "5000000", "allowances": []}).collateral())[1] == {}
    assert (await gw({"balance": "5000000", "allowances": "x"}).collateral())[1] == {}



_CID = "0xe1648bc0c286911bcb5fc228972268d3ca413aa4a6b4a6b03b8c983ba706f957"
_CLOB = {"condition_id": _CID, "question": "Bitcoin Up or Down - 5m", "closed": False, "active": True, "accepting_orders": True,
         "end_date_iso": "2026-09-25T00:00:00Z", "tokens": []}


class _Venues:
    """a fake CLOB + Gamma; `gamma` can be swapped between calls"""
    def __init__(self, gamma):
        self.gamma, self.gamma_calls = gamma, 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "clob":
            return httpx.Response(200, json=_CLOB)
        self.gamma_calls += 1
        return self.gamma(req)


def _at(end):
    return lambda req: httpx.Response(200, json=[{"conditionId": _CID, "endDate": end}])


def _gw(venues):
    http = httpx.AsyncClient(transport=httpx.MockTransport(venues))
    return PolymarketGateway(PolymarketConfig(clobUrl="http://clob", signatureType=0), Silent(), http)


async def test_takes_the_end_time_from_gamma_the_clob_gives_short_markets_only_a_date():
    assert (await _gw(_Venues(_at("2026-09-25T03:45:00Z"))).market(_CID, 30_000, True)).endDate == "2026-09-25T03:45:00Z"


@pytest.mark.parametrize("gamma", [
    lambda req: httpx.Response(502, text="oops"),
    lambda req: httpx.Response(200, json=[]),
    lambda req: httpx.Response(200, json=[{"conditionId": "0xother", "endDate": "2026-09-25T03:45:00Z"}]),
    lambda req: httpx.Response(200, json=[{"conditionId": _CID, "endDate": "soon"}]),
], ids=["down", "unknown market", "another market", "no usable date"])
async def test_falls_back_to_the_clob_date_when_gamma_cannot_say(gamma):
    assert (await _gw(_Venues(gamma)).market(_CID, 30_000, True)).endDate == "2026-09-25T00:00:00Z"


async def test_asks_gamma_only_when_the_caller_needs_the_end_date():
    v = _Venues(_at("2026-09-25T03:45:00Z"))
    assert (await _gw(v).market(_CID, 0)).endDate == "2026-09-25T00:00:00Z"
    assert v.gamma_calls == 0


async def test_follows_an_end_date_that_moves():
    v = _Venues(_at("2026-09-25T03:45:00Z"))
    g = _gw(v)
    assert (await g.market(_CID, 30_000, True)).endDate == "2026-09-25T03:45:00Z"
    v.gamma = _at("2026-09-26T03:45:00Z")
    assert (await g.market(_CID, 0, True)).endDate == "2026-09-26T03:45:00Z"


async def test_remembers_that_gamma_failed_so_an_outage_costs_one_wait_per_market():
    v = _Venues(lambda req: httpx.Response(502, text="oops"))
    g = _gw(v)
    await g.market(_CID, 0, True)
    assert (await g.market(_CID, 0, True)).endDate == "2026-09-25T00:00:00Z"
    assert v.gamma_calls == 1


async def test_gives_up_on_a_slow_gamma_as_a_whole_not_per_phase(monkeypatch):
    import asyncio
    from pmwallets_copytrade import polymarket
    monkeypatch.setattr(polymarket, "GAMMA_TIMEOUT_S", 0.05)

    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "clob":
            return httpx.Response(200, json=_CLOB)
        await asyncio.sleep(5)
        return _at("2026-09-25T03:45:00Z")(req)

    g = PolymarketGateway(PolymarketConfig(clobUrl="http://clob", signatureType=0), Silent(),
                          httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    m = await asyncio.wait_for(g.market(_CID, 0, True), 1.0)
    assert m.endDate == "2026-09-25T00:00:00Z"
