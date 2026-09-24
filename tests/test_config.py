import pytest

from pmwallets_copytrade.config import build_config, substitute_env

KEY = {"pmwallets": {"apiKey": "pmw_a_b"}}


def test_defaults():
    c = build_config(KEY)
    assert c.mode == "dry-run"
    assert (c.copy.orderSizeUsdc, c.copy.maxBuysPerOutcome, c.copy.minPrice, c.copy.maxPrice, c.copy.minBookDepthUsdc) == (10, 3, 0.05, 0.95, 50)


def test_live_needs_key_and_funder():
    with pytest.raises(ValueError, match="privateKey"):
        build_config({**KEY, "mode": "live"})
    with pytest.raises(ValueError, match="funderAddress"):
        build_config({**KEY, "mode": "live", "polymarket": {"privateKey": "ab" * 32}})
    c = build_config({**KEY, "mode": "live", "polymarket": {"privateKey": "ab" * 32, "signatureType": 0}})
    assert c.polymarket.privateKey == "0x" + "ab" * 32


def test_rejects_settings_that_block_every_trade():
    with pytest.raises(ValueError, match="minPrice"):
        build_config({**KEY, "copy": {"minPrice": 0.9, "maxPrice": 0.5}})
    with pytest.raises(ValueError, match="roles"):
        build_config({**KEY, "copy": {"roles": []}})
    with pytest.raises(ValueError, match=r"targets\[0\]"):
        build_config({**KEY, "targets": ["0x4b96…984e"]})


def test_targets():
    c = build_config({**KEY, "targets": ["0xABCDEFabcdefABCDEFabcdefABCDEFabcdefABCD", {"entity": "7KQ2MF9X4B1C", "orderSizeUsdc": 5}]})
    assert c.targets[0].entity == "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    assert (c.targets[1].entity, c.targets[1].orderSizeUsdc) == ("7KQ2MF9X4B1C", 5)


def test_env():
    assert substitute_env("k: ${A}", {"A": "x"}) == "k: x"
    with pytest.raises(ValueError, match="B"):
        substitute_env("k: ${B}", {})


def test_shared_config_contract():
    """the Node suite checks the same file: every YAML scalar form normalises to the same values"""
    import json
    from dataclasses import asdict
    from pathlib import Path

    from pmwallets_copytrade.config import load_config

    root = Path(__file__).resolve().parents[1] / "testdata"
    c = load_config(str(root / "config-contract.yaml"), {})
    expected = json.loads((root / "config-contract.expected.json").read_text())

    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    got = clean({"mode": c.mode, "polymarket": asdict(c.polymarket), "targets": [asdict(t) for t in c.targets],
                 "copy": asdict(c.copy), "risk": asdict(c.risk), "dataDir": c.dataDir})
    assert got == expected
    # counts come back as ints, not "2" or 2.0
    assert type(c.polymarket.signatureType) is int and type(c.targets[1].maxBuysPerOutcome) is int
