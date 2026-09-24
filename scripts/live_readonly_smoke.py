"""Manual smoke test against the real, public Polymarket CLOB (read-only). Not part of pytest:
    python scripts/live_readonly_smoke.py
"""
import asyncio

import httpx

from pmwallets_copytrade.config import build_config
from pmwallets_copytrade.filters import book_gate, market_gate
from pmwallets_copytrade.log import ConsoleLogger
from pmwallets_copytrade.polymarket import PolymarketGateway
from pmwallets_copytrade.units import from_micro


async def main() -> None:
    cfg = build_config({"pmwallets": {"apiKey": "pmw_x_y"}})
    pm = PolymarketGateway(cfg.polymarket, ConsoleLogger())
    async with httpx.AsyncClient(timeout=15) as http:
        sample = (await http.get("https://clob.polymarket.com/sampling-markets")).json()
    for m in sample["data"][:3]:
        token_id = m["tokens"][0]["token_id"]
        cid = await pm.condition_id_for(token_id)
        market = await pm.market(cid)
        book = await pm.orderbook(token_id)
        print({
            "question": market.question[:50], "cidMatches": cid == m["condition_id"], "endDate": market.endDate,
            "bestAsk": book.asks and from_micro(book.asks[0].price), "bestBid": book.bids and from_micro(book.bids[0].price),
            "sorted": all(a.price <= b.price for a, b in zip(book.asks, book.asks[1:])) and all(a.price >= b.price for a, b in zip(book.bids, book.bids[1:])),
            "tick": book.tickSize and from_micro(book.tickSize), "minOrder": book.minOrderSize and from_micro(book.minOrderSize),
            "marketGate": market_gate(market, "buy", cfg.copy), "bookGate": book_gate(book, "buy", cfg.copy),
        })


asyncio.run(main())
