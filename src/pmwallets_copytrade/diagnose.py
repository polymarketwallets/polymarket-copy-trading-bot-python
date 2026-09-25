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
from .secrets import add_config_secrets, add_raw_config_secrets, redact, redact_text

# how much of each log a bundle carries: the newest part, where the trouble usually is
TAIL_BYTES = 5 * 1024 * 1024
REDACTED = "<redacted>"
DEFAULT_DATA_DIR = Config.__dataclass_fields__["dataDir"].default

Check = Callable[[str, Callable[[str], None]], Awaitable[int]]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def diagnose(config_path: str, check: Check, *, env: Optional[Mapping[str, str]] = None,
                   now: Optional[datetime] = None, out_dir: str = ".") -> str:
    """Everything support needs to see what the bot did, in one file a user can send: versions, the `check` result,
    the config without its keys, the state files, and the newest logs and decisions of both modes.
    Keys are removed twice: from the config by field, then by value from every string in the bundle — so a key that
    turned up anywhere else (an error message, a log line) does not leave the machine either."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    raw_config = ""
    try:
        raw_config = Path(config_path).read_bytes().decode("utf8", errors="replace")
    except (OSError, ValueError):
        pass  # no file: load_config says so
    add_raw_config_secrets(raw_config, env)
    cfg: Optional[Config] = None
    config_error: Optional[str] = None
    # a parser error quotes the line it failed on — cut short, so no value match can catch it: keep the first line
    try:
        cfg = load_config(config_path, env)
    except Exception as e:
        config_error = _first_line(e)
    add_config_secrets(cfg, env)

    lines: list[str] = []
    check_exit: Optional[int] = None
    try:
        check_exit = await check(config_path, lines.append)
    except Exception as e:
        lines.append(f"check failed: {_first_line(e)}")

    # a config that does not load still says where the data is; guessing the default would bundle the wrong files
    m = re.search(r"^dataDir:\s*(.+?)\s*(?:#.*)?$", raw_config, re.M)
    raw_data_dir = re.sub(r"^['\"]|['\"]$", "", m.group(1)) if m else None
    data_dir = cfg.dataDir if cfg else raw_data_dir if raw_data_dir is not None else DEFAULT_DATA_DIR
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
        "dataDir": data_dir if cfg else f"{data_dir} ({'read from the config text' if raw_data_dir else 'the default'}: the config did not load)",
        "files": files,
    }
    # strings first, then the text once more: the second pass is only a backstop
    text = redact_text(json.dumps(redact(bundle), indent=1, default=str, ensure_ascii=False))
    out = Path(out_dir) / f"pmw-diagnose-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    out.write_bytes(gzip.compress(text.encode("utf8")))
    return str(out)


def _first_line(e: BaseException) -> str:
    return str(e).split("\n")[0]


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
