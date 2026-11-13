"""The long-running bot (PLAN.md §4, Phase 5).

Loops (all in one asyncio process):
  facebook   every 12–18 min (45 min in 01:00–06:30 IST), groups visited one by one with 20–60 s gaps
  reddit     every 15–20 min (45 min at night)
  digest     09:00 and 21:00 IST (+ daily summary at 21:00)
  health     every 5 min: no successful fetch for 60 min -> one alert per incident (+ recovery note)
  telegram   /status /pause [h] /resume [source] /digest /last [n], 👍/👎 buttons
  outbox     re-sends Telegram messages queued during an outage

Account safety: intervals are constants here, matching PLAN.md §1. Don't lower them.
A checkpoint / login wall / block page pauses the whole source and alerts; nothing retries it.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from . import store
from .config import AppConfig, auth_source
from .notify import Notifier, digest_messages, esc
from .pipeline import Ctx, process
from .telegram import Telegram, TelegramError

IST = ZoneInfo("Asia/Kolkata")

FB_INTERVAL_MIN = (12, 18)
REDDIT_INTERVAL_MIN = (15, 20)
NIGHT_INTERVAL_MIN = 45
NIGHT = ((1, 0), (6, 30))          # IST
FB_GROUP_GAP_S = (20, 60)
REDDIT_SUB_GAP_S = (8, 20)
HEARTBEAT_MIN = 60
HEALTH_EVERY_S = 300
EMPTY_FETCHES_ALERT = 3            # consecutive 0-post fetches of one group -> "parser broken?"


def is_night(t: datetime) -> bool:
    t = t.astimezone(IST)
    return NIGHT[0] <= (t.hour, t.minute) < NIGHT[1]


def next_interval_s(source: str, t: datetime | None = None, rng: random.Random | None = None) -> float:
    rng = rng or random
    t = t or datetime.now(timezone.utc)
    if is_night(t):
        return NIGHT_INTERVAL_MIN * 60 + rng.uniform(0, 120)
    lo, hi = FB_INTERVAL_MIN if source == "facebook" else REDDIT_INTERVAL_MIN
    return rng.uniform(lo, hi) * 60


def due_digests(now_ist: datetime, times: list[str], sent: set[str], grace_h: float = 3) -> list[str]:
    """Digest slots (\"YYYY-MM-DD HH:MM\") that are due now and not yet sent (catch-up within grace_h)."""
    due = []
    for day in (now_ist.date() - timedelta(days=1), now_ist.date()):
        for hhmm in times:
            h, m = map(int, hhmm.split(":"))
            slot = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
            key = f"{day.isoformat()} {hhmm}"
            if slot <= now_ist < slot + timedelta(hours=grace_h) and key not in sent:
                due.append(key)
    return due


def startup_delay_s(conn, source: str, now: float | None = None) -> float:
    """Never start a cycle sooner than the minimum interval after the last successful one,
    so restarts (or launchd crash loops) can't hammer the sites."""
    now = now or time.time()
    floor = 5 if source == "facebook" else 90          # stagger the two sources
    last = store.last_success(conn, source)
    if not last:
        return floor
    if is_night(datetime.fromtimestamp(now, tz=timezone.utc)):
        lo = NIGHT_INTERVAL_MIN * 60
    else:
        lo = (FB_INTERVAL_MIN if source == "facebook" else REDDIT_INTERVAL_MIN)[0] * 60
    return max(floor, last + lo - now)


def global_pause_until(conn) -> float | None:
    v = store.kv_get(conn, "paused_until")
    if v is None:
        return None
    if v == "inf":
        return float("inf")
    if float(v) <= time.time():
        store.kv_set(conn, "paused_until", None)
        return None
    return float(v)


class Runner:
    def __init__(self, cfg: AppConfig, conn: sqlite3.Connection, tg: Telegram, *, headless: bool = True):
        self.cfg, self.conn, self.tg = cfg, conn, tg
        self.notifier = Notifier(tg)
        self.pctx = Ctx(conn, cfg, self.notifier)
        self.browser_lock = asyncio.Lock()
        self.headless = headless
        self.started = time.time()

    # ------------------------------------------------------------ fetching --

    async def _fetch_cycle(self, source: str) -> None:
        from .browser import BlockedError, detect_block, is_logged_in, open_context
        from .sources.facebook import fetch_group
        from .sources.reddit import fetch_sub

        conn = self.conn
        keys = list(self.cfg.sources.facebook if source == "facebook" else self.cfg.sources.reddit)
        random.shuffle(keys)
        visit_gap = FB_GROUP_GAP_S if source == "facebook" else REDDIT_SUB_GAP_S
        collected = []
        async with self.browser_lock:
            async with open_context(headless=self.headless) as ctx:
                if not await is_logged_in(ctx, source):
                    await self._pause(source, "not logged in (session expired?)")
                    return
                visited = 0
                for key in keys:
                    if store.source_paused(conn, source):
                        return
                    if until := store.in_backoff(conn, source, key):
                        logger.info("{} {} in backoff for {} more min", source, key, (until - store.now()) // 60)
                        continue
                    if visited:
                        await asyncio.sleep(random.uniform(*visit_gap))
                    visited += 1
                    run_id = store.start_run(conn, source, key)
                    try:
                        if source == "facebook":
                            st = store.get_state(conn, source, key)
                            title, posts, stats = await fetch_group(
                                ctx, key, save_fixtures=False, detect_block=detect_block,
                                known_until=store.ts_to_dt(st["last_seen_post_time"]))
                        else:
                            title, posts, stats = await fetch_sub(ctx, key, save_fixtures=False,
                                                                  detect_block=detect_block)
                    except BlockedError as e:
                        store.finish_run(conn, run_id, False, error=f"blocked: {e}")
                        await self._pause(source, str(e), key)
                        return
                    except Exception as e:
                        mins = store.record_failure(conn, source, key, str(e))
                        store.finish_run(conn, run_id, False, error=f"{type(e).__name__}: {e}")
                        logger.warning("{} {} failed ({}: {}); backoff {} min", source, key, type(e).__name__,
                                       str(e)[:200], mins)
                        continue
                    newest = max((int(p.created_at.timestamp()) for p in posts if p.created_at), default=None)
                    empty = store.record_success(conn, source, key, n_posts=len(posts), newest=newest,
                                                 display_name=title)
                    had_gap = bool(stats.get("gap"))
                    store.finish_run(conn, run_id, True, seen=len(posts), gap=had_gap)
                    if had_gap:
                        logger.warning("{} {}: possible gap: {} posts loaded in {} scrolls but none reached "
                                       "the last seen post; some posts may have been missed",
                                       source, title or key, len(posts), stats.get("scrolls"))
                    logger.info("{} {}: {} posts ({})", source, title or key, len(posts),
                                ", ".join(f"{k}={v}" for k, v in stats.items() if k != "fixture_dir"))
                    if empty == EMPTY_FETCHES_ALERT:
                        await self.notifier.send(
                            f"⚠️ <b>flatwatch</b>: {esc(title or key)} returned 0 posts {empty} times in a row. "
                            "The page structure may have changed; check the logs.")
                    collected += posts
        # browser released; LLM work happens outside the lock
        if collected:
            st = await process(collected, self.pctx)
            logger.info("{} cycle: {}", source, st)

    async def _pause(self, source: str, reason: str, key: str | None = None) -> None:
        store.pause_source(self.conn, source, reason)
        logger.error("{} PAUSED: {} ({})", source, reason, key or "")
        await self.notifier.send(
            f"⛔ <b>flatwatch: {source} paused</b>\n{esc(reason)}" + (f"\n{esc(key)}" if key else "")
            + f"\nNothing will retry. Check the account in the bot's Chrome profile "
              f"(<code>flatwatch login {source}</code>), then send /resume {source}.")

    async def fetch_loop(self, source: str) -> None:
        keys = self.cfg.sources.facebook if source == "facebook" else self.cfg.sources.reddit
        if not keys:
            return
        await asyncio.sleep(startup_delay_s(self.conn, source))
        while True:
            try:
                if global_pause_until(self.conn):
                    logger.info("{}: globally paused", source)
                elif reason := store.source_paused(self.conn, source):
                    logger.info("{}: paused ({})", source, reason)
                else:
                    await self._fetch_cycle(source)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("{} cycle crashed", source)
            wait = next_interval_s(source)
            logger.info("{}: next cycle in {:.1f} min", source, wait / 60)
            await asyncio.sleep(wait)

    # -------------------------------------------------------------- digest --

    async def send_digest(self, title: str = "Digest", *, force: bool = False) -> int:
        rows = store.pending_digest(self.conn, self.cfg.alerts.digest_max_age_days * 86400)
        if not rows:
            if force:
                await self.notifier.send("📭 Digest: nothing new.")
            return 0
        names = {r["post_id"]: store.display_name(self.conn, r["source"], r["source_key"]) for r in rows}
        for msg in digest_messages(rows, names, title):
            await self.notifier.send(msg)
        store.mark_digested(self.conn, [r["decision_id"] for r in rows])
        return len(rows)

    def summary_text(self, since: int, title: str) -> str:
        c = store.day_counts(self.conn, since)
        spend, calls = store.month_spend_usd(self.conn)
        lines = [f"<b>{esc(title)}</b>"]
        for src, d in c.items():
            lines.append(f"{src}: {d['fetches']} fetches · {d['scanned']} scanned · {d['new']} new · "
                         f"{d['prefiltered']} prefiltered · {d['llm']} LLM'd · {d['alerted']} alerted · {d['digest']} digest"
                         + (f" · ⚠️ {d['gaps']} possible gap(s)" if d.get("gaps") else ""))
        lines.append(f"LLM this month: ${spend:.3f} over {calls} calls")
        return "\n".join(lines)

    async def digest_loop(self) -> None:
        while True:
            try:
                now_ist = datetime.now(IST)
                sent = set(filter(None, (store.kv_get(self.conn, "digests_sent") or "").split("|")))
                for slot in due_digests(now_ist, self.cfg.alerts.digest_times, sent):
                    n = await self.send_digest(f"Digest {slot[-5:]}")
                    if slot.endswith(self.cfg.alerts.digest_times[-1]):
                        midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
                        await self.notifier.send(self.summary_text(int(midnight.timestamp()), "Daily summary"),
                                                 silent=True)
                    sent.add(slot)
                    logger.info("digest {} sent ({} items)", slot, n)
                    store.kv_set(self.conn, "digests_sent", "|".join(sorted(sent)[-10:]))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("digest loop error")
            await asyncio.sleep(60)

    # -------------------------------------------------------------- health --

    async def health_check(self) -> None:
        if global_pause_until(self.conn):
            return
        for source in ("facebook", "reddit"):
            if not getattr(self.cfg.sources, source) or store.source_paused(self.conn, source):
                continue
            last = max(store.last_success(self.conn, source) or 0, self.started)
            stale_min = (time.time() - last) / 60
            flag = f"health_alert:{source}"
            if stale_min > HEARTBEAT_MIN and not store.kv_get(self.conn, flag):
                store.kv_set(self.conn, flag, str(store.now()))
                logger.error("{}: no successful fetch for {:.0f} min", source, stale_min)
                await self.notifier.send(f"⚠️ <b>flatwatch</b>: no successful {source} fetch for "
                                         f"{stale_min:.0f} min. Network down, Mac asleep or a fetch error loop.")
            elif stale_min <= HEARTBEAT_MIN and store.kv_get(self.conn, flag):
                store.kv_set(self.conn, flag, None)
                await self.notifier.send(f"✅ flatwatch: {source} fetching again.", silent=True)

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_EVERY_S)
            try:
                await self.health_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("health loop error")

    # ------------------------------------------------------------ telegram --

    async def bot_loop(self) -> None:
        from .bot import handle_update

        offset = int(store.kv_get(self.conn, "tg_offset") or 0) or None
        while True:
            try:
                updates = await self.tg.get_updates(offset=offset, timeout=50)
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                logger.warning("getUpdates failed: {}", e)
                await asyncio.sleep(30)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                store.kv_set(self.conn, "tg_offset", str(offset))
                try:
                    await handle_update(self, u)
                except Exception:
                    logger.exception("error handling telegram update")

    # ----------------------------------------------------------------- run --

    async def run(self) -> None:
        msg = (f"▶️ <b>flatwatch started</b>\n{len(self.cfg.sources.facebook)} FB groups · "
               f"{len(self.cfg.sources.reddit)} subreddits · LLM: {esc(auth_source(self.cfg))}")
        for src in ("facebook", "reddit"):
            if r := store.source_paused(self.conn, src):
                msg += f"\n⛔ {src} is paused ({esc(r)}): /resume {src}"
        await self.notifier.send(msg, silent=True)
        tasks = [asyncio.create_task(c, name=n) for n, c in [
            ("facebook", self.fetch_loop("facebook")), ("reddit", self.fetch_loop("reddit")),
            ("digest", self.digest_loop()), ("health", self.health_loop()),
            ("bot", self.bot_loop()), ("outbox", self.notifier.run_flusher()),
        ]]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
