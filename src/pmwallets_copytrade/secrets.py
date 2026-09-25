"""Every credential this process knows, so that nothing it writes to a file — the run log, a support bundle — can carry
one: the config's keys, credential-named environment variables, and the CLOB API credentials the gateway derives at
run time. Values are removed from each string before it is serialized, so JSON escaping cannot hide a match.
Same registry as the Node bot's secrets.ts."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Mapping, Optional

if TYPE_CHECKING:
    from .config import Config

_known: set[str] = set()
_ordered: list[str] = []
REDACTED = "<redacted>"
# environment variables whose values are credentials, whatever the config says
SECRET_NAME = re.compile(r"KEY|SECRET|PASS|TOKEN|PRIVATE", re.I)
_RAW_FIELD = re.compile(r"^\s*[\w-]*(?:key|secret|pass|token|private)[\w-]*\s*:\s*(.+)$", re.I | re.M | re.A)
_RAW_TOKEN = re.compile(r"pmw_[A-Za-z0-9]+_[A-Za-z0-9]+|(?:0x)?[0-9a-fA-F]{64}")
_USERINFO = re.compile(r'//[^/@\s"]*:[^/@\s"]*@')


def add_secret(v: Optional[str]) -> None:
    global _ordered
    # short values would blank out ordinary text; no real credential is this short
    if not v or len(v) < 8:
        return
    bare = re.sub(r"^0x", "", v, flags=re.I)
    for b in (bare, bare.lower(), bare.upper()):
        _known.update((b, f"0x{b}", f"0X{b}"))
    _known.add(v)
    _ordered = sorted(_known, key=len, reverse=True)


def add_config_secrets(cfg: Optional["Config"], env: Mapping[str, str]) -> None:
    if cfg:
        for v in (cfg.pmwallets.apiKey, cfg.polymarket.privateKey, cfg.polymarket.apiKey, cfg.polymarket.apiSecret, cfg.polymarket.apiPassphrase):
            add_secret(v)
    for k, v in env.items():
        if SECRET_NAME.search(k):
            add_secret(v)


def add_raw_config_secrets(raw: str) -> None:
    """Credential-looking values in a config file's raw text, for when it does not load: the value of every key named
    like a credential, and any PMWallets key or 32-byte hex (a private key; a config holds no transaction hash)."""
    for m in _RAW_FIELD.finditer(raw):
        v = re.sub(r"^['\"]|['\"]$", "", re.sub(r"\s+#.*$", "", m.group(1)).strip())
        if not re.fullmatch(r"\$\{\w+\}", v, re.A):
            add_secret(v)
    for m in _RAW_TOKEN.finditer(raw):
        add_secret(m.group(0))


def redact_text(s: str) -> str:
    """`s` with every known credential, and any user:password in a URL, replaced"""
    for v in _ordered:
        if v in s:
            s = s.replace(v, REDACTED)
    return _USERINFO.sub("//***@", s)


def redact(value: Any) -> Any:
    """`value` with `redact_text` applied to every string in it, keys included"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {redact_text(k) if isinstance(k, str) else k: redact(v) for k, v in value.items()}
    return value


def clear_secrets() -> None:
    """for tests"""
    global _ordered
    _known.clear()
    _ordered = []
