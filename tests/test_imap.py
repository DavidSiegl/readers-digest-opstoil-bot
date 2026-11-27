"""imap.fetch against a scripted in-process IMAP server, and read_password with a fake runner.

Every fetch in these tests also checks that only read-only commands went out
and that the session ended with LOGOUT (see `run`).
"""

import imaplib
import re
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime

import pytest

from readers_digest import imap
from readers_digest.imap import SourceError, fetch, read_password
from readers_digest.store import Cursor

USER = "alex@example.com"  # the login of CONFIG's account
# A quote and a backslash, to exercise imaplib's quoting of the password.
PASSWORD = 'p"w\\x'
TODAY = date(2026, 11, 20)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Everything fetch may ever send. imaplib itself sends CAPABILITY on connect.
READ_ONLY = {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
# A fetch item that would set \Seen.
NON_PEEK_BODY = re.compile(r"(?<!\.PEEK)\bBODY\[")


def raw_mail(subject: str, sent: datetime | str, mid: str | None = "<m@example.com>",
             body: str = "Hello there", ctype: str = "text/plain") -> bytes:
    """A whole message as the server stores it; `mid` None leaves out Message-ID."""
    lines = ["From: news@example.com", f"Subject: {subject}",
             f"Date: {sent if isinstance(sent, str) else format_datetime(sent)}"]
    if mid is not None:
        lines.append(f"Message-ID: {mid}")
    lines.append(f"Content-Type: {ctype}; charset=utf-8")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body.encode()


def msg(days_ago: int, subject: str = "hi", **kw) -> tuple[date, bytes]:
    """A mailbox entry (internal date, raw message) sent `days_ago` days before TODAY at 10:00 UTC."""
    sent = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), UTC).replace(hour=10)
    return sent.date(), raw_mail(subject, sent, **kw)


def unquote(s: str) -> str:
    assert s[0] == s[-1] == '"', s
    return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def header_fields(raw: bytes, names: set[str]) -> bytes:
    """The named header fields of a raw message, as BODY[HEADER.FIELDS (...)] returns them."""
    head = raw.split(b"\r\n\r\n", 1)[0]
    out = [line for line in head.split(b"\r\n") if line.split(b":", 1)[0].decode().upper() in names]
    return b"\r\n".join(out) + b"\r\n\r\n"


def verb(cmd: str) -> str:
    return " ".join(cmd.split(" ")[:2]) if cmd.startswith("UID ") else cmd.split(" ")[0]


