"""State -> Markdown digest: open tickets by state, what is new, stale or closed, new mail, unread Teams chats.

Reads only the store and the config: no network, no clock. The caller
passes `now` and `since`, so the same state always renders the same text.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from email.utils import parseaddr

from readers_digest.config import Config, DigestSettings
from readers_digest.store import Mail, RunStatus, Source, Store, Tag, Ticket
from readers_digest.teams.chat import Message, Unread, display_title, link, unread

# The subjects of unparsed mail the footer lists, newest first.
UNPARSED_SHOWN = 5
# What md escapes: enough that no text renders as a link, an image, emphasis,
# code or HTML. Bare URLs still autolink, but show what they lead to.
MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>])")
# A Teams message's text shown on one line, before "…".
SAID_WIDTH = 120


@dataclass(frozen=True)
class Digest:
    """What the digest shows; format_digest renders it, and the window shows the same."""

    now: datetime
    since: datetime
    # Only tickets in DigestSettings.focus (or of unknown category) are in the lists below.
    focus_only: bool
    settings: DigestSettings
    # Open tickets with mail within window_days, grouped by state: the
    # states of DigestSettings.state_order in that order, then the others
    # by name, then None (no mail named one yet). Oldest first in each.
    # States are grouped ignoring case, and named as state_order spells
    # them, or else as the group's oldest ticket does.
    open_by_state: list[tuple[str | None, list[Ticket]]]
    # Open, first seen at or after `since`, oldest first.
    new: list[Ticket]
    # Open, with no mail for stale_days or more; quietest first.
    stale: list[Ticket]
    # Closed at or after `since`, by when.
    closed: list[Ticket]
    # How many open tickets are left out for having no mail within window_days.
    hidden: int
    # The web page of each listed ticket whose rule has a url.
    links: dict[str, str]
    # (account, folder, its unmatched mail sent at or after `since`, newest
    # first), for every `mail` folder in config order, empty ones included.
    mail: list[tuple[str, str, list[Mail]]]
    # The review's tag of each reviewed mail (by Message-ID) and Teams
    # conversation (by teams.chat.review_key).
    tags: dict[str, Tag]
    mail_status: RunStatus
    # Teams is read at all: its section is shown only then.
    teams_enabled: bool
    teams_status: RunStatus
    # What Teams marks unread, by conversation, most recent first. Teams'
    # own read marker decides, not `since`.
    teams_unread: list[Unread]
    # A name for each conversation of teams_unread: its title, or who is in it.
    teams_titles: dict[str, str]
    # Mail of tickets folders no rule matched: how many, and the newest subjects.
    unparsed: int
    unparsed_subjects: list[str]


def collect(store: Store, cfg: Config, since: datetime, now: datetime, focus_only: bool = True) -> Digest:
    """The digest of what `store` holds, as of `now`, with "new" meaning at or after `since`.

    With focus_only, a ticket whose category DigestSettings.focus does not
    find is left out of every list; one with no category yet stays in.
    """
    s = cfg.digest
    tickets = [t for t in store.tickets() if not focus_only or in_focus(t, s)]
    window_start = now - timedelta(days=s.window_days)
    all_open = [t for t in tickets if not s.is_closed(t.state)]
    open_ = [t for t in all_open if t.last_seen >= window_start]
    stale_before = now - timedelta(days=s.stale_days)
    closed = sorted((t for t in tickets if s.is_closed(t.state) and closed_at(t) >= since),
                    key=lambda t: (closed_at(t), t.id))
    mails = store.mail(since)
    convs, msgs = store.teams() if cfg.teams.enabled else ([], [])
    by_conv: dict[str, list[Message]] = {}
    for m in msgs:
        by_conv.setdefault(m.conversation_id, []).append(m)
    teams_unread = unread(convs, msgs)
    unparsed, subjects = store.unparsed(UNPARSED_SHOWN)
    return Digest(
        now=now,
        since=since,
        focus_only=focus_only,
        settings=s,
        open_by_state=_by_state(open_, s.state_order),
        new=[t for t in open_ if t.first_seen >= since],
        stale=sorted((t for t in open_ if t.last_seen <= stale_before), key=lambda t: (t.last_seen, t.id)),
        closed=closed,
        hidden=len(all_open) - len(open_),
        links={t.id: url for t in open_ + closed if (url := cfg.rules.url(t.rule, t.id))},
        mail=[(a, f, [m for m in mails if (m.account, m.folder) == (a, f)]) for a, f in cfg.mailboxes()],
        tags=store.tags(),
        mail_status=store.status(Source.MAIL),
        teams_enabled=cfg.teams.enabled,
        teams_status=store.status(Source.TEAMS),
        teams_unread=teams_unread,
        teams_titles={u.conversation.id: display_title(u.conversation, by_conv.get(u.conversation.id, []))
                      for u in teams_unread},
        unparsed=unparsed,
        unparsed_subjects=subjects,
    )


def in_focus(t: Ticket, s: DigestSettings) -> bool:
    """`t` is one the reader works on: no focus is set, its category is unknown, or the focus finds it."""
    return s.focus is None or t.category is None or s.focus.search(t.category) is not None


def closed_at(t: Ticket) -> datetime:
    """When a closed ticket closed: the first mail that gave it its closed state."""
    return t.state_at or t.last_seen


def mail_group(tag: Tag | None) -> int:
    """Where a mail is listed: 0 for the reader to act on; 1 the rest, unreviewed included; 2 FYI and noise.

    Group 2 is listed like the others, never only counted: the tag comes from
    a model reading text other people wrote, so a mail could ask to be tagged
    noise, and must not vanish from the digest by doing so.
    """
    if tag is None:
        return 1
    if tag.act == "me":
        return 0
    return 2 if tag.act == "nobody" and tag.kind in ("fyi", "noise") else 1


def format_digest(d: Digest, tz: tzinfo | None = None) -> str:
    """`d` as Markdown, times in `tz` (None: the local zone)."""
    def t(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    out = [f"# {'Tickets in focus' if d.focus_only else 'All tickets'}, {t(d.now)}", ""]
    out += _banner("Last mail sync", "The data below is from the last successful sync", d.mail_status, t)
    if d.teams_enabled:
        out += _banner("Last Teams read", "Chats are from the last successful read", d.teams_status, t)

    n_open = sum(len(ts) for _, ts in d.open_by_state)
    out += [f"## Open ({n_open})", ""]
    if not d.open_by_state:
        out += ["None.", ""]
    for state, ts in d.open_by_state:
        out += [f"### {md(state) if state else 'State unknown'} ({len(ts)})", ""]
        out += [_open_line(x, d) for x in ts]
        out.append("")

    _section(out, f"New since {t(d.since)}", [_short_line(x, d) for x in d.new])
    _section(out, f"Stale: no mail for {d.settings.stale_days} days or more",
             [f"{_short_line(x, d)} · last mail {age(d.now - x.last_seen)} ago" for x in d.stale])
    _section(out, f"Closed since {t(d.since)}", [_short_line(x, d) for x in d.closed])

    for account, folder, mails in d.mail:
        groups = [[m for m in mails if mail_group(d.tags.get(m.message_id)) == g] for g in range(3)]
        lines = [_mail_line(m, d, t) for m in groups[0] + groups[1]]
        if groups[2]:
            lines += [""] if lines else []
            lines += [f"FYI / noise ({len(groups[2])}):", ""]
            lines += [_mail_line(m, d, t) for m in groups[2]]
        _section(out, f"New mail in {md(account)}/{md(folder)} since {t(d.since)}", lines, len(mails))

    # Only once Teams has been read: "None" would be a claim before then.
    if d.teams_enabled and d.teams_status.last_ok_at:
        _section(out, "Unread in Teams", [_unread_line(u, d, t) for u in d.teams_unread])

    last_ok = t(d.mail_status.last_ok_at) if d.mail_status.last_ok_at else "never"
    footer = f"Last successful sync: {last_ok} · Unparsed subjects: {d.unparsed}"
    if d.teams_enabled and d.teams_status.last_ok_at:
        footer += f" · Last Teams read: {t(d.teams_status.last_ok_at)}"
    out += ["---", "", footer]
    if d.hidden:
        out.append(f"Not shown: {d.hidden} open ticket{'s' if d.hidden != 1 else ''} with no mail in the last "
                   f"{d.settings.window_days} days")
    if d.unparsed_subjects:
        out += ["", "Newest unparsed subjects (a rule may be missing; try `readers-digest match`):", ""]
        out += [f"- {md(s) or '(no subject)'}" for s in d.unparsed_subjects]
    out.append("")
    return "\n".join(out)


def previous_working_day(now: datetime, tz: tzinfo | None = None) -> datetime:
    """Midnight starting the working day before `now`'s, in `tz` (None: the local zone): the default `since`.

    Covers yesterday and today, and on a Monday reaches back to Friday.
    Midnight takes the offset it has on that day, which differs from now's
    across a change to or from summer time.
    """
    d = now.astimezone(tz).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    midnight = datetime.combine(d, time(0))
    return midnight.replace(tzinfo=tz) if tz is not None else midnight.astimezone()


def _banner(what: str, data_from: str, st: RunStatus, t: Callable[[datetime], str]) -> list[str]:
    if not st.failed:
        return []
    return [f"> **{what} failed** at {t(st.last_error_at)}: {md(st.last_error or '')}",
            f"> {data_from}, {t(st.last_ok_at)}." if st.last_ok_at else "> None has succeeded yet.", ""]


def _by_state(tickets: list[Ticket], order: tuple[str, ...]) -> list[tuple[str | None, list[Ticket]]]:
    # States that differ only in case are one group: rules match subjects
    # case-insensitively, and two ticket systems may spell a state apart.
    named = {o.casefold(): o for o in order}
    groups: dict[str | None, list[Ticket]] = {}
    names: dict[str | None, str | None] = {}
    for x in sorted(tickets, key=lambda x: (x.first_seen, x.id)):
        key = x.state.casefold() if x.state is not None else None
        groups.setdefault(key, []).append(x)
        names.setdefault(key, named.get(key, x.state))
    folded = list(named)

    def rank(key: str | None) -> tuple:
        if key is None:
            return (2, 0, "")
        if key in named:
            return (0, folded.index(key), "")
        return (1, 0, key)

    return [(names[k], groups[k]) for k in sorted(groups, key=rank)]


def _ticket(x: Ticket, d: Digest) -> str:
    # Angle brackets: the URL may hold characters that end a plain link.
    return f"[{md(x.id)}](<{d.links[x.id]}>)" if x.id in d.links else md(x.id)


def _open_line(x: Ticket, d: Digest) -> str:
    parts = [_ticket(x, d), md(x.title or "") or "(no title)", _category(x), f"open {age(d.now - x.first_seen)}"]
    if x.reminders:
        parts.append(f"{'⚠ ' if x.reminders >= 2 else ''}{x.reminders} reminder{'s' if x.reminders > 1 else ''}")
    return "- " + " · ".join(parts)


def _short_line(x: Ticket, d: Digest) -> str:
    return f"- {_ticket(x, d)} · {md(x.title or '') or '(no title)'} · {_category(x)} · " \
           f"{md(x.state or '') or 'state unknown'}"


def _mail_line(m: Mail, d: Digest, t: Callable[[datetime], str]) -> str:
    tag = d.tags.get(m.message_id)
    line = f"- {t(m.sent_at)} · {_tag_text(tag)}{md(sender_name(m.sender))} · {md(m.subject) or '(no subject)'}"
    return line + (f"\n  - {md(tag.summary)}" if tag and tag.summary else "")


def _tag_text(tag: Tag | None) -> str:
    return f"[{md(tag.act)} · {md(tag.kind)}] · " if tag else ""


def _unread_line(u: Unread, d: Digest, t: Callable[[datetime], str]) -> str:
    c, last = u.conversation, u.messages[-1]
    title = md(d.teams_titles.get(c.id, c.title))
    return (f"- [{title}](<{link(c)}>) ({c.kind}) · {len(u.messages)} unread · {t(last.sent_at)} · "
            f"{md(said(last))}")


def said(m: Message) -> str:
    """`m` on one line: who, and what they wrote, cut at SAID_WIDTH."""
    text = clean(m.text) or "(text expired)"
    if len(text) > SAID_WIDTH:
        text = text[:SAID_WIDTH] + "…"
    return f"{clean(m.sender_name) or 'someone'}: {text}"


def sender_name(sender: str) -> str:
    """The display name of a From: header, or its address when it has none, on one line."""
    name, addr = parseaddr(sender)
    return clean(name or addr or sender) or "(no sender)"


def _section(out: list[str], heading: str, lines: list[str], count: int | None = None) -> None:
    out += [f"## {heading} ({len(lines) if count is None else count})", ""]
    out += lines or ["None."]
    out.append("")


def _category(x: Ticket) -> str:
    return md(x.category) if x.category else "unknown category"


def age(delta: timedelta) -> str:
    """`delta` as a short age: whole hours below a day ("5h"), else whole days ("3d")."""
    hours = int(delta.total_seconds() // 3600)
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


def clean(s: str) -> str:
    """`s` on one line, runs of whitespace collapsed.

    Subjects, names and messages are written by other people: nothing in
    one may break the one-item-per-line layout.
    """
    return " ".join(s.split())


def md(s: str) -> str:
    """`s` cleaned (see clean), with the characters that start a Markdown link, emphasis, code or HTML escaped.

    For text other people wrote on its way into a Markdown file: none of it
    may render as a link or markup that reads as the tool's own.
    """
    return MARKDOWN_SPECIAL.sub(r"\\\1", clean(s))
