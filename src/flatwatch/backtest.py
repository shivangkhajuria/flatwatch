"""One-off backtest: what would flatwatch have sent over the past N days?

Deep-scrolls each source ONCE (approved for a 2-day backtest;
the live bot stays at ≤5 scrolls), runs the normal pipeline with the instant-alert age
window widened to N days, labels every Telegram message 🧪 BACKTEST, then sends a
backtest digest and a coverage summary. Posts are stored in the live DB, so the live
bot won't alert on them again.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from . import store
from .config import AppConfig
from .notify import Notifier, digest_messages, esc
from .pipeline import Ctx, process
from .telegram import Telegram

IST = ZoneInfo("Asia/Kolkata")
LABEL = "🧪 <b>BACKTEST</b>\n"


@dataclass
class LabelledNotifier(Notifier):
    async def send(self, text, markup=None, on_sent=None, *, silent=False):
        return await super().send(LABEL + text, markup, on_sent, silent=silent)

    async def edit(self, message_id, text, markup=None):
        return await super().edit(message_id, LABEL + text, markup)


@dataclass
class Coverage:
    source: str
    name: str
    posts: int = 0
    in_window: int = 0
    oldest: datetime | None = None
    scrolls: int | None = None
    error: str | None = None

    def line(self, cutoff: datetime) -> str:
        if self.error:
            return f"• {esc(self.name)}: ❌ {esc(self.error)}"
        reach = "full window" if self.oldest and self.oldest <= cutoff else (
            f"back to {self.oldest.astimezone(IST):%a %H:%M}" if self.oldest else "no posts")
        return f"• {esc(self.name)}: {self.in_window} posts · {reach} · {self.scrolls} scrolls"


async def run_backtest(cfg: AppConfig, conn, tg: Telegram, *, days: float, fb_max_scrolls: int,
                       reddit_max_scrolls: int) -> int:
    from .browser import BlockedError, detect_block, is_logged_in, open_context
    from .sources.facebook import fetch_group
    from .sources.reddit import fetch_sub

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    bt_cfg = cfg.model_copy(deep=True)
    bt_cfg.alerts.max_age_hours = int(days * 24) + 1
    notifier = LabelledNotifier(tg)
    await notifier.send(f"Starting a {days:g}-day backtest (back to {cutoff.astimezone(IST):%a %d %b %H:%M}). "
                        "Live polling is paused meanwhile; results follow.", silent=True)

    collected, coverage = [], []
    async with open_context(headless=True) as ctx:
        for source, keys in (("facebook", cfg.sources.facebook), ("reddit", cfg.sources.reddit)):
            if not keys:
                continue
            if reason := store.source_paused(conn, source):
                coverage.append(Coverage(source, source, error=f"paused: {reason}"))
                continue
            if not await is_logged_in(ctx, source):
                coverage.append(Coverage(source, source, error="not logged in"))
                continue
            for i, key in enumerate(keys):
                if i:
                    gap = random.uniform(90, 180) if source == "facebook" else random.uniform(20, 40)
                    logger.info("waiting {:.0f}s before next {}", gap, source)
                    await asyncio.sleep(gap)
                name = store.display_name(conn, source, key)
                cov = Coverage(source, name)
                run_id = store.start_run(conn, source, key)
                try:
                    if source == "facebook":
                        title, posts, stats = await fetch_group(
                            ctx, key, save_fixtures=True, detect_block=detect_block, known_until=cutoff,
                            max_scrolls=fb_max_scrolls, scroll_wait_ms=(6000, 10000))
                    else:
                        title, posts, stats = await fetch_sub(
                            ctx, key, save_fixtures=True, detect_block=detect_block,
                            max_scrolls=reddit_max_scrolls, until=cutoff)
                except BlockedError as e:
                    store.finish_run(conn, run_id, False, error=f"blocked: {e}")
                    store.pause_source(conn, source, str(e))
                    await notifier.send(f"⛔ {source} paused during backtest: {esc(str(e))}\n"
                                        f"Check the account, then /resume {source}.")
                    coverage.append(Coverage(source, name, error=f"blocked: {e}"))
                    break
                except Exception as e:
                    store.finish_run(conn, run_id, False, error=f"{type(e).__name__}: {e}")
                    coverage.append(Coverage(source, name, error=f"{type(e).__name__}"))
                    logger.exception("backtest fetch failed for {}", key)
                    continue
                store.finish_run(conn, run_id, True, seen=len(posts))
                newest = max((int(p.created_at.timestamp()) for p in posts if p.created_at), default=None)
                store.record_success(conn, source, key, n_posts=len(posts), newest=newest, display_name=title)
                cov.name = title or name
                cov.posts, cov.scrolls = len(posts), stats.get("scrolls")
                times = [p.created_at for p in posts if p.created_at]
                cov.oldest = min(times) if times else None
                window = [p for p in posts if p.created_at is None or p.created_at >= cutoff]
                cov.in_window = len(window)
                collected += window
                coverage.append(cov)
                logger.info("backtest {} {}: {} posts in window, oldest {}, {} scrolls",
                            source, cov.name, cov.in_window, cov.oldest, cov.scrolls)

    st = await process(collected, Ctx(conn, bt_cfg, notifier))
    logger.info("backtest pipeline: {}", st)

    rows = [r for r in store.pending_digest(conn, int(days * 86400) + 3600)]
    names = {r["post_id"]: store.display_name(conn, r["source"], r["source_key"]) for r in rows}
    for msg in digest_messages(rows, names, f"Digest, past {days:g} days"):
        await notifier.send(msg)
    store.mark_digested(conn, [r["decision_id"] for r in rows])

    already = conn.execute(
        """SELECT COUNT(*) FROM alerts a JOIN posts p ON p.id=a.post_id
           WHERE a.kind='instant' AND COALESCE(p.created_at, p.fetched_at) >= ?""",
        (int(cutoff.timestamp()),)).fetchone()[0]
    summary = [f"<b>Backtest summary, past {days:g} days</b>",
               f"{st.seen} posts fetched in the window · {st.new} not seen before · {st.duplicates} cross-posts · "
               f"{st.prefiltered} to the LLM",
               f"Instant alerts: {st.instant} new (+{already - st.instant} sent live earlier) · "
               f"digest items: {len(rows)}",
               "", "<b>Coverage</b>"] + [c.line(cutoff) for c in coverage]
    await notifier.send("\n".join(summary))
    print("\n".join(s.replace("<b>", "").replace("</b>", "") for s in summary))
    return 0
