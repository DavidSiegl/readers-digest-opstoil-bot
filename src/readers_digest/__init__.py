"""Reads ticket notifications and mail over IMAP, read-only, into a daily digest of open tickets and new mail.

What every command shares: the exit statuses a command returns, the one
line it prints for a failure that stopped it, and how it writes a file
only its owner may read.
"""

import os
import zipimport
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("readers-digest-opstoil-bot")
except PackageNotFoundError:  # run from a checkout that was never installed
    __version__ = "0+unknown"

# The zip archive this package was imported from, as the release's
# readers-digest.pyz is, absolute but with symlinks kept, so that a link
# moved to a newer archive counts as a replacement; None when it was
# imported from files.
# Another Python finds the package only with the archive on its path, and
# GTK reads no file inside it.
ARCHIVE = Path(__loader__.archive).absolute() if isinstance(__loader__, zipimport.zipimporter) else None

__all__ = ["ARCHIVE", "EXIT_FAILED", "EXIT_OK", "__version__", "failure_line", "write_private"]

# A command's exit status; argparse exits 2 on its own for a usage error.
EXIT_OK = 0
EXIT_FAILED = 1


def failure_line(e: BaseException) -> str:
    """`e` as one line for stderr or a digest banner: its first line, or its type's name if it has no text.

    An error of the browser driver (Playwright) gets a "browser: " prefix,
    since its text alone does not say which part failed.
    """
    text = str(e).splitlines()[0] if str(e) else type(e).__name__
    return f"browser: {text}" if type(e).__module__.startswith("playwright") else text


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text` (UTF-8), readable by its owner only; raises OSError.

    Whoever has the file open sees the old text or the new, never half.
    The file is created owner-only, so its text is never readable by
    others, even for a moment; a `.tmp` next to it is used on the way.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)  # in case an older .tmp was left behind
        f.write(text)
    os.replace(tmp, path)
