"""Rotated append-only files and their tails; same layout as the Node bot's (`name`, `name.1` … `name.<keep>`)."""
from __future__ import annotations

import os
from pathlib import Path


class RotatingFile:
    """An append-only file that never grows far past `keep + 1` pieces of `max_bytes`: `name` is written, `name.1` is
    the piece before it, … `name.<keep>` the oldest. Rotation first moves the current file aside under one name; only
    once that worked are the older pieces shifted, so a rename that keeps failing (Windows, a file open elsewhere)
    never eats them. After a failure the next attempt waits for another `max_bytes`: a log must never stop the bot,
    and losing the size limit for a while is better than losing lines."""

    def __init__(self, path: str | os.PathLike[str], max_bytes: int, keep: int) -> None:
        self.path = str(path)
        self._max_bytes = max_bytes
        self._keep = keep
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._aside = f"{self.path}.rotating"
        self._size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        if os.path.exists(self._aside):
            self._shift_in()  # a rotation cut short by a crash

    def append(self, line: str) -> None:
        data = line.encode("utf8")
        if self._size > 0 and self._size + len(data) > self._max_bytes:
            self._rotate()
        with open(self.path, "ab") as f:
            f.write(data)
        self._size += len(data)

    def _rotate(self) -> None:
        self._size = 0  # whatever happens, the next attempt waits for another max_bytes
        # a piece still set aside must be filed first: moving the live file onto it would overwrite it
        if os.path.exists(self._aside) and not self._shift_in():
            return
        try:
            os.replace(self.path, self._aside)
        except OSError:
            return
        self._shift_in()

    def _shift_in(self) -> bool:
        """`.rotating` becomes `.1`. The pieces above it move up only as far as the first free number, and the oldest
        is deleted only when there is none — so a shift cut short and retried finds its own gap and deletes nothing
        more."""
        try:
            free = 1
            while free <= self._keep and os.path.exists(f"{self.path}.{free}"):
                free += 1
            if free > self._keep:
                os.unlink(f"{self.path}.{self._keep}")
                free = self._keep
            for i in range(free - 1, 0, -1):
                os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
            os.replace(self._aside, f"{self.path}.1")
            return True
        except OSError:
            return False


def tail_of(path: str | os.PathLike[str], max_bytes: int) -> str:
    """The last `max_bytes` of a rotated file (older piece first), starting at a whole line; '' when there is none."""
    p = str(path)
    pieces = [x for x in (f"{p}.1", p) if os.path.exists(x)]
    chunks: list[bytes] = []
    left = max_bytes
    cut = False
    for piece in reversed(pieces):
        size = os.path.getsize(piece)
        take = min(size, left)
        if take < size:
            cut = True
        if take <= 0:
            break
        with open(piece, "rb") as f:
            f.seek(size - take)
            chunks.insert(0, f.read(take))
        left -= take
    text = b"".join(chunks).decode("utf8", errors="replace")
    if not cut:
        return text
    nl = text.find("\n")
    return text[nl + 1:] if nl >= 0 else ""
