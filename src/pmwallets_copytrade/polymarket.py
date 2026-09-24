"""The Polymarket side: market lookups, order books and order placement on the CLOB v2.

Ported from a copy-trading bot that ran live on Polymarket (via the Node port of this package). The comments
keep the reason for each rule — every one answers an order that was rejected or a fill that was lost.

py-clob-client-v2 is synchronous; every call to it runs in a worker thread (asyncio.to_thread).
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from .config import PolymarketConfig
from .log import Logger
from .units import UNIT, clamp_limit, from_micro, round_buy_shares, round_sell_shares, to_micro


@dataclass
class Level:
    price: int
    size: int


@dataclass
class Book:
    tokenId: str
    bids: list[Level]
    asks: list[Level]
    tickSize: Optional[int] = None
    minOrderSize: Optional[int] = None


@dataclass
class Token:
    tokenId: str
    outcome: str
    winner: Optional[bool] = None
    price: Optional[float] = None


@dataclass
class Market:
    conditionId: str
    question: str
    closed: bool
    active: bool
    acceptingOrders: Optional[bool] = None
    endDate: Optional[str] = None
    tokens: list[Token] = field(default_factory=list)


@dataclass
class OrderOutcome:
    orderId: str
    # filled  = got shares (possibly fewer than asked for a FAK)
    # none    = the exchange took the order and matched nothing
    # failed  = nothing was sent, or the exchange rejected it outright: no fill is possible
    # unknown = sent, but no usable answer came back (timeout, dropped connection): it may have filled
    status: str
    shares: int = 0
    usdc: int = 0  # paid (BUY) or received (SELL), fees not deducted
    feeUsdc: int = 0
    netShares: int = 0  # BUY fees are taken in shares; this is what actually lands in the wallet
    reason: Optional[str] = None
    recheck: bool = False  # "killed" responses have carried real partial fills — re-check later


@dataclass
class TradeFill:
    shares: int = 0
    usdc: int = 0
    feeUsdc: int = 0
    feeShares: int = 0
    orderIds: list[str] = field(default_factory=list)


@dataclass
class FillMatch:
    """How to recognise our fills when the order id never came back."""

    tokenId: str
    side: str  # "buy" | "sell"
    is_booked: Callable[[str], bool]


def trade_time_ms(v: Any) -> float:
    """match_time comes as unix seconds (string or number) or an ISO string."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v * 1000 if v < 1e12 else v
    s = str(v if v is not None else "")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        n = float(s)
        return n * 1000 if n < 1e12 else n
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except ValueError:
        return 0


_ZERO_FILL = re.compile(r"no orders found|couldn't be fully filled|fully filled or killed", re.I)


def _to_micro_balance(v: Any) -> int:
    s = str(v)
    return to_micro(s) if "." in s else int(s)


