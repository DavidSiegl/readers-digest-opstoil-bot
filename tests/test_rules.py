import re

import pytest

from readers_digest.rules import Kind, Rule, Rules


def rule(name="r", pattern=r"^\[(?P<ticket>T-\d+)\] (?P<title>.*)$", **kw):
    return Rule(name, re.compile(pattern, re.IGNORECASE), **kw)


def test_first_matching_rule_wins_and_reads_the_groups():
    rules = Rules([rule("a", r"^\[(?P<ticket>T-\d+)\] (?P<title>.*) is (?P<state>\w+)$"), rule("b")])
    m = rules.match("[T-1]  Disk   full is Closed")
    assert (m.rule, m.ticket, m.title, m.state, m.kind) == ("a", "T-1", "Disk full", "Closed", Kind.UPDATE)
    assert rules.match("[T-2] Disk full").rule == "b"
    assert rules.match("nothing") is None


def test_fixed_state_and_category_fill_in_for_missing_groups():
    m = Rules([rule(state="Open", category="Ops")]).match("[T-1] x")
    assert (m.state, m.category) == ("Open", "Ops")


def test_sender_pattern_must_match_too():
    rules = Rules([rule(sender=re.compile(r"@tracker\.example$"))])
    assert rules.match("[T-1] x", "bot@tracker.example") is not None
    assert rules.match("[T-1] x", "someone@else.example") is None
    assert rules.match("[T-1] x", "Tracker Bot <BOT@Tracker.example>") is not None


def test_empty_group_counts_as_absent():
    m = Rules([rule(pattern=r"^\[(?P<ticket>T-\d+)\](?P<title>\s*)$")]).match("[T-1]  ")
    assert m.title is None


def test_a_rule_needs_a_ticket_group_and_an_update_or_reminder_kind():
    with pytest.raises(ValueError, match="no .*ticket"):
        Rules([rule(pattern=r"^x$")])
    with pytest.raises(ValueError, match="kind"):
        Rules([rule(kind=Kind.MAIL)])


def test_url_fills_in_the_ticket():
    rules = Rules([rule(url="https://t.example/{ticket}")])
    assert rules.url("r", "T-1") == "https://t.example/T-1"
    assert rules.url("gone", "T-1") is None


def test_fingerprint_changes_with_what_matching_depends_on_only():
    base = Rules([rule()]).fingerprint
    assert Rules([rule()]).fingerprint == base
    assert Rules([rule(url="https://x/{ticket}")]).fingerprint == base
    assert Rules([rule(state="Open")]).fingerprint != base
    assert Rules([rule(pattern=r"^\[(?P<ticket>T-\d+)\]")]).fingerprint != base
