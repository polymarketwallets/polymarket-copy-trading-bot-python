from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

from .config import CopyConfig
from .polymarket import Book, Market
from .units import from_micro

Gate = dict  # {"ok": True} | {"ok": False, "reason": str}
PASS: Gate = {"ok": True}

_END = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d))?$")


def parse_market_end_date(value: Optional[str]) -> Optional[float]:
    """ISO or date-only strings only (epoch ms). A bare number such as "0" (upstream's "unknown") is not a date;
    calendar overflow (Feb 31) is rejected."""
    if not value:
        return None
    raw = value.strip()
    m = _END.match(raw)
    if not m:
        return None
    try:
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    iso = raw if "T" in raw else raw + "T00:00:00Z"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


def market_gate(market: Market, side: str, cfg: CopyConfig, now: Optional[float] = None) -> Gate:
    """Market lifecycle, from the CLOB. `side` is OUR side: time-to-settle limits are an entry policy, so a SELL
    is never blocked by them. A past endDate is not a reject (sports markets trade after the whistle)."""
    now = time.time() * 1000 if now is None else now
    if market.closed:
        return {"ok": False, "reason": "market_closed"}
    if market.active is False:
        return {"ok": False, "reason": "market_inactive"}
    if market.acceptingOrders is False:
        return {"ok": False, "reason": "market_not_accepting_orders"}
    if side == "sell":
        return PASS
    end = parse_market_end_date(market.endDate)
    if end is None:
        return {"ok": False, "reason": "market_end_date_unknown"} if cfg.maxSecondsToEndDate > 0 else PASS
    secs = (end - now) / 1000
    if 0 < secs < cfg.minSecondsToEndDate:
        return {"ok": False, "reason": f"market_settles_in_{int(secs // 1)}s"}
    if cfg.maxSecondsToEndDate > 0 and secs > cfg.maxSecondsToEndDate:
        return {"ok": False, "reason": f"market_settles_in_{int(secs // 1)}s_too_far"}
    return PASS


def book_gate(book: Book, side: str, cfg: CopyConfig) -> Gate:
    """Depth and price on the side we take. The price band applies to BUYs only: for an exit any bid beats
    redeeming at zero."""
    levels = book.asks if side == "buy" else book.bids
    if not levels:
        return {"ok": False, "reason": "book_empty_side"}
    if side == "sell":
        return PASS
    px = from_micro(levels[0].price)
    if px < cfg.minPrice or px > cfg.maxPrice:
        return {"ok": False, "reason": f"price_{px:.3f}_out_of_band"}
    depth = 0.0
    for lv in levels:
        depth += from_micro(lv.price) * from_micro(lv.size)
        if depth >= cfg.minBookDepthUsdc:
            return PASS
    return {"ok": False, "reason": f"depth_{depth:.2f}_below_{cfg.minBookDepthUsdc:g}"}


def slippage_gate(best_ask: int, target_price: int, cfg: CopyConfig) -> Gate:
    """We see the fill after the target; if the ask has run away, the copy is a different trade."""
    over = from_micro(best_ask) - from_micro(target_price)
    if over > cfg.maxSlippage + 1e-9:
        return {"ok": False, "reason": f"ask_{from_micro(best_ask):.3f}_vs_target_{from_micro(target_price):.3f}"}
    return PASS
