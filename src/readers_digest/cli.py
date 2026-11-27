"""readers-digest: a daily digest of your open tickets, new mail and unread Teams chats, read without changing a thing.

Exit statuses: 0 success; 1 the run failed (a source, the config, the
notifier, the review, writing a file); 2 a usage error (argparse's own).
"""

import argparse
import importlib.util
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from readers_digest import EXIT_FAILED, EXIT_OK, __version__, failure_line, imap, lock, notify, write_private
from readers_digest import config as config_
from readers_digest import review as review_
from readers_digest.config import Config, ConfigError
from readers_digest.digest import collect, format_digest, previous_working_day
from readers_digest.imap import SourceError
from readers_digest.store import Source, Store, default_path
from readers_digest.teams import commands as teams_cmds

# How long a sync waits for another one (the timer's, or the window's) to end.
SYNC_WAIT_S = 10 * 60
# How long a review waits for another one: a review sends every batch to the
# model in turn, each up to [review] timeout_s.
REVIEW_WAIT_S = 30 * 60
# The window needs PyGObject. A uv tool's venv usually runs on a Python the
# distribution's python3-gi was not built for, so `gui` falls back to
# starting the window on the system's own Python (gui()).
SYSTEM_PYTHON = "/usr/bin/python3"


def main(argv: list[str] | None = None) -> int:
    """Run the command `argv` (default: sys.argv[1:]) names; returns its exit status."""
    p = argparse.ArgumentParser(prog="readers-digest", description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=None, help=f"config file (default: {config_.default_path()})")
    p.add_argument("--db", type=Path, default=None, help=f"state database (default: {default_path()})")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="read new mail (and Teams chats, if enabled) into the state db")
    s.add_argument("--rescan", action="store_true",
                   help="read every folder's last backfill_days again, cursor or not; what is stored already is"
                        " skipped")
    s.add_argument("--no-teams", action="store_true", help="skip Teams this time")

    sh = sub.add_parser("show", help="print the digest and write digest.md next to the state db")
    sh.add_argument("--all", action="store_true", help="every ticket, not only those in [digest] focus")
    sh.add_argument("--since", type=_timestamp, default=None, metavar="TS",
                    help="ISO 8601 time for the new and closed sections and new mail; without a zone it is local"
                         " time (default: start of the previous working day)")
    sh.add_argument("--notify", action="store_true", help="send a desktop notification for what is new")
    sh.add_argument("--quiet", action="store_true", help="do not print the digest")

    rv = sub.add_parser("review", help="have [review] command tag the mail and Teams chats not reviewed yet, into"
                                       " review.md next to the state db (sends their text to that command)")
    rv.add_argument("--since", type=_timestamp, default=None, metavar="TS",
                    help="look no further back than this ISO 8601 time (default: start of the previous working"
                         " day)")
    rv.add_argument("--quiet", action="store_true", help="do not print the review")

    m = sub.add_parser("match", help="say which rule reads a subject and what it reads, for writing rules")
    m.add_argument("subject")
    m.add_argument("--from", dest="sender", default="", metavar="SENDER",
                   help="the sender: an address, or a whole From: value")

    pr = sub.add_parser("prune", help="delete what has been quiet for N days from the state db, then shrink it")
    pr.add_argument("--days", type=int, required=True, metavar="N",
                    help="at least [sync] backfill_days, or sync --rescan would store it again")
    pr.add_argument("--dry-run", action="store_true", help="only say what would be deleted")

    g = sub.add_parser("gui", help="open the digest in a window with a tray icon (needs PyGObject, GTK 4 and"
                                   " libadwaita)")
    g.add_argument("--hidden", action="store_true", help="start in the tray, e.g. at login")
    g.add_argument("--check", action="store_true", help="only check that the window can start: exit 0 if it can")

    sub.add_parser("teams-login", help="log in to Teams in the tool's own browser profile (opens a window)")
    sub.add_parser("teams-probe", help="for development: record the shape of Teams' chat calls, no content,"
                                       " into teams-probe.json next to the state db")

    args = p.parse_args(argv)
    if args.command == "gui":
        # The window loads the config and opens the db itself, in its own process.
        return gui(args.config, args.db, args.hidden, args.check)
    try:
        cfg = config_.load(args.config or config_.default_path())
    except ConfigError as e:
        print(f"readers-digest: {e}", file=sys.stderr)
        return EXIT_FAILED
    if args.command == "match":
        return match(cfg, args.subject, args.sender)
    if args.command == "prune" and args.days < cfg.sync.backfill_days:
        p.error(f"--days must be at least [sync] backfill_days ({cfg.sync.backfill_days}): below that, sync"
                f" --rescan would store it again")
    db = args.db or default_path()
    if args.command in ("teams-login", "teams-probe"):
        return teams_cmds.profile_command(args.command, db, cfg.teams)

    with Store(db, cfg.rules) as store:
        if args.command == "sync":
            def sync_all() -> int:
                code = sync(store, cfg, args.rescan)
                if cfg.teams.enabled and not args.no_teams:
                    code = max(code, teams_cmds.sync(store, db, cfg.teams))
                store.expire_text(datetime.now(UTC) - timedelta(days=cfg.sync.retention_days))
                return code

            return _locked("sync", SYNC_WAIT_S, sync_all)
        if args.command == "show":
            return show(store, cfg, db.parent / "digest.md", args.since, not args.all, args.notify, args.quiet)
        if args.command == "review":
            # Two reviews at once would send the same mail to the model twice.
            return _locked("review", REVIEW_WAIT_S,
                           lambda: review(store, cfg, db.parent / "review.md", args.since, args.quiet))
        return prune(store, args.days, args.dry_run)


