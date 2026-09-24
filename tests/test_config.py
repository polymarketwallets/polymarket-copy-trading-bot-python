import json
from pathlib import Path

import pytest

from pmwallets_copytrade.config import build_config, substitute_env

KEY = {"pmwallets": {"apiKey": "pmw_a_b"}}


def test_defaults():
    c = build_config(KEY)
    assert c.mode == "dry-run"
    assert (c.copy.orderSizeUsdc, c.copy.maxBuysPerOutcome, c.copy.minPrice, c.copy.maxPrice, c.copy.minBookDepthUsdc) == (10, 3, 0.05, 0.95, 50)


def test_live_needs_key_explicit_account_type_and_funder():
    pk = "ab" * 32
    funder = "0x1111111111111111111111111111111111111111"
    with pytest.raises(ValueError, match="privateKey"):
        build_config({**KEY, "mode": "live"})
    # no default account type: signing as the wrong one gets every order rejected
    with pytest.raises(ValueError, match="signatureType: 3 for accounts created on polymarket.com since 2026-05-04"):
        build_config({**KEY, "mode": "live", "polymarket": {"privateKey": pk}})
    with pytest.raises(ValueError, match="funderAddress"):
        build_config({**KEY, "mode": "live", "polymarket": {"privateKey": pk, "signatureType": 3}})
    with pytest.raises(ValueError, match=r"signatureType must be a number in \[0, 3\]"):
        build_config({**KEY, "mode": "live", "polymarket": {"privateKey": pk, "signatureType": 4, "funderAddress": funder}})
    dw = build_config({**KEY, "mode": "live", "polymarket": {"privateKey": pk, "signatureType": "3", "funderAddress": funder}})
    assert dw.polymarket.signatureType == 3 and type(dw.polymarket.signatureType) is int
    eoa = build_config({**KEY, "mode": "live", "polymarket": {"privateKey": pk, "signatureType": 0}})
    assert eoa.polymarket.privateKey == "0x" + pk


def test_missing_account_type_message_lists_every_type():
    with pytest.raises(ValueError) as e:
        build_config({**KEY, "mode": "live", "polymarket": {"privateKey": "ab" * 32}})
    assert str(e.value) == ("set polymarket.signatureType: 3 for accounts created on polymarket.com since 2026-05-04 (Deposit Wallet), "
                            "1 for older email/Google accounts, 2 for older browser-wallet accounts, 0 for a plain wallet — see the README")


def test_dry_run_needs_no_polymarket_settings():
    assert build_config(KEY).polymarket.signatureType is None


def test_yaml_scalars_stay_strings(tmp_path):
    from pmwallets_copytrade.config import load_config

    pk = "0x" + "0" * 63 + "1"  # the default loader would read this as the integer 1
    f = tmp_path / "c.yaml"
    f.write_text(f"""mode: live
pmwallets:
  apiKey: pmw_a_b
polymarket:
  privateKey: {pk}
  signatureType: 0
copy:
  orderSizeUsdc: 20
  roles: [taker]
""")
    c = load_config(str(f), {})
    assert c.polymarket.privateKey == pk
    assert c.polymarket.signatureType == 0 and c.polymarket.funderAddress is None
    assert c.copy.orderSizeUsdc == 20 and c.copy.maxFillAgeSec == 60 and c.copy.roles == ["taker"]
    assert c.dataDir == "./pmw-data"


def test_empty_sections_are_the_defaults(tmp_path):
    from pmwallets_copytrade.config import load_config

    f = tmp_path / "c.yaml"
    f.write_text("pmwallets:\n  apiKey: pmw_a_b\npolymarket:\ncopy:\nrisk:\ntargets: []\n")
    c = load_config(str(f), {})
    assert c.copy.orderSizeUsdc == 10 and c.risk.maxDailySpendUsdc == 200 and c.targets == []


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



INVALID = json.loads((Path(__file__).resolve().parents[1] / "testdata" / "config-invalid.json").read_text())["cases"]


@pytest.mark.parametrize("case", INVALID, ids=[c["name"] for c in INVALID])
def test_configs_both_implementations_refuse(tmp_path, case):
    """shared testdata/config-invalid.json: a field that is present but empty is refused, never defaulted"""
    from pmwallets_copytrade.config import load_config

    f = tmp_path / "c.yaml"
    f.write_text(case["yaml"])
    with pytest.raises(ValueError):
        load_config(str(f), {})


def test_empty_targets_and_data_dir_messages():
    with pytest.raises(ValueError, match="^targets must be a list — write targets: \\[\\] for none$"):
        build_config({**KEY, "targets": ""})
    with pytest.raises(ValueError, match="^dataDir is empty: remove the line to use ./pmw-data, or give a directory$"):
        build_config({**KEY, "dataDir": ""})
