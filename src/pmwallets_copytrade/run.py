from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from pathlib import Path
from typing import Any, Optional

from pmwallets import AsyncClient, FileStateStore, FillStream, PmwError

from .config import Config, TargetConfig
from .engine import CopyEngine
from .log import Logger
from .polymarket import PolymarketGateway
from .state import BotState
from .units import fmt_usd, to_micro

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


async def resolve_targets(cfg: Config, client: AsyncClient, log: Logger) -> Optional[dict[str, TargetConfig]]:
    """Which entities to copy, as addresses. Handles resolve through the entity endpoint, which only returns the
    address for an entity you own — one you subscribe to or bought."""
    subs = [s for s in await client.subscriptions() if s.get("status") != "canceled"]
    active = {s["entityId"].lower(): s for s in subs if s.get("status") == "active"}
    for s in subs:
        if s.get("status") == "paused":
            log.warn("subscription is PAUSED (balance ran out) — no fills will arrive for it until you resume it", {"entity": s["entityId"], "id": s.get("id")})
        elif "ws" not in (s.get("channels") or []):
            log.warn("subscription does not include the ws channel — its fills reach the bot only through replay", {"entity": s["entityId"]})
    if not cfg.targets:
        if not active:
            log.warn("no active subscriptions: subscribe to an entity on pmwallets.com (or with the SDK) and the bot will copy it")
        else:
            log.info(f"copying every subscribed entity ({len(active)})", {"entities": list(active)})
        return None
    out: dict[str, TargetConfig] = {}
    for t in cfg.targets:
        addr = t.entity.lower()
        if not _ADDRESS.match(t.entity):
            try:
                e = await client.entity(t.entity)
            except Exception as err:
                raise RuntimeError(f"cannot resolve handle {t.entity}: {err}") from err
            eid = str(e.get("entityId") or "")
            if not _ADDRESS.match(eid):
                raise RuntimeError(f"handle {t.entity}: the API did not return its address — subscribe to it (or buy its address) first")
            addr = eid.lower()
        if addr not in active:
            log.warn("target has no ACTIVE subscription: nothing will be copied from it until you subscribe", {"entity": addr})
        out[addr] = TargetConfig(entity=addr, orderSizeUsdc=t.orderSizeUsdc, maxBuysPerOutcome=t.maxBuysPerOutcome)
    log.info(f"copying {len(out)} target(s)", {"entities": list(out)})
    return out


def _proxy_from_env() -> Optional[str]:
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(k):
            return re.sub(r"//[^@/]*@", "//***@", os.environ[k])
    return None


async def run(cfg: Config, log: Logger) -> int:
    """Returns the process exit code. httpx and websockets pick HTTPS_PROXY up from the environment themselves."""
    proxy = _proxy_from_env()
    if proxy:
        log.info("using proxy from the environment", {"proxy": proxy})
    client = AsyncClient(api_key=cfg.pmwallets.apiKey, base_url=cfg.pmwallets.baseUrl)
    pm_cfg = cfg.polymarket
    if cfg.mode != "live":
        from dataclasses import replace
        pm_cfg = replace(pm_cfg, privateKey=None)
    exchange = PolymarketGateway(pm_cfg, log)
    state = BotState(cfg.dataDir, cfg.mode)

    log.info(f"pmwallets-copytrade starting in {cfg.mode.upper()} mode", {"state": str(state.file), "decisions": str(state.decisions_file)})
    if cfg.mode == "dry-run":
        log.info("dry-run: no order is sent; fills are simulated at the best price on the book")

    try:
        targets = await resolve_targets(cfg, client, log)
    except PmwError as e:
        if e.status == 401:
            raise RuntimeError("PMWallets rejected the API key (401)") from e
        raise

    if cfg.mode == "live":
        await exchange.connect()
        usdc = await exchange.collateral_balance()
        log.info("polymarket balance", {"usdc": fmt_usd(usdc)})
        if usdc < to_micro(cfg.copy.orderSizeUsdc):
            log.warn("balance is below one order: BUYs will be rejected until you deposit")

    engine = CopyEngine(cfg, exchange, state, log, targets)
    exit_code = {"code": 0}
    stop_event = asyncio.Event()

    def on_event(e: dict[str, Any]) -> None:
        t = e["type"]
        if t == "hello":
            log.info("connected to the PMWallets fill stream", {"session": e["session"]})
        elif t == "gap" and e.get("skipped"):
            log.info("reconnected before any fill was handled; nothing to replay")
        elif t == "gap":
            log.warn("missed fills detected; replaying from the last one handled", {"reason": e["reason"], "fromBlock": e["fromBlock"]})
        elif t == "replayed" and e.get("delivered"):
            log.info("replay done", {"delivered": e["delivered"]})
        elif t == "replaced":
            log.warn("another connection with this API account took over the stream (one per account) — close the other bot or the browser feed page")
        elif t == "disconnected":
            log.warn("stream disconnected; reconnecting", {"code": e["code"], **({"reason": e["reason"]} if e.get("reason") else {})})
        elif t == "error":
            log.warn("stream error", {"error": str(e["error"])})
        elif t == "fatal":
            log.error("stream stopped", {"error": str(e["error"])})
            exit_code["code"] = 1
            stop_event.set()

    stream = FillStream(client=client, on_fill=engine.on_fill, on_event=on_event,
                        store=FileStateStore(Path(cfg.dataDir) / f"stream.{cfg.mode}.json"))

    await engine.sweep_settled()

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(600)
            await engine.sweep_settled()

    sweep_task = asyncio.create_task(sweeper())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop_event.set)
    await stream.start()
    try:
        await stop_event.wait()
    finally:
        log.info("stopping…")
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task
        await engine.stop()
        await stream.stop()
        await client.aclose()
    return exit_code["code"]
