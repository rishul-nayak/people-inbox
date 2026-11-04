"""End to end: Telegram updates in, Notion rows and Telegram replies out (all fakes)."""
import json

import pytest

import bot as botmod
from bot import Bot
from digest import build
from extract import QuotaExhausted
from notion_client import prop
from store import Store
from tests.fakes import OWNER, FakeExtractor, FakeNotion, FakeTelegram, button, cfg, msg

TODAY = "2026-11-04"
NEHA = {"people": [{"name": "Neha Sharma", "company": "Razorpay", "role": "PM", "how_met": "AI meetup",
                    "cares_about": "Hiring PMs for AI payments", "common_ground": "Both from Jabalpur", "mutual_contacts": "Ankit Jain introduced us",
                    "ideas_to_help": "Intro to a strong AI engineer"}],
        "summary": "Met at AI meetup", "type": "Event", "date": TODAY,
        "promises": [{"who": "me", "person": "Neha", "what": "Send my pricing teardown", "due": ""}], "question": "",
        "follow_up_draft": "Hi Neha, great meeting you at the AI meetup. Sending my pricing teardown tomorrow. — Rishul"}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(Bot, "today", lambda self: TODAY)


def make(updates, script, owner=str(OWNER), state=None):
    c = cfg()
    n, tg = FakeNotion(), FakeTelegram(updates)
    b = Bot(tg, Store(n, c), FakeExtractor(c, script), c, owner, state if state is not None else {})
    return b, n, tg, c


def people(n):
    return [p for p in n.rows("people_ds_id")]


def test_text_note_creates_person_interaction_promise():
    b, n, tg, c = make([msg(1, "Met Neha Sharma from Razorpay at the AI meetup...")], [NEHA])
    assert b.run()["saved"] == 1
    [p] = people(n)
    assert prop(p, "Name") == "Neha Sharma" and prop(p, "Tier") == "Tier 1"
    assert prop(p, "Last contact") == TODAY and prop(p, "Next contact") == "2026-11-05"   # 24-hour follow-up first
    assert prop(p, "Common ground") == "Both from Jabalpur" and prop(p, "Mutual contacts") == "Ankit Jain introduced us"
    [i] = n.rows("interactions_ds_id")
    assert prop(i, "Status") == "Saved" and prop(i, "People") == [p["id"]]
    [pr] = n.rows("promises_ds_id")
    assert prop(pr, "Who") == "Me" and prop(pr, "Person") == [p["id"]] and prop(pr, "Due") == "2026-11-06"
    card = tg.sent[-1]
    assert "Neha Sharma" in card["text"] and "new · Tier 1 · next 5 Nov" in card["text"] and "due 6 Nov" in card["text"]
    assert "🎁 For Neha Sharma: Intro to a strong AI engineer" in card["text"]
    assert "<pre>Hi Neha, great meeting you" in card["text"]
    assert card["buttons"][-1][0][0] == "↩️ Undo"
    assert tg.acked == [1]


def test_second_note_updates_same_person_and_undo_restores():
    first = dict(NEHA, date="2026-10-20")
    later = dict(NEHA, promises=[], people=[{"name": "Neha Sharma", "personal_notes": "Runs marathons", "company": "Razorpay"}])
    b, n, tg, c = make([msg(1, "note 1")], [first, later])
    b.run()
    tg.queue = [msg(2, "note 2")]
    b.run()
    [p] = people(n)                                                  # no duplicate
    assert prop(p, "Last contact") == TODAY and prop(p, "Next contact") == "2026-12-04"    # then Tier 1 = 30 days
    assert prop(p, "Personal notes") == "[2026-11-04] Runs marathons"
    assert "updated" in tg.sent[-1]["text"]
    undo = tg.sent[-1]["buttons"][-1][0][1]
    tg.queue = [button(3, undo)]
    b.run()
    p = n.get_page(p["id"])
    assert prop(p, "Last contact") == "2026-10-20" and prop(p, "Next contact") == "2026-10-21"
    assert prop(p, "Personal notes") == ""   # back to before
    assert "Undone" in tg.edits[-1]["text"]


def test_undo_new_person_trashes_everything():
    b, n, tg, c = make([msg(1, "note")], [NEHA])
    b.run()
    tg.queue = [button(2, tg.sent[-1]["buttons"][-1][0][1])]
    b.run()
    assert people(n) == [] and n.rows("promises_ds_id") == []


def test_maybe_duplicate_then_merge():
    first = dict(NEHA, promises=[])
    vague = {"people": [{"name": "Neha", "personal_notes": "Loves chess"}], "summary": "Call", "type": "Call",
             "date": TODAY, "promises": [{"who": "them", "person": "Neha", "what": "Intro to her CTO", "due": ""}]}
    b, n, tg, c = make([msg(1, "a")], [first, vague])
    b.run()
    tg.queue = [msg(2, "b")]
    b.run()
    assert len(people(n)) == 2
    card = tg.sent[-1]
    assert "is this Neha Sharma?" in card["text"]
    same_btn = card["buttons"][0][0]
    assert same_btn[0] == "Same as Neha Sharma"
    tg.queue = [button(3, same_btn[1])]
    b.run()
    [p] = people(n)
    assert prop(p, "Name") == "Neha Sharma" and prop(p, "Personal notes") == "[2026-11-04] Loves chess"
    [pr] = n.rows("promises_ds_id")
    assert prop(pr, "Person") == [p["id"]] and prop(pr, "Who") == "Them"
    assert "Same as" not in json.dumps(tg.edits[-1]["buttons"])