def _locked(name: str, wait_s: float, run: Callable[[], int]) -> int:
    """The exit status of `run()`, run holding lock `name` (lock.path); EXIT_FAILED, not run, if it stays busy.

    Waits up to `wait_s` seconds for another run holding it, saying so on stderr.
    """
    try:
        with lock.held(lock.path(name), wait_s, lambda s: print(f"readers-digest: {name} {s}", file=sys.stderr)):
            return run()
    except lock.Busy as e:
        print(f"readers-digest: {name} {e}", file=sys.stderr)
        return EXIT_FAILED


def sync(store: Store, cfg: Config, rescan: bool = False) -> int:
    """Read every account's new mail into `store`; returns an exit status.

    With rescan, no cursor is passed, so every folder reads its last
    backfill_days again. An account or folder that fails does not stop the
    others; the failures are recorded as Source.MAIL's, for the digest's
    banner.
    """
    now = datetime.now(UTC)
    failures = []
    try:
        for a in cfg.accounts:
            try:
                password = imap.read_password(a.password_command)
                results = imap.fetch(a, password, {f.name: None if rescan else store.cursor(a.name, f.name)
                                                   for f in a.folders}, date.today(), cfg.sync)
            except SourceError as e:
                failures.append(f"{a.name}: {e}")
                continue
            for r in results:
                if r.failure is not None:
                    failures.append(f"{r.account}/{r.folder}: {r.failure}")
                    continue
                new = store.record(r.mails, r.cursor)
                for why in r.skipped:
                    print(f"readers-digest: skipped {why}", file=sys.stderr)
                how = f"backfill of {cfg.sync.backfill_days} days" if r.backfilled else "incremental"
                print(f"{r.account}/{r.folder}: {new} new ({len(r.mails)} read, {how}, {len(r.skipped)} skipped)")
    except Exception as e:
        # Not a source failure, so it surfaces with its traceback, but the
        # digest still learns that this sync did not happen.
        store.mark_failed(Source.MAIL, now, f"{type(e).__name__}: {e}")
        raise
    if failures:
        store.mark_failed(Source.MAIL, now, "; ".join(failures))
        for f in failures:
            print(f"readers-digest: sync failed: {f}", file=sys.stderr)
        return EXIT_FAILED
    store.mark_ok(Source.MAIL, now)
    return EXIT_OK


def show(store: Store, cfg: Config, out: Path, since: datetime | None, focus_only: bool, notify_: bool,
         quiet: bool) -> int:
    """Write the digest to `out`, print it unless `quiet`, and with notify_ announce what is new; an exit status."""
    now = _now()
    since = since or previous_working_day(now)
    text = format_digest(collect(store, cfg, since, now, focus_only))
    try:
        write_private(out, text)
    except OSError as e:
        print(f"readers-digest: cannot write {out}: {e}", file=sys.stderr)
        return EXIT_FAILED
    if not quiet:
        print(text, end="")
    if not notify_:
        return EXIT_OK
    line, keys = notify.decide(store, cfg, since, now)
    if line is not None:
        try:
            notify.send(line)
        except notify.NotifyError as e:
            # Not marked as announced, so the next run tries again.
            print(f"readers-digest: {e}", file=sys.stderr)
            return EXIT_FAILED
    store.mark_announced(keys, now)
    return EXIT_OK


