import asyncio
import json
from collections import deque
from datetime import date, datetime, timedelta, timezone

import pytest

from flatwatch.models import Decision, Listing, RawPost
from flatwatch.notify import Notifier, ago, alert_markup, alert_text, digest_messages, money, with_also_in
from flatwatch.telegram import TelegramError

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def test_money():
    assert money(32000) == "₹32k" and money(150000) == "₹1.5L" and money(200000) == "₹2L"
    assert money(13250) == "₹13.2k" and money(None) is None


def test_ago():
    assert ago(NOW - timedelta(minutes=6), NOW) == "6 min ago"
    assert ago(NOW - timedelta(hours=5), NOW) == "5 h ago"
    assert ago(None, NOW) == "time unknown"


def test_alert_text_layout():
    post = RawPost(source="facebook", source_key="g", post_id="1",
                   text="🏠 𝗦𝗽𝗮𝗰𝗶𝗼𝘂𝘀 1BHK for rent near 12th Main <b>",
                   created_at=NOW - timedelta(minutes=6))
    lst = Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="Indiranagar",
                  locality_raw="HAL 2nd Stage", rent_inr=32000, deposit_inr=150000, furnishing="semi",
                  available_from=date(2026, 12, 1), balcony=True, gender_pref="any", broker=False,
                  contact="9000012345", one_line="Spacious 1BHK near 12th Main")
    t = alert_text(post, lst, Decision(tier="P1", tags=["balcony"]), "Flats & Flatmates <BLR>", NOW)
    lines = t.split("\n")
    assert lines[0] == "<b>🟢 1BHK · Indiranagar (HAL 2nd Stage) · ₹32k</b>"     # rent in the header
    assert lines[1] == "Semi-furnished · deposit ₹1.5L · from 1 Dec · 🌿 balcony"
    assert lines[2] == "👥 open to anyone · ✅ no brokerage · 📞 9000012345"
    assert lines[3] == ""                                                           # facts / post text separator
    assert lines[4] == "<i>🏠 Spacious 1BHK for rent near 12th Main &lt;b&gt;</i>"  # real excerpt, bold folded, escaped
    assert lines[5] == "Flats &amp; Flatmates &lt;BLR&gt; · 6 min ago · P1 whole flat"


def test_locality_shown_once():
    post = RawPost(source="facebook", source_key="g", post_id="1", text="x", created_at=NOW)
    lst = Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="HSR",
                  locality_raw="HSR Layout Sector 1", rent_inr=31500)
    assert alert_text(post, lst, Decision(tier="P1"), "G", NOW).split("\n")[0] == "<b>🟢 1BHK · HSR Layout Sector 1 · ₹31.5k</b>"


def test_long_excerpt_cut_on_word():
    post = RawPost(source="facebook", source_key="g", post_id="1", text="word " * 100, created_at=NOW)
    lst = Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="HSR", rent_inr=20000)
    body = alert_text(post, lst, Decision(tier="P1"), "G", NOW).split("\n")[-2]
    assert body.endswith(" …</i>") and len(body) <= 130


def test_p2_alert_shows_room_gender_bath():
    post = RawPost(source="reddit", source_key="s", post_id="1", text="x", created_at=NOW)
    lst = Listing(intent="offering", offering_type="flatmate", unit="2bhk", locality="HSR", rent_inr=16000,
                  gender_pref="male", private_bathroom=True)
    t = alert_text(post, lst, Decision(tier="P2"), "r/x", NOW)
    assert t.startswith("<b>🔵 Room in 2BHK · HSR · ₹16k</b>")
    assert "🚿 private bathroom" in t and "👨 men only" in t and t.endswith("P2 room")


def test_markup_and_also_in():
    m = alert_markup("https://fb/p/1", 7)
    assert m["inline_keyboard"][0][0]["url"] == "https://fb/p/1"
    assert m["inline_keyboard"][1][0]["callback_data"] == "fb|7|1"
    t = with_also_in("base", ["Group A"])
    assert t == "base\n↪ also in: Group A"
    assert with_also_in(t, ["Group A", "Group B"]) == "base\n↪ also in: Group A, Group B"


def test_digest_chunks_and_balcony_first():
    rows = []
    for i in range(60):
        lst = Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="HSR", rent_inr=None,
                      one_line="x" * 80)
        rows.append({"tags": json.dumps(["balcony"] if i == 59 else []), "reasons": json.dumps(["rent unknown"]),
                     "created_at": int(NOW.timestamp()), "listing_json": lst.model_dump_json(),
                     "url": f"https://fb/{i}", "post_id": f"facebook:{i}", "text": ""})
    msgs = digest_messages(rows, {}, "Digest")
    assert len(msgs) > 1 and all(len(m) <= 4096 for m in msgs)
    assert "🌿" in msgs[0].split("\n•")[1]          # balcony item sorted first
    assert digest_messages([], {}, "Digest") == []


class FakeTG:
    def __init__(self, fail_times=0):
        self.fail_times, self.sent = fail_times, []

    async def send_message(self, text, reply_markup=None, silent=False):
        if self.fail_times:
            self.fail_times -= 1
            raise TelegramError("sendMessage: network error", network=True)
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    async def edit_message_text(self, message_id, text, reply_markup=None):
        self.sent.append(("edit", message_id, text))
        return {"message_id": message_id}


def test_notifier_queues_on_outage_and_flushes_in_order():
    async def go():
        tg = FakeTG(fail_times=3)
        n = Notifier(tg)
        got = []
        assert await n.send("a", on_sent=lambda m: got.append(m["message_id"])) is None
        assert await n.send("b") is None         # queued behind a, not sent out of order
        assert len(n.outbox) == 2
        assert await n.flush() == 0              # still down (2nd failure)
        assert await n.flush() == 0              # 3rd failure
        assert await n.flush() == 2
        assert tg.sent == ["a", "b"] and got == [1] and not n.outbox
    asyncio.run(go())


def test_notifier_drops_permanent_errors():
    class Bad(FakeTG):
        async def send_message(self, text, reply_markup=None, silent=False):
            raise TelegramError("sendMessage: 400 Bad Request: can't parse entities")

    async def go():
        n = Notifier(Bad())
        assert await n.send("<b>") is None and not n.outbox
    asyncio.run(go())


def test_silent_flag_reaches_telegram():
    class TG(FakeTG):
        async def send_message(self, text, reply_markup=None, silent=False):
            self.sent.append((text, silent))
            return {"message_id": 1}

    async def go():
        tg = TG()
        n = Notifier(tg)
        await n.send("status", silent=True)
        await n.send("alert")
        assert tg.sent == [("status", True), ("alert", False)]
    asyncio.run(go())
