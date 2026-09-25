import json

import httpx
import pytest

from pmwallets_copytrade.geo import GeoVerdict, check_geo, classify_geo


@pytest.mark.parametrize("country,region,want", [
    ("US", "CA", "close-only"), ("GB", "ENG", "close-only"), ("DE", "", "close-only"), ("CA", "ON", "close-only"),
    ("IR", "", "blocked"), ("UA", "43", "blocked"),
    ("IE", "L", "website-only"), ("JP", "13", "website-only"), ("NL", "", "website-only"),
    ("CA", "NS", "ok"), ("ES", "MD", "ok"), ("HK", "", "ok"),
])
def test_where_polymarket_accepts_api_orders_from(country, region, want):
    assert classify_geo(country, region) == want


async def test_website_only_countries_are_allowed_for_the_api_whatever_the_endpoint_says():
    body = json.dumps({"blocked": True, "ip": "1.2.3.4", "country": "IE", "region": "L"})
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    async with httpx.AsyncClient(transport=transport) as http:
        v = await check_geo(http)
    assert v == GeoVerdict(api="ok", country="IE", region="L", ip="1.2.3.4", websiteRestricted=True)
