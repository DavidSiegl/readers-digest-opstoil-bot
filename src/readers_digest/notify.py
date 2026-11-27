"""One-line desktop notification after a timer run.

Announces new open tickets in focus, Teams messages written to the reader,
and a failing source once per outage, so a server that is down all day does
not raise a popup on every run. What has been announced is kept in the store.
"""

import subprocess
from collections.abc import Callable
from datetime import datetime

from readers_digest.config import Config
from readers_digest.digest import collect
from readers_digest.store import Source, Store
from readers_digest.teams.chat import review_key, to_me

# Before a source's name, the key announcing its current outage.
FAILURE = "failure:"
# Ids a popup names before "and N more"; the full list is in digest.md.
MAX_IDS = 3


class NotifyError(Exception):
    """notify-send could not be run, or failed."""


def decide(store: Store, cfg: Config, since: datetime, now: datetime) -> tuple[str | None, list[str]]:
    """What to announce: (one line or None, the keys to mark announced once it is sent).

    Only tickets first seen at or after `since` are candidates, so a backfill
    of old mail does not announce weeks of history. A Teams conversation is
    announced again when a newer message to the reader arrives in it.
    """
    parts, keys = [], []
    for source in Source:
        st = store.status(source)
        key = FAILURE + source.value
        prev = store.announced([key]).get(key)
        # Announced already, unless a success came after that.
        if st.failed and (prev is None or (st.last_ok_at is not None and prev <= st.last_ok_at)):
            parts.append(f"{source.value} failed: {st.last_error}")
            keys.append(key)

    d = collect(store, cfg, since, now)
    # Only tickets known to be in focus: one of unknown category is in the digest, not worth a popup.
    tickets = [t.id for t in d.new if t.category is not None or cfg.digest.focus is None]
    chats = {review_key(u): d.teams_titles[u.conversation.id] for u in d.teams_unread
             if any(to_me(u.conversation, m) for m in u.messages)}
    seen = store.announced(tickets + list(chats))
    tickets = [k for k in tickets if k not in seen]
    chats = {k: v for k, v in chats.items() if k not in seen}
    if tickets:
        parts.append(_list(tickets, f"{len(tickets)} new ticket{'s' if len(tickets) > 1 else ''}"))
    if chats:
        parts.append(_list(list(chats.values()), f"Teams: {len(chats)} chat{'s' if len(chats) > 1 else ''} for you"))
    return ("; ".join(parts) or None), keys + tickets + list(chats)


def _list(names: list[str], what: str) -> str:
    shown = ", ".join(names[:MAX_IDS])
    more = f" and {len(names) - MAX_IDS} more" if len(names) > MAX_IDS else ""
    return f"{what} ({shown}{more})"


def send(line: str, run: Callable = subprocess.run) -> None:
    """Show `line` as a desktop notification; raises NotifyError."""
    try:
        p = run(["notify-send", "--app-name=readers-digest", "readers-digest", line], capture_output=True, text=True,
                timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NotifyError(f"cannot run notify-send: {e}") from e
    if p.returncode != 0:
        detail = (p.stderr or "").strip().splitlines()
        raise NotifyError(f"notify-send exited {p.returncode}: {detail[0] if detail else ''}".rstrip(": "))
