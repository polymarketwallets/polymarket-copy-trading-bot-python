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


def test_a_shift_cut_short_and_finished_after_a_restart_deletes_nothing_more(tmp_path, monkeypatch):
    p = tmp_path / "bot.log"
    for i, t in ((1, "A\n"), (2, "B\n"), (3, "C\n")):
        (tmp_path / f"bot.log.{i}").write_text(t)
    fail_once = set()
    real_replace, real_rename = os.replace, os.rename

    def guard(real):
        def move(src, dst, *a, **k):
            if str(src) in fail_once:
                fail_once.discard(str(src))
                raise PermissionError("EBUSY")
            return real(src, dst, *a, **k)
        return move
    monkeypatch.setattr(os, "replace", guard(real_replace))
    monkeypatch.setattr(os, "rename", guard(real_rename))
    f = RotatingFile(p, 10, 3)
    f.append("LLLLLLLLL\n")
    fail_once.add(f"{p}.1")  # .1 -> .2 fails midway: C is gone (the oldest, as it should be), B moved to .3
    f.append("MMMMMMMMM\n")
    RotatingFile(p, 10, 3)  # restart: files the piece set aside
    assert [(tmp_path / f"bot.log.{i}").read_text() for i in (1, 2, 3)] == ["LLLLLLLLL\n", "A\n", "B\n"]
    assert p.read_text() == "MMMMMMMMM\n"
