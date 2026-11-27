"""Shared test data: a config with one account, a tickets and a mail folder, and two rules."""

import tomllib
from datetime import UTC, datetime

import pytest

from readers_digest import config
from readers_digest.store import Mail, Store

CONFIG = r'''
[[account]]
name = "work"
host = "imap.example.com"
user = "alex@example.com"
password_command = ["printf", "secret"]

[[account.folder]]
name = "Tickets"
role = "tickets"

[[account.folder]]
name = "INBOX"
role = "mail"

[[rule]]
name = "reminder"
subject = '^Reminder: (?P<ticket>OPS-\d+)'
kind = "reminder"

[[rule]]
name = "update"
subject = '^\[(?P<ticket>(?P<category>[A-Z]+)-\d+)\] (?P<title>.+?)(?: is now (?P<state>.+))?$'
url = "https://tracker.example.com/{ticket}"

[digest]
focus = '^OPS$'
state_order = ["Open", "In Progress"]
'''


def make_config(extra: str = "", base: str = CONFIG) -> config.Config:
    """The test config, with the TOML in `extra` appended."""
    return config.parse(tomllib.loads(base + extra))


def at(day: int, hour: int = 10, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


def mail(subject: str, sent: datetime, mid: str | None = None, folder: str = "Tickets", sender: str = "t@example.com",
         body: str | None = None) -> Mail:
    """A mail as sync would hand it to the store; role follows the folder, as in CONFIG."""
    role = "tickets" if folder == "Tickets" else "mail"
    return Mail(mid or f"<{subject}|{sent.isoformat()}@test>", "work", folder, role, sent, sender, subject, body)


@pytest.fixture
def cfg() -> config.Config:
    return make_config()


@pytest.fixture
def store(tmp_path, cfg):
    with Store(tmp_path / "state.db", cfg.rules) as s:
        yield s
