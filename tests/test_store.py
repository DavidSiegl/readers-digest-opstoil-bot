import os
import sqlite3
from datetime import timedelta

import pytest
from conftest import at, mail, make_config

from readers_digest.store import SCHEMA_VERSION, Cursor, Source, Store, Tag
from readers_digest.teams.chat import Conversation, Message


def test_record_dedups_by_message_id_and_moves_the_cursor(store):
    m = mail("[OPS-1] Disk full", at(1))
    assert store.record([m], Cursor("work", "Tickets", 7, 42)) == 1
    assert store.record([m, m]) == 0
    assert store.cursor("work", "Tickets") == Cursor("work", "Tickets", 7, 42)
    assert store.cursor("work", "INBOX") is None


def test_fold_takes_the_latest_values_whatever_order_mail_arrives_in(store):
    history = [
        mail("[OPS-1] Disk full", at(1)),
        mail("Reminder: OPS-1", at(2)),
        mail("[OPS-1] Disk full is now In Progress", at(3)),
        mail("Reminder: OPS-1", at(4)),
        mail("[OPS-1] Disk still full", at(5)),
    ]
    store.record(reversed(history[2:]))
    store.record(reversed(history[:2]))
    [t] = store.tickets()
    assert (t.id, t.title, t.category, t.state) == ("OPS-1", "Disk still full", "OPS", "In Progress")
    assert (t.state_at, t.first_seen, t.last_seen, t.reminders) == (at(3), at(1), at(5), 1)
    assert t.rule == "update"


def test_a_repeated_state_keeps_when_it_was_entered(store):
    store.record([mail("[OPS-1] x is now Open", at(1)), mail("[OPS-1] x is now Open", at(2))])
    assert store.tickets()[0].state_at == at(1)


def test_unmatched_mail_is_unparsed_in_tickets_folders_and_mail_in_mail_folders(store):
    store.record([mail("Weekly report", at(1)), mail("Lunch?", at(2), folder="INBOX", body="12:00?")])
    assert store.unparsed(5) == (1, ["Weekly report"])
    [m] = store.mail(at(1))
    assert (m.subject, m.body_text) == ("Lunch?", "12:00?")
    assert store.mail(at(3)) == []


def test_changed_rules_rematch_stored_mail(tmp_path, cfg):
    db = tmp_path / "state.db"
    with Store(db, cfg.rules) as s:
        s.record([mail("Incident INC-9: printer on fire", at(1))])
        assert s.unparsed(1)[0] == 1 and s.tickets() == []
    fixed = make_config('[[rule]]\nname = "inc"\nsubject = \'^Incident (?P<ticket>INC-\\d+): (?P<title>.+)$\'\n')
    with Store(db, fixed.rules) as s:
        assert s.unparsed(1)[0] == 0
        assert [(t.id, t.title) for t in s.tickets()] == [("INC-9", "printer on fire")]
    with Store(db, cfg.rules) as s:
        assert s.tickets() == []


def test_db_is_owner_only_and_refuses_a_newer_schema(tmp_path, cfg):
    db = tmp_path / "sub" / "state.db"
    Store(db, cfg.rules).close()
    assert os.stat(db).st_mode & 0o077 == 0
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(sqlite3.DatabaseError, match="newer"):
        Store(db, cfg.rules)


def test_naive_timestamps_are_refused(store):
    with pytest.raises(ValueError, match="timezone"):
        store.record([mail("x", at(1).replace(tzinfo=None))])


def test_run_status(store):
    assert not store.status(Source.MAIL).failed
    store.mark_ok(Source.MAIL, at(1))
    store.mark_failed(Source.MAIL, at(2), "IMAP: down")
    st = store.status(Source.MAIL)
    assert st.failed and st.last_error == "IMAP: down"
    store.mark_ok(Source.MAIL, at(3))
    assert not store.status(Source.MAIL).failed
    assert store.status(Source.TEAMS).last_ok_at is None


def conv(cid="c1", unread=True, read_at=None):
    return Conversation(cid, "1:1", "", at(5), None, unread, read_at)


def msg(cid, mid, day, text="hi"):
    return Message(cid, mid, at(day), "8:orgid:bob", "Bob", text, False, False)


