from pmwallets_copytrade.files import RotatingFile, tail_of


def test_rolls_over_at_the_size_limit_and_keeps_only_keep_old_pieces(tmp_path):
    p = tmp_path / "sub" / "bot.log"
    f = RotatingFile(p, 20, 2)
    for line in ["aaaaaaaaa\n", "bbbbbbbbb\n", "ccccccccc\n", "ddddddddd\n", "eeeeeeeee\n", "fffffffff\n", "ggggggggg\n"]:
        f.append(line)
    assert p.read_text() == "ggggggggg\n"
    assert (tmp_path / "sub" / "bot.log.1").read_text() == "eeeeeeeee\nfffffffff\n"
    assert (tmp_path / "sub" / "bot.log.2").read_text() == "ccccccccc\nddddddddd\n"
    assert not (tmp_path / "sub" / "bot.log.3").exists()


def test_a_crash_between_the_two_steps_of_a_rotation_loses_nothing(tmp_path):
    p = tmp_path / "bot.log"
    (tmp_path / "bot.log.rotating").write_text("moved aside\n")
    (tmp_path / "bot.log.1").write_text("older\n")
    RotatingFile(p, 100, 3).append("new\n")
    assert (tmp_path / "bot.log.1").read_text() == "moved aside\n"
    assert (tmp_path / "bot.log.2").read_text() == "older\n"
    assert p.read_text() == "new\n"


def test_picks_up_the_size_of_a_file_it_did_not_write(tmp_path):
    p = tmp_path / "bot.log"
    p.write_text("x" * 15 + "\n")
    RotatingFile(p, 20, 2).append("yyyyyyyyy\n")
    assert p.read_text() == "yyyyyyyyy\n"


def test_a_failed_rotation_keeps_appending(tmp_path, monkeypatch):
    import os
    p = tmp_path / "bot.log"
    f = RotatingFile(p, 20, 2)
    f.append("aaaaaaaaa\n")

    def boom(*a, **k):
        raise OSError("busy")
    monkeypatch.setattr(os, "replace", boom)
    f.append("bbbbbbbbbbbbbbb\n")
    assert p.read_text() == "aaaaaaaaa\nbbbbbbbbbbbbbbb\n"


def test_tails_across_the_last_rotation_and_starts_at_a_whole_line(tmp_path):
    p = tmp_path / "bot.log"
    (tmp_path / "bot.log.1").write_text("one\ntwo\n")
    p.write_text("three\n")
    assert tail_of(p, 1000) == "one\ntwo\nthree\n"
    assert tail_of(p, 9) == "three\n"  # 'o\nthree\n' cut to the whole line
    assert tail_of(p, 12) == "two\nthree\n"
    assert tail_of(tmp_path / "none.log", 100) == ""


def test_a_failed_log_write_never_raises_out_of_the_logger(tmp_path):
    from pmwallets_copytrade.log import TeeLogger

    seen = []

    class Inner:
        def info(self, m, f=None): seen.append(m)
        warn = error = info

    class Broken:
        def append(self, line):
            raise OSError("disk full")
    log = TeeLogger(Inner(), Broken())
    log.info("hello", {"a": 1})
    log.error("bad")
    assert seen == ["hello", "bad"]
    ok = TeeLogger(Inner(), RotatingFile(tmp_path / "bot.log", 1000, 1))
    ok.warn("w", {"n": 2})
    import json
    line = json.loads((tmp_path / "bot.log").read_text())
    assert line["level"] == "warn" and line["msg"] == "w" and line["n"] == 2 and line["ts"].endswith("Z")
