"""Every credential this process knows, so that nothing it writes to a file — the run log, a support bundle — can carry
one: the config's keys, credential-named environment variables, and the CLOB API credentials the gateway derives at
run time. Values are removed from each string before it is serialized, so JSON escaping cannot hide a match.
Same registry as the Node bot's secrets.ts."""
from __future__ import annotations

import os
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
_PLACEHOLDER = re.compile(r"\$\{(\w+)\}", re.A)
_FIRST_SCALAR = re.compile(r"""(?:"([^"]*)"|'([^']*)'|([^\s"'#,\]}]+))""")
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


def add_raw_config_secrets(raw: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Credential-looking values in a config file's raw text, for when it does not load: the value of every key named
    like a credential, and any PMWallets key or 32-byte hex (a private key; a config holds no transaction hash)."""
    env = os.environ if env is None else env
    # valid YAML that fails only the bot's own checks: the parser sees every form a key can take (quoted names, block
    # scalars, flow maps); the line scan below is for text the parser cannot read at all
    # placeholders are filled in first, as load_config does: `{apiSecret: ${X}}` only parses once `${X}` is gone
    tree = _parse(_PLACEHOLDER.sub(lambda p: env.get(p.group(1), p.group(0)), raw))
    if tree is not _UNPARSED:
        _walk(tree, False, env)
    for m in _RAW_FIELD.finditer(raw):
        v = re.sub(r"\s+#.*$", "", m.group(1)).strip()
        # a placeholder names the variable holding the key, whatever that variable is called
        for p in _PLACEHOLDER.finditer(v):
            add_secret(env.get(p.group(1)))
        if _PLACEHOLDER.fullmatch(v):
            continue
        add_secret(re.sub(r"^['\"]|['\"]$", "", v))
        # a line that does not parse may carry junk after the key: the first scalar on it is the likeliest key
        first = _FIRST_SCALAR.match(v)
        if first:
            add_secret(next((g for g in first.groups() if g is not None), None))
    for m in _RAW_TOKEN.finditer(raw):
        add_secret(m.group(0))


_UNPARSED = object()
_CREDENTIAL_NAME = re.compile(r"key|secret|pass|token|private", re.I)


def _parse(text: str) -> Any:
    """the text's YAML node graph (every scalar a string, a key given twice kept twice); _UNPARSED when it is not YAML"""
    import yaml
    try:
        return yaml.compose(text, Loader=yaml.BaseLoader)
    except Exception:
        return _UNPARSED


def _walk(node: Any, credential: bool, env: Mapping[str, str], seen: Optional[set[tuple[int, bool]]] = None, depth: int = 0) -> None:
    """Every scalar under a credential-named key, read off the node graph rather than a constructed object: an object
    keeps only the last of a key given twice, and a config that fails for that very reason is when this scan matters.
    An alias is the node its anchor marks, so `apiSecret: *a` reaches it wherever it stands."""
    import yaml
    seen = set() if seen is None else seen
    # aliases can point back up the tree: a node already walked with the same flag has nothing new to give
    if depth > 50 or node is None or (id(node), credential) in seen:
        return
    seen.add((id(node), credential))
    if isinstance(node, yaml.ScalarNode):
        if not credential:
            return
        v = str(node.value or "")
        for p in _PLACEHOLDER.finditer(v):
            add_secret(env.get(p.group(1)))
        if not _PLACEHOLDER.fullmatch(v.strip()):
            add_secret(v.strip())
    elif isinstance(node, yaml.MappingNode):
        for k, v in node.value:  # every pair, a key given twice included
            name = str(k.value or "") if isinstance(k, yaml.ScalarNode) else ""
            _walk(v, credential or bool(_CREDENTIAL_NAME.search(name)), env, seen, depth + 1)
    elif isinstance(node, yaml.SequenceNode):
        for v in node.value:
            _walk(v, credential, env, seen, depth + 1)


_PMW_KEY = re.compile(r"pmw_[A-Za-z0-9]+_[A-Za-z0-9]+")


def redact_text(s: str) -> str:
    """`s` with every known credential, any PMWallets key by its shape (one rotated away since it was written is
    known to no one here), and any user:password in a URL, replaced"""
    for v in _ordered:
        if v in s:
            s = s.replace(v, REDACTED)
    return _USERINFO.sub("//***@", _PMW_KEY.sub(REDACTED, s))


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