class FakeServer:
    """A scripted IMAP server that speaks just enough of the protocol for imaplib.

    mailboxes: {folder: {uid: (internal date, raw message)}}; both of CONFIG's
    folders exist, empty, unless given. `commands` logs every command sent,
    without its tag and with LOGIN's password left out.
    """

    def __init__(self, mailboxes=None, uidvalidity=7):
        self.mailboxes = {"Tickets": {}, "INBOX": {}, **(mailboxes or {})}
        self.uidvalidity = uidvalidity
        self.folder = None
        self.commands = []
        self.out = bytearray(b"* OK [CAPABILITY IMAP4rev1] fake ready\r\n")

    @property
    def messages(self):
        return self.mailboxes[self.folder]

    def readline(self):
        i = self.out.find(b"\n")
        if i < 0:
            return b""
        line = bytes(self.out[: i + 1])
        del self.out[: i + 1]
        return line

    def read(self, size):
        data = bytes(self.out[:size])
        del self.out[:size]
        return data

    def receive(self, data):
        tag, cmd = data.decode().rstrip("\r\n").split(" ", 1)
        v = verb(cmd)
        self.commands.append(cmd if v != "LOGIN" else f"LOGIN {cmd.split(' ')[1]}")
        reply = getattr(self, "_" + v.replace(" ", "_").lower(), None)
        *untagged, tagged = reply(cmd) if reply else ["BAD unknown command"]
        for u in untagged:
            self.out += u if isinstance(u, bytes) else f"* {u}\r\n".encode()
        self.out += f"{tag} {tagged}\r\n".encode()

    def _capability(self, cmd):
        return ["CAPABILITY IMAP4rev1", "OK CAPABILITY completed"]

    def _login(self, cmd):
        _, user, pw = cmd.split(" ", 2)
        return ["OK LOGIN completed"] if user == USER and unquote(pw) == PASSWORD else ["NO LOGIN failed"]

    def _examine(self, cmd):
        folder = unquote(cmd.split(" ", 1)[1])
        if folder not in self.mailboxes:
            return ["NO folder not found"]
        self.folder = folder
        return [f"{len(self.messages)} EXISTS", f"OK [UIDVALIDITY {self.uidvalidity}]",
                "OK [READ-ONLY] EXAMINE completed"]

    def _uid_search(self, cmd):
        _, _, key, arg = cmd.split(" ")
        if key == "UID":
            lo = int(arg.split(":")[0])
            hits = [u for u in self.messages if u >= lo]
            if not hits and self.messages:
                hits = [max(self.messages)]  # RFC 3501: "*" is the highest UID, even below lo
        else:
            d, mon, y = arg.split("-")
            since = date(int(y), MONTHS.index(mon) + 1, int(d))
            hits = [u for u, (when, _) in self.messages.items() if when >= since]
        return [("SEARCH " + " ".join(map(str, sorted(hits)))).rstrip(), "OK SEARCH completed"]

    def _uid_fetch(self, cmd):
        _, _, uid_set, items = cmd.split(" ", 3)
        out = []
        for seq, uid in enumerate(map(int, uid_set.split(",")), 1):
            if uid not in self.messages:
                continue
            raw = self.messages[uid][1]
            if items == imap.HEADER_ITEMS:
                fields = "SUBJECT FROM DATE MESSAGE-ID"
                body = header_fields(raw, set(fields.split()))
                attrs = f"RFC822.SIZE {len(raw)} BODY[HEADER.FIELDS ({fields})]"
            elif items == imap.BODY_ITEMS:
                body, attrs = raw, "BODY[]"
            else:
                raise AssertionError(f"unexpected fetch items {items}")
            out.append(f"* {seq} FETCH (UID {uid} {attrs} {{{len(body)}}}\r\n".encode() + body + b")\r\n")
        return [*out, "OK FETCH completed"]

    def _logout(self, cmd):
        return ["BYE logging out", "OK LOGOUT completed"]


class FakeIMAP(imaplib.IMAP4):
    """imaplib's client, wired to a FakeServer instead of a socket."""

    def __init__(self, server):
        self.server = server
        super().__init__("fake", 1143)

    def open(self, host="", port=0, timeout=None):
        self.host, self.port, self.sock = host, port, None

    def send(self, data):
        self.server.receive(data)

    def readline(self):
        return self.server.readline()

    def read(self, size):
        return self.server.read(size)

    def shutdown(self):
        pass


def run(server, cfg, cursors=None, password=PASSWORD, **sync):
    """fetch CONFIG's account from `server`, with SyncSettings overridden by `sync`.

    Afterwards, however fetch ended, checks that only READ_ONLY commands
    were sent, no FETCH asked for a non-PEEK body, and LOGOUT came last.
    """
    try:
        return fetch(cfg.accounts[0], password, cursors or {}, TODAY, replace(cfg.sync, **sync),
                     connect=lambda a, t: FakeIMAP(server))
    finally:
        for cmd in server.commands:
            assert verb(cmd) in READ_ONLY, cmd
            assert not NON_PEEK_BODY.search(cmd), cmd
        assert server.commands[-1] == "LOGOUT"


def by_folder(results):
    return {r.folder: r for r in results}


def searches(server):
    return [c for c in server.commands if c.startswith("UID SEARCH")]


def fetched(server, items=imap.HEADER_ITEMS):
    """The UID sets of every UID FETCH of `items`, in order."""
    return [c.split(" ")[2] for c in server.commands if c.startswith("UID FETCH") and c.endswith(items)]


def imap_date(days_ago):
    d = TODAY - timedelta(days=days_ago)
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


# --- what goes over the wire ----------------------------------------------


