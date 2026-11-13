import asyncio
import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from flatwatch import bot, db, scheduler, store
from flatwatch.config import EXAMPLE_CONFIG, Sources, load_config
from flatwatch.models import Decision, Listing, RawPost
from flatwatch.scheduler import Runner, due_digests, global_pause_until, is_night, next_interval_s

IST = ZoneInfo("Asia/Kolkata")
CFG = load_config(EXAMPLE_CONFIG).model_copy(update={"sources": Sources(
    facebook=[f"https://www.facebook.com/groups/{g}/" for g in ("111", "222", "333", "example-group")],
    reddit=["ExampleSubOne", "ExampleSubTwo"])})


def ist(h, m, day=12):
    return datetime(2026, 11, day, h, m, tzinfo=IST)


# ------------------------------------------------------------- timing --
@pytest.mark.parametrize("h,m,night", [(0, 59, False), (1, 0, True), (3, 30, True), (6, 29, True), (6, 30, False), (14, 0, False)])
def test_is_night(h, m, night):
    assert is_night(ist(h, m)) is night


def test_intervals_never_below_plan():
    rng = random.Random(0)
    for _ in range(500):
        fb = next_interval_s("facebook", ist(14, 0), rng)
        rd = next_interval_s("reddit", ist(14, 0), rng)
        nt = next_interval_s("facebook", ist(3, 0), rng)
        assert 12 * 60 <= fb <= 18 * 60
        assert 15 * 60 <= rd <= 20 * 60
        assert nt >= 45 * 60


def test_due_digests_catch_up_and_no_repeat():
    t = ["09:00", "21:00"]
    assert due_digests(ist(8, 59), t, set()) == []
    assert due_digests(ist(9, 0), t, set()) == ["2026-11-12 09:00"]
    assert due_digests(ist(10, 30), t, set()) == ["2026-11-12 09:00"]          # Mac woke up late
    assert due_digests(ist(10, 30), t, {"2026-11-12 09:00"}) == []
    assert due_digests(ist(12, 5), t, set()) == []                             # beyond 3h grace
    assert due_digests(ist(0, 30, 13), t, set()) == []                         # 3.5 h late: skipped
    assert due_digests(ist(0, 30, 13), t, set(), grace_h=4) == ["2026-11-12 21:00"]  # yesterday's slot


# --------------------------------------------------------------- fakes --
class FakeTG:
    chat_id = 42

    def __init__(self):
        self.sent, self.callbacks, self.markups = [], [], []

    async def send_message(self, text, reply_markup=None, silent=False):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    async def edit_message_text(self, message_id, text, reply_markup=None):
        return {"message_id": message_id}

    async def answer_callback_query(self, cid, text=None):
        self.callbacks.append(text)
        return True

    async def edit_message_reply_markup(self, message_id, markup, chat_id=None):
        self.markups.append(markup)
        return {}


def runner():
    return Runner(CFG, db.connect(":memory:"), FakeTG())


def cmd(text, chat=42):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


# ---------------------------------------------------------------- bot --
def test_pause_resume_commands():
    r = runner()
    asyncio.run(bot.handle_update(r, cmd("/pause 2")))
    assert global_pause_until(r.conn) and "paused for 2 h" in r.tg.sent[-1]
    asyncio.run(bot.handle_update(r, cmd("/resume")))
    assert global_pause_until(r.conn) is None
    asyncio.run(bot.handle_update(r, cmd("/pause")))
    assert global_pause_until(r.conn) == float("inf")


def test_resume_source_after_checkpoint():
    r = runner()
    store.pause_source(r.conn, "facebook", "checkpoint URL")
    asyncio.run(bot.handle_update(r, cmd("/status")))
    assert "PAUSED (checkpoint URL)" in r.tg.sent[-1]
    asyncio.run(bot.handle_update(r, cmd("/resume facebook")))
    assert store.source_paused(r.conn, "facebook") is None


def test_unknown_chat_ignored():
    r = runner()
    asyncio.run(bot.handle_update(r, cmd("/pause", chat=999)))
    assert r.tg.sent == [] and global_pause_until(r.conn) is None


def test_status_lists_every_source():
    r = runner()
    asyncio.run(bot.handle_update(r, cmd("/status")))
    s = r.tg.sent[-1]
    assert "facebook" in s and "r/ExampleSubOne" in s and "LLM this month" in s


def _seed_alert(r):
    p = RawPost(source="facebook", source_key="g", post_id="1", text="t", url="https://fb/p/1",
                created_at=datetime.now(timezone.utc))
    with r.conn:
        store.insert_post(r.conn, p, norm_text="t", phone=None, locality_hint=None, numbers_sig="",
                          duplicate_of=None, prefilter_pass=True)
        lst = Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="HSR", rent_inr=23000,
                      one_line="1BHK HSR 23k")
        ext = store.insert_extraction(r.conn, p.id, "m", "v3", lst)
        store.insert_decision(r.conn, p.id, ext, Decision(tier="P1"))
        return store.insert_alert(r.conn, p.id, "instant", 77, "text")


def test_feedback_button_stores_and_updates_markup():
    r = runner()
    aid = _seed_alert(r)
    u = {"update_id": 2, "callback_query": {"id": "cb1", "data": f"fb|{aid}|1",
                                            "message": {"message_id": 77, "chat": {"id": 42}}}}
    asyncio.run(bot.handle_update(r, u))
    assert r.conn.execute("SELECT relevant FROM feedback").fetchone()[0] == 1
    assert r.tg.callbacks and "✅ marked relevant" in str(r.tg.markups[-1])


def test_last_command():
    r = runner()
    _seed_alert(r)
    asyncio.run(bot.handle_update(r, cmd("/last 3")))
    assert "1BHK HSR 23k" in r.tg.sent[-1]


def test_digest_command_sends_and_marks():
    r = runner()
    p = RawPost(source="reddit", source_key="ExampleSubOne", post_id="z", text="t", url="https://r/z",
                created_at=datetime.now(timezone.utc))
    with r.conn:
        store.insert_post(r.conn, p, norm_text="t", phone=None, locality_hint=None, numbers_sig="",
                          duplicate_of=None, prefilter_pass=True)
        lst = Listing(intent="offering", offering_type="full_flat", unit="1bhk", locality="HSR", one_line="1BHK HSR")
        ext = store.insert_extraction(r.conn, p.id, "m", "v3", lst)
        store.insert_decision(r.conn, p.id, ext, Decision(tier="DIGEST", reasons=["rent unknown"]))
    asyncio.run(bot.handle_update(r, cmd("/digest")))
    assert "1BHK HSR" in r.tg.sent[-1] and "rent unknown" in r.tg.sent[-1]
    asyncio.run(bot.handle_update(r, cmd("/digest")))
    assert "nothing new" in r.tg.sent[-1]


# -------------------------------------------------------------- health --
def test_health_alert_once_then_recovery(monkeypatch):
    r = runner()
    r.started = time.time() - 2 * 3600                     # running for 2 h, never a success
    asyncio.run(r.health_check())
    asyncio.run(r.health_check())
    alerts = [s for s in r.tg.sent if "no successful" in s]
    assert len(alerts) == 2                                 # one per source, not repeated
    store.record_success(r.conn, "facebook", CFG.sources.facebook[0], n_posts=5, newest=None, display_name="G")
    asyncio.run(r.health_check())
    assert any("facebook fetching again" in s for s in r.tg.sent)


def test_health_quiet_when_paused():
    r = runner()
    r.started = time.time() - 2 * 3600
    store.kv_set(r.conn, "paused_until", "inf")
    asyncio.run(r.health_check())
    assert r.tg.sent == []


