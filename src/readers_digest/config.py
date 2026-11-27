"""The configuration file: mail accounts and their folders, the subject rules, and the other commands' settings.

TOML, at $XDG_CONFIG_HOME/readers-digest/config.toml unless --config names
another; config.example.toml in the repository documents every key. load()
checks the whole file up front, so a typo stops a run with the key's name
instead of surfacing halfway through a sync.
"""

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from readers_digest.rules import Kind, Rule, Rules


class ConfigError(Exception):
    """The config file is missing, is not TOML, or has a key that is missing, unknown or of the wrong type."""


class Role(StrEnum):
    """What a folder holds, which decides what becomes of a mail no rule matches."""

    # Only ticket notifications: an unmatched mail is counted as unparsed.
    TICKETS = "tickets"
    # Mail of any kind: an unmatched mail is listed as new mail, with its text, for review.
    MAIL = "mail"


class Security(StrEnum):
    """How an account's IMAP connection is protected."""

    SSL = "ssl"  # TLS from the start (IMAPS, port 993)
    STARTTLS = "starttls"  # plain connect, then STARTTLS before LOGIN (port 143)
    # No TLS at all: only to a gateway on this machine, e.g. one that speaks
    # IMAP for a server that does not.
    PLAIN = "plain"


@dataclass(frozen=True)
class Folder:
    """One IMAP folder an account reads, and what kind of mail it holds."""

    name: str  # the IMAP mailbox name, as the server lists it
    role: Role


@dataclass(frozen=True)
class Account:
    """One IMAP login and the folders sync reads with it."""

    # Shown in the digest, and the key of the account's cursors in the state db:
    # renaming it makes the next sync read its backfill again.
    name: str
    host: str
    port: int
    security: Security
    user: str
    # Run without a shell; the first line of its stdout is the password.
    password_command: tuple[str, ...]
    folders: tuple[Folder, ...]


@dataclass(frozen=True)
class SyncSettings:
    """[sync]: how far back sync reads, and what mail text it keeps."""

    # Days of mail a folder's first sync reads, and every sync after its UIDVALIDITY changed.
    backfill_days: int = 30
    # Days re-read on every sync, whatever the cursor says, in case the server
    # renumbered without changing UIDVALIDITY; Message-ID dedup drops the repeats.
    overlap_days: int = 2
    # A mail's text is fetched only when it is this recent, and dropped once older.
    retention_days: int = 7
    max_body_bytes: int = 5_000_000
    max_body_chars: int = 100_000
    timeout_s: float = 120


@dataclass(frozen=True)
class DigestSettings:
    """[digest]: which tickets the digest lists, and how it groups them."""

    # An open ticket with no mail for this many days is listed as stale.
    stale_days: int = 5
    # An open ticket with no mail for this many days is no longer listed at all,
    # only counted in the footer: a ticket whose closing mail never came would stay forever.
    window_days: int = 14
    # Searched (case-insensitively) in a ticket's category; the digest shows only
    # tickets it finds or whose category is unknown, unless asked for all. None shows all.
    focus: re.Pattern[str] | None = None
    # States in which a ticket counts as closed, compared casefolded.
    closed_states: frozenset[str] = frozenset({"closed", "done", "resolved"})
    # The order open tickets are grouped by state in. States not listed come after, in name order.
    state_order: tuple[str, ...] = ()

    def is_closed(self, state: str | None) -> bool:
        """`state` is one of closed_states, ignoring case; None (no state yet) is open."""
        return state is not None and state.casefold() in self.closed_states


@dataclass(frozen=True)
class ReviewSettings:
    """[review]: the command review sends mail and chats to, and who they are read for."""

    # Gets the prompt on stdin and must print the model's answer on stdout. None: review is off.
    command: tuple[str, ...] | None = None
    # Who the mail is read for, as mail names them: "Alex Example".
    reader: str = "the reader"
    # A sentence about them and their work, so the model can tell what concerns them.
    about: str = ""
    # The language reply drafts are written in.
    draft_language: str = "English"
    timeout_s: float = 300