def test_folders_are_examined_read_only_in_config_order(cfg):
    server = FakeServer()
    run(server, cfg)
    assert server.commands[:2] == ["CAPABILITY", f"LOGIN {USER}"]
    assert [c for c in server.commands if verb(c) == "EXAMINE"] == ['EXAMINE "Tickets"', 'EXAMINE "INBOX"']


# --- cursors ----------------------------------------------------------------


def test_first_sync_backfills_and_returns_a_cursor(cfg):
    server = FakeServer({"Tickets": {3: msg(40, mid="<3@example.com>"), 5: msg(10, mid="<5@example.com>")}})
    r = by_folder(run(server, cfg))["Tickets"]
    assert r.backfilled
    assert searches(server)[0] == f"UID SEARCH SINCE {imap_date(cfg.sync.backfill_days)}"
    assert [m.message_id for m in r.mails] == ["<5@example.com>"]
    assert r.cursor == Cursor("work", "Tickets", 7, 5)


def test_empty_backfill_leaves_the_cursor_unset(cfg):
    assert by_folder(run(FakeServer(), cfg))["Tickets"].cursor is None


def test_incremental_sync_reads_newer_uids_and_the_overlap(cfg):
    server = FakeServer({"Tickets": {
        2: msg(20, mid="<2@example.com>"),  # seen, outside the overlap
        4: msg(1, mid="<4@example.com>"),  # seen, inside the overlap
        6: msg(20, mid="<6@example.com>"),  # new
    }})
    r = by_folder(run(server, cfg, {"Tickets": Cursor("work", "Tickets", 7, 5)}))["Tickets"]
    assert not r.backfilled
    assert searches(server)[:2] == ["UID SEARCH UID 6:*", f"UID SEARCH SINCE {imap_date(cfg.sync.overlap_days)}"]
    assert fetched(server)[0] == "4,6"
    assert r.cursor == Cursor("work", "Tickets", 7, 6)


def test_a_lower_uid_that_star_matches_is_not_read_again(cfg):
    server = FakeServer({"Tickets": {5: msg(20)}})
    r = by_folder(run(server, cfg, {"Tickets": Cursor("work", "Tickets", 7, 9)}))["Tickets"]
    assert r.mails == []
    assert fetched(server) == []
    assert r.cursor == Cursor("work", "Tickets", 7, 9)


def test_a_changed_uidvalidity_backfills_again(cfg):
    server = FakeServer({"Tickets": {5: msg(10)}}, uidvalidity=8)
    r = by_folder(run(server, cfg, {"Tickets": Cursor("work", "Tickets", 7, 999)}))["Tickets"]
    assert r.backfilled
    assert searches(server)[0] == f"UID SEARCH SINCE {imap_date(cfg.sync.backfill_days)}"
    assert r.cursor == Cursor("work", "Tickets", 8, 5)


# --- bodies -----------------------------------------------------------------


def test_a_tickets_folder_never_fetches_bodies(cfg):
    server = FakeServer({"Tickets": {1: msg(1)}})
    [m] = by_folder(run(server, cfg))["Tickets"].mails
    assert fetched(server, imap.BODY_ITEMS) == []
    assert (m.role, m.body_text, m.body_note) == ("tickets", None, None)


def test_a_mail_folder_fetches_recent_bodies(cfg):
    server = FakeServer({"INBOX": {1: msg(1, body="Hello there\r\n")}})
    [m] = by_folder(run(server, cfg))["INBOX"].mails
    assert fetched(server, imap.BODY_ITEMS) == ["1"]
    assert (m.role, m.body_text, m.body_note) == ("mail", "Hello there", None)


def test_a_mail_older_than_retention_gets_a_note_instead_of_its_body(cfg):
    server = FakeServer({"INBOX": {1: msg(cfg.sync.retention_days + 1)}})
    [m] = by_folder(run(server, cfg))["INBOX"].mails
    assert fetched(server, imap.BODY_ITEMS) == []
    assert (m.body_text, m.body_note) == (None, f"older than {cfg.sync.retention_days} days")


