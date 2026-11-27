from datetime import UTC, datetime, timedelta, timezone

from conftest import at, mail, make_config

from readers_digest.digest import clean, collect, format_digest, mail_group, previous_working_day, sender_name
from readers_digest.store import Source, Tag


def ids(tickets):
    return [t.id for t in tickets]


def test_open_tickets_are_grouped_by_state_order_then_by_name_then_stateless(store, cfg):
    store.record([
        mail("[OPS-1] a is now Waiting", at(1)),
        mail("[OPS-2] b is now In Progress", at(2)),
        mail("[OPS-3] c is now Open", at(3)),
        mail("[OPS-4] d is now Blocked", at(4)),
        mail("Reminder: OPS-5", at(5)),
        mail("[OPS-6] f is now Open", at(6)),
    ])
    d = collect(store, cfg, at(1), at(10))
    assert [(s, ids(ts)) for s, ts in d.open_by_state] == [
        ("Open", ["OPS-3", "OPS-6"]), ("In Progress", ["OPS-2"]),
        ("Blocked", ["OPS-4"]), ("Waiting", ["OPS-1"]), (None, ["OPS-5"]),
    ]


def test_states_that_differ_only_in_case_form_one_group(store, cfg):
    store.record([mail("[OPS-1] a is now open", at(1)), mail("[OPS-2] b is now OPEN", at(2)),
                  mail("[OPS-3] c is now blocked", at(3)), mail("[OPS-4] d is now Blocked", at(4))])
    d = collect(store, cfg, at(1), at(5))
    assert [(s, ids(ts)) for s, ts in d.open_by_state] == [("Open", ["OPS-1", "OPS-2"]),
                                                          ("blocked", ["OPS-3", "OPS-4"])]


def test_a_ticket_with_no_state_is_listed_under_state_unknown(store, cfg):
    store.record([mail("[OPS-1] Disk full", at(1))])
    text = format_digest(collect(store, cfg, at(1), at(2)), UTC)
    assert "### State unknown (1)" in text
    assert "OPS-1](<https://tracker.example.com/OPS-1>) · Disk full · OPS · open 1d" in text


def test_focus_hides_other_categories_but_keeps_unknown_ones(store, cfg):
    store.record([mail("[OPS-1] mine", at(1)), mail("[DEV-1] theirs", at(1)), mail("Reminder: OPS-2", at(1))])
    focused = collect(store, cfg, at(1), at(2))
    assert sorted(t.id for _, ts in focused.open_by_state for t in ts) == ["OPS-1", "OPS-2"]
    # Out-of-focus tickets are not part of the footer's "Not shown" count, which is only the window's.
    assert focused.hidden == 0
    everything = collect(store, cfg, at(1), at(2), focus_only=False)
    assert "DEV-1" in [t.id for _, ts in everything.open_by_state for t in ts]
    assert format_digest(everything, UTC).startswith("# All tickets, ")


def test_quiet_tickets_are_stale_and_quieter_ones_leave_the_window_and_are_counted(store, cfg):
    store.record([mail("[OPS-1] gone quiet", at(1)), mail("[OPS-2] quiet", at(14)), mail("[OPS-3] busy", at(18))])
    d = collect(store, cfg, at(19), at(20))
    assert ids(d.stale) == ["OPS-2"]
    assert sorted(t.id for _, ts in d.open_by_state for t in ts) == ["OPS-2", "OPS-3"]
    assert d.hidden == 1
    text = format_digest(d, UTC)
    assert "## Stale: no mail for 5 days or more (1)" in text and "last mail 6d ago" in text
    assert "Not shown: 1 open ticket with no mail in the last 14 days" in text


def test_tickets_closed_since_are_listed_and_older_closed_ones_are_not(store, cfg):
    store.record([
        mail("[OPS-1] early is now Closed", at(1)),
        mail("[OPS-2] late is now In Progress", at(1)),
        mail("[OPS-2] late is now Resolved", at(3)),
        mail("[OPS-3] still open", at(3)),
    ])
    d = collect(store, cfg, at(2), at(4))
    assert ids(d.closed) == ["OPS-2"]
    assert ids(t for _, ts in d.open_by_state for t in ts) == ["OPS-3"]
    assert "OPS-2" in d.links and "OPS-1" not in d.links


def test_links_come_from_the_url_template_of_the_rule_that_read_the_ticket(store, cfg):
    store.record([mail("[OPS-1] has a page", at(1)), mail("Reminder: OPS-2", at(1))])
    d = collect(store, cfg, at(1), at(2))
    assert d.links == {"OPS-1": "https://tracker.example.com/OPS-1"}
    text = format_digest(d, UTC)
    assert "- [OPS-1](<https://tracker.example.com/OPS-1>)" in text
    assert "- OPS-2 · (no title) · unknown category · open 1d · 1 reminder" in text


def test_a_failed_mail_sync_shows_a_banner_with_the_last_good_one(store, cfg):
    assert "failed" not in format_digest(collect(store, cfg, at(1), at(3)), UTC)
    store.mark_ok(Source.MAIL, at(1))
    store.mark_failed(Source.MAIL, at(2), "IMAP:\n down")
    text = format_digest(collect(store, cfg, at(1), at(3)), UTC)
    assert "> **Last mail sync failed** at 2026-11-02 10:00: IMAP: down\n" in text
    assert "> The data below is from the last successful sync, 2026-11-01 10:00." in text


