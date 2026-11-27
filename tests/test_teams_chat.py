import json
from datetime import UTC, datetime, timedelta

from conftest import at

from readers_digest.store import Tag
from readers_digest.teams.chat import (
    MEETING_DAYS,
    Conversation,
    Message,
    conversations,
    display_title,
    link,
    messages,
    my_mri,
    review_key,
    to_me,
    unread,
)
from readers_digest.teams.shape import skeleton, url_pattern

ME = my_mri("00000000-aaaa")
BOB = "8:orgid:11111111-bbbb"


def chat(cid="19:c1@thread.v2", members=(ME, BOB), **kw) -> dict:
    return {"id": cid, "members": [{"mri": m} for m in members], "title": "", "isRead": False,
            "lastMessage": {"composeTime": "2026-11-05T10:00:00.000Z"}, **kw}


def raw(mid, when="2026-11-05T10:00:00.1234567Z", content="hi", kind="Text", sender=BOB, **kw) -> dict:
    return {"id": mid, "messagetype": kind, "content": content, "from": f"https://example.com/v1/users/ME/contacts/{sender}",
            "imdisplayname": " Bob Example ", "originalarrivaltime": when, **kw}


def conv(cid="c1", kind="group", unread_=True, read_at=None, title="") -> Conversation:
    return Conversation(cid, kind, title, at(5), None, unread_, read_at)


def msg(cid, mid, day, mine=False, mentions=False, name="Bob") -> Message:
    return Message(cid, mid, at(day), ME if mine else BOB, name, "hi", mine, mentions)


def test_conversations_keep_the_chats_the_reader_is_in_and_wants():
    me = {"chats": [
        chat("one", isOneOnOne=True, tenantId="t-1", consumptionHorizon={"originalArrivalTime": "1793872800000"}),
        chat("group", title=" Ops ", isRead=True),
        chat("left", members=(BOB,)),
        chat("muted", isMuted=True),
        chat("deleted", isConversationDeleted=True),
        chat("old-meeting", threadType="meeting", lastMessage={"composeTime": "2026-09-01T10:00:00Z"}),
        chat("meeting", threadType="meeting"),
    ]}
    got = {c.id: c for c in conversations(me, ME, at(6))}
    assert list(got) == ["one", "group", "meeting"]
    assert (got["one"].kind, got["one"].tenant, got["one"].unread) == ("1:1", "t-1", True)
    assert got["one"].read_at == datetime.fromtimestamp(1793872800, UTC)
    assert (got["group"].kind, got["group"].title, got["group"].unread, got["group"].read_at) == ("group", "Ops", False,
                                                                                                  None)
    assert got["meeting"].last_message_at == at(5)


def test_a_meeting_chat_counts_until_it_has_been_quiet_for_meeting_days():
    me = {"chats": [chat("m", threadType="meeting")]}
    assert conversations(me, ME, at(5) + timedelta(days=MEETING_DAYS - 1))
    assert not conversations(me, ME, at(5) + timedelta(days=MEETING_DAYS + 1))


def test_messages_keep_text_oldest_first_and_mark_mine_and_mentions():
    data = {"messages": [
        raw("3", "2026-11-05T12:00:00Z", "<p>Fish &amp; chips?</p><p>At <b>12</b></p>", "RichText/Html",
            properties={"mentions": json.dumps([{"mri": ME, "mentionType": "person"}])}),
        raw("2", "2026-11-05T11:00:00Z", "mine", sender=ME),
        raw("1", "2026-11-05T10:00:00.1234567Z", "plain"),
        raw("x", content="call ended", kind="Event/Call"),
        raw("d", content="gone", properties={"deletetime": "1793872800000"}),
        raw("e", content="<p> </p>", kind="RichText/Html"),
    ]}
    got = messages("c1", data, ME)
    assert [m.id for m in got] == ["1", "2", "3"]
    first, second, third = got
    assert (first.text, first.sender_id, first.sender_name, first.mine) == ("plain", BOB, "Bob Example", False)
    assert first.sent_at == datetime(2026, 11, 5, 10, 0, 0, 123456, UTC)
    assert second.mine and not second.mentions_me
    assert third.text == "Fish & chips?\n\nAt 12" and third.mentions_me


