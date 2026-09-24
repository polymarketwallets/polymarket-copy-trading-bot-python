import subprocess
import sys


def test_the_cli_module_imports_and_prints_its_help():
    # the CLI is not imported by any other test; a broken import there would otherwise ship
    r = subprocess.run([sys.executable, "-m", "pmwallets_copytrade.cli", "--help"], capture_output=True, text=True)
    assert "pmwallets-copytrade check" in r.stdout