def test_quota_saves_pending_then_retries_next_run():
    b, n, tg, c = make([msg(1, "", voice={"file_id": "F1", "mime_type": "audio/ogg"})],
                       [QuotaExhausted("429"), QuotaExhausted("429"), NEHA])
    b.run()
    [i] = n.rows("interactions_ds_id")
    assert prop(i, "Status") == "Pending" and "busy" in tg.sent[-1]["text"]
    b.run()                                                          # still busy: stays pending, tries = 1
    assert json.loads(prop(n.get_page(i["id"]), "Data"))["tries"] == 1
    b.run()                                                          # now it works, same row is reused
    [i] = n.rows("interactions_ds_id")
    assert prop(i, "Status") == "Saved" and len(people(n)) == 1
    assert b.ex.calls[-1]["media"] == [(b"OggS-fake-audio", "audio/ogg")]


def test_strangers_are_ignored_and_setup_reply_shows_id():
    stranger = msg(1, "hi")
    stranger["message"]["from"]["id"] = 999
    b, n, tg, c = make([stranger], [])
    b.run()
    assert tg.sent == [] and tg.acked == [1]
    b2, n2, tg2, c2 = make([msg(1, "hi")], [], owner=None)
    b2.run()
    assert "Your Telegram ID is <code>111</code>" in tg2.sent[0]["text"]


def test_no_person_found_saves_nothing():
    b, n, tg, c = make([msg(1, "buy milk")], [{"people": [], "summary": "x", "type": "Note", "promises": []}])
    b.run()
    assert n.pages == {} and "couldn't find a person" in tg.sent[-1]["text"]


def test_reply_and_forward_and_event_reach_gemini():
    state = {}
    b, n, tg, c = make([msg(1, "/event AI Summit")], [NEHA], state=state)
    b.run()
    assert state["event"]["name"] == "AI Summit"
    fwd = msg(2, "Hey, great meeting you!", forward_origin={"type": "user", "sender_user": {"first_name": "Neha"}},
              reply_to_message={"text": "✅ Saved · Event"})
    tg.queue = [fwd]
    b.run()
    call = b.ex.calls[-1]
    assert call["event"] == "AI Summit" and "Forwarded message from Neha" in call["text"] and "earlier note" in call["text"]
    assert prop(n.rows("interactions_ds_id")[0], "Event") == "AI Summit"


def test_who_and_promises_commands():
    b, n, tg, c = make([msg(1, "note")], [NEHA])
    b.run()
    tg.queue = [msg(2, "/who neha"), msg(3, "/promises")]
    b.run()
    who, proms = tg.sent[-2]["text"], tg.sent[-1]["text"]
    assert "Neha Sharma" in who and "Hiring PMs" in who and "You owe: Send my pricing teardown" in who
    assert "Mutual contacts:</b> Ankit Jain introduced us" in who
    assert "You owe" in proms and "Neha Sharma: Send my pricing teardown" in proms


def test_one_bad_update_does_not_stop_the_run():
    b, n, tg, c = make([msg(1, "a"), msg(2, "b")], [RuntimeError("boom"), NEHA])
    counts = b.run()
    assert counts["errors"] == 1 and counts["saved"] == 1 and tg.acked == [1, 2]


def test_digest_lists_due_promises_and_yesterdays_people():
    yesterday = dict(NEHA, date="2026-11-03", promises=[{"who": "me", "person": "Neha", "what": "Send deck", "due": "2026-11-03"}])
    b, n, tg, c = make([msg(1, "note")], [yesterday])
    b.run()
    text = build(b.store, TODAY)
    assert "Send deck · 3 Nov (late)" in text and "Met yesterday" in text and "Neha Sharma" in text
    assert "Ping today" not in text            # Neha is already in "met yesterday"
    assert build(Store(FakeNotion(), c), TODAY) is None


def test_digest_pings_allies_first_with_a_reason():
    c = cfg()
    n = FakeNotion()
    s = Store(n, c)
    for name, tier, nxt in [("Tara", "Tier 2", "2026-11-01"), ("Arjun", "Ally", "2026-11-04"), ("Zoya", "Tier 1", "2026-11-02"),
                            ("Late Later", "Tier 1", "2026-12-01"), ("Mom", "Inner circle", "2026-11-01")]:
        n.create_page("people_ds_id", {"Name": {"title": [{"type": "text", "text": {"content": name}}]},
                                       "Tier": {"select": {"name": tier}}, "Next contact": {"date": {"start": nxt}},
                                       "Cares about": {"rich_text": [{"type": "text", "text": {"content": "[2026-10-01] Raising a seed round"}}]}})
    text = build(s, TODAY)
    lines = [l for l in text.splitlines() if l.startswith("• ")]
    assert [l.split(" · ")[0][2:] for l in lines] == ["Arjun", "Zoya", "Tara"]     # Ally, Tier 1, Tier 2; max 3
    assert lines[0].endswith(": Raising a seed round") and "Mom" not in text and "Late Later" not in text


def test_card_buttons_fit_telegram_limit():
    c = {"id": "1234abcd-1234-1234-1234-123456789abc", "maybe": [{"k": 0, "names": ["A" * 40, "B"]}]}
    for row in botmod.card_buttons(c):
        for _, data in row:
            assert len(data.encode()) <= 64
