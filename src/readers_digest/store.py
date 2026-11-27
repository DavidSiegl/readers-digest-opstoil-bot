"""SQLite state: the mail sync read, the tickets folded from it, IMAP cursors, and what review and the notifier did.

`message` is the source of truth. Each row also holds what the rules made
of it (rules.Match), and `ticket` holds one row per ticket, recomputed from
that ticket's whole history in Date: order whenever a mail about it
arrives. So the order mail arrives in never matters, and a ticket's row is
the same however its mail was read.

The rules' fingerprint is stored too. A Store opened with different rules
matches every stored subject again and refolds every ticket before it does
anything else, so a rule fixed in the config also fixes the mail it used to
misread, with nothing to remember to run.
"""

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Self

from readers_digest.rules import Kind, Match, Rules
from readers_digest.teams.chat import Conversation, Message

SCHEMA = """
-- Every mail a sync read, once, by Message-ID: a mail in two folders is kept
-- as the first one read. kind to rule are what the rules made of the subject
-- (rules.Match), all NULL but kind when no rule matched. body_text is NULL
-- when the text was not fetched or has expired; body_note then says why,
-- unless the mail is in a tickets folder, whose text is never fetched.
CREATE TABLE IF NOT EXISTS message (
    message_id TEXT PRIMARY KEY,
    account    TEXT NOT NULL,
    folder     TEXT NOT NULL,
    role       TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    sender     TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body_text  TEXT,
    body_note  TEXT,
    kind       TEXT NOT NULL,
    ticket_id  TEXT,
    title      TEXT,
    state      TEXT,
    category   TEXT,
    rule       TEXT
);
CREATE INDEX IF NOT EXISTS message_ticket ON message (ticket_id);
CREATE INDEX IF NOT EXISTS message_sent_at ON message (sent_at);

-- One row per ticket, folded from its messages (_fold). rule is the rule that
-- read its newest mail, for the ticket's link.
CREATE TABLE IF NOT EXISTS ticket (
    id         TEXT PRIMARY KEY,
    rule       TEXT NOT NULL,
    title      TEXT,
    category   TEXT,
    state      TEXT,
    state_at   TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    reminders  INTEGER NOT NULL
);

-- How far each folder has been read: the highest UID seen, valid while the
-- server's UIDVALIDITY for the folder stays the same.
CREATE TABLE IF NOT EXISTS cursor (
    account     TEXT NOT NULL,
    folder      TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL,
    PRIMARY KEY (account, folder)
);

-- The outcome of the latest attempts to read each source (Source), for the
-- digest's footer and its banners.
CREATE TABLE IF NOT EXISTS run (
    source        TEXT PRIMARY KEY,
    last_ok_at    TEXT,
    last_error_at TEXT,
    last_error    TEXT
);

-- Teams chats as the last read listed them (teams.chat.Conversation), and the
-- text messages read from them (teams.chat.Message). text is NULL once
-- expired; a message still unread keeps it.
CREATE TABLE IF NOT EXISTS teams_conversation (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    last_message_at TEXT,
    tenant          TEXT,
    unread          INTEGER NOT NULL,
    read_at         TEXT
);
CREATE TABLE IF NOT EXISTS teams_message (
    conversation_id TEXT NOT NULL,
    id              TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    sender_id       TEXT NOT NULL,
    sender_name     TEXT NOT NULL,
    text            TEXT,
    mine            INTEGER NOT NULL,
    mentions_me     INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, id)
);

-- What the notifier has announced, so each thing is announced once: a ticket
-- id, or a source's failure key for its current outage (notify.decide).
CREATE TABLE IF NOT EXISTS announced (
    key TEXT PRIMARY KEY,
    at  TEXT NOT NULL
);

-- What review's model made of a mail or a Teams conversation (review.py), by
-- the entry's key: a mail's Message-ID, or teams.chat.review_key. An entry with
-- a tag has been reviewed and is not sent again. draft is a reply for the
-- reader to edit, or NULL.
CREATE TABLE IF NOT EXISTS tag (
    key        TEXT PRIMARY KEY,
    act        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    draft      TEXT,
    at         TEXT NOT NULL
);

-- Settings of the db itself; key 'rules' holds Rules.fingerprint of the rules
-- the message rows were matched with.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# PRAGMA user_version of a db this code made. A change to a table of SCHEMA
# needs this raised and a step that takes an older db there, run by _upgrade;
# tables that are only added need neither, since SCHEMA creates them anywhere.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Mail:
    """One mail as sync read it."""

    message_id: str
    account: str
    folder: str
    # config.Role's value: "tickets" or "mail".
    role: str
    sent_at: datetime
    sender: str
    subject: str
    body_text: str | None = None
    body_note: str | None = None


@dataclass(frozen=True)
class Cursor:
    """Where a folder's last sync stopped: the folder's UIDVALIDITY then, and the highest UID it read."""

    account: str
    folder: str
    uidvalidity: int
    last_uid: int


@dataclass(frozen=True)
class Ticket:
    """One ticket as folded from all its mail (the module docstring says how)."""

    id: str
    # The rule that read its newest mail (Rules.url).
    rule: str
    title: str | None
    category: str | None
    # None while no mail about it has named a state.
    state: str | None
    # When it entered `state`: the Date: of the first mail that gave the state it has now.
    state_at: datetime | None
    first_seen: datetime
    last_seen: datetime
    # Reminders since it entered its state, or since it was first seen if it has none.
    reminders: int


@dataclass(frozen=True)
class Tag:
    """What review's model made of one mail or Teams conversation."""

    # The entry's key: a mail's Message-ID, or teams.chat.review_key.
    key: str
    # Who has to do something: review.ACTS.
    act: str
    # What it is: review.KINDS.
    kind: str
    summary: str
    draft: str | None


