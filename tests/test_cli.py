import importlib.util
import os
from datetime import UTC, datetime, timedelta

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


def test_gui_check_is_passed_to_the_window(home, monkeypatch):
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "gi" else find_spec(name, *a))
    calls = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append(argv))
    assert main(home, "gui", "--check") == 0
    [argv] = calls
    assert argv[-1] == "--check"


def test_review_waits_for_another_review_and_gives_up_without_sending(home, capsys, monkeypatch):
    """Two reviews at once would send the same mail text to the model twice."""
    from readers_digest import lock, review

    monkeypatch.setattr(cli, "REVIEW_WAIT_S", 0)
    sent = []
    monkeypatch.setattr(review, "review", lambda *a, **k: sent.append(a) or (None, None))
    with lock.held(lock.path("review"), 0, lambda s: None):
        assert main(home, "review", "--quiet") == 1
    assert sent == []
    assert "review still in use" in capsys.readouterr().err


def test_a_review_that_stopped_part_way_writes_what_it_got_and_exits_1(home, capsys, monkeypatch):
    from readers_digest import review

    monkeypatch.setattr(review, "review", lambda *a, **k: ("# Review, partial\n", review.ReviewError("quota")))
    assert main(home, "review", "--quiet") == 1
    assert (home / "db" / "review.md").read_text() == "# Review, partial\n"
    assert "review: quota" in capsys.readouterr().err


def test_a_sync_that_finds_the_lock_busy_touches_nothing(home, capsys, monkeypatch):
    from readers_digest import lock

    monkeypatch.setattr(cli, "SYNC_WAIT_S", 0)
    expired = []
    monkeypatch.setattr(Store, "expire_text", lambda self, before: expired.append(before) or 0)
    monkeypatch.setattr(cli, "sync", lambda *a, **k: pytest.fail("sync ran without the lock"))
    with lock.held(lock.path("sync"), 0, lambda s: None):
        assert main(home, "sync") == 1
    assert expired == []
    assert "nothing done" in capsys.readouterr().err


def test_a_sync_that_gets_the_lock_expires_text_older_than_retention_days(home, monkeypatch):
    with (home / "config.toml").open("a") as f:
        f.write("\n[sync]\nretention_days = 3\n")
    expired = []
    monkeypatch.setattr(Store, "expire_text", lambda self, before: expired.append(before) or 0)
    monkeypatch.setattr(cli, "sync", lambda *a, **k: 0)
    start = datetime.now(UTC)
    assert main(home, "sync", "--no-teams") == 0
    retention = timedelta(days=3)
    assert len(expired) == 1 and start - retention <= expired[0] <= datetime.now(UTC) - retention


def test_a_browser_driver_error_says_it_came_from_the_browser():
    from readers_digest import failure_line

    PlaywrightError = type("Error", (Exception,), {"__module__": "playwright._impl._errors"})
    assert failure_line(PlaywrightError("Target closed\ncall log: ...")) == "browser: Target closed"
    assert failure_line(OSError("disk full")) == "disk full"


def test_sync_stores_the_folders_it_read_and_reports_the_one_it_could_not(store, cfg, monkeypatch, capsys):
    from readers_digest import imap
    from readers_digest.store import Cursor, Source

    ok = imap.FolderResult("work", "INBOX", [mail("Hi", at(3), folder="INBOX")], Cursor("work", "INBOX", 7, 1), False)
    bad = imap.FolderResult("work", "Tickets", [], None, False, failure="IMAP: EXAMINE Tickets: NO gone")
    monkeypatch.setattr(imap, "read_password", lambda c: "pw")
    monkeypatch.setattr(imap, "fetch", lambda *a, **k: [bad, ok])
    assert cli.sync(store, cfg) == 1
    assert store.cursor("work", "INBOX") == Cursor("work", "INBOX", 7, 1)
    assert "work/Tickets: IMAP: EXAMINE Tickets: NO gone" in store.status(Source.MAIL).last_error
    assert "work/Tickets" in capsys.readouterr().err


def test_write_private_replaces_the_file_owner_only_even_under_an_open_umask(tmp_path):
    from readers_digest import write_private

    old = os.umask(0)
    try:
        path = tmp_path / "out.json"
        write_private(path, "one")
        write_private(path, "two")
    finally:
        os.umask(old)
    assert path.read_text() == "two" and os.stat(path).st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]
