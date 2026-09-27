"""The start-up story, written without the vendored library.

`log.log` is opened by the vendored logger (`library.log`), and `app.display.app_log`
swallows every exception that path raises. That is right for a status line in the middle
of a tick and wrong for the one sentence that says why the process stopped: `vendor/` is
a pin plus a provenance note, not a checkout, so a clean install has no logger at all —
and the app runs as `pythonw.exe`, where `print()` vanishes too. On exactly the machine
where a start-up failure most needs explaining, the app's only diagnostic path was a
no-op.

So this is the other path. Stdlib only, nothing to import that can fail, nothing to
configure, and it never raises: a log that becomes the reason the app stopped would be
the second bug of the same kind. It is also *bounded*, because a log that fills the disk
is its own outage — the file is rewritten from its own tail when it passes `MAX_BYTES`.

It is deliberately not a second copy of `log.log`. It gets the start-up steps, the
restart decisions, and the death sentence: enough to answer "did it start, what did it
manage, and what killed it" on a machine where `log.log` may not exist.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "boot.log"

MAX_BYTES = 512 * 1024          # bounded: a bootstrap log must never be an outage
KEEP_BYTES = 256 * 1024         # ... and this much of the tail survives a rotation


def _stamp(when: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()
                                                             if when is None else when))


def _rotate(p: Path) -> None:
    """Keep the tail. Cheap, synchronous, and only ever called near the size cap."""
    try:
        if p.stat().st_size <= MAX_BYTES:
            return
        data = p.read_bytes()[-KEEP_BYTES:]
        cut = data.find(b"\n")
        p.write_bytes(b"" if cut < 0 else data[cut + 1:])
    except OSError:
        pass


def note(msg: str, path: Path | str | None = None, when: float | None = None) -> bool:
    """Append one line. True if it landed, and never raises either way.

    The return value is not decoration: `main.status()` uses it to decide whether the
    vendored logger is the only reader left, and a writer that always reports success
    would make that decision a guess.
    """
    p = PATH if path is None else Path(path)
    try:
        _rotate(p)
        with open(p, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"{_stamp(when)} pid={os.getpid()} {msg}\n")
        return True
    except (OSError, ValueError):
        return False


def text(path: Path | str | None = None) -> str:
    """What the bootstrap log says, or "" when there is nothing to read.

    Read with errors replaced rather than strict: this is the path used when something
    has already gone wrong, and a UnicodeDecodeError here would hide the one line that
    explains it.
    """
    p = PATH if path is None else Path(path)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def tail(n: int = 5, path: Path | str | None = None) -> list[str]:
    return [ln for ln in text(path).splitlines() if ln.strip()][-n:]
