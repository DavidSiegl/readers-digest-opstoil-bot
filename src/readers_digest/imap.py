"""Read new mail from an IMAP account, read-only.

The mailbox is the reader's own, and this tool only looks: it sends
CAPABILITY, STARTTLS, LOGIN, EXAMINE (never SELECT), UID SEARCH, UID FETCH
of RFC822.SIZE and BODY.PEEK (never BODY, which sets \\Seen), and LOGOUT.
Nothing it does changes a flag, moves a mail or shows in another client.
"""

import email
import email.policy
import hashlib
import imaplib
import re
import ssl
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from email.errors import HeaderParseError, MessageError
from email.header import Header, decode_header
from email.message import Message
from email.utils import parsedate_to_datetime

from readers_digest.config import Account, Folder, Role, Security, SyncSettings
from readers_digest.store import Cursor, Mail
from readers_digest.text import html_to_text

HEADER_ITEMS = "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
BODY_ITEMS = "(BODY.PEEK[])"
FETCH_CHUNK = 200
BODY_CHUNK = 20

# IMAP dates use English month names whatever the locale, so %b won't do.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID = re.compile(rb"\bUID (\d+)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")


class SourceError(Exception):
    """A failure to read mail, attributed to `component`: "password" (the password command) or "IMAP"."""

    def __init__(self, component: str, message: str, login_rejected: bool = False):
        super().__init__(f"{component}: {message}")
        self.component = component
        # The server refused the password. Retrying with the same one only
        # counts towards a lockout, so nothing in this tool retries it.
        self.login_rejected = login_rejected


@dataclass(frozen=True)
class FolderResult:
    """What one sync read from one folder."""

    account: str
    folder: str
    # New and overlapping mail, oldest UID first; the store drops what it has already.
    mails: list[Mail]
    # Where the folder's next sync starts; None when the folder had nothing to move it to.
    cursor: Cursor | None
    # The folder was read from backfill_days back: its first sync, or its UIDVALIDITY changed.
    backfilled: bool
    # One line per mail that could not be read and was left out.
    skipped: list[str] = field(default_factory=list)