class Source(StrEnum):
    """What a run reads, each with its own status."""

    MAIL = "mail"
    TEAMS = "teams"


@dataclass(frozen=True)
class RunStatus:
    """When a Source was last read successfully and when it last failed, with that failure's one-line error."""

    last_ok_at: datetime | None
    last_error_at: datetime | None
    last_error: str | None

    @property
    def failed(self) -> bool:
        """The latest attempt failed."""
        return self.last_error_at is not None and (self.last_ok_at is None or self.last_error_at > self.last_ok_at)


class Pruned(NamedTuple):
    """How many tickets and stored messages a prune deleted, or would delete."""

    tickets: int
    messages: int


# A Teams message the reader has not read yet: it keeps its text and is not
# pruned, however old, until Teams says it is read.
_STILL_UNREAD = ("EXISTS (SELECT 1 FROM teams_conversation c WHERE c.id = teams_message.conversation_id"
                 " AND c.unread = 1 AND (c.read_at IS NULL OR teams_message.sent_at > c.read_at))")
# The review key of every stored Teams message (teams.chat.review_key, which this must spell the same).
_REVIEW_KEYS = "SELECT 'teams:' || conversation_id || ':' || id FROM teams_message"


def default_path() -> Path:
    """Where the state db is without --db.

    That is readers-digest/state.db in $XDG_STATE_HOME,
    or in ~/.local/state when that is unset or relative.
    """
    base = os.environ.get("XDG_STATE_HOME", "")
    # The XDG base directory spec says a relative value is to be ignored.
    root = Path(base) if os.path.isabs(base) else Path.home() / ".local/state"
    return root / "readers-digest" / "state.db"


