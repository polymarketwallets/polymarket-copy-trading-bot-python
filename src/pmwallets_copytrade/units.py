"""Fixed-point helpers. Prices, shares and USDC are ints in micro-units (1e-6), the resolution Polymarket
settles in — floats only at the edges. The size rounding is lifted from a bot that ran live on Polymarket;
each rule exists because the CLOB rejected an order without it."""
from __future__ import annotations

import math
import re
from datetime import datetime
from math import gcd
from typing import Optional, Union

UNIT = 1_000_000


def to_micro(x: Union[int, float, str]) -> int:
    n = float(x)
    if not math.isfinite(n):
        raise ValueError(f"not a number: {x}")
    # round half away from zero like JS Math.round for positives
    return int(math.floor(n * 1e6 + 0.5))


def from_micro(v: int) -> float:
    return v / 1e6


def js_num(x: float) -> str:
    """A number printed the way JavaScript prints it (19 not 19.0) — messages must read the same as the Node bot's."""
    return str(int(x)) if math.isfinite(x) and x == int(x) else repr(x)


def fmt_usd(v: int) -> str:
    return f"${from_micro(v):.2f}"


def clamp_limit(price: int, tick: Optional[int] = None) -> int:
    """Highest price the CLOB accepts is 1 - tick (0.99 when the tick is unknown). Clamping a limit DOWN is
    execution-safe: a match fills at the resting order's price."""
    mx = UNIT - tick if tick and 0 < tick < UNIT else 990_000
    return mx if price > mx else price


def round_buy_shares(shares: int, price: int) -> int:
    """BUY size must sit on the SDK's 0.01-share grid AND make size × price a whole cent; round DOWN to the
    lcm of the two steps."""
    if price <= 0:
        return 0
    cent_step = 10_000_000_000 // gcd(price, 10_000_000_000)
    share_step = 10_000
    step = cent_step * share_step // gcd(cent_step, share_step)
    return (shares // step) * step


def round_sell_shares(shares: int) -> int:
    """SELL: no cent rule — floor to 1e-4 share so a full exit leaves no dust."""
    return (shares // 100) * 100


_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def parse_fill_ts(ts: str) -> float:
    """Block time from the feed (`YYYY-MM-DD HH:MM:SS`, UTC) → epoch ms; NaN when malformed."""
    if not isinstance(ts, str) or not _TS.match(ts):
        return math.nan
    iso = ts.replace(" ", "T", 1)
    if not re.search(r"[zZ]|[+-]\d{2}:?\d{2}$", iso):
        iso += "+00:00"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00").replace("z", "+00:00")).timestamp() * 1000
    except ValueError:
        return math.nan
