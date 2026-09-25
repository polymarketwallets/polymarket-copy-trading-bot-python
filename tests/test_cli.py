import subprocess
import sys


def test_the_cli_module_imports_and_prints_its_help():
    # the CLI is not imported by any other test; a broken import there would otherwise ship
    r = subprocess.run([sys.executable, "-m", "pmwallets_copytrade.cli", "--help"], capture_output=True, text=True)
    assert "pmwallets-copytrade check" in r.stdout


def test_everything_printed_is_redacted_library_logging_and_tracebacks_included(tmp_path):
    key = "0x" + "cd34" * 16
    code = (
        "import logging, sys\n"
        "from pmwallets_copytrade.cli import _guard_console\n"
        f"_guard_console(['run', '--config', {str(tmp_path / 'config.yaml')!r}])\n"
        f"print('printed {key[2:]}')\n"
        f"logging.getLogger('py_clob_client_v2').error('request error body=%s', {key.upper()!r})\n"
        f"raise RuntimeError('boom {key}')\n"
    )
    (tmp_path / "config.yaml").write_text(f"polymarket:\n  privateKey: {key}\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert key[2:] not in out and key[2:].upper() not in out
    assert "printed <redacted>" in r.stdout
    assert "request error body=<redacted>" in r.stderr
    assert "RuntimeError: boom <redacted>" in r.stderr
