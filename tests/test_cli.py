import importlib.util
import os

import pytest
from conftest import CONFIG, at, mail

from readers_digest import cli
from readers_digest.store import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A config file holding CONFIG, and every XDG directory and HOME inside tmp_path."""
    for var in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return tmp_path


def main(home, *argv):
    return cli.main(["--config", str(home / "config.toml"), "--db", str(home / "db" / "state.db"), *argv])


def test_match_exits_0_and_says_what_a_rule_reads(home, capsys):
    assert main(home, "match", "[OPS-7] Disk full is now Open") == 0
    out = capsys.readouterr().out
    assert "rule:     update" in out and "state:    Open" in out
    assert "url:      https://tracker.example.com/OPS-7" in out


def test_match_exits_1_when_no_rule_reads_the_subject(home, capsys):
    assert main(home, "match", "Lunch on Friday?") == 1
    assert capsys.readouterr().out.startswith("no rule matches")


def test_a_missing_config_exits_1(home, capsys):
    assert cli.main(["--config", str(home / "nope.toml"), "match", "x"]) == 1
    assert "no such file" in capsys.readouterr().err


def test_show_writes_digest_md_owner_only(home, capsys):
    assert main(home, "show", "--quiet") == 0
    digest = home / "db" / "digest.md"
    assert os.stat(digest).st_mode & 0o777 == 0o600
    assert digest.read_text().startswith("# Tickets in focus, ")
    assert capsys.readouterr().out == ""


def test_prune_dry_run_counts_and_keeps(home, cfg, capsys):
    db = home / "db" / "state.db"
    with Store(db, cfg.rules) as s:
        s.record([mail("[OPS-1] long ago", at(1, month=1))])
    assert main(home, "prune", "--days", "30", "--dry-run") == 0
    assert capsys.readouterr().out == "prune: would delete 1 tickets and 1 messages quiet for 30 days or more\n"
    with Store(db, cfg.rules) as s:
        assert [t.id for t in s.tickets()] == ["OPS-1"]


def test_prune_refuses_fewer_days_than_backfill(home):
    with pytest.raises(SystemExit) as e:
        main(home, "prune", "--days", "3")
    assert e.value.code == 2


def test_gui_without_gi_execs_the_system_python(home, monkeypatch):
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "gi" else find_spec(name, *a))
    calls = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    assert main(home, "gui", "--hidden") == 0
    [(path, argv, env)] = calls
    assert path == "/usr/bin/python3"
    assert argv[:3] == ["/usr/bin/python3", "-m", "readers_digest.gui"]
    assert argv[-1] == "--hidden" and str(home / "config.toml") in argv
    gui_path = env["PYTHONPATH"].split(os.pathsep)[0]
    assert gui_path.startswith(str(home)) and os.path.islink(os.path.join(gui_path, "readers_digest"))