def review(store: Store, cfg: Config, out: Path, since: datetime | None, quiet: bool) -> int:
    """Review what is new, write review.md to `out` and print it unless `quiet`; an exit status.

    review.md is replaced only when some entry was answered, so the last
    review stays readable until the next one. A review the model stopped
    part way writes what it answered and exits EXIT_FAILED.
    """
    now = _now()
    since = since or previous_working_day(now)
    text, err = review_.review(store, cfg, since, now)
    if err is not None:
        print(f"readers-digest: review: {failure_line(err)}", file=sys.stderr)
    if text is None:
        if err is None:
            print(f"review: nothing new since {since.isoformat(timespec='minutes')}")
        return EXIT_FAILED if err else EXIT_OK
    try:
        write_private(out, text)
    except OSError as e:
        print(f"readers-digest: cannot write {out}: {e}", file=sys.stderr)
        return EXIT_FAILED
    if not quiet:
        print(text, end="")
    return EXIT_FAILED if err else EXIT_OK


def match(cfg: Config, subject: str, sender: str) -> int:
    """Print what the rules read from `subject`; EXIT_FAILED when no rule matches it."""
    m = cfg.rules.match(subject, sender)
    if m is None:
        print("no rule matches: in a tickets folder this mail counts as unparsed, in a mail folder it is new mail")
        return EXIT_FAILED
    print(f"rule:     {m.rule}\nkind:     {m.kind}\nticket:   {m.ticket}\ntitle:    {m.title}\n"
          f"state:    {m.state}\ncategory: {m.category}")
    if url := cfg.rules.url(m.rule, m.ticket):
        print(f"url:      {url}")
    return EXIT_OK


def prune(store: Store, days: int, dry_run: bool) -> int:
    """Delete what has been quiet for `days` days (Store.prune), then shrink the file; an exit status."""
    r = store.prune(_now() - timedelta(days=days), dry_run)
    verb = "would delete" if dry_run else "deleted"
    print(f"prune: {verb} {r.tickets} tickets and {r.messages} messages quiet for {days} days or more")
    if not dry_run:
        store.con.execute("VACUUM")
    return EXIT_OK


def gui(config: Path | None, db: Path | None, hidden: bool = False, check: bool = False,
        execve: Callable | None = None) -> int:
    """Run the window: in this process if it can load PyGObject, else on the system Python, replacing this one.

    The window runs this CLI, on this Python (`python -m readers_digest`), for sync and review.
    `check` only loads what the window needs, and the config, and exits.
    """
    argv = ["--cli-python", sys.executable]
    argv += ["--config", str(config)] if config else []
    argv += ["--db", str(db)] if db else []
    argv += ["--hidden"] if hidden else []
    argv += ["--check"] if check else []
    if importlib.util.find_spec("gi") is not None:
        from readers_digest.gui.window import main as window
        return window(argv)
    try:
        path = os.pathsep.join(x for x in (str(_gui_path()), os.environ.get("PYTHONPATH")) if x)
        (execve or os.execve)(SYSTEM_PYTHON, [SYSTEM_PYTHON, "-m", "readers_digest.gui", *argv],
                              {**os.environ, "PYTHONPATH": path})
    except OSError as e:
        print(f"readers-digest: cannot start the window with {SYSTEM_PYTHON}: {e}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK  # only reached when execve is a test double


def _gui_path() -> Path:
    """A directory that holds only a link to this package, for the system Python's path.

    The package's own parent is the tool's site-packages, whose compiled
    modules are built for the tool's Python, not the system's. The link
    names the package's directory, which a reinstall replaces in place, so
    a running window finds the new version there.
    """
    d = default_path().parent / "gui-path"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    link, target = d / "readers_digest", Path(__file__).resolve().parent
    if not (link.is_symlink() and link.resolve() == target):
        tmp = d / "readers_digest.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target, target_is_directory=True)
        os.replace(tmp, link)
    return d


def _now() -> datetime:
    return datetime.now().astimezone()


def _timestamp(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {s!r}") from None
    return dt if dt.tzinfo else dt.astimezone()
