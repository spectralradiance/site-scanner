"""Progress output: timestamped lines to the console (stdout, flushed) and to a log file."""

from __future__ import annotations

import sys
import time
from pathlib import Path


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Progress:
    """Callable logger. Every line is prefixed with the time elapsed since the scan started."""

    def __init__(self, log_path: Path | None = None, stream=None):
        self.start = time.monotonic()
        self.stream = stream if stream is not None else sys.stdout
        self.file = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(log_path, "a", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = f"[{_clock(time.monotonic() - self.start)}] {msg}"
        try:
            print(line, file=self.stream, flush=True)
        except (UnicodeEncodeError, ValueError):
            enc = getattr(self.stream, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc, "replace"), file=self.stream, flush=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()

    def close(self) -> None:
        if self.file:
            self.file.close()
            self.file = None


class Counter:
    """Numbered progress for a phase with a known total, with an ETA."""

    def __init__(self, log, label: str, total: int):
        self.log, self.label, self.total = log, label, total
        self.done = 0
        self.start = time.monotonic()

    def tick(self, msg: str, count: bool = True) -> None:
        if count:
            self.done += 1
        elapsed = time.monotonic() - self.start
        eta = ""
        if 0 < self.done < self.total:
            eta = f" ETA {_clock(elapsed / self.done * (self.total - self.done))}"
        self.log(f"[{self.label} {self.done}/{self.total}{eta}] {msg}")