def test_a_mail_over_max_body_bytes_gets_a_note_instead_of_its_body(cfg):
    entry = msg(1)
    server = FakeServer({"INBOX": {1: entry}})
    [m] = by_folder(run(server, cfg, max_body_bytes=10))["INBOX"].mails
    assert fetched(server, imap.BODY_ITEMS) == []
    assert (m.body_text, m.body_note) == (None, f"too large: {len(entry[1])} bytes")


def test_an_html_only_mail_is_turned_into_text(cfg):
    html = "<html><body><p>Build <b>passed</b>, see <a href='https://example.com'>log</a></p></body></html>"
    server = FakeServer({"INBOX": {1: msg(1, body=html, ctype="text/html")}})
    [m] = by_folder(run(server, cfg))["INBOX"].mails
    assert m.body_text == "Build passed, see log"


def test_a_long_body_is_truncated_at_max_body_chars(cfg):
    server = FakeServer({"INBOX": {1: msg(1, body="Hello there")}})
    [m] = by_folder(run(server, cfg, max_body_chars=5))["INBOX"].mails
    assert m.body_text == "Hello\n[truncated]"


# --- headers ----------------------------------------------------------------


def test_a_mail_without_message_id_gets_a_stable_synthetic_one(cfg):
    entry = msg(1, mid=None)
    [first] = by_folder(run(FakeServer({"Tickets": {1: entry}}), cfg))["Tickets"].mails
    # The same mail under another UID, as after the server renumbered.
    [again] = by_folder(run(FakeServer({"Tickets": {9: entry}}), cfg))["Tickets"].mails
    assert re.fullmatch(r"<[0-9a-f]{32}@no-message-id\.invalid>", first.message_id)
    assert again.message_id == first.message_id
    [other] = by_folder(run(FakeServer({"Tickets": {1: msg(1, "other", mid=None)}}), cfg))["Tickets"].mails
    assert other.message_id != first.message_id


def test_a_mail_with_an_unparsable_date_is_skipped(cfg):
    good = msg(1, mid="<good@example.com>")
    bad = (good[0], raw_mail("hi", "not a date", mid="<bad@example.com>"))
    r = by_folder(run(FakeServer({"Tickets": {1: bad, 2: good}}), cfg))["Tickets"]
    assert [m.message_id for m in r.mails] == ["<good@example.com>"]
    assert len(r.skipped) == 1
    assert r.skipped[0].startswith("work/Tickets UID 1: unparsable Date")
    assert r.cursor == Cursor("work", "Tickets", 7, 2)


def test_raw_8bit_message_id_and_date_headers_do_not_stop_the_sync(cfg):
    good = msg(1, mid="<good@example.com>")
    eight_bit_id = msg(1, subject="Grüße", mid="<grüße@example.com>")
    eight_bit_date = (good[0], raw_mail("hi", "Freitag, 20. Nov 2026 10:00 MEZ ä", mid="<d@example.com>"))
    server = FakeServer({"Tickets": {1: eight_bit_id, 2: eight_bit_date, 3: good}})
    r = by_folder(run(server, cfg))["Tickets"]
    assert [m.message_id for m in r.mails] == ["<grüße@example.com>", "<good@example.com>"]
    assert r.mails[0].subject == "Grüße"
    assert [s.split(":")[0] for s in r.skipped] == ["work/Tickets UID 2"]
    assert r.cursor == Cursor("work", "Tickets", 7, 3)


# --- login and password -----------------------------------------------------


def test_a_rejected_login_raises_and_still_logs_out(cfg):
    server = FakeServer()
    with pytest.raises(SourceError) as e:
        run(server, cfg, password="wrong")
    assert (e.value.component, e.value.login_rejected) == ("IMAP", True)
    assert not any(verb(c) == "EXAMINE" for c in server.commands)


def test_a_non_ascii_password_is_refused_before_login(cfg):
    server = FakeServer()
    with pytest.raises(SourceError) as e:
        run(server, cfg, password="pässword")
    assert (e.value.component, e.value.login_rejected) == ("password", False)
    assert not any(verb(c) == "LOGIN" for c in server.commands)


