"""Where Polymarket lets orders come from.

The bot trades through the API, so what matters is the API rule for the machine's public IP — which is not the same
as the website's: Polymarket's geoblock endpoint answers `blocked: true` for countries that restrict only the website
(Ireland, Japan, …), where API orders are accepted. Lists from https://docs.polymarket.com/api-reference/geoblock
(checked 2026-09-25); review them when Polymarket changes its policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"

# OFAC: no new orders, positions cannot be closed either
BLOCKED = ["IR", "SY", "CU", "KP", "UA-43", "UA-14", "UA-09"]
# close-only on the website AND the API: new BUYs are refused
API_CLOSE_ONLY = ["AU", "BY", "BE", "BI", "BR", "CA-BC", "CA-ON", "CA-AB", "CA-QC", "CF", "CD", "ET", "FR", "DE", "IQ", "IT", "LB", "LY",
                  "MM", "NZ", "NI", "PL", "RU", "SG", "SO", "SK", "SS", "SD", "TW", "TH", "GB", "US", "UM", "VE", "YE", "ZW"]
# close-only on the website only: the API — and so this bot — can still open positions
WEBSITE_ONLY = ["IE", "JP", "MT", "NL", "KR"]


@dataclass
class GeoVerdict:
    api: str  # "ok" | "close-only" | "blocked"
    country: str
    region: str
    ip: str
    websiteRestricted: bool


def classify_geo(country: str, region: str) -> str:
    """"blocked" | "close-only" | "website-only" | "ok" """
    c = country.upper()
    sub = f"{c}-{region.upper()}" if region else ""
    if c in BLOCKED or (sub and sub in BLOCKED):
        return "blocked"
    if c in API_CLOSE_ONLY or (sub and sub in API_CLOSE_ONLY):
        return "close-only"
    if c in WEBSITE_ONLY:
        return "website-only"
    return "ok"


async def check_geo(http: Optional[httpx.AsyncClient] = None) -> GeoVerdict:
    """where this machine's orders appear to come from, and what the API allows there"""
    client = http or httpx.AsyncClient(timeout=10.0)
    try:
        r = await client.get(GEOBLOCK_URL, timeout=10.0)
    finally:
        if http is None:
            await client.aclose()
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"geoblock lookup → HTTP {r.status_code}")
    b = r.json()
    b = b if isinstance(b, dict) else {}
    country = str(b.get("country") if b.get("country") is not None else "")
    region = str(b.get("region") if b.get("region") is not None else "")
    if not country:
        raise RuntimeError("geoblock lookup returned no country")
    k = classify_geo(country, region)
    return GeoVerdict(api="ok" if k == "website-only" else k, country=country, region=region,
                      ip=str(b.get("ip") if b.get("ip") is not None else ""), websiteRestricted=k != "ok")
