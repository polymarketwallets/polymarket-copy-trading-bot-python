from __future__ import annotations

import re

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import check_trading_config, load_config
from .log import ConsoleLogger
from .run import run
from .state import BotState, InstanceLock
from .units import fmt_usd, from_micro, js_num, to_micro

HELP = """pmwallets-copytrade — copy the Polymarket wallets you follow on PMWallets

  pmwallets-copytrade init [config.yaml]        write an example config
  pmwallets-copytrade run [--config config.yaml] [--json]
  pmwallets-copytrade check [--config config.yaml]    verify the trading setup (read-only; places no order)
  pmwallets-copytrade status [--config config.yaml]
  pmwallets-copytrade reconcile [<key> --none | <key> --filled <shares> --usdc <usdc>] [--config config.yaml]
      settle an order the bot could not verify by itself (see `status`); run it with the bot stopped

Starts in dry-run: nothing is traded until you set `mode: live`.
Docs: https://pmwallets.com/copy-trading"""


def _arg(argv: list[str], name: str, fallback: str) -> str:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return fallback


def _iso_ms(ms: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _reconcile(cfg: Any, key: str, result: Any) -> None:
    from .engine import CopyEngine
    from .polymarket import PolymarketGateway
    from dataclasses import replace

    log = ConsoleLogger()
    st = BotState(cfg.dataDir, cfg.mode)
    engine = CopyEngine(cfg, PolymarketGateway(replace(cfg.polymarket, privateKey=None), log), st, log, None)
    await engine.reconcile(key, result)


ACCOUNT_TYPES = ["0 · plain wallet (EOA)", "1 · Proxy Wallet (older email/Google account)", "2 · Safe Wallet (older browser-wallet account)",
                 "3 · Deposit Wallet (polymarket.com account since 2026-05-04)"]


class _Quiet:
    def info(self, *a: Any, **k: Any) -> None: ...
    def warn(self, *a: Any, **k: Any) -> None: ...

    def error(self, msg: str, *a: Any, **k: Any) -> None:
        print(msg, file=sys.stderr)


async def _check(path: str) -> int:
    """Everything live trading depends on, checked without trading: the config, the PMWallets key and its
    subscriptions, the Polymarket credentials, and whether the account the orders would come from is the one holding
    the money. Exit 0 only when all of it is in order."""
    from pmwallets import Client

    from .polymarket import PolymarketGateway
    from .run import _proxy_from_env, funder_mismatch
    from .geo import check_geo, describe_geo
    from .wallets import check_funder

    def ok(m: str) -> None:
        print(f"  ✓ {m}")

    def bad(m: str) -> None:
        print(f"  ✗ {m}")

    problems = 0
    cfg = load_config(path)
    proxy = _proxy_from_env()
    if proxy:
        ok(f"proxy {proxy}")

    print("PMWallets")
    try:
        with Client(api_key=cfg.pmwallets.apiKey, base_url=cfg.pmwallets.baseUrl) as client:
            subs = client.subscriptions()
        active = [s for s in subs if s.get("status") == "active"]
        paused = f", {len(subs) - len(active)} paused" if len(subs) > len(active) else ""
        ok(f"API key accepted; {len(active)} active subscription(s){paused}")
        if not active:
            bad("nothing to copy yet: subscribe to a trader on pmwallets.com")
            problems += 1
    except Exception as e:
        bad(f"API key: {e}")
        problems += 1

    print("Polymarket")
    try:
        check_trading_config(cfg)
    except Exception as e:
        bad(str(e))
        return problems + 1
    sig_type = cfg.polymarket.signatureType
    ok(f"account type {ACCOUNT_TYPES[sig_type]}")
    gw = PolymarketGateway(cfg.polymarket, _Quiet())
    try:
        await gw.connect()
    except Exception as e:
        bad(f"could not derive the trading credentials: {e}")
        return problems + 1
    ok(f"signer {gw.signer_address}")
    fc = check_funder(gw.signer_address, cfg.polymarket.funderAddress, sig_type)
    if fc["ok"]:
        ok(f"funds held by {cfg.polymarket.funderAddress if cfg.polymarket.funderAddress is not None else gw.signer_address} — this key's {ACCOUNT_TYPES[sig_type].split(' (')[0]}")
    else:
        bad(funder_mismatch(cfg.polymarket.funderAddress, sig_type, fc))
        problems += 1
    try:
        usdc, allowances = await gw.collateral()
        zero = [k for k, v in allowances.items() if v == 0]
        if not allowances:
            # no approval data at all is not "approved": it cannot be confirmed, so it does not pass
            bad("Polymarket returned no exchange approvals for this account, so they cannot be confirmed")
            problems += 1
        elif len(zero) == len(allowances):
            bad("no exchange contract may spend this wallet's USDC yet: approve them before the first trade (see the README)" if sig_type == 0
                else "no exchange contract may spend this account's USDC: finish setting up trading on polymarket.com (make one trade or deposit there) first")
            problems += 1
        elif zero:
            print(f"  ! no approval yet for {', '.join(zero)} — orders routed through it will fail")
        else:
            ok("exchange approvals in place")
        if usdc > 0:
            ok(f"balance {fmt_usd(usdc)} available to trade")
        else:
            bad("balance $0.00 — if polymarket.com shows money in this account, signatureType or funderAddress is wrong")
            problems += 1
        if 0 < usdc < to_micro(cfg.copy.orderSizeUsdc):
            bad(f"balance is below one order (copy.orderSizeUsdc = ${js_num(cfg.copy.orderSizeUsdc)})")
            problems += 1
    except Exception as e:
        # Polymarket's answer when the funder is not a Deposit Wallet owned by this key
        if re.search(r"no deposit wallet found", str(e), re.I):
            bad(f"this key's Deposit Wallet {cfg.polymarket.funderAddress} is not deployed yet: sign up on polymarket.com with this wallet and make a deposit first"
                if fc["ok"] else
                f"Polymarket finds no account wallet at {cfg.polymarket.funderAddress} owned by this key — funderAddress, privateKey or signatureType is wrong")
        else:
            bad(f"balance lookup failed: {e}")
        problems += 1
    try:
        g = await check_geo()
        if g.api == "ok":
            ok(describe_geo(g))
        else:
            bad(describe_geo(g))
            problems += 1
    except Exception as e:
        bad(f"region lookup failed: {e}")
        problems += 1
    try:
        if await gw.closed_only():
            bad("Polymarket lets this account only close positions (region or account restriction): BUYs will be rejected")
            problems += 1
        else:
            ok("account may open positions")
    except Exception as e:
        bad(f"restriction lookup failed: {e}")
        problems += 1

    print(f"\n{problems} problem(s): fix them before mode: live" if problems else f"\nready for mode: live (the bot is in {cfg.mode} mode now)")
    return 1 if problems else 0


def _example() -> Path:
    here = Path(__file__).resolve().parent
    for p in (here / "config.example.yaml", here.parent.parent / "config.example.yaml"):
        if p.exists():
            return p
    raise FileNotFoundError("config.example.yaml is missing from the installation")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else None
    try:
        if cmd == "init":
            dest = Path(argv[1] if len(argv) > 1 else "config.yaml")
            if dest.exists():
                raise RuntimeError(f"{dest} already exists")
            shutil.copyfile(_example(), dest)
            print(f"wrote {dest} — fill in PMW_API_KEY (and the Polymarket keys for live mode), then: pmwallets-copytrade run")
            return
        if cmd == "run":
            cfg = load_config(_arg(argv, "--config", "config.yaml"))
            code = asyncio.run(run(cfg, ConsoleLogger("--json" in argv)))
            sys.exit(code)
        if cmd == "check":
            sys.exit(asyncio.run(_check(_arg(argv, "--config", "config.yaml"))))
        if cmd == "status":
            cfg = load_config(_arg(argv, "--config", "config.yaml"))
            st = BotState(cfg.dataDir, cfg.mode)
            ps = st.positions()
            print(f"{cfg.mode}: {len(ps)} open position(s), spent today ${from_micro(st.spent_today()):.2f}")
            for p in ps:
                print(f"  {p['target'][:10]}…  {(p.get('question') or p['conditionId'])[:60]}  [{p.get('outcome') or '?'}]  "
                      f"{from_micro(int(p['shares'])):.2f} sh  cost ${from_micro(int(p['costUsdc'])):.2f}  buys {p['buyCount']}")
            po = st.pending_orders()
            if po:
                print(f"{len(po)} order(s) not yet confirmed:")
            for p in po:
                nr = p.get("needsReconcile")
                print(f"  {'NEEDS RECONCILE' if nr else 'checking'}  {p['side']}  {(p.get('question') or p['conditionId'])[:50]}  sent {_iso_ms(p['sentAt'])}  "
                      f"order {p.get('orderId') or '(no id)'}  key {p['key']}" + (f"\n      {nr}" if nr else ""))
            if st.pending_exits():
                print(f"{len(st.pending_exits())} exit(s) being retried")
            return
        if cmd == "reconcile":
            cfg = load_config(_arg(argv, "--config", "config.yaml"))
            lock = InstanceLock(cfg.dataDir, cfg.mode)
            lock.acquire()  # the bot must not be running while its books are edited
            try:
                key = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else None
                if not key:
                    st = BotState(cfg.dataDir, cfg.mode)
                    open_ = [p for p in st.pending_orders() if p.get("needsReconcile")]
                    if not open_:
                        print("nothing to reconcile")
                        return
                    for p in open_:
                        print(f"{p['key']}\n  {p['side']} {p.get('question') or p['conditionId']} [{p.get('outcome') or '?'}] sent {_iso_ms(p['sentAt'])} "
                              f"order {p.get('orderId') or '(no id)'}\n  {p['needsReconcile']}")
                    print("\nCheck your trade history on polymarket.com, then: reconcile <key> --none  |  reconcile <key> --filled <shares> --usdc <usdc>")
                    return
                if "--none" in argv:
                    result = None
                elif "--filled" in argv and "--usdc" in argv:
                    result = (to_micro(_arg(argv, "--filled", "")), to_micro(_arg(argv, "--usdc", "")))
                else:
                    raise RuntimeError("say what happened: --none, or --filled <shares> --usdc <usdc>")
                asyncio.run(_reconcile(cfg, key, result))
                print("reconciled")
            finally:
                lock.release()
            return
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(HELP)
    if cmd and cmd not in ("help", "--help"):
        sys.exit(1)


if __name__ == "__main__":
    main()
