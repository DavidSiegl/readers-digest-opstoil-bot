"""Digest -> what the desktop window shows: pages of groups of rows.

Plain data and no GTK, so that pytest covers it; window.py only turns these
into widgets. Every string here may come from mail other people wrote, so
the window must show it as text, never as markup.
"""

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import NamedTuple

from readers_digest.digest import Digest, age, clean, mail_group, said, sender_name
from readers_digest.store import Mail, RunStatus, Tag, Ticket
from readers_digest.teams.chat import Unread, link, review_key

TIME_FORMAT = "%d.%m. %H:%M"
# Unread messages a Teams row opens to, the latest; a chat unread for months can have hundreds.
UNREAD_SHOWN = 20


class Badge(NamedTuple):
    """A short label at the end of a row."""

    text: str
    # Drawn in the warning colour: something the reader has to act on.
    warn: bool = False


@dataclass(frozen=True)
class Row:
    """One ticket, mail or Teams conversation in a group. Every string is plain text, never markup."""

    title: str
    subtitle: str = ""
    badges: tuple[Badge, ...] = ()
    # Lines shown when the row is opened; a row without any cannot be opened.
    details: tuple[str, ...] = ()
    # A web page the row's open button shows in the browser; None for no button.
    link: str | None = None
    # The reply the review drafted, which the row's copy button puts on the clipboard.
    draft: str | None = None
    # Names the row across reloads, so the window can keep it open: a ticket id, a Message-ID, a review key.
    key: str = ""
    warn: bool = False


@dataclass(frozen=True)
class Group:
    """A heading and its rows; a group with no rows is shown as "None"."""

    title: str
    rows: tuple[Row, ...]
    # Shown collapsed until opened, but still listed in full: closed tickets, FYI and noise mail.
    folded: bool = False


@dataclass(frozen=True)
class Page:
    """One page of the window's switcher: Tickets, Mail or Teams."""

    # The page's id in the window's stack, stable across reloads.
    name: str
    title: str
    # A symbolic icon name from the GTK icon theme.
    icon: str
    groups: tuple[Group, ...]
    # What the page switcher's badge shows; 0 shows none.
    count: int


@dataclass(frozen=True)
class View:
    """Everything the window shows of one Digest."""

    title: str
    banners: tuple[str, ...]
    pages: tuple[Page, ...]
    footer: str
    # One key per thing that needs the reader, keyed so that news gets a new
    # key. A key the reader has not seen yet lights the tray icon's dot.
    actions: frozenset[str] = frozenset()


def build(d: Digest, tz: tzinfo | None = None) -> View:
    """What the window shows of `d`, times in `tz` (None: the local zone).

    The pages are Tickets and Mail, and Teams when the digest reads Teams.
    """
    def t(dt: datetime) -> str:
        return dt.astimezone(tz).strftime(TIME_FORMAT)

    pages = [_tickets(d, t), _mail(d, t)]
    if d.teams_enabled:
        pages.append(_teams(d, t))
    return View(
        title=f"Synced {t(d.mail_status.last_ok_at)}" if d.mail_status.last_ok_at else "Never synced",
        banners=_banners(d, t),
        pages=tuple(pages),
        footer=_footer(d, t),
        actions=_actions(d),
    )


def summary(v: View) -> str:
    """The tray icon's tooltip: what is open, then any banner."""
    counts = {p.name: p.count for p in v.pages}
    n = counts["tickets"]
    parts = [f"{n} open ticket{'' if n == 1 else 's'}"]
    if counts["mail"]:
        parts.append(f"{counts['mail']} mail for you")
    if counts.get("teams"):
        parts.append(f"{counts['teams']} unread in Teams")
    return "\n".join((" · ".join(parts), *v.banners))


def _actions(d: Digest) -> frozenset[str]:
    """New tickets, each new reminder, mail tagged for the reader, unread Teams messages, and failed reads."""
    out = {f"ticket {x.id}" for x in d.new}
    out.update(f"reminders {x.id} {x.reminders}" for _, xs in d.open_by_state for x in xs if x.reminders)
    out.update(f"mail {m.message_id}" for _, _, ms in d.mail for m in ms if mail_group(d.tags.get(m.message_id)) == 0)
    if d.teams_enabled:
        out.update(f"teams {review_key(u)}" for u in d.teams_unread)
    for name, st in (("mail", d.mail_status), ("teams", d.teams_status)):
        if st.failed and (name == "mail" or d.teams_enabled):
            out.add(f"{name} failed {st.last_error_at.isoformat()}")
    return frozenset(out)


def _banners(d: Digest, t) -> tuple[str, ...]:
    def banner(what: str, st: RunStatus) -> str:
        when = f"last good one {t(st.last_ok_at)}" if st.last_ok_at else "none has succeeded yet"
        return f"{what} failed at {t(st.last_error_at)} ({when}): {clean(st.last_error or '')}"

    out = []
    if d.mail_status.failed:
        out.append(banner("Mail sync", d.mail_status))
    if d.teams_enabled and d.teams_status.failed:
        out.append(banner("Teams read", d.teams_status))
    return tuple(out)


