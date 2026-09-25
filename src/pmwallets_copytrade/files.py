"""Rotated append-only files and their tails; same layout as the Node bot's (`name`, `name.1` … `name.<keep>`)."""
from __future__ import annotations

import os
from pathlib import Path


class RotatingFile:
    """An append-only file that never grows past `keep + 1` pieces of `max_bytes`: `name` is written, `name.1` is the
    piece before it, … `name.<keep>` the oldest. A failed rotation keeps appending to the current file — losing the
    size limit is better than losing the line, and a log must never stop the bot."""

    def __init__(self, path: str | os.PathLike[str], max_bytes: int, keep: int) -> None:
        self.path = str(path)
        self._max_bytes = max_bytes
        self._keep = keep
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._size = os.path.getsize(self.path) if os.path.exists(self.path) else 0

    def append(self, line: str) -> None:
        data = line.encode("utf8")
        if self._size > 0 and self._size + len(data) > self._max_bytes:
            self._rotate()
        with open(self.path, "ab") as f:
            f.write(data)
        self._size += len(data)

    def _rotate(self) -> None:
        try:
            oldest = f"{self.path}.{self._keep}"
            if os.path.exists(oldest):
                os.unlink(oldest)
            for i in range(self._keep - 1, 0, -1):
                if os.path.exists(f"{self.path}.{i}"):
                    os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
            os.replace(self.path, f"{self.path}.1")
            self._size = 0
        except OSError:
            pass  # keep writing where we are


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
