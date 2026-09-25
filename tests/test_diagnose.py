import gzip
import json
import re
from datetime import datetime, timezone

import pytest

from pmwallets_copytrade import __version__
from pmwallets_copytrade.config import load_config
from pmwallets_copytrade.diagnose import diagnose
from pmwallets_copytrade.files import RotatingFile
from pmwallets_copytrade.log import ConsoleLogger, TeeLogger
from pmwallets_copytrade.secrets import add_config_secrets, add_secret, clear_secrets, redact, redact_text

KEY = "0x" + "ab12" * 16
PMW = "pmw_abcd1234_secretsecretsecret"


@pytest.fixture(autouse=True)
def _fresh_secrets():
    clear_secrets()
    yield
    clear_secrets()


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


BROKEN = [
    ("an unterminated quote", f'mode: live\npmwallets:\n  apiKey: "{PMW}\npolymarket:\n  privateKey: {KEY}\n'),
    ("a key where a group belongs", f"mode: live\npmwallets: {PMW}\n"),
    ("a bad indent", f"mode: live\npmwallets:\n  apiKey: {PMW}\n    privateKey: '{KEY}'\n  apiSecret: c2VjcmV0c2VjcmV0c2VjcmV0\n"),
]


@pytest.mark.parametrize("yaml_text", [y for _, y in BROKEN], ids=[n for n, _ in BROKEN])
async def test_keeps_literal_keys_out_of_the_bundle_when_the_config_does_not_parse(tmp_path, yaml_text):
    setup(tmp_path)
    (tmp_path / "broken.yaml").write_text(yaml_text)

    # the real check reads the config first, and its error is the parser's — excerpt and all
    async def check(p, out):
        load_config(p, {})
        return 0
    file = await diagnose(str(tmp_path / "broken.yaml"), check, env={}, out_dir=str(tmp_path))
    text = gzip.decompress(open(file, "rb").read()).decode("utf8")
    # the parser's excerpt cuts a long line short, so a prefix of the key must not be there either
    for s in [PMW, KEY[:40], KEY[2:40], "c2VjcmV0c2VjcmV0c2VjcmV0"]:
        assert s not in text
    assert "\n" not in json.loads(text)["config"]["error"]


async def test_reads_data_dir_from_a_config_that_does_not_load_instead_of_guessing_the_default(tmp_path):
    env = setup(tmp_path)
    data = tmp_path / "elsewhere"
    data.mkdir()
    (data / "decisions.live.jsonl").write_text('{"decision":"bought"}\n')
    (tmp_path / "bad.yaml").write_text(f'mode: live\nnonsense: 1\ndataDir: "{data}"   # custom\n')

    async def check(_, out):
        return 1
    b = json.loads(gzip.decompress(open(await diagnose(str(tmp_path / "bad.yaml"), check, env=env, out_dir=str(tmp_path)), "rb").read()))
    assert "read from the config text" in b["dataDir"]
    assert list(b["files"]) == ["decisions.live.jsonl"]


def test_matches_before_json_escaping_and_in_every_0x_case_form():
    add_secret('abcd"efgh\\ij')
    add_secret("0x" + "ab" * 32)
    text = json.dumps(redact({"e": 'pw abcd"efgh\\ij', "k": ["0X" + "AB" * 32, "0x" + "AB" * 32, "ab" * 32]}), separators=(",", ":"))
    assert text == '{"e":"pw <redacted>","k":["<redacted>","<redacted>","<redacted>"]}'


def test_keeps_them_out_of_the_decisions_file(tmp_path):
    from pmwallets_copytrade.state import BotState
    add_secret(KEY)
    BotState(str(tmp_path), "live").log_decision({"decision": "buy_rejected", "reason": f"signer {KEY.upper()} refused"})
    text = (tmp_path / "decisions.live.jsonl").read_text()
    assert KEY[2:].upper() not in text
    assert "signer <redacted> refused" in text


def test_registers_the_keys_of_every_config_it_loads_whatever_the_environment_calls_them(tmp_path):
    setup(tmp_path)
    load_config(str(tmp_path / "config.yaml"), {"PMW_API_KEY": PMW, "POLY_PRIVATE_KEY": KEY})
    assert redact_text(f"{PMW} {KEY}") == "<redacted> <redacted>"


def test_removes_a_pmwallets_key_by_its_shape_even_one_no_longer_in_the_config():
    assert redact_text("old key pmw_zz99yy88_rotatedawaylongago in a 0.1.3 log") == "old key <redacted> in a 0.1.3 log"


def test_never_treats_a_short_value_as_a_secret():
    add_config_secrets(None, {"MY_TOKEN": "abc", "PATH": "/usr/bin/longenough"})
    assert redact_text("abc /usr/bin/longenough") == "abc /usr/bin/longenough"
    assert redact_text("x https://a:b@h/ y") == "x https://***@h/ y"


def test_keeps_them_out_of_the_run_log_the_terminal_and_the_file_alike(tmp_path, capsys):
    add_config_secrets(None, {"POLY_PRIVATE_KEY": KEY, "PMW_API_KEY": PMW})
    add_secret("derived-clob-secret==")
    TeeLogger(ConsoleLogger(), RotatingFile(tmp_path / "bot.log", 10**6, 2)).warn(
        f"order failed for {PMW}", {"error": f"bad key {KEY[2:].upper()}", "stack": "at x (derived-clob-secret==)"})
    printed = capsys.readouterr()
    for out in [(tmp_path / "bot.log").read_text(), printed.out + printed.err]:
        for s in [PMW, KEY[2:], KEY[2:].upper(), "derived-clob-secret"]:
            assert s not in out
        assert "order failed for <redacted>" in out
