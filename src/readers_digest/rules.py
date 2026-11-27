"""Subject rules: which mail is about a ticket, which ticket, and what it says about it. Pure, no I/O.

Ticket systems write their notification subjects from a fixed template, so
a regular expression per template reads them exactly, and no language
model is needed. A rule's pattern names its parts with groups:

    (?P<ticket>…)    required: the ticket's id, the key everything is folded by
    (?P<title>…)     the ticket's title
    (?P<state>…)     its state after this mail ("Open", "Resolved", …)
    (?P<category>…)  its queue, component or type: what the digest's focus filters on

A rule gives a fixed `state` or `category` for a template that names
neither. Rules are tried in the order the config lists them; the first that
matches wins.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from email.utils import parseaddr
from enum import StrEnum
from urllib.parse import quote


class Kind(StrEnum):
    """What a stored mail is to the digest. The store keeps it per mail and folds tickets from it."""

    # A ticket's notification: activity, and its state if the rule reads one.
    UPDATE = "update"
    # A nag about a ticket ("2nd reminder: …"): counted, never changes the state.
    REMINDER = "reminder"
    # Mail in a `mail` folder that no rule matched: listed as new mail.
    MAIL = "mail"
    # Mail in a `tickets` folder that no rule matched: counted in the digest's
    # footer, so a template the rules do not know yet shows up instead of vanishing.
    UNPARSED = "unparsed"


@dataclass(frozen=True)
class Rule:
    """One notification template."""

    name: str
    subject: re.Pattern[str]
    # Matched against the sender's address too when set; None matches any sender.
    sender: re.Pattern[str] | None = None
    # UPDATE or REMINDER.
    kind: Kind = Kind.UPDATE
    # Used when `subject` has no group of that name, or the group did not take part in the match.
    state: str | None = None
    category: str | None = None
    # The ticket's web page, with {ticket} for its id; None for no link. Not
    # part of what a rule makes of a mail, so changing it needs no re-match.
    url: str | None = None


@dataclass(frozen=True)
class Match:
    """What a rule read from one subject."""

    rule: str
    kind: Kind
    ticket: str
    title: str | None
    state: str | None
    category: str | None


class Rules:
    """The configured rules, in order."""

    def __init__(self, rules: tuple[Rule, ...] | list[Rule]):
        """Check `rules`, tried in this order by match().

        Raises ValueError for a rule whose subject pattern has no `ticket`
        group, or whose kind is neither UPDATE nor REMINDER.
        """
        for r in rules:
            if "ticket" not in r.subject.groupindex:
                raise ValueError(f"rule {r.name!r}: subject pattern has no (?P<ticket>…) group")
            if r.kind not in (Kind.UPDATE, Kind.REMINDER):
                raise ValueError(f"rule {r.name!r}: kind must be update or reminder, not {r.kind}")
        self.rules = tuple(rules)

    def match(self, subject: str, sender: str = "") -> Match | None:
        """What the first rule matching `subject` and `sender` reads from it; None if none does.

        `sender` is a From: header as the mail has it ("Name <addr>") or a
        bare address; a rule's sender pattern sees only its address, lower-case.
        """
        address = parseaddr(sender)[1].lower() or sender.strip().lower()
        for r in self.rules:
            if r.sender is not None and not r.sender.search(address):
                continue
            m = r.subject.search(subject)
            if m is None or not m["ticket"]:
                continue
            return Match(r.name, r.kind, m["ticket"].strip(), _group(m, "title"),
                         _group(m, "state") or r.state, _group(m, "category") or r.category)
        return None

    def url(self, rule: str, ticket: str) -> str | None:
        """The web page of `ticket`, read by the rule named `rule`; None when that rule has no url or is gone.

        `ticket` comes from a subject someone else wrote, so it is
        percent-encoded: it can fill in one path segment or query value, never
        change the rest of the URL.
        """
        r = next((r for r in self.rules if r.name == rule), None)
        return None if r is None or r.url is None else r.url.replace("{ticket}", quote(ticket, safe=""))

    @property
    def fingerprint(self) -> str:
        """Changes whenever a change to the rules could change what match() returns for some mail."""
        parts = [[r.name, r.subject.pattern, r.subject.flags, r.sender and r.sender.pattern,
                  r.sender and r.sender.flags, r.kind, r.state, r.category] for r in self.rules]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _group(m: re.Match[str], name: str) -> str | None:
    if name not in m.re.groupindex or m[name] is None:
        return None
    # Subjects arrive folded and padded; an empty group counts as absent.
    return " ".join(m[name].split()) or None