def test_a_banner_for_a_sync_that_never_succeeded(store, cfg):
    store.mark_failed(Source.MAIL, at(2), "no route")
    text = format_digest(collect(store, cfg, at(1), at(3)), UTC)
    assert "> None has succeeded yet." in text and "Last successful sync: never" in text


def test_previous_working_day_skips_the_weekend():
    plus_one = timezone(timedelta(hours=1))
    monday, tuesday = datetime(2026, 11, 30, 9, tzinfo=plus_one), datetime(2026, 11, 24, 9, tzinfo=plus_one)
    assert previous_working_day(monday, plus_one) == datetime(2026, 11, 27, tzinfo=plus_one)
    assert previous_working_day(tuesday, plus_one) == datetime(2026, 11, 23, tzinfo=plus_one)
    for weekend_day in (28, 29):
        now = datetime(2026, 11, weekend_day, 9, tzinfo=plus_one)
        assert previous_working_day(now, plus_one) == datetime(2026, 11, 27, tzinfo=plus_one)


def test_previous_working_day_starts_at_midnight_across_a_dst_change():
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")  # summer time from Sunday 8 March 2026
    since = previous_working_day(datetime(2026, 3, 9, 9, tzinfo=ny), ny)
    assert since == datetime(2026, 3, 6, tzinfo=ny) and since.utcoffset() == timedelta(hours=-5)


def test_mail_group_puts_the_readers_mail_first_and_fyi_noise_last():
    def tag(act, kind):
        return Tag("k", act, kind, "", None)

    assert mail_group(None) == 1
    assert mail_group(tag("me", "noise")) == 0
    assert mail_group(tag("nobody", "fyi")) == mail_group(tag("nobody", "noise")) == 2
    assert mail_group(tag("team", "fyi")) == mail_group(tag("nobody", "question")) == 1


def test_new_mail_is_listed_by_group_with_its_tag_and_summary(store, cfg):
    store.record([
        mail("Newsletter", at(2), "<n@x>", folder="INBOX", sender="News <news@example.com>"),
        mail("Lunch?", at(3), "<l@x>", folder="INBOX", sender="bob@example.com"),
        mail("Please review", at(4), "<r@x>", folder="INBOX", sender="Sam Sender <sam@example.com>"),
    ])
    store.record_tags([Tag("<r@x>", "me", "request", "Sam wants\na review", None),
                       Tag("<n@x>", "nobody", "noise", "", None)], at(5))
    text = format_digest(collect(store, cfg, at(1), at(5)), UTC)
    section = text.split("## New mail in work/INBOX since 2026-11-01 10:00 (3)\n\n")[1].split("\n\n---")[0]
    assert section.splitlines() == [
        "- 2026-11-04 10:00 · [me · request] · Sam Sender · Please review",
        "  - Sam wants a review",
        "- 2026-11-03 10:00 · bob@example.com · Lunch?",
        "",
        "FYI / noise (1):",
        "",
        "- 2026-11-02 10:00 · [nobody · noise] · News · Newsletter",
    ]


def test_clean_keeps_text_from_others_on_one_line():
    assert clean("  Disk\r\nfull\n\n\tagain ") == "Disk full again"
    assert sender_name('"Bob\nBuilder" <bob@example.com>') == "Bob Builder"
    assert sender_name("") == "(no sender)"


def test_format_digest_renders_every_section(store):
    cfg = make_config("[teams]\nenabled = true\n")
    store.record([mail("[OPS-1] new one", at(3)), mail("[OPS-2] x is now Done", at(3)), mail("Weekly report", at(3)),
                  mail("Hi", at(3), folder="INBOX")])
    store.mark_ok(Source.MAIL, at(3))
    store.mark_ok(Source.TEAMS, at(3))
    text = format_digest(collect(store, cfg, at(2), at(4)), UTC)
    for heading in ["# Tickets in focus, 2026-11-04 10:00", "## Open (1)", "### State unknown (1)",
                    "## New since 2026-11-02 10:00 (1)", "## Stale: no mail for 5 days or more (0)",
                    "## Closed since 2026-11-02 10:00 (1)", "## New mail in work/INBOX since 2026-11-02 10:00 (1)",
                    "## Unread in Teams (0)", "Last successful sync: 2026-11-03 10:00 · Unparsed subjects: 1"
                    " · Last Teams read: 2026-11-03 10:00", "- Weekly report"]:
        assert heading in text, heading
    assert text.endswith("\n")


def test_text_from_others_cannot_make_a_link_or_markup_in_the_digest(store, cfg):
    store.record([mail("[Reset your password](https://phish.example) <b>now</b>", at(3), "<p@x>", folder="INBOX",
                       sender="Mallory_* <m@example.com>")])
    text = format_digest(collect(store, cfg, at(1), at(5)), UTC)
    line = next(x for x in text.splitlines() if "Reset" in x)
    assert line == (r"- 2026-11-03 10:00 · Mallory\_\* · \[Reset your password\](https://phish.example) "
                    r"\<b\>now\</b\>")
