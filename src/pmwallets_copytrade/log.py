from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from .files import RotatingFile


class Logger(Protocol):
    def info(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None: ...
    def warn(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None: ...
    def error(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None: ...


class ConsoleLogger:
    """One line per event on stdout: human-readable, with the fields as JSON."""

    def __init__(self, as_json: bool = False) -> None:
        self.as_json = as_json

    def _write(self, level: str, msg: str, fields: Optional[dict[str, Any]]) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if self.as_json:
            print(json.dumps({"ts": ts, "level": level, "msg": msg, **(fields or {})}, default=str), flush=True)
            return
        extra = f" {json.dumps(fields, default=str)}" if fields else ""
        print(f"{ts} {level.upper():<5} {msg}{extra}", file=sys.stderr if level == "error" else sys.stdout, flush=True)

    def info(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        self._write("info", msg, fields)

    def warn(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        self._write("warn", msg, fields)

    def error(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        self._write("error", msg, fields)


class TeeLogger:
    """`inner`, and a JSON line per event in `file` — the log a user can send us when something went wrong, whether or
    not their terminal kept it. A failed write is dropped: the log must never stop the bot."""

    def __init__(self, inner: Logger, file: "RotatingFile") -> None:
        self.inner = inner
        self.file = file

    def _write(self, level: str, msg: str, fields: Optional[dict[str, Any]]) -> None:
        getattr(self.inner, level)(msg, fields)
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            self.file.append(json.dumps({"ts": ts, "level": level, "msg": msg, **(fields or {})}, default=str, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            pass  # see above

    def info(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        self._write("info", msg, fields)

    def warn(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        self._write("warn", msg, fields)

    def error(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        self._write("error", msg, fields)
