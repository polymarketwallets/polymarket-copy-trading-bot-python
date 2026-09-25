import os

from pmwallets_copytrade.files import RotatingFile


def test_rotating_a_file_that_is_open_elsewhere_never_eats_the_older_pieces(tmp_path, monkeypatch):
    # Windows: a log file open in another program cannot be renamed, while its older pieces still can
    p = tmp_path / "bot.log"
    (tmp_path / "bot.log.1").write_text("one\n")
    (tmp_path / "bot.log.2").write_text("two\n")
    locked: set[str] = set()
    real_replace, real_rename = os.replace, os.rename

    def guard(real):
        def move(src, dst, *a, **k):
            if str(src) in locked:
                raise PermissionError("EBUSY")
            return real(src, dst, *a, **k)
        return move
    monkeypatch.setattr(os, "replace", guard(real_replace))
    monkeypatch.setattr(os, "rename", guard(real_rename))
    f = RotatingFile(p, 20, 2)
    locked.add(str(p))
    for _ in range(12):
        f.append("xxxxxxxxx\n")
    assert (tmp_path / "bot.log.1").read_text() == "one\n"
    assert (tmp_path / "bot.log.2").read_text() == "two\n"
    assert p.read_text() == "xxxxxxxxx\n" * 12
    locked.discard(str(p))
    f.append("yyyyyyyyy\n")  # unlocked: the next rotation goes through
    assert (tmp_path / "bot.log.1").read_text() == "xxxxxxxxx\n" * 12
    assert (tmp_path / "bot.log.2").read_text() == "one\n"
