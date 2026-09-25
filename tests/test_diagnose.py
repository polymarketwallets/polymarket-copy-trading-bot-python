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


CLOB_BROKEN = [
    ("a placeholder for a variable of any name", "polymarket:\n  apiSecret: ${CLOB_CREDENTIAL}\n  : broken\n"),
    ("junk after a literal", 'polymarket:\n  apiSecret: "clobsecretvalue123" junk\n'),
    ("a quoted key name, and an unknown setting", 'polymarket:\n  "apiSecret": clobsecretvalue123\n  nonsense: 1\n'),
    ("a folded block scalar, and an unknown setting", "polymarket:\n  apiSecret: >-\n    clobsecretvalue123\n  nonsense: 1\n"),
    ("a double-quoted key with a placeholder", 'polymarket:\n  "apiSecret": ${CLOB_CREDENTIAL}\n  nonsense: 1\n'),
    ("a single-quoted key with a placeholder", "polymarket:\n  'apiPassphrase': ${CLOB_CREDENTIAL}\n  nonsense: 1\n"),
    ("a flow mapping with a placeholder", "polymarket: {apiSecret: ${CLOB_CREDENTIAL}}\nnonsense: 1\n"),
    ("a quoted key given twice, the first holding it", 'polymarket:\n  "apiSecret": clobsecretvalue123\n  "apiSecret": secondsecret67890\n'),
    ("a block scalar given twice, the first holding it",
     "polymarket:\n  apiSecret: >-\n    clobsecretvalue123\n  apiSecret: >-\n    secondsecret67890\n"),
    ("an alias to a value anchored elsewhere", "stash: &k clobsecretvalue123\npolymarket:\n  apiSecret: *k\n"),
    ("a literal block scalar in a flow of lines", "polymarket:\n  apiPassphrase: |-\n    clobsecretvalue123\nnonsense: 1\n"),
]


@pytest.mark.parametrize("yaml_text", [y for _, y in CLOB_BROKEN], ids=[n for n, _ in CLOB_BROKEN])
async def test_keeps_a_clob_credential_out_of_the_bundle_when_the_config_does_not_load(tmp_path, yaml_text):
    setup(tmp_path)
    (tmp_path / "broken.yaml").write_text(f"mode: live\ndataDir: {tmp_path / 'data'}\n{yaml_text}")
    # an older build logged an exchange error quoting it
    (tmp_path / "data" / "decisions.live.jsonl").write_text('{"reason":"401 for key clobsecretvalue123"}\n')
    env = {"CLOB_CREDENTIAL": "clobsecretvalue123"}

    async def check(p, out):
        load_config(p, env)
        return 0
    file = await diagnose(str(tmp_path / "broken.yaml"), check, env=env, out_dir=str(tmp_path))
    assert "clobsecretvalue123" not in gzip.decompress(open(file, "rb").read()).decode("utf8")


async def test_a_key_put_where_a_group_belongs_is_not_quoted_back_in_the_bundle_or_anywhere(tmp_path):
    setup(tmp_path)
    (tmp_path / "broken.yaml").write_text("mode: live\npolymarket: [apiSecret, clobsecretvalue123]\n")

    async def check(p, out):
        load_config(p, {})
        return 0
    file = await diagnose(str(tmp_path / "broken.yaml"), check, env={}, out_dir=str(tmp_path))
    assert "clobsecretvalue123" not in gzip.decompress(open(file, "rb").read()).decode("utf8")


def test_config_errors_do_not_quote_a_long_value_back(tmp_path):
    (tmp_path / "c.yaml").write_text("mode: live\npmwallets:\n  apiKey: pmw_a_b\npolymarket: [apiSecret, CLOBSECRET-ROTATED-123456]\n")
    with pytest.raises(ValueError, match=r"polymarket must be a group of settings, not a \d+-character value"):
        load_config(str(tmp_path / "c.yaml"), {})
    (tmp_path / "d.yaml").write_text("mode: live\npmwallets:\n  apiKey: pmw_a_b\nrisk: 10\n")
    with pytest.raises(ValueError, match='risk must be a group of settings, not "10"'):
        load_config(str(tmp_path / "d.yaml"), {})


def test_keeps_them_out_of_the_state_file_where_unfinished_orders_keep_the_exchange_error(tmp_path):
    from pmwallets_copytrade.state import BotState
    add_secret("clobsecretvalue123")
    st = BotState(str(tmp_path), "live")
    st.add_pending_order({"key": "k", "side": "buy", "orderId": None, "target": "t", "tokenId": "1", "conditionId": "c", "shares": "1",
                          "limit": "1", "reserveUsdc": "1", "sentAt": 0, "attempts": 1,
                          "needsReconcile": "exchange said: bad creds clobsecretvalue123"})
    st.save()
    assert "clobsecretvalue123" not in (tmp_path / "state.live.json").read_text()
    assert "clobsecretvalue123" in st.pending_orders()[0]["needsReconcile"]  # memory keeps what it had


async def test_keeps_only_the_structure_of_what_a_release_before_0_1_4_wrote(tmp_path):
    """a credential since replaced is known to no one"""
    from pmwallets_copytrade.state import BotState
    env = setup(tmp_path)
    data = tmp_path / "data"
    # 0.1.3: unredacted decisions and a state file quoting an exchange error with the old CLOB secret
    (data / "decisions.live.jsonl").write_text('{"at":"2026-09-24T00:00:00Z","decision":"buy_rejected","tx":"0xabc","reason":"401 oldclobsecret999"}\n')
    (data / "state.live.json").write_text(json.dumps({"version": 1, "positions": {}, "processed": [], "handledTx": [], "spend": {"day": "", "usdc": "0"},
        "pendingOrders": [{"key": "old", "side": "buy", "orderId": None, "target": "t", "tokenId": "1", "conditionId": "c", "shares": "1", "limit": "1",
                           "reserveUsdc": "1", "sentAt": 0, "attempts": 1, "needsReconcile": "lookup failed: oldclobsecret999"}],
        "pendingExits": [], "bookedOrderIds": []}))
    # 0.1.4 opens it, adds its own lines and a fresh reason, and saves
    st = BotState(str(data), "live")
    st.log_decision({"decision": "skipped_slippage", "reason": "ask_0.55_vs_target_0.49"})
    st.add_pending_order({"key": "new", "side": "buy", "orderId": None, "target": "t", "tokenId": "2", "conditionId": "c", "shares": "1", "limit": "1",
                          "reserveUsdc": "1", "sentAt": 0, "attempts": 1})
    st.update_pending_order("new", needsReconcile="the order id never came back")
    st.save()

    async def check(_, out):
        return 0
    b = json.loads(gzip.decompress(open(await diagnose(str(tmp_path / "config.yaml"), check, env=env, out_dir=str(tmp_path)), "rb").read()))
    assert "oldclobsecret999" not in json.dumps(b)
    lines = [json.loads(line) for line in b["files"]["decisions.live.jsonl"].strip().split("\n")]
    assert lines[0]["decision"] == "buy_rejected" and lines[0]["tx"] == "0xabc" and "before 0.1.4" in lines[0]["note"]
    assert "reason" not in lines[0]
    assert lines[1]["decision"] == "skipped_slippage" and lines[1]["reason"] == "ask_0.55_vs_target_0.49"
    orders = json.loads(b["files"]["state.live.json"])["pendingOrders"]
    assert "before 0.1.4" in next(o for o in orders if o["key"] == "old")["needsReconcile"]
    assert next(o for o in orders if o["key"] == "new")["needsReconcile"] == "the order id never came back"


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
