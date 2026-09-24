from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .log import ConsoleLogger
from .run import run
from .state import BotState, InstanceLock
from .units import from_micro, to_micro

HELP = """pmwallets-copytrade — copy the Polymarket wallets you follow on PMWallets

  pmwallets-copytrade init [config.yaml]        write an example config
  pmwallets-copytrade run [--config config.yaml] [--json]
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
