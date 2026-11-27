import pytest
from conftest import CONFIG, make_config

from readers_digest import config
from readers_digest.config import ConfigError, Role, Security


def test_example_config_parses():
    cfg = config.load(config.Path(__file__).parent.parent / "config.example.toml")
    assert [a.name for a in cfg.accounts] == ["work"]
    assert cfg.teams.enabled is False
    assert cfg.rules.match("[JIRA] (OPS-7) Disk full on db01").ticket == "OPS-7"


def test_defaults_and_derived_values(cfg):
    a = cfg.accounts[0]
    assert (a.port, a.security) == (993, Security.SSL)
    assert [f.role for f in a.folders] == [Role.TICKETS, Role.MAIL]
    assert cfg.mailboxes() == [("work", "INBOX")]
    assert cfg.digest.is_closed("RESOLVED") and not cfg.digest.is_closed(None)
    assert cfg.review.command is None


@pytest.mark.parametrize("extra, message", [
    ("[digest]\nstale_dayz = 3\n", "stale_dayz: unknown key"),
    ("[sync]\nbackfill_days = true\n", "backfill_days: must be a whole number"),
    ("[sync]\ntimeout_s = 0\n", "timeout_s: must be more than 0"),
    ("[teams]\nenabled = 1\n", "enabled: must be true or false"),
    ("[digest]\nstale_days = 14\nwindow_days = 14\n", "stale_days: must be less than window_days"),
    ("[[rule]]\nname = \"x\"\nsubject = \"(\"\n", "not a regular expression"),
    ("[[rule]]\nname = \"x\"\nsubject = \"x\"\n", "no .*ticket"),
    ("[[rule]]\nname = \"update\"\nsubject = \"(?P<ticket>x)\"\n", "used twice"),
    ("[[rule]]\nname = \"y\"\nsubject = \"(?P<ticket>x)\"\nurl = \"https://x/\"\n", "must contain {ticket}"),
])
def test_mistakes_name_the_key(extra, message):
    # [digest] is already in CONFIG, so those cases replace it instead.
    base = CONFIG.split("[digest]")[0] if extra.startswith("[digest]") else CONFIG
    with pytest.raises(ConfigError, match=message):
        make_config(extra, base)


def test_plain_imap_only_to_this_machine():
    plain = CONFIG.replace('host = "imap.example.com"', 'host = "imap.example.com"\nsecurity = "plain"')
    with pytest.raises(ConfigError, match="unencrypted"):
        make_config(base=plain)
    local = plain.replace('host = "imap.example.com"', 'host = "127.0.0.1"')
    assert make_config(base=local).accounts[0].port == 143


def test_missing_file_says_where_to_start(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        config.load(tmp_path / "nope.toml")


def test_default_path_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.default_path() == tmp_path / "readers-digest" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert config.default_path().parent.parent == config.Path.home() / ".config"
