"""Configuration: the same YAML keys (camelCase) as the Node bot, so one config.yaml works for both."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import yaml


@dataclass
class TargetConfig:
    entity: str  # 0x address of the entity, or its 12-character handle (resolved at start-up)
    orderSizeUsdc: Optional[float] = None
    maxBuysPerOutcome: Optional[int] = None


@dataclass
class CopyConfig:
    orderSizeUsdc: float = 10  # USDC spent on each mirrored BUY
    roles: list[str] = field(default_factory=lambda: ["taker", "maker"])
    maxBuysPerOutcome: int = 3  # mirror the first N BUYs into the same outcome (DCA), skip the rest
    maxOpenPositions: int = 20
    maxOpenPositionsPerTarget: int = 5
    maxFillAgeSec: float = 60  # a replay after downtime must not trade history
    minTargetNotionalUsdc: float = 25  # bots trade $1-2 fills
    minPrice: float = 0.05
    maxPrice: float = 0.95
    maxSlippage: float = 0.03  # price units: 0.03 = 3 cents above what the target paid
    minBookDepthUsdc: float = 50
    minSecondsToEndDate: float = 600
    maxSecondsToEndDate: float = 0  # 0 = no limit
    sellMode: str = "all"  # all | none


@dataclass
class RiskConfig:
    maxDailySpendUsdc: float = 200  # 0 = no limit


@dataclass
class PolymarketConfig:
    clobUrl: str = "https://clob.polymarket.com"
    privateKey: Optional[str] = None
    signatureType: int = 2  # 0 = EOA, 1 = POLY_PROXY (email/Magic), 2 = POLY_GNOSIS_SAFE (browser wallet)
    funderAddress: Optional[str] = None  # your Polymarket profile address (holds the USDC)
    apiKey: Optional[str] = None
    apiSecret: Optional[str] = None
    apiPassphrase: Optional[str] = None


@dataclass
class PmwConfig:
    apiKey: str
    baseUrl: str = "https://api.pmwallets.com"


@dataclass
class Config:
    mode: str
    pmwallets: PmwConfig
    polymarket: PolymarketConfig
    targets: list[TargetConfig]  # empty = every entity the account subscribes to
    copy: CopyConfig
    risk: RiskConfig
    dataDir: str = "./pmw-data"


DEFAULT_COPY = CopyConfig()

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HANDLE = re.compile(r"^[0-9A-Z]{12}$")
_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def substitute_env(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """`${NAME}` → env NAME; a missing variable is an error, not an empty string."""
    env = os.environ if env is None else env

    def rep(m: re.Match[str]) -> str:
        v = env.get(m.group(1))
        if v is None or v == "":
            raise ValueError(f"config refers to ${{{m.group(1)}}} but it is not set")
        return v

    return _ENV.sub(rep, text)


def _num(v: Any, path: str, lo: float, hi: float) -> float:
    n = v
    if isinstance(v, str):
        try:
            n = float(v)
        except ValueError:
            n = None
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < lo or n > hi:
        raise ValueError(f"{path} must be a number in [{lo:g}, {hi:g}], got {v!r}")
    return n


def _merge(cls: Any, raw: Optional[Mapping[str, Any]], **base: Any) -> Any:
    obj = cls(**base)
    for k, v in (raw or {}).items():
        if hasattr(obj, k):  # unknown keys are ignored, as in the Node bot
            setattr(obj, k, v)
    return obj


def build_config(raw: Mapping[str, Any]) -> Config:
    """Merge onto the defaults and validate. Fail loud on anything that would make the bot trade wrong."""
    pmw = raw.get("pmwallets") or {}
    targets_raw = raw.get("targets") or []
    targets: list[TargetConfig] = []
    for i, t in enumerate(targets_raw):
        t = {"entity": t} if isinstance(t, str) else t
        if not isinstance(t, Mapping) or not isinstance(t.get("entity"), str) or not (_ADDRESS.match(t["entity"]) or _HANDLE.match(t["entity"])):
            raise ValueError(f"targets[{i}].entity must be a 0x address or a 12-character handle")
        tc = TargetConfig(entity=t["entity"].lower() if _ADDRESS.match(t["entity"]) else t["entity"],
                          orderSizeUsdc=t.get("orderSizeUsdc"), maxBuysPerOutcome=t.get("maxBuysPerOutcome"))
        if tc.orderSizeUsdc is not None:
            _num(tc.orderSizeUsdc, f"targets[{i}].orderSizeUsdc", 1, 1_000_000)
        if tc.maxBuysPerOutcome is not None:
            _num(tc.maxBuysPerOutcome, f"targets[{i}].maxBuysPerOutcome", 1, 1000)
        targets.append(tc)

    c = Config(
        mode=raw.get("mode", "dry-run"),
        pmwallets=PmwConfig(apiKey=pmw.get("apiKey"), baseUrl=pmw.get("baseUrl") or "https://api.pmwallets.com"),
        polymarket=_merge(PolymarketConfig, raw.get("polymarket")),
        targets=targets,
        copy=_merge(CopyConfig, raw.get("copy")),
        risk=_merge(RiskConfig, raw.get("risk")),
        dataDir=raw.get("dataDir") or "./pmw-data",
    )
    if c.mode not in ("dry-run", "live"):
        raise ValueError(f"mode must be dry-run or live, got {c.mode!r}")
    if not isinstance(c.pmwallets.apiKey, str) or not c.pmwallets.apiKey.startswith("pmw_"):
        raise ValueError("pmwallets.apiKey is required (pmw_…, from https://pmwallets.com/keys)")

    cp = c.copy
    _num(cp.orderSizeUsdc, "copy.orderSizeUsdc", 1, 1_000_000)
    _num(cp.maxBuysPerOutcome, "copy.maxBuysPerOutcome", 1, 1000)
    _num(cp.maxOpenPositions, "copy.maxOpenPositions", 1, 100_000)
    _num(cp.maxOpenPositionsPerTarget, "copy.maxOpenPositionsPerTarget", 1, 100_000)
    _num(cp.maxFillAgeSec, "copy.maxFillAgeSec", 1, 86_400)
    _num(cp.minTargetNotionalUsdc, "copy.minTargetNotionalUsdc", 0, 1_000_000_000)
    _num(cp.minPrice, "copy.minPrice", 0, 1)
    _num(cp.maxPrice, "copy.maxPrice", 0, 1)
    _num(cp.maxSlippage, "copy.maxSlippage", 0, 1)
    _num(cp.minBookDepthUsdc, "copy.minBookDepthUsdc", 0, 1_000_000_000)
    _num(cp.minSecondsToEndDate, "copy.minSecondsToEndDate", 0, 1e9)
    _num(cp.maxSecondsToEndDate, "copy.maxSecondsToEndDate", 0, 1e9)
    _num(c.risk.maxDailySpendUsdc, "risk.maxDailySpendUsdc", 0, 1e12)
    if cp.minPrice >= cp.maxPrice:
        raise ValueError(f"copy.minPrice {cp.minPrice} >= copy.maxPrice {cp.maxPrice}: every BUY would be rejected")
    if cp.maxSecondsToEndDate > 0 and cp.maxSecondsToEndDate <= cp.minSecondsToEndDate:
        raise ValueError("copy.maxSecondsToEndDate must be above copy.minSecondsToEndDate (or 0 to disable)")
    if not isinstance(cp.roles, list) or not cp.roles or any(r not in ("taker", "maker") for r in cp.roles):
        raise ValueError("copy.roles must be a non-empty list of taker / maker")
    if cp.sellMode not in ("all", "none"):
        raise ValueError("copy.sellMode must be all or none")

    pm = c.polymarket
    _num(pm.signatureType, "polymarket.signatureType", 0, 2)
    pm.signatureType = int(pm.signatureType)
    if c.mode == "live":
        if not pm.privateKey or not re.match(r"^(0x)?[0-9a-fA-F]{64}$", str(pm.privateKey)):
            raise ValueError("live mode needs polymarket.privateKey (64 hex characters)")
        if not pm.privateKey.startswith("0x"):
            pm.privateKey = "0x" + pm.privateKey
        if pm.signatureType != 0 and not (pm.funderAddress and _ADDRESS.match(pm.funderAddress)):
            raise ValueError("live mode with a proxy/safe signature type needs polymarket.funderAddress (your Polymarket profile address)")
    return c


def load_config(path: str, env: Optional[Mapping[str, str]] = None) -> Config:
    with open(path, encoding="utf8") as f:
        text = f.read()
    # substitute only in non-comment content so an unset variable named in a comment is not an error
    without_comments = "\n".join("" if re.match(r"^\s*#", line) else line for line in text.split("\n"))
    return build_config(yaml.safe_load(substitute_env(without_comments, env)) or {})
