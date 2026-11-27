"""The commands that drive the Teams web client: sync's Teams part, teams-login and teams-probe.

web, which imports Playwright, is imported only when one of them runs, so
every other command starts without it and works without the `teams` extra.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from readers_digest import EXIT_FAILED, EXIT_OK, failure_line
from readers_digest.config import TeamsSettings
from readers_digest.store import Source, Store

_NO_EXTRA = "Playwright is not installed: install the `teams` extra, then run `playwright install chromium`"


def sync(store: Store, db: Path, settings: TeamsSettings) -> int:
    """Read the Teams chat list and unread messages into `store`; returns an exit status.

    `db` is the state db's path: the Teams browser profile lives next to
    it. A failure is recorded as Source.TEAMS's, for the digest's banner,
    and printed to stderr.
    """
    now = datetime.now(UTC)

    def failed(why: str) -> int:
        store.mark_failed(Source.TEAMS, now, why)
        print(f"readers-digest: teams: {why}", file=sys.stderr)
        return EXIT_FAILED

    try:
        from readers_digest.teams import web
    except ImportError:
        return failed(_NO_EXTRA)
    if web.last_login(db) is None:
        return failed("no Teams login yet: run readers-digest teams-login")
    try:
        convs, msgs, skipped = web.read(db, settings, store.teams_latest())
    except (web.TeamsError, web.PlaywrightError) as e:
        return failed(failure_line(e))
    new = store.record_teams(convs, msgs)
    store.mark_ok(Source.TEAMS, now)
    for why in skipped:
        print(f"readers-digest: teams: skipped {why}", file=sys.stderr)
    print(f"teams: {len(convs)} chats, {sum(c.unread for c in convs)} unread, {new} new messages ({len(msgs)} read)"
          + (f", {len(skipped)} conversations not read" if skipped else ""))
    return EXIT_OK


def profile_command(command: str, db: Path, settings: TeamsSettings) -> int:
    """Run teams-login or teams-probe on the Teams profile next to `db`; returns an exit status.

    Neither touches the state db. teams-probe writes teams-probe.json next to it.
    """
    try:
        from readers_digest.teams import web
    except ImportError:
        print(f"readers-digest: {command}: {_NO_EXTRA}", file=sys.stderr)
        return EXIT_FAILED
    try:
        if command == "teams-login":
            web.login(db, settings)
            print(f"teams-login: logged in; the session is kept in {web.profile_dir(db)}")
        else:
            out = db.parent / "teams-probe.json"
            n = web.probe(db, settings, out)
            print(f"teams-probe: {n} chat and channel calls recorded in {out} (shapes only, no content)")
    except (web.TeamsError, web.PlaywrightError) as e:
        print(f"readers-digest: {command}: {failure_line(e)}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK
