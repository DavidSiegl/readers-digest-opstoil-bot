import subprocess

from conftest import at, mail, make_config

from readers_digest.notify import decide, send
from readers_digest.store import Source
from readers_digest.teams.chat import Conversation, Message


def run(store, cfg, now, since=None):
    """One timer run: decide, then mark what it announced, as `show --notify` does after sending."""
    line, keys = decide(store, cfg, since or at(1), now)
    store.mark_announced(keys, now)
    return line


def test_a_sync_failure_is_announced_once_per_outage(store, cfg):
    store.mark_failed(Source.MAIL, at(2), "IMAP: down")
    assert run(store, cfg, at(2)) == "mail failed: IMAP: down"
    store.mark_failed(Source.MAIL, at(3), "IMAP: still down")
    assert run(store, cfg, at(3)) is None
    store.mark_ok(Source.MAIL, at(4))
    assert run(store, cfg, at(4)) is None
    store.mark_failed(Source.MAIL, at(5), "IMAP: down again")
    assert run(store, cfg, at(5)) == "mail failed: IMAP: down again"


def test_new_focus_tickets_with_a_known_category_are_announced_once(store, cfg):
    store.record([mail("[OPS-1] old", at(1)), mail("[OPS-2] new", at(3)), mail("[OPS-3] newer", at(4)),
                  mail("[DEV-1] not mine", at(3)), mail("Reminder: OPS-4", at(3))])
    assert run(store, cfg, at(5), since=at(2)) == "2 new tickets (OPS-2, OPS-3)"
    assert run(store, cfg, at(6), since=at(2)) is None
    store.record([mail("[OPS-5] another", at(6))])
    assert run(store, cfg, at(7), since=at(2)) == "1 new ticket (OPS-5)"


def test_more_than_three_new_tickets_are_summed_up(store, cfg):
    store.record([mail(f"[OPS-{i}] t", at(2)) for i in range(1, 6)])
    assert run(store, cfg, at(3)) == "5 new tickets (OPS-1, OPS-2, OPS-3 and 2 more)"


def test_teams_messages_to_the_reader_are_announced_once_per_new_message(store):
    cfg = make_config("[teams]\nenabled = true\n")

    def msg(cid, mid, day, mentions_me=False):
        return Message(cid, mid, at(day), "8:orgid:bob", "Bob Example", "ping", False, mentions_me)

    convs = [Conversation("dm", "1:1", "", at(3), unread=True), Conversation("grp", "group", "Ops chat", at(3),
                                                                             unread=True)]
    store.record_teams(convs, [msg("dm", "1", 2), msg("grp", "2", 2)])
    assert run(store, cfg, at(3)) == "Teams: 1 chat for you (Bob Example)"
    assert run(store, cfg, at(4)) is None
    store.record_teams(convs, [msg("dm", "1", 2), msg("grp", "2", 2), msg("grp", "3", 4, mentions_me=True)])
    assert run(store, cfg, at(5)) == "Teams: 1 chat for you (Ops chat)"


def test_teams_is_not_announced_when_teams_is_off(store, cfg):
    store.record_teams([Conversation("dm", "1:1", "", at(3), unread=True)],
                       [Message("dm", "1", at(2), "8:orgid:bob", "Bob", "ping", False, False)])
    assert decide(store, cfg, at(1), at(3)) == (None, [])


def test_send_escapes_markup_and_keeps_the_line_on_one_line():
    argv = []
    send("Teams: 1 chat for you (<b>URGENT</b>\n& more)", run=lambda a, **kw: argv.extend(a) or
         subprocess.CompletedProcess(a, 0, "", ""))
    assert argv[-1] == "Teams: 1 chat for you (&lt;b&gt;URGENT&lt;/b&gt; &amp; more)"
