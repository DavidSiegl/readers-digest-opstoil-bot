"""One readers-digest run at a time for some work, across all of a user's runs: an flock on a file."""

import fcntl
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from readers_digest.store import default_path

POLL_S = 0.1


class Busy(Exception):
    """Another run still held the lock when the wait ran out. The message names that run."""


def path(name: str) -> Path:
    """The lock file for the work called `name`: one per user, whatever state db a run uses."""
    return default_path().parent / f"{name}.lock"


@contextmanager
def held(lock: Path, wait_s: float, warn: Callable[[str], None]) -> Iterator[None]:
    """Hold an exclusive flock on `lock` until the block ends, waiting up to `wait_s` seconds for it.

    While another run holds it, says once through `warn` who that is ("is
    in use by pid …: readers-digest …; waiting …"). Raises Busy, before the
    block runs, if the wait runs out. The lock's directory is made if
    missing. A run that is killed frees the lock: the kernel drops a flock
    with its file.
    """
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as f:
        deadline = time.monotonic() + wait_s
        holder = None
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if holder is None:
                    f.seek(0)
                    pid, _, argv = f.read().partition(" ")
                    holder = f"pid {pid}: {argv}" if pid else "another readers-digest run"
                    warn(f"is in use by {holder}; waiting up to {wait_s / 60:g} min for it to end")
                if time.monotonic() >= deadline:
                    raise Busy(f"still in use by {holder} after {wait_s / 60:g} min; nothing done") from None
                time.sleep(POLL_S)
        # Who holds it, for the message of a run that has to wait.
        f.truncate(0)
        f.write(f"{os.getpid()} {' '.join(['readers-digest', *sys.argv[1:]])}")
        f.flush()
        yield