class Store:
    """The state db at `path`, made if missing, its messages matched with `rules`.

    Opening it makes the file owner-only (it holds mail text), and the
    default state directory too; upgrades its schema; and re-matches every
    message if `rules` differ from the rules it was last matched with. Raises OSError or sqlite3.Error,
    noting the path, when it cannot be opened, and sqlite3.DatabaseError
    for a db a newer version of this tool wrote.
    """

    def __init__(self, path: Path, rules: Rules):
        self.path = path
        self.rules = rules
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # mkdir's mode does nothing for a directory that exists: teams-login
            # or the gui may have made it first, with the umask's permissions.
            # A directory chosen with --db is left as its owner set it.
            if path.parent == default_path().parent:
                os.chmod(path.parent, 0o700)
            # isolation_level=None: no implicit BEGIN. Every transaction is
            # opened by _transaction(), so what is atomic is visible here.
            self.con = sqlite3.connect(path, isolation_level=None, timeout=30)
            os.chmod(path, 0o600)
            self.con.row_factory = sqlite3.Row
            self._upgrade()
        except (OSError, sqlite3.Error) as e:
            e.add_note(f"opening state db {path}")
            raise

    def close(self) -> None:
        """Close the database; leaving the `with` block does the same."""
        self.con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _upgrade(self) -> None:
        # Most opens find nothing to do. Checked without the write lock, so
        # the window does not wait on a sync's long transaction to learn that.
        if self._current():
            return
        with self._transaction():
            version = self.con.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                e = sqlite3.DatabaseError(f"schema version {version} is newer than this version's {SCHEMA_VERSION}")
                e.add_note("a newer readers-digest wrote it; upgrade, or pass another --db")
                raise e
            for statement in _statements(SCHEMA):
                self.con.execute(statement)
            self.con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            stored = self.con.execute("SELECT value FROM meta WHERE key = 'rules'").fetchone()
            if stored is None or stored[0] != self.rules.fingerprint:
                self._rematch()

    def _current(self) -> bool:
        """The schema is this version's and the messages were matched with self.rules."""
        if self.con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return False
        stored = self.con.execute("SELECT value FROM meta WHERE key = 'rules'").fetchone()
        return stored is not None and stored[0] == self.rules.fingerprint

    def _rematch(self) -> None:
        """Match every stored subject with self.rules and refold every ticket. Inside a transaction."""
        rows = self.con.execute("SELECT message_id, role, sender, subject FROM message").fetchall()
        for r in rows:
            self.con.execute(
                "UPDATE message SET kind = ?, ticket_id = ?, title = ?, state = ?, category = ?, rule = ?"
                " WHERE message_id = ?",
                (*_matched(self.rules, r["role"], r["sender"], r["subject"]), r["message_id"]),
            )
        self.con.execute("DELETE FROM ticket")
        ids = self.con.execute("SELECT DISTINCT ticket_id FROM message WHERE ticket_id IS NOT NULL")
        _refold(self.con, {r[0] for r in ids})
        self.con.execute("INSERT INTO meta (key, value) VALUES ('rules', ?)"
                         " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (self.rules.fingerprint,))

    def record(self, mails: Iterable[Mail], cursor: Cursor | None = None) -> int:
        """Store the mails not stored yet, refold the tickets they are about, and move `cursor`, atomically.

        A mail whose Message-ID is stored already is skipped, whatever
        folder it is in. Returns how many were new. Raises ValueError for a
        mail whose sent_at has no timezone.
        """
        new = 0
        touched = set()
        with self._transaction():
            for m in mails:
                matched = _matched(self.rules, m.role, m.sender, m.subject)
                c = self.con.execute(
                    "INSERT INTO message (message_id, account, folder, role, sent_at, sender, subject, body_text,"
                    " body_note, kind, ticket_id, title, state, category, rule)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING",
                    (m.message_id, m.account, m.folder, m.role, _utc(m.sent_at), m.sender, m.subject,
                     m.body_text, m.body_note, *matched),
                )
                if c.rowcount:
                    new += 1
                    if matched[1] is not None:
                        touched.add(matched[1])
            _refold(self.con, touched)
            if cursor is not None:
                self.con.execute(
                    "INSERT INTO cursor (account, folder, uidvalidity, last_uid) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (account, folder) DO UPDATE"
                    " SET uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid",
                    (cursor.account, cursor.folder, cursor.uidvalidity, cursor.last_uid),
                )
        return new

    def cursor(self, account: str, folder: str) -> Cursor | None:
        """Where the last sync of `account`'s `folder` stopped; None when it has never been read."""
        r = self.con.execute("SELECT * FROM cursor WHERE account = ? AND folder = ?", (account, folder)).fetchone()
        return None if r is None else Cursor(account, folder, r["uidvalidity"], r["last_uid"])

    def tickets(self) -> list[Ticket]:
        """Every ticket, oldest first."""
        return [
            Ticket(r["id"], r["rule"], r["title"], r["category"], r["state"], _dt(r["state_at"]),
                   _dt(r["first_seen"]), _dt(r["last_seen"]), r["reminders"])
            for r in self.con.execute("SELECT * FROM ticket ORDER BY first_seen, id")
        ]

    def mail(self, since: datetime) -> list[Mail]:
        """Mail of `mail` folders that no rule matched, sent at or after `since`, newest first."""
        rows = self.con.execute(
            "SELECT * FROM message WHERE kind = ? AND sent_at >= ? ORDER BY sent_at DESC, message_id",
            (Kind.MAIL.value, _utc(since)),
        )
        return [_mail(r) for r in rows]

    def unparsed(self, limit: int) -> tuple[int, list[str]]:
        """(how many mails of tickets folders no rule matched, the subjects of the newest `limit` of them)."""
        n = self.con.execute("SELECT count(*) FROM message WHERE kind = ?", (Kind.UNPARSED.value,)).fetchone()[0]
        rows = self.con.execute("SELECT subject FROM message WHERE kind = ? ORDER BY sent_at DESC LIMIT ?",
                                (Kind.UNPARSED.value, limit))
        return n, [r[0] for r in rows]

    def expire_text(self, before: datetime) -> int:
        """Drop the text of mail and of read Teams messages sent before `before`; returns how many lost it.

        A mail's body_note says it expired. A Teams message still unread keeps
        its text, however old, until Teams says it is read.
        """
        cut = _utc(before)
        with self._transaction():
            n = self.con.execute(
                "UPDATE message SET body_text = NULL, body_note = 'expired'"
                " WHERE body_text IS NOT NULL AND sent_at < ?",
                (cut,)).rowcount
            return n + self.con.execute(
                f"UPDATE teams_message SET text = NULL WHERE text IS NOT NULL AND sent_at < ? AND NOT {_STILL_UNREAD}",
                (cut,)).rowcount

    def mark_ok(self, source: Source, at: datetime) -> None:
        """Note that reading `source` succeeded at `at`."""
        self._mark(source, "last_ok_at = ?", (_utc(at),))

    def mark_failed(self, source: Source, at: datetime, error: str) -> None:
        """Note that reading `source` failed at `at`, for the reason `error`, one line."""
        self._mark(source, "last_error_at = ?, last_error = ?", (_utc(at), error))

    def _mark(self, source: Source, assignments: str, values: tuple) -> None:
        with self._transaction():
            self.con.execute("INSERT INTO run (source) VALUES (?) ON CONFLICT (source) DO NOTHING", (source.value,))
            self.con.execute(f"UPDATE run SET {assignments} WHERE source = ?", (*values, source.value))

    def status(self, source: Source) -> RunStatus:
        """How the latest reads of `source` went; all None when it was never read."""
        r = self.con.execute("SELECT * FROM run WHERE source = ?", (source.value,)).fetchone()
        if r is None:
            return RunStatus(None, None, None)
        return RunStatus(_dt(r["last_ok_at"]), _dt(r["last_error_at"]), r["last_error"])

    def record_teams(self, convs: Iterable[Conversation], msgs: Iterable[Message]) -> int:
        """Replace the stored chat list with `convs`, and store the messages of `msgs` not stored yet, atomically.

        A conversation missing from `convs` is no longer read (left, muted,
        deleted): it goes, with its messages. Returns how many messages were new.
        """
        convs = list(convs)
        new = 0
        with self._transaction():
            self.con.execute("CREATE TEMP TABLE IF NOT EXISTS listed (id TEXT PRIMARY KEY)")
            self.con.execute("DELETE FROM temp.listed")
            self.con.executemany("INSERT OR IGNORE INTO temp.listed (id) VALUES (?)", [(c.id,) for c in convs])
            self.con.execute("DELETE FROM teams_message WHERE conversation_id NOT IN (SELECT id FROM temp.listed)")
            self.con.execute("DELETE FROM teams_conversation WHERE id NOT IN (SELECT id FROM temp.listed)")
            self.con.executemany(
                "INSERT OR REPLACE INTO teams_conversation (id, kind, title, last_message_at, tenant, unread, read_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(c.id, c.kind, c.title, _opt_utc(c.last_message_at), c.tenant, int(c.unread), _opt_utc(c.read_at))
                 for c in convs],
            )
            for m in msgs:
                new += self.con.execute(
                    "INSERT INTO teams_message (conversation_id, id, sent_at, sender_id, sender_name, text, mine,"
                    " mentions_me) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (m.conversation_id, m.id, _utc(m.sent_at), m.sender_id, m.sender_name, m.text, int(m.mine),
                     int(m.mentions_me)),
                ).rowcount
        return new

    def teams(self) -> tuple[list[Conversation], list[Message]]:
        """The stored chat list, and every stored message, oldest first. An expired message's text is ""."""
        convs = [Conversation(r["id"], r["kind"], r["title"], _dt(r["last_message_at"]), r["tenant"],
                              bool(r["unread"]), _dt(r["read_at"]))
                 for r in self.con.execute("SELECT * FROM teams_conversation ORDER BY id")]
        msgs = [Message(r["conversation_id"], r["id"], _dt(r["sent_at"]), r["sender_id"], r["sender_name"],
                        r["text"] or "", bool(r["mine"]), bool(r["mentions_me"]))
                for r in self.con.execute("SELECT * FROM teams_message ORDER BY sent_at, id")]
        return convs, msgs

    def teams_latest(self) -> dict[str, datetime]:
        """The newest stored message's time, per conversation that has one."""
        return {r[0]: _dt(r[1]) for r in
                self.con.execute("SELECT conversation_id, max(sent_at) FROM teams_message GROUP BY conversation_id")}

    def announced(self, keys: Iterable[str]) -> dict[str, datetime]:
        """When each of `keys` that has been announced was."""
        keys = list(keys)
        rows = self.con.execute(
            f"SELECT key, at FROM announced WHERE key IN ({', '.join('?' * len(keys))})", keys
        ) if keys else []
        return {r["key"]: _dt(r["at"]) for r in rows}

    def mark_announced(self, keys: Iterable[str], at: datetime) -> None:
        """Note `keys` as announced at `at`; one announced before gets the new time."""
        with self._transaction():
            self.con.executemany(
                "INSERT INTO announced (key, at) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET at = excluded.at",
                [(k, _utc(at)) for k in keys],
            )

    def tags(self) -> dict[str, Tag]:
        """The tag of every reviewed entry, by its key."""
        return {r["key"]: Tag(r["key"], r["act"], r["kind"], r["summary"], r["draft"])
                for r in self.con.execute("SELECT * FROM tag")}

    def record_tags(self, tags: Iterable[Tag], at: datetime) -> None:
        """Store `tags`, replacing any earlier tag of the same entry."""
        with self._transaction():
            self.con.executemany(
                "INSERT OR REPLACE INTO tag (key, act, kind, summary, draft, at) VALUES (?, ?, ?, ?, ?, ?)",
                [(t.key, t.act, t.kind, t.summary, t.draft, _utc(at)) for t in tags],
            )

    def prune(self, before: datetime, dry_run: bool = False) -> Pruned:
        """Delete what has been quiet since before `before`; with dry_run, only count it.

        A ticket with no mail since then goes whole, with all its mail, so
        that a refold never sees half a history. Other mail goes by its own
        Date:, and so do Teams messages, except unread ones; a tag goes with
        what it tags. An announcement goes when its ticket does or when it
        is older than `before`. Cursors stay, so pruned mail is not read again.
        """
        cut = _utc(before)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            quiet = "(SELECT id FROM ticket WHERE last_seen < :cut)"
            self.con.execute(f"DELETE FROM announced WHERE key IN {quiet} OR at < :cut", {"cut": cut})
            messages = self.con.execute(
                f"DELETE FROM message WHERE ticket_id IN {quiet} OR (ticket_id IS NULL AND sent_at < :cut)",
                {"cut": cut}).rowcount
            tickets = self.con.execute("DELETE FROM ticket WHERE last_seen < ?", (cut,)).rowcount
            messages += self.con.execute(f"DELETE FROM teams_message WHERE sent_at < ? AND NOT {_STILL_UNREAD}",
                                         (cut,)).rowcount
            self.con.execute(f"DELETE FROM tag WHERE key NOT IN (SELECT message_id FROM message)"
                             f" AND key NOT IN ({_REVIEW_KEYS})")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("ROLLBACK" if dry_run else "COMMIT")
        return Pruned(tickets, messages)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock up front, so a manual run that
        # overlaps the timer's waits instead of failing halfway. Nested use
        # joins the transaction already open.
        if self.con.in_transaction:
            yield
            return
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")


