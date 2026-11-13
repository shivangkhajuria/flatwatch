import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from flatwatch import db, store
from flatwatch.config import EXAMPLE_CONFIG, load_config
from flatwatch.extract import AuthError, ExtractResult, Usage
from flatwatch.models import Listing, RawPost
from flatwatch.notify import Notifier
from flatwatch.pipeline import Ctx, process

CFG = load_config(EXAMPLE_CONFIG)
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
P1_TEXT = "Spacious 1BHK for rent in HAL 2nd Stage Indiranagar, 28k rent, deposit 1.5L, balcony, owner post"
P2_TEXT = "Male flatmate needed in 2BHK at HSR Sector 7, attached bathroom, rent 16k, deposit 40k, DM me"


class FakeTG:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, text, reply_markup=None, silent=False):
        self.sent.append((text, reply_markup))
        return {"message_id": 100 + len(self.sent)}

    async def edit_message_text(self, message_id, text, reply_markup=None):
        self.edits.append((message_id, text))
        return {"message_id": message_id}


def P(pid, text, key="https://www.facebook.com/groups/a/", age_min=5, images=()):
    return RawPost(source="facebook", source_key=key, post_id=pid, text=text,
                   url=f"{key}posts/{pid}/", image_urls=list(images), created_at=NOW - timedelta(minutes=age_min))


def listing_for(text):
    if "1BHK" in text:
        return Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="Indiranagar",
                       rent_inr=28000, balcony=True, one_line="1BHK HAL 2nd Stage 28k")
    if "flatmate" in text:
        return Listing(intent="offering", offering_type="flatmate", unit="2bhk", locality="HSR", rent_inr=16000,
                       gender_pref="male", private_bathroom=True, one_line="Room in 2BHK HSR 16k")
    return Listing(intent="seeking")


def make_ctx(extractor=None):
    conn = db.connect(":memory:")
    tg = FakeTG()
    calls = []

    async def fake_extract(posts, cfg):
        calls.append([p.id for p in posts])
        return ExtractResult({p.id: listing_for(p.text) for p in posts}, [],
                             [Usage("agent_sdk", "m", len(posts), 10, 5, 0.001)], model="m")

    ctx = Ctx(conn, CFG, Notifier(tg), extractor or fake_extract, now=lambda: NOW)
    return ctx, tg, calls


def test_instant_alerts_and_storage():
    ctx, tg, calls = make_ctx()
    st = asyncio.run(process([P("1", P1_TEXT), P("2", P2_TEXT), P("3", "Selling my projector, 1 month old")], ctx))
    assert (st.new, st.prefiltered, st.instant) == (3, 2, 2)
    assert calls == [["facebook:1", "facebook:2"]]          # one batched LLM call, projector skipped
    assert len(tg.sent) == 2 and tg.sent[0][0].startswith("<b>🟢") and tg.sent[0][0].endswith("P1 whole flat") and tg.sent[1][0].endswith("P2 room")
    a = store.instant_alert_for(ctx.conn, "facebook:1")
    assert a["telegram_msg_id"] == 101
    assert ctx.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2
    assert ctx.conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1
    assert ctx.conn.execute("SELECT prefilter_pass FROM posts WHERE id='facebook:3'").fetchone()[0] == 0


def test_no_duplicate_alerts_on_refetch():
    ctx, tg, calls = make_ctx()
    asyncio.run(process([P("1", P1_TEXT)], ctx))
    st = asyncio.run(process([P("1", P1_TEXT)], ctx))
    assert st.new == 0 and len(tg.sent) == 1 and len(calls) == 1


def test_crosspost_edits_original_instead_of_alerting():
    ctx, tg, calls = make_ctx()
    store.record_success(ctx.conn, "facebook", "https://www.facebook.com/groups/b/", n_posts=1, newest=None,
                         display_name="Group B")
    asyncio.run(process([P("1", P1_TEXT)], ctx))
    st = asyncio.run(process([P("9", P1_TEXT + " 🙏", key="https://www.facebook.com/groups/b/")], ctx))
    assert st.duplicates == 1 and st.instant == 0 and len(tg.sent) == 1 and len(calls) == 1
    assert tg.edits and tg.edits[0][0] == 101 and "also in: Group B" in tg.edits[0][1]
    # a third copy in the same group B doesn't edit again
    asyncio.run(process([P("10", P1_TEXT + "!!", key="https://www.facebook.com/groups/b/")], ctx))
    assert len(tg.edits) == 1


def test_old_posts_stored_but_not_alerted():
    ctx, tg, _ = make_ctx()
    st = asyncio.run(process([P("1", P1_TEXT, age_min=60 * 30)], ctx))
    assert st.instant == 0 and tg.sent == []
    assert ctx.conn.execute("SELECT tier FROM decisions").fetchone()[0] == "P1"


def test_llm_failure_falls_back_to_unverified_digest_and_warns_once():
    async def broken(posts, cfg):
        raise AuthError("Claude login problem")

    ctx, tg, _ = make_ctx(broken)
    st = asyncio.run(process([P("1", P1_TEXT), P("2", P2_TEXT)], ctx))
    assert st.fallback == 2 and st.instant == 0 and st.digest == 2
    assert len(tg.sent) == 1 and "LLM unavailable" in tg.sent[0][0] and "claude auth login" in tg.sent[0][0]
    rows = ctx.conn.execute("SELECT tier, unverified FROM decisions").fetchall()
    assert all(r["tier"] == "DIGEST" and r["unverified"] == 1 for r in rows)
    asyncio.run(process([P("3", P1_TEXT.replace("28k", "29k"))], ctx))
    assert len(tg.sent) == 1                                 # warned only once

    # recovery message when the LLM comes back
    ctx2_extract = make_ctx()[0].extractor
    ctx.extractor = ctx2_extract
    asyncio.run(process([P("4", P2_TEXT.replace("16k", "17k"))], ctx))
    assert any("working again" in t for t, _ in tg.sent)


def test_digest_tier_not_sent_instantly():
    ctx, tg, _ = make_ctx()

    async def digesty(posts, cfg):
        return ExtractResult({p.id: Listing(intent="offering", offering_type="full_flat", unit="1bhk",
                                            locality="HSR", rent_inr=33000) for p in posts}, [], [], model="m")

    ctx.extractor = digesty
    st = asyncio.run(process([P("1", "1BHK available in HSR Layout, 33k, slightly over budget")], ctx))
    assert st.digest == 1 and tg.sent == []
    assert len(store.pending_digest(ctx.conn, 86400 * 3)) == 1


def test_post_without_rent_is_never_sent():
    ctx, tg, _ = make_ctx()

    async def no_rent(posts, cfg):
        return ExtractResult({p.id: Listing(intent="offering", offering_type="full_flat", unit="1bhk",
                                            locality="HSR", rent_inr=None) for p in posts}, [], [], model="m")

    ctx.extractor = no_rent
    st = asyncio.run(process([P("1", "1BHK available in HSR Layout, DM for rent and photos")], ctx))
    assert st.instant == 0 and st.digest == 0 and tg.sent == []
    assert store.pending_digest(ctx.conn, 86400 * 3) == []
