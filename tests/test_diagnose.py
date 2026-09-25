import gzip
import json
import re
from datetime import datetime, timezone

import pytest

from pmwallets_copytrade import __version__
from pmwallets_copytrade.diagnose import diagnose, scrub, secrets_of

KEY = "0x" + "ab12" * 16
PMW = "pmw_abcd1234_secretsecretsecret"


def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "config.yaml").write_text("\n".join([
        "mode: live",
        "pmwallets:", "  apiKey: ${PMW_API_KEY}",
        "polymarket:", "  signatureType: 3", "  privateKey: ${POLY_PRIVATE_KEY}", '  funderAddress: "0x' + "1" * 40 + '"',
        f"dataDir: {data}",
    ]))
    (data / "state.live.json").write_text('{"positions":{}}')
    (data / "bot.live.log").write_text(f'{{"msg":"started"}}\n{{"msg":"oops, key was {KEY[2:].upper()}"}}\n')
    (data / "decisions.live.jsonl").write_text('{"decision":"bought"}\n')
    return {"PMW_API_KEY": PMW, "POLY_PRIVATE_KEY": KEY, "HTTPS_PROXY": "http://user:hunter22@proxy:8080"}


async def test_bundles_what_support_needs_and_not_a_single_key(tmp_path):
    env = setup(tmp_path)

    async def check(_, out):
        out(f"  ✓ signer ok ({PMW})")
        out("  ✗ region")
        return 1
    now = datetime(2026, 9, 25, 6, 30, 0, 123000, tzinfo=timezone.utc)
    file = await diagnose(str(tmp_path / "config.yaml"), check, env=env, now=now, out_dir=str(tmp_path))
    assert file == str(tmp_path / "pmw-diagnose-20260925T063000Z.json.gz")
    text = gzip.decompress(open(file, "rb").read()).decode("utf8")
    for s in [KEY, KEY[2:], KEY[2:].upper(), PMW, "hunter22"]:
        assert s not in text
    b = json.loads(text)
    assert b["format"] == 1 and b["version"] == __version__ and b["check"]["exitCode"] == 1
    assert b["createdAt"] == "2026-09-25T06:30:00.123Z"
    assert set(b["runtime"]) == {"python", "platform", "arch"}
    pm = b["config"]["polymarket"]
    assert pm["privateKey"] == "<redacted>" and pm["funderAddress"] == "0x" + "1" * 40 and pm["signatureType"] == 3
    assert "apiSecret" not in pm  # unset fields are left out, as in the Node bundle
    assert b["config"]["pmwallets"]["apiKey"] == "<redacted>"
    assert b["check"]["output"] == ["  ✓ signer ok (<redacted>)", "  ✗ region"]
    assert sorted(b["files"]) == ["bot.live.log", "decisions.live.jsonl", "state.live.json"]
    assert "oops, key was <redacted>" in b["files"]["bot.live.log"]


async def test_still_writes_a_bundle_when_the_config_does_not_load(tmp_path):
    env = setup(tmp_path)
    (tmp_path / "bad.yaml").write_text("mode: live\nnonsense: 1\n")

    async def check(_, out):
        raise RuntimeError("no config")
    file = await diagnose(str(tmp_path / "bad.yaml"), check, env=env, out_dir=str(tmp_path))
    b = json.loads(gzip.decompress(open(file, "rb").read()))
    assert re.search("nonsense", b["config"]["error"])
    assert b["check"]["output"] == ["check failed: no config"]


def test_never_treats_a_short_value_as_a_secret():
    assert secrets_of(None, {"MY_TOKEN": "abc", "PATH": "/usr/bin/longenough"}) == []
    assert scrub("x https://a:b@h/ y", []) == "x https://***@h/ y"