def test_record_teams_replaces_the_chat_list(store):
    assert store.record_teams([conv("c1"), conv("c2")], [msg("c1", "1", 1), msg("c2", "2", 2)]) == 2
    assert store.record_teams([conv("c1")], [msg("c1", "1", 1), msg("c1", "3", 3)]) == 1
    convs, msgs = store.teams()
    assert [c.id for c in convs] == ["c1"]
    assert [m.id for m in msgs] == ["1", "3"]
    assert store.teams_latest() == {"c1": at(3)}


def test_expire_text_keeps_unread_teams_messages(store):
    store.record([mail("Hi", at(1), folder="INBOX", body="text")])
    store.record_teams([conv("c1", read_at=at(1, 12)), conv("c2", unread=False)],
                       [msg("c1", "old", 1), msg("c1", "new", 2), msg("c2", "read", 2)])
    assert store.expire_text(at(10)) == 3
    assert store.mail(at(1))[0].body_note == "expired"
    assert {m.id: m.text for m in store.teams()[1]} == {"old": "", "new": "hi", "read": ""}


def test_prune_removes_quiet_tickets_whole_and_their_tags(store):
    store.record([mail("[OPS-1] old", at(1)), mail("[OPS-1] old again", at(2)), mail("[OPS-2] busy", at(1)),
                  mail("[OPS-2] busy", at(20)), mail("Hi", at(1), "<hi@x>", folder="INBOX")])
    store.record_tags([Tag("<hi@x>", "me", "fyi", "s", None)], at(2))
    store.mark_announced(["OPS-1", "OPS-2"], at(2))
    assert store.prune(at(10), dry_run=True) == (1, 3)
    assert len(store.tickets()) == 2
    assert store.prune(at(10)) == (1, 3)
    assert [t.id for t in store.tickets()] == ["OPS-2"]
    assert store.tags() == {}
    assert store.announced(["OPS-1", "OPS-2"]) == {}


def test_tags_and_announcements_round_trip(store):
    store.record_tags([Tag("k", "me", "question", "Bob asks", "Hi Bob")], at(1))
    store.record_tags([Tag("k", "team", "fyi", "", None)], at(2))
    assert store.tags()["k"] == Tag("k", "team", "fyi", "", None)
    store.mark_announced(["a"], at(1))
    assert store.announced(["a", "b"]) == {"a": at(1)}
    assert store.announced([]) == {}


def test_record_is_atomic(store):
    bad = mail("x", at(1).replace(tzinfo=None))
    with pytest.raises(ValueError):
        store.record([mail("[OPS-1] a", at(1)), bad], Cursor("work", "Tickets", 1, 5))
    assert store.tickets() == [] and store.cursor("work", "Tickets") is None


def test_age_of_tickets_never_depends_on_now(store):
    store.record([mail("[OPS-1] a", at(1) + timedelta(minutes=30))])
    assert store.tickets()[0].first_seen == at(1) + timedelta(minutes=30)


def test_opening_tightens_a_default_state_dir_made_world_readable(cfg, tmp_path, monkeypatch):
    # teams-login and the gui's path lookup can create it before any Store, with umask permissions.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from readers_digest.store import default_path

    default_path().parent.mkdir(mode=0o755)
    with Store(default_path(), cfg.rules):
        pass
    assert os.stat(default_path().parent).st_mode & 0o777 == 0o700


def test_opening_leaves_a_db_dir_chosen_with_db_alone(cfg, tmp_path):
    d = tmp_path / "shared"
    d.mkdir(mode=0o755)
    with Store(d / "state.db", cfg.rules):
        pass
    assert os.stat(d).st_mode & 0o777 == 0o755


def test_opening_an_up_to_date_db_does_not_wait_for_a_writer(store, cfg):
    import threading

    writer = sqlite3.connect(store.path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    opened = []
    t = threading.Thread(target=lambda: opened.append(Store(store.path, cfg.rules).close()), daemon=True)
    t.start()
    t.join(5)
    writer.execute("ROLLBACK")
    writer.close()
    assert opened, "opening waited for the writer's lock"