def fake_run(returncode=0, stdout="", stderr="", raises=None):
    """A stand-in for subprocess.run that records its argv and answers as told."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    run.calls = calls
    return run


def test_read_password_returns_the_first_line():
    run = fake_run(stdout="secret\nsecond line\n")
    assert read_password(("pass", "show", "mail"), run=run) == "secret"
    assert run.calls == [["pass", "show", "mail"]]


@pytest.mark.parametrize("run", [
    fake_run(returncode=1, stderr="no such entry\n"),
    fake_run(stdout=""),
    fake_run(stdout="\nsecret\n"),
    fake_run(raises=FileNotFoundError("pass")),
    fake_run(raises=subprocess.TimeoutExpired("pass", 60)),
], ids=["nonzero-exit", "no-output", "empty-first-line", "oserror", "timeout"])
def test_read_password_failures_are_password_errors(run):
    with pytest.raises(SourceError) as e:
        read_password(("pass", "show", "mail"), run=run)
    assert e.value.component == "password"


@pytest.mark.parametrize("subject, decoded", [
    ("=?utf-8?q?Ticket_=5BT-1=5D?= Grüße", "Ticket [T-1] Grüße"),
    ("=?utf-8?q?Ticket_=5BT-1=5D?= € 5", "Ticket [T-1] € 5"),
    ("=?utf-8?q?Gr=C3=BC=C3=9Fe?= Grüße", "Grüße Grüße"),
    # 8-bit UTF-8 inside an encoded word, which RFC 2047 forbids but mailers send.
    ("=?utf-8?q?Grüße?= x", "Grüße x"),
    ("=?utf-8?q?€_5?= x", "€ 5 x"),
    # A backslash beside an encoded word is text, not an escape.
    (r"=?utf-8?q?Ticket_=5BT-1=5D?= see C:\users\bob", r"Ticket [T-1] see C:\users\bob"),
    (r"=?utf-8?q?Ticket?= path \u0041 Grüße", r"Ticket path \u0041 Grüße"),
], ids=["8bit-latin1-beside", "8bit-above-ff-beside", "8bit-and-encoded", "8bit-inside-word",
        "8bit-above-ff-inside", "ascii-backslash", "8bit-backslash"])
def test_encoded_words_are_decoded_beside_any_text(cfg, subject, decoded):
    server = FakeServer({"Tickets": {1: msg(1, subject=subject)}})
    [m] = by_folder(run(server, cfg))["Tickets"].mails
    assert m.subject == decoded


def test_folder_names_go_out_in_modified_utf7():
    assert imap.quote_mailbox("~peter/mail/台北/日本語") == '"~peter/mail/&U,BTFw-/&ZeVnLIqe-"'  # RFC 3501 5.1.3
    assert imap.quote_mailbox('Éléments envoyés & "co"') == '"&AMk-l&AOk-ments envoy&AOk-s &- \\"co\\""'


def test_a_non_ascii_folder_is_examined_by_its_encoded_name(cfg):
    from readers_digest.config import Folder, Role
    server = FakeServer({"Entw&APw-rfe": {}})
    account = replace(cfg.accounts[0], folders=(Folder("Entwürfe", Role.MAIL),))
    fetch(account, PASSWORD, {}, TODAY, cfg.sync, connect=lambda a, t: FakeIMAP(server))
    assert 'EXAMINE "Entw&APw-rfe"' in server.commands


def test_a_non_ascii_user_is_refused_before_login(cfg):
    server = FakeServer()
    account = replace(cfg.accounts[0], user="jürgen@example.com")
    with pytest.raises(SourceError, match="not ASCII"):
        fetch(account, PASSWORD, {}, TODAY, cfg.sync, connect=lambda a, t: FakeIMAP(server))
    assert not any(verb(c) == "LOGIN" for c in server.commands)


def test_a_folder_that_cannot_be_read_does_not_cost_the_others_their_mail(cfg):
    server = FakeServer({"INBOX": dict([(1, msg(1, "kept"))])})
    del server.mailboxes["Tickets"]
    got = by_folder(run(server, cfg))
    assert "NO folder not found" in got["Tickets"].failure
    assert (got["Tickets"].mails, got["Tickets"].cursor) == ([], None)
    assert got["INBOX"].failure is None and [m.subject for m in got["INBOX"].mails] == ["kept"]