class PolymarketGateway:
    def __init__(self, cfg: PolymarketConfig, log: Logger, http: Optional[httpx.AsyncClient] = None) -> None:
        self.cfg = cfg
        self.log = log
        self.clob: Any = None
        self._http = http or httpx.AsyncClient(timeout=10.0)  # trust_env: honours HTTPS_PROXY
        self._token_to_condition: dict[str, str] = {}
        self._tick_sizes: dict[str, int] = {}
        self._market_cache: dict[str, tuple[float, Market]] = {}

    @property
    def can_trade(self) -> bool:
        return self.clob is not None

    async def connect(self) -> None:
        """Build the trading client and its L2 credentials. Without a private key the gateway is read-only."""
        if not self.cfg.privateKey:
            return
        from py_clob_client_v2 import ApiCreds, ClobClient

        cfg = self.cfg

        def build() -> Any:
            base = dict(host=cfg.clobUrl, chain_id=137, key=cfg.privateKey, signature_type=cfg.signatureType, funder=cfg.funderAddress)
            creds = None
            if cfg.apiKey and cfg.apiSecret and cfg.apiPassphrase:
                creds = ApiCreds(api_key=cfg.apiKey, api_secret=cfg.apiSecret, api_passphrase=cfg.apiPassphrase)
            else:
                l1 = ClobClient(**base)
                # derive first: idempotent, returns the key this wallet already has. create-or-derive tries
                # create() first, and on an existing key the server answers 400.
                try:
                    creds = l1.derive_api_key()
                except Exception:
                    creds = None
                if not creds or not creds.api_key:
                    creds = l1.create_api_key()
                if not creds or not creds.api_key or not creds.api_secret or not creds.api_passphrase:
                    raise RuntimeError("could not derive Polymarket API credentials")
            return ClobClient(**base, creds=creds)

        self.clob = await asyncio.to_thread(build)
        signer = self.clob.get_address()
        self.log.info("polymarket trading client ready", {"signer": signer, "funder": cfg.funderAddress or signer})

    async def _get_json(self, path: str) -> Any:
        res = await self._http.get(f"{self.cfg.clobUrl}{path}")
        try:
            body = res.json()
        except ValueError:
            body = res.text
        if res.status_code >= 400:
            raise RuntimeError(f"CLOB {path} → HTTP {res.status_code}: {str(res.text)[:200]}")
        return body

    async def condition_id_for(self, token_id: str) -> str:
        hit = self._token_to_condition.get(token_id)
        if hit:
            return hit
        r = await self._get_json(f"/markets-by-token/{quote(token_id, safe='')}")
        cid = r.get("condition_id") if isinstance(r, dict) else None
        if not isinstance(cid, str) or not cid:
            raise RuntimeError(f"no market for token {token_id[:16]}…")
        self._token_to_condition[token_id] = cid
        return cid

    async def market(self, condition_id: str, max_age_ms: float = 30_000) -> Market:
        """CLOB market by condition id; cached for `max_age_ms`."""
        hit = self._market_cache.get(condition_id)
        if hit and time.time() * 1000 - hit[0] < max_age_ms:
            return hit[1]
        m = await self._get_json(f"/markets/{quote(condition_id, safe='')}")
        if not isinstance(m, dict) or m.get("error"):
            raise RuntimeError(f"CLOB market {condition_id}: {str(m)[:200]}")

        def b(v: Any, d: bool) -> bool:
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() == "true"
            return d

        end = m.get("end_date_iso")
        market = Market(
            conditionId=m.get("condition_id") or condition_id,
            question=m.get("question") or "",
            closed=b(m.get("closed"), False),
            active=b(m.get("active"), True),
            acceptingOrders=None if m.get("accepting_orders") is None else b(m.get("accepting_orders"), True),
            endDate=end if isinstance(end, str) and end else None,
            tokens=[
                Token(tokenId=str(t.get("token_id") or ""), outcome=str(t.get("outcome") or ""),
                      winner=t.get("winner") if isinstance(t.get("winner"), bool) else None,
                      price=t.get("price") if isinstance(t.get("price"), (int, float)) and not isinstance(t.get("price"), bool) else None)
                for t in (m.get("tokens") or [])
            ],
        )
        self._market_cache[condition_id] = (time.time() * 1000, market)
        return market

    async def orderbook(self, token_id: str) -> Book:
        """The CLOB returns asks DESCENDING and bids ASCENDING — the best price is LAST. Every caller reads
        levels[0] as the best, so normalise: asks ascending, bids descending."""
        ob = await self._get_json(f"/book?token_id={quote(token_id, safe='')}")
        if not isinstance(ob, dict) or not isinstance(ob.get("bids"), list) or not isinstance(ob.get("asks"), list):
            raise RuntimeError(f"malformed book for {token_id[:16]}…")
        tick = to_micro(ob["tick_size"]) if ob.get("tick_size") else None
        if tick:
            self._tick_sizes[token_id] = tick
        lv = lambda l: Level(to_micro(l["price"]), to_micro(l["size"]))  # noqa: E731
        return Book(
            tokenId=token_id,
            asks=sorted((lv(l) for l in ob["asks"]), key=lambda x: x.price),
            bids=sorted((lv(l) for l in ob["bids"]), key=lambda x: -x.price),
            tickSize=tick,
            minOrderSize=to_micro(ob["min_order_size"]) if ob.get("min_order_size") else None,
        )

    def _require_clob(self) -> Any:
        if self.clob is None:
            raise RuntimeError("trading client not connected (live mode needs a private key)")
        return self.clob

    @staticmethod
    def _failed(reason: str) -> OrderOutcome:
        return OrderOutcome(orderId="", status="failed", reason=reason)

    @staticmethod
    def _error_body(e: Exception) -> Optional[dict[str, Any]]:
        """py-clob-client-v2 raises on any non-200, but the FOK/FAK zero-fill answer is a 400 whose body
        carries the orderID — hand that body to settle() exactly like the TS client's returned body."""
        body = getattr(e, "error_msg", None)
        return body if isinstance(body, dict) else None

    async def buy_fok(self, token_id: str, condition_id: str, limit: int, shares: int) -> OrderOutcome:
        """BUY exactly `shares` at `limit` or better, or nothing (FOK). Share-denominated create_order + post_order,
        NOT a USDC-budget market order: that can overfill when the book improves between our read and the match."""
        clob = self._require_clob()
        price = clamp_limit(limit, self._tick_sizes.get(token_id))
        size = round_buy_shares(shares, price)
        if size <= 0:
            return self._failed("size_zero_after_rounding")
        since = time.time() * 1000 - 30_000
        from py_clob_client_v2 import OrderArgs, OrderType

        try:
            signed = await asyncio.to_thread(clob.create_order, OrderArgs(token_id=token_id, price=from_micro(price), size=from_micro(size), side="BUY"))
        except Exception as e:
            return self._failed(f"sign_error: {e}"[:300])  # nothing left this machine
        return await self._post(clob, signed, OrderType.FOK, "buy", condition_id, since, size, price)

    async def sell_fak(self, token_id: str, condition_id: str, limit: int, shares: int) -> OrderOutcome:
        """SELL up to `shares`, taking whatever crosses at `limit` or better, cancelling the rest (FAK)."""
        clob = self._require_clob()
        price = clamp_limit(limit, self._tick_sizes.get(token_id))
        size = round_sell_shares(shares)
        if size <= 0:
            return self._failed("size_zero_after_rounding")
        since = time.time() * 1000 - 30_000
        from py_clob_client_v2 import MarketOrderArgs, OrderType

        try:
            args = MarketOrderArgs(token_id=token_id, amount=from_micro(size), side="SELL", price=from_micro(price), order_type=OrderType.FAK)
            signed = await asyncio.to_thread(clob.create_market_order, args, None)
        except Exception as e:
            return self._failed(f"sign_error: {e}"[:300])
        return await self._post(clob, signed, OrderType.FAK, "sell", condition_id, since, size, price)

    async def _post(self, clob: Any, signed: Any, order_type: Any, side: str, condition_id: str, since: float, size: int, price: int) -> OrderOutcome:
        """Once an order has left, only a definitive answer settles it. py-clob-client-v2 raises on any non-200:
        an exception WITH a response (status code) is still the exchange's answer — its body is processed like the
        TS client's returned body (the FOK/FAK zero-fill reply is a 400 carrying the orderID; a plain rejection is
        'failed'). Only a transport error (no response at all) leaves it 'unknown': it may have filled."""
        try:
            resp = await asyncio.to_thread(clob.post_order, signed, order_type)
        except Exception as e:
            if getattr(e, "status_code", None) is None:
                return OrderOutcome(orderId="", status="unknown", reason=f"post_error: {e}"[:300], recheck=True)
            body = self._error_body(e)
            if body is None:
                return self._failed(f"rejected: HTTP {getattr(e, 'status_code', '?')}: {getattr(e, 'error_msg', e)}"[:300])
            resp = body
        return await self._settle(resp, side, condition_id, since, size, price)

    async def _settle(self, resp: Any, side: str, condition_id: str, since: float, asked_shares: int, asked_price: int) -> OrderOutcome:
        """Turn the post-order response into what we actually got (see the Node gateway for the full story)."""
        if not isinstance(resp, dict):
            return self._failed("empty_response")
        order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id") or ""
        err = str(resp.get("errorMsg") or resp.get("error") or "")
        if err and not order_id:
            return self._failed(err)
        if err and _ZERO_FILL.search(err):
            return OrderOutcome(orderId=order_id, status="none", reason=err[:200], recheck=True)
        if err:
            self.log.warn("order response carried an error with an order id; reading fills", {"orderId": order_id, "err": err[:200]})

        fill: Optional[TradeFill]
        try:
            fill = await self.fills_of(order_id, condition_id, since)
        except Exception:
            fill = None
        if fill is None or fill.shares == 0:
            # trade history can lag the match; take the response's own amounts
            try:
                making = float(resp.get("makingAmount") or 0)
                taking = float(resp.get("takingAmount") or 0)
            except (TypeError, ValueError):
                making = taking = 0.0
            if making > 0 and taking > 0:
                fill = TradeFill(shares=to_micro(taking if side == "buy" else making), usdc=to_micro(making if side == "buy" else taking), orderIds=[order_id])
            elif str(resp.get("status") or "").lower() == "matched":
                fill = TradeFill(shares=asked_shares, usdc=asked_shares * asked_price // UNIT, orderIds=[order_id])
        if fill is None or fill.shares == 0:
            return OrderOutcome(orderId=order_id, status="none", reason="no_fill_found", recheck=True)
        net = fill.shares - fill.feeShares if side == "buy" else fill.shares
        return OrderOutcome(orderId=order_id, status="filled", shares=fill.shares, usdc=fill.usdc, feeUsdc=fill.feeUsdc, netShares=net)

    async def fills_of(self, order_id: Optional[str], condition_id: str, since_ms: float, match: Optional[FillMatch] = None) -> TradeFill:
        """Our taker fills for one order — or, when the order id never came back (`order_id` None), the taker fills in
        this token and side since `since_ms` not attributed to any order already booked. The CLOB cannot filter
        trades by order id, so scope by market + time and match here, all pages. BUY taker fees are charged in
        shares: size × fee_rate_bps / 10000, converted at the fill price."""
        clob = self._require_clob()
        from py_clob_client_v2 import TradeParams

        trades = await asyncio.to_thread(clob.get_trades, TradeParams(market=condition_id, after=int(since_ms // 1000)), False)
        out = TradeFill()
        oid = order_id.lower() if order_id else None
        for t in trades if isinstance(trades, list) else []:
            taker = str(t.get("taker_order_id") or "").lower()
            if oid:
                if taker != oid:
                    continue
            else:
                if match is None or not taker or match.is_booked(taker):
                    continue
                if str(t.get("trader_side") or "").upper() != "TAKER":
                    continue
                if str(t.get("asset_id")) != match.tokenId or str(t.get("side") or "").lower() != match.side:
                    continue
                if trade_time_ms(t.get("match_time")) < since_ms:
                    continue
            size = to_micro(t["size"])
            price = to_micro(t["price"])
            fee_shares = size * int(t.get("fee_rate_bps") or 0) // 10_000
            out.shares += size
            out.usdc += size * price // UNIT
            out.feeShares += fee_shares
            out.feeUsdc += fee_shares * price // UNIT
            if taker not in out.orderIds:
                out.orderIds.append(taker)
        return out

    async def token_balance(self, token_id: str) -> int:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        r = await asyncio.to_thread(self._require_clob().get_balance_allowance, BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id))
        if isinstance(r, dict) and (r.get("error") or r.get("errorMsg")):
            raise RuntimeError(str(r.get("errorMsg") or r.get("error")))
        return _to_micro_balance(r["balance"]) if isinstance(r, dict) and r.get("balance") else 0

    async def collateral_balance(self) -> int:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        r = await asyncio.to_thread(self._require_clob().get_balance_allowance, BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        if isinstance(r, dict) and (r.get("error") or r.get("errorMsg")):
            raise RuntimeError(str(r.get("errorMsg") or r.get("error")))
        return _to_micro_balance(r["balance"]) if isinstance(r, dict) and r.get("balance") else 0
