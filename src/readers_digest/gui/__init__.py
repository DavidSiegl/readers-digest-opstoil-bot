"""The desktop window (window), its tray icon (tray), and what they show (model).

Needs PyGObject with GTK 4 and libadwaita, which distributions ship for
their own Python: `readers-digest gui` runs the window in its own process
when it can import them, and otherwise on /usr/bin/python3 (cli.gui).
The window imports nothing outside the standard library, PyGObject and
this package, and runs sync and review as readers-digest subprocesses.
"""

from pathlib import Path

# The readers_digest package this was loaded from.
PACKAGE_ROOT = Path(__file__).parent.parent


def code_stamp(root: Path = PACKAGE_ROOT) -> tuple | None:
    """What the .py files under `root` are now: equal stamps mean no file was replaced, added or removed.

    None when there is none to read, as while an install has the package
    half removed. A stamp is good only for comparing with another one.
    """
    try:
        stamp = tuple(sorted((str(p.relative_to(root)), s.st_mtime_ns, s.st_size)
                             for p in root.rglob("*.py") for s in [p.stat()]))
    except OSError:
        return None
    return stamp or None
