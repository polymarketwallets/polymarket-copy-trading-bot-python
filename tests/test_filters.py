from datetime import datetime, timezone

from pmwallets_copytrade.config import CopyConfig
from pmwallets_copytrade.filters import book_gate, market_gate, parse_market_end_date, slippage_gate
from pmwallets_copytrade.polymarket import Book, Level, Market
from pmwallets_copytrade.units import to_micro

cfg = CopyConfig()


def mkt(**o):
    base = dict(conditionId="c", question="q", closed=False, active=True, acceptingOrders=True, endDate="2030-01-01T00:00:00Z", tokens=[])
    base.update(o)
    return Market(**base)


def lv(p, s):
    return Level(to_micro(p), to_micro(s))


def ms(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def test_market_gate():
    now = ms("2029-12-31T23:55:00Z")
    g = market_gate(mkt(), "buy", cfg, now)
    assert g["ok"] is False and "settles_in_300s" in g["reason"]
    assert market_gate(mkt(), "sell", cfg, now) == {"ok": True}
    assert market_gate(mkt(closed=True), "sell", cfg, now)["ok"] is False
    assert market_gate(mkt(endDate=None), "buy", CopyConfig(maxSecondsToEndDate=3600), now)["reason"] == "market_end_date_unknown"


def test_book_gate():
    book = Book("t", asks=[lv("0.97", "1000")], bids=[lv("0.96", "1")])
    assert "out_of_band" in book_gate(book, "buy", cfg)["reason"]
    assert book_gate(book, "sell", cfg) == {"ok": True}
    assert book_gate(Book("t", asks=[lv("0.5", "10"), lv("0.51", "10")], bids=[]), "buy", cfg)["reason"] == "depth_10.10_below_50"


def test_slippage():
    assert slippage_gate(to_micro("0.53"), to_micro("0.50"), cfg)["ok"]
    assert not slippage_gate(to_micro("0.54"), to_micro("0.50"), cfg)["ok"]


def test_end_dates():
    assert parse_market_end_date("2026-02-31") is None
    assert parse_market_end_date("0") is None
    assert parse_market_end_date("2026-02-28") == ms("2026-02-28T00:00:00Z")
