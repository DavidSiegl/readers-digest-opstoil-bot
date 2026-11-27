"""Reads ticket notifications and mail over IMAP, read-only, into a daily digest of open tickets and new mail.

What every command shares: the exit statuses a command returns, and the
one line it prints for a failure that stopped it.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("readers-digest-bot")
except PackageNotFoundError:  # run from a checkout that was never installed
    __version__ = "0+unknown"

__all__ = ["EXIT_FAILED", "EXIT_OK", "__version__", "failure_line"]

# A command's exit status; argparse exits 2 on its own for a usage error.
EXIT_OK = 0
EXIT_FAILED = 1


def failure_line(e: BaseException) -> str:
    """`e` as one line for stderr or a digest banner: its first line, or its type's name if it has no text."""
    return str(e).splitlines()[0] if str(e) else type(e).__name__
