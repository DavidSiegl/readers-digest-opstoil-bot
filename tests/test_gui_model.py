"""gui.model: what the window shows of a digest. Pure, so no GTK is needed."""

from datetime import UTC, datetime, timedelta

import pytest

from readers_digest.config import parse
from readers_digest.digest import collect
from readers_digest.gui import model
from readers_digest.store import Mail, Source, Store, Tag
from readers_digest.teams.chat import Conversation, Message

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=1)


def _cfg(teams: bool = False):
    return parse({
        "account": [{"name": "work", "host": "imap.example.com", "user": "a@example.com",
                     "password_command": ["true"],
                     "folder": [{"name": "Tickets", "role": "tickets"}, {"name": "INBOX", "role": "mail"}]}],
        "rule": [{"name": "t", "subject": r"^\[(?P<ticket>T-\d+)\] (?P<title>.+?)(?: is (?P<state>\w+))?$",
                  "url": "https://tracker.example.com/{ticket}"}],
        "teams": {"enabled": teams},
    })


def _mail(mid: str, subject: str, at: datetime, folder: str = "Tickets", body: str | None = None) -> Mail:
    role = "tickets" if folder == "Tickets" else "mail"
    return Mail(mid, "work", folder, role, at, "Sam Sender <sam@example.com>", subject, body)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db", _cfg().rules) as s:
        yield s


def _view(store, cfg=None):
    return model.build(collect(store, cfg or _cfg(), SINCE, NOW), UTC)


def _page(v: model.View, name: str) -> model.Page:
    return next(p for p in v.pages if p.name == name)


def test_tickets_page_groups_new_state_and_closed_with_links(store):
    store.record([
        _mail("<1@x>", "[T-1] Disk full is Open", NOW - timedelta(hours=2)),
        _mail("<2@x>", "[T-2] Old one is Open", NOW - timedelta(days=8)),
        _mail("<3@x>", "[T-3] Done thing is Closed", NOW - timedelta(hours=1)),
    ])
    p = _page(_view(store), "tickets")
    titles = [g.title for g in p.groups]
    assert titles[0].startswith("New since")
    assert [r.key for r in p.groups[0].rows] == ["T-1"]
    assert "Open" in titles
    stale = next(g for g in p.groups if g.title.startswith("Stale"))
    assert [r.key for r in stale.rows] == ["T-2"]
    closed = p.groups[-1]
    assert closed.folded and [r.key for r in closed.rows] == ["T-3"]
    assert p.count == 2
    assert p.groups[0].rows[0].link == "https://tracker.example.com/T-1"
    assert model.Badge("new") in p.groups[0].rows[0].badges


def test_mail_page_splits_by_tag_and_folds_noise(store):
    store.record([
        _mail("<a@x>", "Please review", NOW - timedelta(hours=3), "INBOX", "can you look?"),
        _mail("<b@x>", "Newsletter", NOW - timedelta(hours=2), "INBOX", "news"),
        _mail("<c@x>", "Unreviewed", NOW - timedelta(hours=1), "INBOX", "hm"),
    ])
    store.record_tags([Tag("<a@x>", "me", "request", "Sam wants a review", "Hi Sam, …"),
                       Tag("<b@x>", "nobody", "noise", "", None)], NOW)
    v = _view(store)
    p = _page(v, "mail")
    by_title = {g.title: g for g in p.groups}
    mine = by_title["work/INBOX · for you"].rows
    assert [r.key for r in mine] == ["<a@x>"] and mine[0].warn and mine[0].draft == "Hi Sam, …"
    assert mine[0].details[0] == "Sam wants a review"
    assert [r.key for r in by_title["work/INBOX"].rows] == ["<c@x>"]
    assert model.Badge("not reviewed") in by_title["work/INBOX"].rows[0].badges
    assert by_title["work/INBOX · FYI / noise"].folded
    assert p.count == 1
    assert "mail <a@x>" in v.actions


def test_empty_mail_folder_still_listed(store):
    assert [g.title for g in _page(_view(store), "mail").groups] == ["work/INBOX"]


def test_teams_page_only_when_enabled(tmp_path):
    cfg = _cfg(teams=True)
    with Store(tmp_path / "t.db", cfg.rules) as s:
        conv = Conversation("19:c@thread", "1:1", "", NOW, "tenant", True, NOW - timedelta(hours=2))
        msg = Message("19:c@thread", "m1", NOW - timedelta(hours=1), "8:orgid:o", "Pat", "ping", False, False)
        s.record_teams([conv], [msg])
        s.mark_ok(Source.TEAMS, NOW)
        v = _view(s, cfg)
        assert [p.name for p in _view(s).pages] == ["tickets", "mail"]
    p = _page(v, "teams")
    (row,) = p.groups[0].rows
    assert row.title == "Pat · 1:1"
    assert row.link.startswith("https://teams.microsoft.com/l/chat/")
    assert p.count == 1
    assert "unread in Teams" in model.summary(v)


def test_failed_sync_is_a_banner_and_an_action(store):
    store.mark_failed(Source.MAIL, NOW, "IMAP: LOGIN rejected")
    v = _view(store)
    assert v.banners and "LOGIN rejected" in v.banners[0]
    assert any(a.startswith("mail failed") for a in v.actions)
    assert v.title == "Never synced"
    assert model.summary(v).splitlines()[0] == "0 open tickets"


def test_code_stamp_changes_when_a_module_is_replaced_added_or_removed(tmp_path):
    from readers_digest.gui import code_stamp

    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "sub" / "b.py").write_text("y = 1\n")
    first = code_stamp(tmp_path / "pkg")
    assert first is not None and code_stamp(tmp_path / "pkg") == first
    (tmp_path / "pkg" / "sub" / "b.py").write_text("y = 22\n")
    second = code_stamp(tmp_path / "pkg")
    assert second != first
    (tmp_path / "pkg" / "c.py").write_text("")
    assert code_stamp(tmp_path / "pkg") != second
    (tmp_path / "pkg" / "c.py").unlink()
    assert code_stamp(tmp_path / "pkg") == second


def test_code_stamp_is_none_for_a_package_mid_install(tmp_path):
    from readers_digest.gui import code_stamp

    assert code_stamp(tmp_path / "gone") is None
    (tmp_path / "empty").mkdir()
    assert code_stamp(tmp_path / "empty") is None


def test_code_stamp_of_a_zip_archive_changes_when_it_is_replaced(tmp_path):
    from readers_digest.gui import code_stamp

    pyz = tmp_path / "readers-digest.pyz"
    pyz.write_bytes(b"one")
    first = code_stamp(pyz)
    assert first is not None and code_stamp(pyz) == first
    pyz.write_bytes(b"three")
    assert code_stamp(pyz) != first
    pyz.unlink()
    assert code_stamp(pyz) is None