def test_backoff_ladder():
    conn = db.connect(":memory:")
    mins = [store.record_failure(conn, "facebook", "g", "x") for _ in range(6)]
    assert mins == [15, 30, 60, 120, 120, 120]
    assert store.in_backoff(conn, "facebook", "g")
    store.record_success(conn, "facebook", "g", n_posts=3, newest=None, display_name=None)
    assert store.in_backoff(conn, "facebook", "g") is None


def test_startup_delay_respects_min_interval():
    from flatwatch.scheduler import startup_delay_s
    conn = db.connect(":memory:")
    assert startup_delay_s(conn, "facebook") == 5                       # never fetched
    day = ist(14, 0).timestamp()
    store.record_success(conn, "facebook", "g", n_posts=1, newest=None, display_name=None)
    conn.execute("UPDATE source_state SET last_success_at=?", (int(day) - 30,))   # fetched 30 s ago, daytime
    d = startup_delay_s(conn, "facebook", now=day)
    assert 11 * 60 < d <= 12 * 60                                       # restart right after a cycle waits
    assert startup_delay_s(conn, "facebook", now=day + 3600) == 5


def test_gaps_counted_and_shown_in_summary():
    r = runner()
    rid = store.start_run(r.conn, "facebook", "g")
    store.finish_run(r.conn, rid, True, seen=15, gap=True)
    rid = store.start_run(r.conn, "facebook", "g")
    store.finish_run(r.conn, rid, True, seen=4)
    assert store.day_counts(r.conn, 0)["facebook"]["gaps"] == 1
    s = r.summary_text(0, "Today")
    assert "⚠️ 1 possible gap(s)" in s and "reddit:" in s and "possible gap" not in s.split("reddit:")[1]


def test_startup_delay_uses_night_interval_at_night():
    from flatwatch.scheduler import startup_delay_s
    conn = db.connect(":memory:")
    store.record_success(conn, "facebook", "g", n_posts=1, newest=None, display_name=None)
    last = store.last_success(conn, "facebook")
    night = ist(2, 0).timestamp()
    conn.execute("UPDATE source_state SET last_success_at=?", (int(night) - 20 * 60,))   # 20 min ago, at night
    assert 24 * 60 <= startup_delay_s(conn, "facebook", now=night) <= 25 * 60   # waits until 45 min
    day = ist(14, 0).timestamp()
    conn.execute("UPDATE source_state SET last_success_at=?", (int(day) - 20 * 60,))
    assert startup_delay_s(conn, "facebook", now=day) == 5                      # 12-min minimum already passed


def test_fetch_cycle_visits_every_group_and_processes(monkeypatch):
    # regression: a shadowed variable crashed the cycle after the first group, so posts were never processed
    import contextlib
    from flatwatch import browser
    from flatwatch.sources import facebook

    r = runner()
    visited, processed = [], []

    @contextlib.asynccontextmanager
    async def fake_ctx(**kw):
        yield object()

    async def logged_in(ctx, source):
        return True

    async def fake_fetch(ctx, key, **kw):
        visited.append(key)
        post = RawPost(source="facebook", source_key=key, post_id=f"p{len(visited)}", text="1BHK HSR 25k",
                       created_at=datetime.now(timezone.utc))
        return "G", [post], {"scrolls": 1, "gap": len(visited) == 1}

    async def no_sleep(s):
        return None

    async def fake_process(posts, ctx):
        processed.extend(posts)
        return {}

    monkeypatch.setattr(browser, "open_context", fake_ctx)
    monkeypatch.setattr(browser, "is_logged_in", logged_in)
    monkeypatch.setattr(facebook, "fetch_group", fake_fetch)
    monkeypatch.setattr(scheduler.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(scheduler, "process", fake_process)
    asyncio.run(r._fetch_cycle("facebook"))
    assert sorted(visited) == sorted(CFG.sources.facebook)
    assert len(processed) == len(CFG.sources.facebook)
    assert store.day_counts(r.conn, 0)["facebook"]["gaps"] == 1