@dataclass(frozen=True)
class TeamsSettings:
    """[teams]: whether sync reads Teams chats, and how its browser reaches Teams."""

    # Read Teams chats on every sync (needs the `teams` extra and a teams-login first).
    enabled: bool = False
    # The address Microsoft's account picker lists the reader under, clicked
    # when a headless run lands on the picker. None: the picker stops the run.
    account: str | None = None
    # The proxy Chromium goes through, as "http://host:port". None: HTTPS_PROXY
    # from the environment, or else a direct connection.
    proxy: str | None = None
    # Chromium's locale and timezone; None leaves Chromium's defaults.
    locale: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class Config:
    """The whole config file, validated; config.load and config.parse make one."""

    accounts: tuple[Account, ...]
    rules: Rules
    sync: SyncSettings = field(default_factory=SyncSettings)
    digest: DigestSettings = field(default_factory=DigestSettings)
    review: ReviewSettings = field(default_factory=ReviewSettings)
    teams: TeamsSettings = field(default_factory=TeamsSettings)

    def mailboxes(self) -> list[tuple[str, str]]:
        """(account, folder) of every `mail` folder, in config order: the order the digest lists new mail in."""
        return [(a.name, f.name) for a in self.accounts for f in a.folders if f.role is Role.MAIL]


def default_path() -> Path:
    """Where the config is read from without --config.

    That is readers-digest/config.toml in $XDG_CONFIG_HOME,
    or in ~/.config when that is unset or relative.
    """
    base = os.environ.get("XDG_CONFIG_HOME", "")
    # The XDG base directory spec says a relative value is to be ignored.
    root = Path(base) if os.path.isabs(base) else Path.home() / ".config"
    return root / "readers-digest" / "config.toml"