def test_messages_tolerate_a_mentions_field_that_is_not_json():
    [m] = messages("c1", {"messages": [raw("1", properties={"mentions": "{oops"})]}, ME)
    assert not m.mentions_me


def test_unread_takes_others_messages_after_the_read_position_most_recent_chat_first():
    convs = [conv("a", read_at=at(2)), conv("b"), conv("read", unread_=False), conv("caught-up", read_at=at(9))]
    msgs = [msg("a", "a1", 1), msg("a", "a3", 3), msg("a", "a4", 4, mine=True), msg("b", "b2", 2),
            msg("read", "r1", 6), msg("caught-up", "c1", 8)]
    got = unread(convs, msgs)
    assert [(u.conversation.id, [m.id for m in u.messages]) for u in got] == [("a", ["a3"]), ("b", ["b2"])]


def test_to_me_is_a_1to1_chat_or_a_mention_never_my_own():
    assert to_me(conv(kind="1:1"), msg("c1", "1", 1))
    assert to_me(conv(), msg("c1", "1", 1, mentions=True))
    assert not to_me(conv(), msg("c1", "1", 1))
    assert not to_me(conv(kind="1:1"), msg("c1", "1", 1, mine=True))


def test_review_key_names_the_latest_unread_message():
    [u] = unread([conv("c1")], [msg("c1", "1", 1), msg("c1", "2", 2)])
    assert review_key(u) == "teams:c1:2"


def test_review_key_is_the_key_the_store_keeps_teams_tags_under(store):
    c = conv("19:c1@thread.v2")
    store.record_teams([c], [msg(c.id, "1700000000001", 1)])
    [u] = unread(*store.teams())
    store.record_tags([Tag(review_key(u), "me", "fyi", "", None), Tag("teams:gone:1", "me", "fyi", "", None)], at(2))
    store.prune(at(1))  # keeps every stored message, so only tags of no stored entry go
    assert set(store.tags()) == {review_key(u)}


def test_link_opens_the_chat_with_its_tenant():
    c = Conversation("19:c1@thread.v2", "group", "", None, "t 1")
    assert link(c) == "https://teams.microsoft.com/l/chat/19%3Ac1%40thread.v2/conversations?tenantId=t%201"
    assert link(Conversation("c1", "group", "", None)) == "https://teams.microsoft.com/l/chat/c1/conversations"


def test_display_title_names_the_others_by_their_latest_message():
    assert display_title(conv(title="Ops"), []) == "Ops"
    ms = [msg("c1", "1", 1, name="Ann"), msg("c1", "2", 2, name="Bob"), msg("c1", "3", 3, mine=True, name="Me")]
    assert display_title(conv(), ms) == "Bob, Ann"
    assert display_title(conv(kind="1:1"), []) == "1:1 chat"


def test_skeleton_keeps_structure_and_hides_text():
    v = {"messagetype": "RichText/Html", "content": "Hi Bob, 42!", "n": 7, "ok": True, "none": None,
         "items": [{"title": "x1"}, {"title": "y2"}], "empty": []}
    assert skeleton(v) == {"messagetype": "RichText/Html", "content": "aa aaa, 99!", "n": "int", "ok": True,
                           "none": None, "items": {"<list>": 2, "first": {"title": "a9"}}, "empty": []}


def test_skeleton_cuts_long_text_and_deep_nesting():
    assert skeleton("x" * 100) == "a" * 80 + "…"
    deep: dict = {}
    d = deep
    for _ in range(12):
        d["k"] = {}
        d = d["k"]
    s, depth = skeleton(deep), 0
    while isinstance(s, dict):
        s, depth = s["k"], depth + 1
    assert s == "dict" and depth == 9


def test_url_pattern_hides_ids_and_query_values():
    url = ("https://teams.example.com/api/chatsvc/emea/v1/users/ME/conversations/19%3Aabc%40thread.v2/messages"
           "?view=msnp24Equivalent&pageSize=200")
    assert url_pattern(url) == ("https://teams.example.com/api/chatsvc/emea/v1/users/ME/conversations/{id}/messages"
                                "?view=…&pageSize=…")
    assert url_pattern("https://example.com/api/users/12345678-aaaa-bbbb") == "https://example.com/api/users/{id}"
    assert url_pattern("https://example.com/api/v3/teams/users/me") == "https://example.com/api/v3/teams/users/me"