def _tickets(d: Digest, t) -> Page:
    new = {x.id for x in d.new}
    stale = {x.id for x in d.stale}
    groups = [Group(f"New since {t(d.since)}", tuple(_open_row(x, d, new, stale) for x in d.new))]
    groups += [Group(clean(state) if state else "State unknown", tuple(_open_row(x, d, new, stale) for x in xs))
               for state, xs in d.open_by_state]
    groups.append(Group(f"Stale: no mail for {d.settings.stale_days} days or more",
                        tuple(_open_row(x, d, new, stale) for x in d.stale)))
    groups.append(Group(f"Closed since {t(d.since)}", tuple(_closed_row(x, d) for x in d.closed), folded=True))
    n_open = sum(len(xs) for _, xs in d.open_by_state)
    return Page("tickets", "Tickets", "view-list-symbolic", tuple(groups), n_open)


def _title(x: Ticket) -> str:
    return f"{clean(x.id)} · {clean(x.title or '') or '(no title)'}"


def _category(x: Ticket) -> str:
    return clean(x.category) if x.category else "unknown category"


def _open_row(x: Ticket, d: Digest, new: set[str], stale: set[str]) -> Row:
    badges = []
    if x.id in new:
        badges.append(Badge("new"))
    if x.reminders:
        badges.append(Badge(f"{x.reminders} reminder{'s' if x.reminders > 1 else ''}", warn=x.reminders >= 2))
    if x.id in stale:
        badges.append(Badge(f"no mail for {age(d.now - x.last_seen)}"))
    sub = [clean(x.state or "") or "state unknown", _category(x), f"open {age(d.now - x.first_seen)}"]
    return Row(_title(x), " · ".join(sub), tuple(badges), link=d.links.get(x.id), key=x.id, warn=x.reminders >= 2)


def _closed_row(x: Ticket, d: Digest) -> Row:
    return Row(_title(x), f"{clean(x.state or '')} · {_category(x)}", link=d.links.get(x.id), key=x.id)


def _mail(d: Digest, t) -> Page:
    """Mail per folder: for you, then the rest (unreviewed with it), then FYI and noise, folded."""
    groups = []
    for account, folder, mails in d.mail:
        name = f"{clean(account)}/{clean(folder)}"
        split = [tuple(_mail_row(m, d, t) for m in mails if mail_group(d.tags.get(m.message_id)) == g)
                 for g in range(3)]
        if not mails:
            groups.append(Group(name, ()))
            continue
        if split[0]:
            groups.append(Group(f"{name} · for you", split[0]))
        if split[1]:
            groups.append(Group(name, split[1]))
        if split[2]:
            groups.append(Group(f"{name} · FYI / noise", split[2], folded=True))
    for_me = sum(1 for _, _, ms in d.mail for m in ms if mail_group(d.tags.get(m.message_id)) == 0)
    return Page("mail", "Mail", "mail-unread-symbolic", tuple(groups), for_me)


def _tag_badges(tag: Tag | None) -> tuple[Badge, ...]:
    if tag is None:
        return (Badge("not reviewed"),)
    return (Badge(clean(tag.act), warn=tag.act == "me"), Badge(clean(tag.kind)))


def _mail_row(m: Mail, d: Digest, t) -> Row:
    tag = d.tags.get(m.message_id)
    details = []
    if tag and tag.summary:
        details.append(tag.summary)
    if m.body_text:
        details.append(m.body_text.strip())
    elif m.body_note:
        details.append(f"(text not kept: {m.body_note})")
    return Row(
        title=clean(m.subject) or "(no subject)",
        subtitle=f"{t(m.sent_at)} · {sender_name(m.sender)}",
        badges=_tag_badges(tag),
        details=tuple(details),
        draft=tag.draft if tag else None,
        key=m.message_id,
        warn=mail_group(tag) == 0,
    )


def _teams(d: Digest, t) -> Page:
    """What Teams marks unread, by conversation, with the review's tag and draft."""
    rows = tuple(_unread_row(u, d, t) for u in d.teams_unread)
    title = "Unread in Teams" if d.teams_status.last_ok_at else "Teams not read yet"
    return Page("teams", "Teams", "system-users-symbolic", (Group(title, rows),), len(rows))


def _unread_row(u: Unread, d: Digest, t) -> Row:
    c, last = u.conversation, u.messages[-1]
    key = review_key(u)
    tag = d.tags.get(key)
    shown = u.messages[-UNREAD_SHOWN:]
    more = len(u.messages) - len(shown)
    details = ((tag.summary,) if tag and tag.summary else ()) + ((f"… {more} earlier unread",) if more else ()) \
        + tuple(f"{t(m.sent_at)} · {said(m)}" for m in shown)
    return Row(
        title=f"{clean(d.teams_titles.get(c.id, c.title))} · {c.kind}",
        subtitle=f"{t(last.sent_at)} · {said(last)}",
        badges=(Badge(f"{len(u.messages)} unread"),) + (_tag_badges(tag) if tag else ()),
        details=details,
        link=link(c),
        draft=tag.draft if tag else None,
        key=key,
        warn=tag is not None and tag.act == "me",
    )


def _footer(d: Digest, t) -> str:
    parts = [f"Last sync {t(d.mail_status.last_ok_at) if d.mail_status.last_ok_at else 'never'}"]
    if d.teams_enabled and d.teams_status.last_ok_at:
        parts.append(f"Teams {t(d.teams_status.last_ok_at)}")
    parts.append(f"{d.unparsed} unparsed")
    if d.hidden:
        parts.append(f"{d.hidden} quiet ticket{'s' if d.hidden > 1 else ''} not shown")
    return " · ".join(parts)