def load(path: Path) -> Config:
    """The config in `path`; raises ConfigError naming the file and the offending key."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: no such file; start from config.example.toml") from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    try:
        return parse(raw)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def parse(raw: dict[str, Any]) -> Config:
    """The config a TOML document holds, already decoded; raises ConfigError naming the offending key."""
    t = _Table(raw, "")
    accounts = tuple(_account(a) for a in t.tables("account"))
    names = [a.name for a in accounts]
    if dup := {n for n in names if names.count(n) > 1}:
        raise ConfigError(f"account name {sorted(dup)[0]!r} is used twice")
    rules = tuple(_rule(r) for r in t.tables("rule"))
    rule_names = [r.name for r in rules]
    if dup := {n for n in rule_names if rule_names.count(n) > 1}:
        raise ConfigError(f"rule name {sorted(dup)[0]!r} is used twice")
    try:
        checked = Rules(rules)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    sync, digest, review, teams = t.table("sync"), t.table("digest"), t.table("review"), t.table("teams")
    t.done()
    return Config(accounts, checked, _sync(sync), _digest(digest), _review(review), _teams(teams))


def _account(t: "_Table") -> Account:
    host = t.string("host")
    security = Security(t.choice("security", [s.value for s in Security], "ssl"))
    if security is Security.PLAIN and not _loopback(host):
        raise ConfigError(f"{t.where}security: plain sends the password unencrypted; it is allowed only "
                          f"for a gateway on this machine (localhost), not {host!r}")
    default_port = 993 if security is Security.SSL else 143
    folders = []
    for f in t.tables("folder"):
        folders.append(Folder(f.string("name"), Role(f.choice("role", [r.value for r in Role]))))
        f.done()
    if not folders:
        raise ConfigError(f"{t.where}folder: an account needs at least one [[account.folder]]")
    command = tuple(t.strings("password_command"))
    if not command:
        raise ConfigError(f"{t.where}password_command: must name a program")
    a = Account(t.string("name"), host, t.whole("port", default_port), security, t.string("user"), command,
                tuple(folders))
    t.done()
    return a


def _rule(t: "_Table") -> Rule:
    name = t.string("name")
    kind = Kind(t.choice("kind", [Kind.UPDATE.value, Kind.REMINDER.value], Kind.UPDATE.value))
    subject = t.regex("subject")
    if subject is None:
        raise ConfigError(f"{t.where}subject: missing")
    url = t.opt_string("url")
    if url is not None and "{ticket}" not in url:
        raise ConfigError(f"{t.where}url: must contain {{ticket}}")
    r = Rule(name, subject, t.regex("sender"), kind, t.opt_string("state"), t.opt_string("category"), url)
    t.done()
    return r


def _sync(t: "_Table") -> SyncSettings:
    d = SyncSettings()
    s = SyncSettings(
        backfill_days=t.whole("backfill_days", d.backfill_days),
        overlap_days=t.whole("overlap_days", d.overlap_days),
        retention_days=t.whole("retention_days", d.retention_days),
        max_body_bytes=t.whole("max_body_bytes", d.max_body_bytes),
        max_body_chars=t.whole("max_body_chars", d.max_body_chars),
        timeout_s=t.number("timeout_s", d.timeout_s),
    )
    t.done()
    return s


def _digest(t: "_Table") -> DigestSettings:
    d = DigestSettings()
    closed = t.strings("closed_states", sorted(d.closed_states))
    s = DigestSettings(
        stale_days=t.whole("stale_days", d.stale_days),
        window_days=t.whole("window_days", d.window_days),
        focus=t.regex("focus"),
        closed_states=frozenset(c.casefold() for c in closed),
        state_order=tuple(t.strings("state_order", [])),
    )
    t.done()
    # A ticket quiet for window_days is no longer listed at all, so it can
    # be listed as stale only before then.
    if s.stale_days >= s.window_days:
        raise ConfigError(f"{t.where}stale_days: must be less than window_days ({s.window_days}), or no ticket is"
                          " ever listed as stale")
    return s


def _review(t: "_Table") -> ReviewSettings:
    d = ReviewSettings()
    command = t.strings("command", [])
    s = ReviewSettings(
        command=tuple(command) or None,
        reader=t.string("reader", d.reader),
        about=t.string("about", d.about),
        draft_language=t.string("draft_language", d.draft_language),
        timeout_s=t.number("timeout_s", d.timeout_s),
    )
    t.done()
    return s


def _teams(t: "_Table") -> TeamsSettings:
    s = TeamsSettings(
        enabled=t.boolean("enabled", False),
        account=t.opt_string("account"),
        proxy=t.opt_string("proxy"),
        locale=t.opt_string("locale"),
        timezone=t.opt_string("timezone"),
    )
    t.done()
    return s


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_MISSING = object()


class _Table:
    """A TOML table being read: each read takes a key, and done() refuses any key left over, as a typo would be."""

    def __init__(self, raw: Any, where: str):
        if not isinstance(raw, dict):
            raise ConfigError(f"{where.rstrip('.') or 'the file'}: must be a table")
        self.raw = dict(raw)
        self.where = where

    def _take(self, key: str, default: Any, types: type | tuple[type, ...], what: str) -> Any:
        value = self.raw.pop(key, default)
        if value is _MISSING:
            raise ConfigError(f"{self.where}{key}: missing")
        # bool is an int to Python, never to a config reader.
        if value is not default and (not isinstance(value, types) or isinstance(value, bool)):
            raise ConfigError(f"{self.where}{key}: must be {what}, not {type(value).__name__}")
        return value

    def string(self, key: str, default: Any = _MISSING) -> str:
        return self._take(key, default, str, "a string")

    def opt_string(self, key: str) -> str | None:
        return self._take(key, None, str, "a string")

    def boolean(self, key: str, default: Any = _MISSING) -> bool:
        value = self.raw.pop(key, default)
        if value is _MISSING:
            raise ConfigError(f"{self.where}{key}: missing")
        if not isinstance(value, bool):
            raise ConfigError(f"{self.where}{key}: must be true or false, not {type(value).__name__}")
        return value

    def whole(self, key: str, default: Any = _MISSING) -> int:
        value = self._take(key, default, int, "a whole number")
        if value < 0:
            raise ConfigError(f"{self.where}{key}: must not be negative")
        return value

    def number(self, key: str, default: Any = _MISSING) -> float:
        value = self._take(key, default, (int, float), "a number")
        if value <= 0:
            raise ConfigError(f"{self.where}{key}: must be more than 0")
        return float(value)

    def choice(self, key: str, choices: list[str], default: Any = _MISSING) -> str:
        value = self.string(key, default)
        if value not in choices:
            raise ConfigError(f"{self.where}{key}: must be one of {', '.join(choices)}, not {value!r}")
        return value

    def strings(self, key: str, default: Any = _MISSING) -> list[str]:
        value = self._take(key, default, list, "a list of strings")
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self.where}{key}: must be a list of strings")
        return list(value)

    def regex(self, key: str) -> re.Pattern[str] | None:
        """The key's regular expression, compiled to match case-insensitively; None when it is absent."""
        value = self.opt_string(key)
        if value is None:
            return None
        try:
            return re.compile(value, re.IGNORECASE)
        except re.error as e:
            raise ConfigError(f"{self.where}{key}: not a regular expression: {e}") from None

    def table(self, key: str) -> "_Table":
        return _Table(self.raw.pop(key, {}), f"{self.where}{key}.")

    def tables(self, key: str) -> list["_Table"]:
        value = self.raw.pop(key, [])
        if not isinstance(value, list):
            raise ConfigError(f"{self.where}{key}: must be an array of tables, [[{key}]]")
        return [_Table(v, f"{self.where}{key}[{i}].") for i, v in enumerate(value)]

    def done(self) -> None:
        if self.raw:
            raise ConfigError(f"{self.where}{sorted(self.raw)[0]}: unknown key")