def read_password(command: tuple[str, ...], run: Callable = subprocess.run) -> str:
    """The first line of what `command` prints, never empty; raises SourceError("password", …).

    `command` runs without a shell. The password then travels only into
    LOGIN: never into argv, the environment, a log or the state db.
    """
    try:
        p = run(list(command), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SourceError("password", f"cannot run {command[0]}: {e}") from e
    if p.returncode != 0:
        detail = (p.stderr or "").strip().splitlines()
        raise SourceError("password", f"{command[0]} exited {p.returncode}: {detail[0] if detail else ''}".rstrip(": "))
    lines = p.stdout.splitlines()
    if not lines or not lines[0]:
        raise SourceError("password", f"{command[0]} printed nothing on its first line")
    return lines[0]


def fetch(
    account: Account,
    password: str,
    cursors: Mapping[str, Cursor | None],
    today: date,
    settings: SyncSettings,
    *,
    connect: Callable[[Account, float], imaplib.IMAP4] | None = None,
) -> list[FolderResult]:
    """Read the mail of every folder of `account` that is new since its cursor, in one session.

    `cursors` maps a folder name to where its last sync stopped; a folder
    without a usable one reads the last backfill_days. Every sync also
    re-reads the last overlap_days. The text of a `mail` folder's mail is
    fetched only within retention_days and under max_body_bytes; otherwise
    its body_note says why not. Raises SourceError as session() does, and
    "IMAP" for a failure while reading. `connect` stands in for opening the
    connection in tests.
    """
    with session(account, password, settings.timeout_s, connect=connect) as m:
        try:
            return [_fetch_folder(m, account, f, cursors.get(f.name), today, settings) for f in account.folders]
        except (OSError, imaplib.IMAP4.error) as e:
            raise SourceError("IMAP", f"reading {account.name}: {e}") from e


@contextmanager
def session(
    account: Account,
    password: str,
    timeout: float,
    *,
    connect: Callable[[Account, float], imaplib.IMAP4] | None = None,
) -> Iterator[imaplib.IMAP4]:
    """A connection to `account`'s server, logged in, and logged out when the block ends, however it ends.

    TLS is as account.security says, with the system's CA certificates and
    host name checks. Entering raises SourceError: "IMAP" when there is no
    connection or no TLS, or for a rejected LOGIN (then with login_rejected
    set, and never retried); "password" for a password LOGIN cannot send.
    An error the block raises passes through; a failed LOGOUT is ignored.
    """
    try:
        m = (connect or _connect)(account, timeout)
    except (OSError, imaplib.IMAP4.error) as e:
        raise SourceError("IMAP", f"cannot connect to {account.host}:{account.port}: {e}") from e
    try:
        _login(m, account, password)
        yield m
    finally:
        # The work is done or has failed already: a lost LOGOUT changes neither.
        with suppress(OSError, imaplib.IMAP4.error):
            m.logout()


def _connect(a: Account, timeout: float) -> imaplib.IMAP4:
    """A connection to `a`'s server with TLS as `a` asks, not logged in; raises OSError or IMAP4.error."""
    if a.security is Security.SSL:
        return imaplib.IMAP4_SSL(a.host, a.port, ssl_context=ssl.create_default_context(), timeout=timeout)
    m = imaplib.IMAP4(a.host, a.port, timeout=timeout)
    if a.security is Security.STARTTLS:
        try:
            m.starttls(ssl.create_default_context())
        except BaseException:
            m.shutdown()
            raise
    return m


def _login(m: imaplib.IMAP4, a: Account, password: str) -> None:
    """session's LOGIN; raises SourceError as session says."""
    # imaplib sends LOGIN arguments as ASCII. Checked here because its
    # UnicodeEncodeError would quote the offending character of the password.
    if not password.isascii():
        raise SourceError("password", f"{a.name}: the password is not ASCII, which IMAP LOGIN cannot send")
    try:
        m.login(a.user, password)
    except (OSError, imaplib.IMAP4.error) as e:
        raise SourceError("IMAP", f"LOGIN as {a.user} at {a.host} rejected, not retried: {e}",
                          login_rejected=True) from e


def _fetch_folder(m: imaplib.IMAP4, a: Account, f: Folder, cursor: Cursor | None, today: date,
                  s: SyncSettings) -> FolderResult:
    typ, data = m.select(quote_mailbox(f.name), readonly=True)  # readonly sends EXAMINE
    require_ok(typ, data, f"EXAMINE {f.name}")
    _, v = m.response("UIDVALIDITY")
    try:
        uidvalidity = int(v[0])
    except (TypeError, ValueError, IndexError) as e:
        raise SourceError("IMAP", f"EXAMINE {f.name}: no usable UIDVALIDITY in {v!r}") from e

    backfilled = cursor is None or cursor.uidvalidity != uidvalidity
    if backfilled:
        uids = _search(m, f, "SINCE", _imap_date(today - timedelta(days=s.backfill_days)))
    else:
        # "n:*" always matches the highest UID, even one below n, so filter.
        newer = {u for u in _search(m, f, "UID", f"{cursor.last_uid + 1}:*") if u > cursor.last_uid}
        uids = newer | _search(m, f, "SINCE", _imap_date(today - timedelta(days=s.overlap_days)))

    # Headers of every mail, and for a mail folder the list of which bodies to fetch.
    keep_text_from = datetime.combine(today - timedelta(days=s.retention_days), time(0), UTC)
    mails: dict[int, Mail] = {}
    skipped = []
    ordered = sorted(uids)
    for i in range(0, len(ordered), FETCH_CHUNK):
        typ, data = m.uid("FETCH", ",".join(map(str, ordered[i : i + FETCH_CHUNK])), HEADER_ITEMS)
        require_ok(typ, data, f"UID FETCH in {f.name}")
        for uid, raw, meta in _fetched_literals(data):
            mail, why = _headers(raw, a.name, f)
            if mail is None:
                skipped.append(f"{a.name}/{f.name} UID {uid}: {why}")
                continue
            if f.role is Role.MAIL:
                size = _SIZE.search(meta)
                mail = replace(mail, body_note=_why_no_body(mail, int(size[1]) if size else None, keep_text_from, s))
            mails[uid] = mail

    # Bodies, only of mail folders: a ticket notification's text adds nothing its subject does not say.
    want = sorted(uid for uid, mail in mails.items() if f.role is Role.MAIL and mail.body_note is None)
    for i in range(0, len(want), BODY_CHUNK):
        typ, data = m.uid("FETCH", ",".join(map(str, want[i : i + BODY_CHUNK])), BODY_ITEMS)
        require_ok(typ, data, f"UID FETCH body in {f.name}")
        for uid, raw, _ in _fetched_literals(data):
            if uid in mails:
                text, note = _body(raw, s.max_body_chars)
                mails[uid] = replace(mails[uid], body_text=text, body_note=note)
    for uid in want:
        if mails[uid].body_text is None and mails[uid].body_note is None:
            mails[uid] = replace(mails[uid], body_note="body missing from FETCH reply")

    last = max(uids, default=None)
    if not backfilled:
        last = max(cursor.last_uid, last or 0)
    new_cursor = None if last is None else Cursor(a.name, f.name, uidvalidity, last)
    return FolderResult(a.name, f.name, [mails[u] for u in sorted(mails)], new_cursor, backfilled, skipped)


def _why_no_body(mail: Mail, size: int | None, keep_text_from: datetime, s: SyncSettings) -> str | None:
    """Why the text of `mail`, of `size` bytes, is not fetched; None when it is."""
    if mail.sent_at < keep_text_from:
        return f"older than {s.retention_days} days"
    if size is None:
        return "size unknown"
    if size > s.max_body_bytes:
        return f"too large: {size} bytes"
    return None


def _search(m: imaplib.IMAP4, f: Folder, *criteria: str) -> set[int]:
    typ, data = m.uid("SEARCH", *criteria)
    require_ok(typ, data, f"UID SEARCH {' '.join(criteria)} in {f.name}")
    try:
        return {int(x) for x in b" ".join(d for d in data if d).split()}
    except ValueError as e:
        raise SourceError("IMAP", f"UID SEARCH in {f.name}: unexpected reply {data!r}") from e


def _fetched_literals(data: list) -> list[tuple[int, bytes, bytes]]:
    """Pull (uid, literal, attributes) out of imaplib's FETCH reply.

    A server may put UID and RFC822.SIZE before or after the literal, so the
    attributes are the text on both sides of it.
    """
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, tuple):
            continue
        meta = item[0]
        if i + 1 < len(data) and isinstance(data[i + 1], bytes):
            meta += b" " + data[i + 1]
        m = _UID.search(meta)
        if not m:
            raise SourceError("IMAP", f"UID FETCH: reply without UID: {item[0]!r}")
        out.append((int(m[1]), item[1], meta))
    return out


