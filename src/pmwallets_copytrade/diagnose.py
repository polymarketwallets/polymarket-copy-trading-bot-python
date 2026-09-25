"""`diagnose`: one file a user can send to support — same bundle as the Node bot's."""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from . import __version__
from .config import Config, load_config
from .files import tail_of

# how much of each log a bundle carries: the newest part, where the trouble usually is
TAIL_BYTES = 5 * 1024 * 1024
REDACTED = "<redacted>"
# environment variables whose values are credentials, whatever the config says
SECRET_ENV = re.compile(r"KEY|SECRET|PASS|TOKEN|PRIVATE", re.I)
DEFAULT_DATA_DIR = Config.__dataclass_fields__["dataDir"].default

Check = Callable[[str, Callable[[str], None]], Awaitable[int]]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def diagnose(config_path: str, check: Check, *, env: Optional[Mapping[str, str]] = None,
                   now: Optional[datetime] = None, out_dir: str = ".") -> str:
    """Everything support needs to see what the bot did, in one file a user can send: versions, the `check` result,
    the config without its keys, the state files, and the newest logs and decisions of both modes.
    Keys are removed twice: from the config by field, then by value from the whole bundle — so a key that turned up
    anywhere else (an error message, a log line) does not leave the machine either."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    cfg: Optional[Config] = None
    config_error: Optional[str] = None
    try:
        cfg = load_config(config_path, env)
    except Exception as e:
        config_error = str(e)

    lines: list[str] = []
    check_exit: Optional[int] = None
    try:
        check_exit = await check(config_path, lines.append)
    except Exception as e:
        lines.append(f"check failed: {e}")

    data_dir = cfg.dataDir if cfg else DEFAULT_DATA_DIR
    files: dict[str, str] = {}
    for mode in ("live", "dry-run"):
        for name in (f"state.{mode}.json", f"stream.{mode}.json"):
            p = Path(data_dir) / name
            if p.exists():
                files[name] = p.read_text("utf8")
        for name in (f"bot.{mode}.log", f"decisions.{mode}.jsonl"):
            t = tail_of(Path(data_dir) / name, TAIL_BYTES)
            if t:
                files[name] = t

    bundle = {
        "format": 1,
        "createdAt": _iso(now),
        "version": __version__,
        "runtime": {"python": platform.python_version(), "platform": sys.platform, "arch": platform.machine()},
        "config": _redact_config(cfg) if cfg else {"error": config_error},
        "check": {"exitCode": check_exit, "output": lines},
        "dataDir": data_dir,
        "files": files,
    }
    text = scrub(json.dumps(bundle, indent=1, default=str, ensure_ascii=False), secrets_of(cfg, env))
    out = Path(out_dir) / f"pmw-diagnose-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    out.write_bytes(gzip.compress(text.encode("utf8")))
    return str(out)


def _drop_none(v: Any) -> Any:
    """unset optional fields are left out, as the Node bundle leaves out `undefined`"""
    if isinstance(v, dict):
        return {k: _drop_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_drop_none(x) for x in v]
    return v


def _redact_config(cfg: Config) -> Any:
    d = dataclasses.asdict(cfg)
    hide = lambda v: REDACTED if v else None  # noqa: E731
    d["pmwallets"]["apiKey"] = hide(cfg.pmwallets.apiKey)
    for k in ("privateKey", "apiKey", "apiSecret", "apiPassphrase"):
        d["polymarket"][k] = hide(getattr(cfg.polymarket, k))
    return _drop_none(d)


def secrets_of(cfg: Optional[Config], env: Mapping[str, str]) -> list[str]:
    """every credential value we know of: the config's, and those of environment variables named like one"""
    vals = [cfg.pmwallets.apiKey, cfg.polymarket.privateKey, cfg.polymarket.apiKey, cfg.polymarket.apiSecret,
            cfg.polymarket.apiPassphrase] if cfg else []
    vals += [v for k, v in env.items() if SECRET_ENV.search(k)]
    out: set[str] = set()
    for v in vals:
        # short values would blank out ordinary text; no real credential is this short
        if not v or len(v) < 8:
            continue
        bare = re.sub(r"^0x", "", v, flags=re.I)
        out.update((v, bare, bare.lower(), bare.upper()))
    return sorted(out, key=len, reverse=True)


def scrub(text: str, secrets: list[str]) -> str:
    """`text` with every secret value, and any user:password in a URL, replaced"""
    for s in secrets:
        text = text.replace(s, REDACTED)
    return re.sub(r'//[^/@\s"]*:[^/@\s"]*@', "//***@", text)
