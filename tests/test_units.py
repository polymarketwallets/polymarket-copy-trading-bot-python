import math
from datetime import datetime, timezone

import pytest

from pmwallets_copytrade.units import clamp_limit, parse_fill_ts, round_buy_shares, round_sell_shares, to_micro


@pytest.mark.parametrize("price,step", [("0.99", 1_000_000), ("0.915", 2_000_000), ("0.997", 10_000_000), ("0.48", 250_000), ("0.5", 20_000)])
def test_buy_rounding_grids(price, step):
    p = to_micro(price)
    assert round_buy_shares(step * 7 + 1, p) == step * 7
    s = round_buy_shares(123_456_789, p)
    assert s % 10_000 == 0  # what the SDK signs
    assert (s * p) % 10_000_000_000 == 0  # what the server checks


def test_live_case_2_usd_at_048():
    assert round_buy_shares(to_micro(2) * 1_000_000 // to_micro("0.48"), to_micro("0.48")) == 4_000_000


def test_sell_floor():
    assert round_sell_shares(50_123_456) == 50_123_400


def test_clamp():
    assert clamp_limit(to_micro("0.999")) == 990_000
    assert clamp_limit(to_micro("0.999"), to_micro("0.001")) == 999_000
    assert clamp_limit(to_micro("0.5"), to_micro("0.01")) == 500_000


def test_fill_ts_is_utc():
    assert parse_fill_ts("2026-09-16 14:02:11") == datetime(2026, 9, 16, 14, 2, 11, tzinfo=timezone.utc).timestamp() * 1000
    assert math.isnan(parse_fill_ts("garbage"))
