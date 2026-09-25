import json

import httpx
import pytest

from pmwallets_copytrade.geo import GeoVerdict, check_geo, classify_geo, describe_geo


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


async def _geo(body):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=json.dumps(body)))
    async with httpx.AsyncClient(transport=transport) as http:
        return await check_geo(http)


async def test_believes_the_endpoint_about_a_restriction_newer_than_its_own_lists():
    g = await _geo({"blocked": True, "ip": "1.2.3.4", "country": "ES", "region": "MD"})
    assert (g.api, g.unlisted, g.websiteRestricted) == ("close-only", True, True)
    assert "not on this bot" in describe_geo(g)


async def test_allows_an_unrestricted_region():
    g = await _geo({"blocked": False, "ip": "1.2.3.4", "country": "ES", "region": "MD"})
    assert (g.api, g.unlisted, g.websiteRestricted) == ("ok", False, False)


async def test_keeps_the_list_verdict_when_the_endpoint_disagrees_permissively():
    g = await _geo({"blocked": False, "ip": "1.2.3.4", "country": "US", "region": "CA"})
    assert (g.api, g.unlisted) == ("close-only", False)


@pytest.mark.parametrize("body", [{"ip": "1.2.3.4", "country": "ES"}, {"blocked": "yes", "country": "ES"}, {"blocked": False}])
async def test_rejects_a_malformed_reply(body):
    with pytest.raises(RuntimeError, match="geoblock lookup"):
        await _geo(body)


def test_says_that_a_sanctioned_region_cannot_even_close_positions():
    g = GeoVerdict(api="blocked", country="IR", region="", ip="", websiteRestricted=True)
    assert "closing positions included" in describe_geo(g)