def _headers(raw: bytes, account: str, f: Folder) -> tuple[Mail | None, str]:
    """(the mail its headers describe, without text; or None and why it cannot be read)."""
    # compat32 hands back a header with raw 8-bit bytes in it as a Header,
    # not a str, so every header goes through _decode.
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    subject = _decode(msg.get("Subject", ""))
    sender = _decode(msg.get("From", ""))
    date = _decode(msg.get("Date", ""))
    try:
        sent_at = parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return None, f"unparsable Date {date!r}"
    if sent_at.tzinfo is None:
        # "-0000" means the zone is unknown; UTC is the least wrong reading.
        sent_at = sent_at.replace(tzinfo=UTC)
    message_id = _decode(msg.get("Message-ID", "")).strip()
    if not message_id:
        # Stable across runs and across UID renumbering, so dedup still works.
        h = hashlib.sha256(f"{sent_at.isoformat()}\0{msg.get('From', '')}\0{subject}".encode()).hexdigest()
        message_id = f"<{h[:32]}@no-message-id.invalid>"
    return Mail(message_id, account, f.name, f.role.value, sent_at, sender, subject), ""


def _body(raw: bytes, max_chars: int) -> tuple[str | None, str | None]:
    """(text, or None and why not) of a whole message: its text/plain part, or else its HTML part as text.

    Attachments are never kept.
    """
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return None, "no text part"
        text = _part_text(part)
    # Besides the expected errors, the default policy's header parser has
    # raised IndexError and AttributeError on malformed headers. One odd mail
    # must not stop the sync, so it is kept with a note instead.
    except (LookupError, UnicodeError, ValueError, IndexError, AttributeError, MessageError) as e:
        return None, f"unreadable body: {type(e).__name__}: {e}"
    # Mail on the wire uses CRLF, and get_content() keeps it.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if part.get_content_subtype() == "html":
        text = html_to_text(text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated]"
    return text, None


def _part_text(part: Message) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError):
        # An unknown or wrong charset: keep the text with replacement marks
        # rather than lose the mail.
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _decode(header: str | Header) -> str:
    """A header's text: RFC 2047 words decoded, raw 8-bit bytes read as UTF-8, folding removed."""
    if isinstance(header, Header):
        # Raw 8-bit bytes, which RFC 5322 forbids but mailers send; their
        # charset is unknown-8bit, and in practice UTF-8. Made a str first,
        # so that encoded words beside them are decoded below too.
        header = "".join(_text(b, cs) for b, cs in decode_header(header))
    unfolded = re.sub(r"\r?\n", "", str(header))
    try:
        parts = decode_header(unfolded)
    except HeaderParseError:
        return unfolded  # kept undecoded: no rule will match it, so it shows as unparsed or as mail
    # decode_header gives the text between encoded words as raw-unicode-escape bytes, with no charset.
    return "".join(_text(b, cs or "raw-unicode-escape") for b, cs in parts)


def _text(b: bytes | str, charset: str | None) -> str:
    if isinstance(b, str):
        return b
    try:
        return b.decode(charset if charset and charset != "unknown-8bit" else "utf-8", errors="replace")
    except LookupError:
        return b.decode("utf-8", errors="replace")


def require_ok(typ: str, data: list, what: str) -> None:
    """Raise SourceError("IMAP", …) naming `what` unless an imaplib command's (typ, data) answer is OK."""
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        raise SourceError("IMAP", f"{what}: {typ} {detail}".rstrip())


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def quote_mailbox(mailbox: str) -> str:
    """`mailbox` as an IMAP quoted string, for EXAMINE."""
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'
