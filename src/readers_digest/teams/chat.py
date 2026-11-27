"""Teams chats as the tool keeps them, and which of them are unread. Pure: no browser, no store.

teams.web fetches the Teams web client's own JSON; this turns it into rows.
The shapes are the client's, as it sends them to itself:

- csa `api/v3/teams/users/me`: `chats`, each with `isOneOnOne`, `threadType`
  (chat or meeting), `title`, `members`, `lastMessage`, `isRead` and the read
  position `consumptionHorizon`; and `teams` with their channels, which are
  not read: only 1:1, group and meeting chats are.
- chatsvc `users/ME/conversations/<id>/messages`: `messages`, newest first,
  each with `messagetype`, `content` (HTML for RichText/Html), `from` (a URL
  ending in the sender's MRI, `8:orgid:<object id>`), `imdisplayname`,
  `composetime`, `originalarrivaltime` and `properties.mentions` (a JSON string).

These are not a published API: Microsoft can change them with any client
release, and the `teams-probe` command records their current shape.

What is unread is Teams' own say, as the reader's Teams client left it: the
tool never marks anything read (teams.web blocks every write).
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from readers_digest.text import html_to_text

# Only these carry what people wrote; the rest are calls, joins, renames.
TEXT_TYPES = ("Text", "RichText/Html")
# A meeting chat counts only while its last message is this recent: Teams
# stops listing old ones, which can stay unread for good.
MEETING_DAYS = 30
# Deep links into the Teams client, opened in the default browser.
LINK_PREFIX = "https://teams.microsoft.com/l/"


@dataclass(frozen=True)
class Conversation:
    id: str
    # "1:1", "group" or "meeting".
    kind: str
    # The chat's title; a 1:1 chat often has none (display_title).
    title: str
    last_message_at: datetime | None
    # The Microsoft tenant it belongs to, for links; None if the list named none.
    tenant: str | None = None
    # Teams' own flag: something in it is unread.
    unread: bool = False
    # The read position: what arrived after it is unread. None when Teams gives none, and then all of it is.
    read_at: datetime | None = None


@dataclass(frozen=True)
class Message:
    conversation_id: str
    id: str
    # When it arrived, as the read position counts.
    sent_at: datetime
    sender_id: str
    sender_name: str
    text: str
    # Written by the reader.
    mine: bool
    mentions_me: bool


@dataclass(frozen=True)
class Unread:
    """A conversation Teams marks unread, with what arrived after the reader's read position."""

    conversation: Conversation
    # Oldest first; never the reader's own.
    messages: tuple[Message, ...]


def my_mri(object_id: str) -> str:
    """The reader's id as Teams writes it in chats, from their Entra object id."""
    return f"8:orgid:{object_id}"


def conversations(me: dict, mri: str, now: datetime) -> list[Conversation]:
    """The chats in the client's chat list (`me`) that the reader, whose id is `mri`, is in and wants, as of `now`.

    Left out: deleted chats, chats the reader has left (no member entry of
    theirs), muted ones, and meeting chats quiet for MEETING_DAYS.
    """
    out = []
    for c in me.get("chats") or []:
        if c.get("isConversationDeleted") or c.get("isMuted") is True:
            continue
        # A chat the reader left stays listed, with no member entry of theirs.
        if not any(m.get("mri") == mri for m in c.get("members") or [] if isinstance(m, dict)):
            continue
        kind = "1:1" if c.get("isOneOnOne") else "meeting" if c.get("threadType") == "meeting" else "group"
        last = _time((c.get("lastMessage") or {}).get("composeTime"))
        if kind == "meeting" and (last is None or last < now - timedelta(days=MEETING_DAYS)):
            continue
        out.append(Conversation(c["id"], kind, (c.get("title") or "").strip(), last, c.get("tenantId") or None,
                                c.get("isRead") is False, _horizon(c)))
    return out


def messages(conversation_id: str, data: dict, me: str) -> list[Message]:
    """The text messages in one page of a conversation's messages (`data`), oldest first; `me` is the reader's MRI."""
    out = []
    for m in data.get("messages") or []:
        props = m.get("properties") or {}
        if m.get("messagetype") not in TEXT_TYPES or props.get("deletetime"):
            continue
        content = m.get("content") or ""
        text = (html_to_text(content) if m.get("messagetype") == "RichText/Html" else content).strip()
        # Arrival, as the read position counts it; composetime can be earlier.
        sent = _time(m.get("originalarrivaltime") or m.get("composetime"))
        if not text or sent is None or not m.get("id"):
            continue
        sender = (m.get("from") or "").rsplit("/", 1)[-1]
        out.append(Message(conversation_id, str(m["id"]), sent, sender, (m.get("imdisplayname") or "").strip(),
                           text, sender == me, me in _mentioned(props.get("mentions"))))
    return sorted(out, key=lambda m: (m.sent_at, m.id))


def unread(convs: list[Conversation], msgs: list[Message]) -> list[Unread]:
    """The unread conversations of `convs` that have unread text messages in `msgs`, the most recent first."""
    by_conv: dict[str, list[Message]] = {}
    for m in msgs:
        by_conv.setdefault(m.conversation_id, []).append(m)
    out = []
    for c in convs:
        if not c.unread:
            continue
        news = (m for m in by_conv.get(c.id, []) if not m.mine and (c.read_at is None or m.sent_at > c.read_at))
        ms = tuple(sorted(news, key=lambda m: (m.sent_at, m.id)))
        if ms:
            out.append(Unread(c, ms))
    return sorted(out, key=lambda u: u.messages[-1].sent_at, reverse=True)


def to_me(c: Conversation, m: Message) -> bool:
    """Written to the reader: in a 1:1 chat, or mentioning them, and not by them."""
    return not m.mine and (c.kind == "1:1" or m.mentions_me)


def review_key(u: Unread) -> str:
    """The key review marks an unread conversation reviewed by, and stores its tag under.

    It names the latest unread message, so a newer one makes a new key and
    the conversation is reviewed again. store._REVIEW_KEYS spells it the same.
    """
    return f"teams:{u.conversation.id}:{u.messages[-1].id}"


def link(c: Conversation) -> str:
    """Where Teams opens the chat, in the form Teams' own notification mails use."""
    url = f"{LINK_PREFIX}chat/{quote(c.id, safe='')}/conversations"
    return url + (f"?tenantId={quote(c.tenant, safe='')}" if c.tenant else "")


def display_title(c: Conversation, msgs: list[Message] | tuple[Message, ...]) -> str:
    """The chat's title, or for an untitled one the other people in it, by their latest messages."""
    if c.title:
        return c.title
    names: list[str] = []
    for m in sorted(msgs, key=lambda m: m.sent_at, reverse=True):
        if not m.mine and m.sender_name and m.sender_name not in names:
            names.append(m.sender_name)
    return ", ".join(names[:3]) or ("1:1 chat" if c.kind == "1:1" else "untitled chat")


def _horizon(c: dict) -> datetime | None:
    # {"originalArrivalTime": <ms>, "timeStamp": <ms>, "clientMessageId": …}
    ms = (c.get("consumptionHorizon") or {}).get("originalArrivalTime")
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC) if ms else None
    except (TypeError, ValueError, OverflowError):
        return None


def _mentioned(raw) -> set[str]:
    # A JSON string of [{"mri": …, "mentionType": "person", …}]; "[]" without.
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw or []
    except ValueError:
        return set()
    return {i["mri"] for i in items if isinstance(i, dict) and isinstance(i.get("mri"), str)}


def _time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # "2026-10-05T09:12:00.1234567Z": fromisoformat takes the Z and cuts the seventh decimal.
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)