def _matched(rules: Rules, role: str, sender: str, subject: str) -> tuple:
    """The (kind, ticket_id, title, state, category, rule) columns of a message."""
    m: Match | None = rules.match(subject, sender)
    if m is None:
        kind = Kind.MAIL if role == "mail" else Kind.UNPARSED
        return (kind.value, None, None, None, None, None)
    return (m.kind.value, m.ticket, m.title, m.state, m.category, m.rule)


def _refold(con: sqlite3.Connection, ticket_ids: set[str]) -> None:
    """Recompute the ticket row of each of `ticket_ids` from its stored messages."""
    for tid in sorted(ticket_ids):
        history = con.execute(
            "SELECT * FROM message WHERE ticket_id = ? ORDER BY sent_at, message_id", (tid,)
        ).fetchall()
        if not history:
            con.execute("DELETE FROM ticket WHERE id = ?", (tid,))
            continue
        con.execute(
            "INSERT OR REPLACE INTO ticket (id, rule, title, category, state, state_at, first_seen, last_seen,"
            " reminders) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _fold(tid, history),
        )


def _fold(tid: str, history: list[sqlite3.Row]) -> tuple:
    """A ticket's row from its messages, oldest first.

    The latest mail that names a title, category or state wins for each,
    so a mail read late changes nothing if a newer one says otherwise.
    """
    title = category = state = state_at = None
    reminders = 0
    for r in history:
        title = r["title"] or title
        category = r["category"] or category
        if r["kind"] == Kind.REMINDER.value:
            reminders += 1
        elif r["state"] is not None and r["state"] != state:
            state, state_at, reminders = r["state"], r["sent_at"], 0
    first, last = history[0], history[-1]
    return (tid, last["rule"], title, category, state, state_at, first["sent_at"], last["sent_at"], reminders)


def _mail(r: sqlite3.Row) -> Mail:
    return Mail(r["message_id"], r["account"], r["folder"], r["role"], _dt(r["sent_at"]), r["sender"],
                   r["subject"], r["body_text"], r["body_note"])


def _statements(script: str) -> Iterator[str]:
    """The SQL statements in `script`, one at a time, for running inside a transaction as executescript cannot."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""


def _utc(at: datetime) -> str:
    # Every timestamp is stored as fixed-width UTC text, so ORDER BY and
    # comparisons on the column are chronological. Microseconds, because
    # Teams' read position and its messages differ within a second. Rows
    # written before had whole seconds; "+" sorts before ".", so they still
    # order before any later time in their second.
    if at.tzinfo is None:
        raise ValueError(f"timestamp {at} has no timezone")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _opt_utc(at: datetime | None) -> str | None:
    return None if at is None else _utc(at)


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)
