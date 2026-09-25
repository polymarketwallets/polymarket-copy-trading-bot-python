import re
from pathlib import Path

from pmwallets_copytrade import __version__


def test_version_matches_pyproject():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf8")
    assert __version__ == re.search(r'^version = "([^"]+)"', text, re.M).group(1)
