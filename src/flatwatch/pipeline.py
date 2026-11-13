"""New posts -> dedupe -> prefilter -> LLM extract -> match -> alert / digest.

    await process(posts, ctx)   # one call per poll cycle

Everything is stored (posts, extractions, decisions) for evals, including drops.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from loguru import logger

from . import store
from .config import AppConfig
from .dedupe import WINDOW_S, Fingerprint, find_original, fingerprint
from .extract import PROMPT_VERSION, AuthError, ExtractError, ExtractResult, extract_batch, fallback_decision
from .match import match
from .models import Decision, RawPost
from .notify import Notifier, alert_markup, alert_text, with_also_in
from .prefilter import prefilter

Extractor = Callable[[list[RawPost], AppConfig], Awaitable[ExtractResult]]


@dataclass
class Ctx:
    conn: sqlite3.Connection
    cfg: AppConfig
    notifier: Notifier
    extractor: Extractor = extract_batch
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


@dataclass
class CycleStats:
    seen: int = 0
    new: int = 0
    duplicates: int = 0
    prefiltered: int = 0
    extracted: int = 0
    fallback: int = 0
    instant: int = 0
    digest: int = 0


async def process(posts: list[RawPost], ctx: Ctx) -> CycleStats:
    conn, cfg = ctx.conn, ctx.cfg
    st = CycleStats(seen=len(posts))
    # de-duplicate within the batch itself (same post seen twice in one cycle)
    uniq: dict[str, RawPost] = {}
    for p in posts:
        if p.id not in uniq or len(p.text) > len(uniq[p.id].text):
            uniq[p.id] = p
    known = store.known_ids(conn, list(uniq))
    fresh = [p for p in uniq.values() if p.id not in known]
    st.new = len(fresh)
    if not fresh:
        return st

    # oldest first, so the earliest copy of a cross-post becomes the original
    fresh.sort(key=lambda p: p.created_at or ctx.now())
    candidates: list[tuple[str, Fingerprint]] = [
        (r["id"], Fingerprint(r["norm_text"] or "", r["phone"], r["locality_hint"], r["numbers_sig"] or ""))
        for r in store.dedupe_candidates(conn, store.now() - WINDOW_S)
    ]
    to_llm: list[RawPost] = []
    with conn:
        for p in fresh:
            fp = fingerprint(p.text, cfg.criteria) if p.text.strip() else None
            original = find_original(fp, candidates) if fp else None
            ok, _reason = prefilter(p, cfg.criteria)
            store.insert_post(conn, p, norm_text=fp.norm if fp else "", phone=fp.phone if fp else None,
                              locality_hint=fp.locality if fp else None, numbers_sig=fp.numbers if fp else "",
                              duplicate_of=original, prefilter_pass=None if original else ok)
            if original:
                st.duplicates += 1
                await _note_crosspost(ctx, original, p)
                continue
            if fp:
                candidates.append((p.id, fp))
            if ok:
                to_llm.append(p)
    st.prefiltered = len(to_llm)

    bs = cfg.extract.batch_size
    for k in range(0, len(to_llm), bs):
        await _extract_and_route(to_llm[k : k + bs], ctx, st)
    return st


async def _extract_and_route(batch: list[RawPost], ctx: Ctx, st: CycleStats) -> None:
    conn, cfg = ctx.conn, ctx.cfg
    try:
        res = await ctx.extractor(batch, cfg)
    except ExtractError as e:
        await _llm_degraded(ctx, e)
        for p in batch:
            with conn:
                ext_id = store.insert_extraction(conn, p.id, cfg.extract.model, PROMPT_VERSION, None, str(e)[:300])
                d = fallback_decision(p, cfg.criteria)
                store.insert_decision(conn, p.id, ext_id, d)
            st.fallback += 1
            st.digest += d.tier == "DIGEST"
        return
    await _llm_ok(ctx)
    with conn:
        for u in res.usage:
            store.insert_llm_usage(conn, u.backend, u.model, u.posts, u.input_tokens, u.output_tokens, u.cost_usd)
    for p in batch:
        listing = res.listings.get(p.id)
        with conn:
            if listing is None:
                ext_id = store.insert_extraction(conn, p.id, res.model, res.prompt_version, None, "no valid listing")
                d = fallback_decision(p, cfg.criteria)
                st.fallback += 1
            else:
                ext_id = store.insert_extraction(conn, p.id, res.model, res.prompt_version, listing)
                d = match(listing, cfg.criteria)
                st.extracted += 1
            store.insert_decision(conn, p.id, ext_id, d)
        if d.tier in ("P1", "P2") and listing is not None:
            if _too_old(p, ctx):
                logger.info("{} matches {} but is older than {}h; not alerting", p.id, d.tier, cfg.alerts.max_age_hours)
                continue
            await _send_alert(ctx, p, listing, d)
            st.instant += 1
        elif d.tier == "DIGEST":
            st.digest += 1


def _too_old(p: RawPost, ctx: Ctx) -> bool:
    return p.created_at is not None and ctx.now() - p.created_at > timedelta(hours=ctx.cfg.alerts.max_age_hours)


async def _send_alert(ctx: Ctx, p: RawPost, listing, d: Decision) -> None:
    conn = ctx.conn
    group = store.display_name(conn, p.source, p.source_key)
    text = alert_text(p, listing, d, group, ctx.now())
    with conn:
        alert_id = store.insert_alert(conn, p.id, "instant", None, text)
    logger.info("ALERT {} {} {}", d.tier, p.id, listing.one_line)

    def on_sent(msg: dict) -> None:
        store.set_alert_msg_id(conn, alert_id, msg["message_id"])

    await ctx.notifier.send(text, alert_markup(p.url, alert_id), on_sent=on_sent)


async def _note_crosspost(ctx: Ctx, original_id: str, dup: RawPost) -> None:
    """Edit the original alert to add 'also in: <group>' instead of alerting again."""
    conn = ctx.conn
    alert = store.instant_alert_for(conn, original_id)
    orig = store.get_post(conn, original_id)
    if alert is None or orig is None or orig["source_key"] == dup.source_key:
        return
    names = store.add_also_in(conn, alert["id"], store.display_name(conn, dup.source, dup.source_key))
    if names and alert["telegram_msg_id"]:
        new_text = with_also_in(alert["text"], names)
        with conn:
            conn.execute("UPDATE alerts SET text=? WHERE id=?", (new_text, alert["id"]))
        await ctx.notifier.edit(alert["telegram_msg_id"], new_text, alert_markup(orig["url"], alert["id"]))


async def _llm_degraded(ctx: Ctx, err: Exception) -> None:
    logger.error("LLM extraction failed, using regex fallback: {}", err)
    if store.kv_get(ctx.conn, "llm_degraded"):
        return
    store.kv_set(ctx.conn, "llm_degraded", str(store.now()))
    hint = "\nRun <code>claude auth login</code> on the Mac." if isinstance(err, AuthError) else ""
    await ctx.notifier.send(
        "⚠️ <b>flatwatch: LLM unavailable</b>\nMatching falls back to keywords. Matches go to the digest "
        f"marked unverified, never as instant alerts.{hint}"
    )


async def _llm_ok(ctx: Ctx) -> None:
    if store.kv_get(ctx.conn, "llm_degraded"):
        store.kv_set(ctx.conn, "llm_degraded", None)
        await ctx.notifier.send("✅ flatwatch: LLM extraction is working again.", silent=True)
