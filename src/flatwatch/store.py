"""All SQL for the running bot lives here (schema in db.py)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import Decision, Listing, RawPost

IST = ZoneInfo("Asia/Kolkata")
WHOLE_SOURCE = "*"   # source_state row that pauses an entire source


def now() -> int:
    return int(time.time())


# ------------------------------------------------------------------- posts --

def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    q = f"SELECT id FROM posts WHERE id IN ({','.join('?' * len(ids))})"
    return {r[0] for r in conn.execute(q, ids)}


def insert_post(conn, post: RawPost, *, norm_text: str, phone: str | None, locality_hint: str | None,
                numbers_sig: str, duplicate_of: str | None, prefilter_pass: bool | None) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO posts(id, source, source_key, post_id, author, text, url, image_urls, created_at,
           fetched_at, norm_text, phone, duplicate_of, prefilter_pass, locality_hint, numbers_sig)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (post.id, post.source, post.source_key, post.post_id, post.author, post.text, post.url,
         json.dumps(post.image_urls), int(post.created_at.timestamp()) if post.created_at else None, now(),
         norm_text, phone, duplicate_of, None if prefilter_pass is None else int(prefilter_pass),
         locality_hint, numbers_sig),
    )


def dedupe_candidates(conn, since: int) -> list[sqlite3.Row]:
    """Originals (not themselves duplicates) seen since `since`."""
    return conn.execute(
        """SELECT id, source_key, norm_text, phone, locality_hint, numbers_sig FROM posts
           WHERE fetched_at >= ? AND duplicate_of IS NULL""",
        (since,),
    ).fetchall()


def get_post(conn, post_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()


# -------------------------------------------------------- extraction/match --

def insert_extraction(conn, post_id: str, model: str, prompt_version: str,
                      listing: Listing | None, error: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO extractions(post_id, model, prompt_version, listing_json, error, created_at) VALUES (?,?,?,?,?,?)",
        (post_id, model, prompt_version, listing.model_dump_json() if listing else None, error, now()),
    )
    return cur.lastrowid


def insert_decision(conn, post_id: str, extraction_id: int | None, d: Decision) -> int:
    cur = conn.execute(
        "INSERT INTO decisions(post_id, extraction_id, tier, reasons, tags, unverified, created_at) VALUES (?,?,?,?,?,?,?)",
        (post_id, extraction_id, d.tier, json.dumps(d.reasons), json.dumps(d.tags), int(d.unverified), now()),
    )
    return cur.lastrowid


def insert_llm_usage(conn, backend: str, model: str, posts: int, input_tokens, output_tokens, cost_usd) -> None:
    conn.execute(
        "INSERT INTO llm_usage(backend, model, posts, input_tokens, output_tokens, cost_usd, created_at) VALUES (?,?,?,?,?,?,?)",
        (backend, model, posts, input_tokens, output_tokens, cost_usd, now()),
    )


def month_spend_usd(conn) -> tuple[float, int]:
    start = datetime.now(IST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    r = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM llm_usage WHERE created_at >= ?", (int(start.timestamp()),)
    ).fetchone()
    return float(r[0]), int(r[1])


# ------------------------------------------------------------------ alerts --

def insert_alert(conn, post_id: str, kind: str, msg_id: int | None, text: str) -> int:
    cur = conn.execute(
        "INSERT INTO alerts(post_id, kind, telegram_msg_id, sent_at, text, also_in) VALUES (?,?,?,?,?, '[]')",
        (post_id, kind, msg_id, now(), text),
    )
    return cur.lastrowid


def set_alert_msg_id(conn, alert_id: int, msg_id: int) -> None:
    with conn:
        conn.execute("UPDATE alerts SET telegram_msg_id=? WHERE id=?", (msg_id, alert_id))


def instant_alert_for(conn, post_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alerts WHERE post_id=? AND kind='instant' ORDER BY id LIMIT 1", (post_id,)
    ).fetchone()


def add_also_in(conn, alert_id: int, name: str) -> list[str] | None:
    """Append a group name; returns the new list, or None if it was already there."""
    row = conn.execute("SELECT also_in FROM alerts WHERE id=?", (alert_id,)).fetchone()
    names = json.loads(row["also_in"] or "[]")
    if name in names:
        return None
    names.append(name)
    conn.execute("UPDATE alerts SET also_in=? WHERE id=?", (json.dumps(names), alert_id))
    return names


def get_alert(conn, alert_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()


def last_alerts(conn, n: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.id, a.sent_at, a.kind, p.url, d.tier, e.listing_json FROM alerts a
           JOIN posts p ON p.id=a.post_id
           LEFT JOIN decisions d ON d.post_id=a.post_id
           LEFT JOIN extractions e ON e.id=d.extraction_id
           WHERE a.kind='instant' GROUP BY a.id ORDER BY a.id DESC LIMIT ?""",
        (n,),
    ).fetchall()


def insert_feedback(conn, alert_id: int, relevant: bool) -> None:
    a = get_alert(conn, alert_id)
    if a is None:
        return
    with conn:
        conn.execute("INSERT INTO feedback(post_id, alert_id, relevant, created_at) VALUES (?,?,?,?)",
                     (a["post_id"], alert_id, int(relevant), now()))


def pending_digest(conn, max_age_s: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.id AS decision_id, d.reasons, d.tags, d.unverified, p.id AS post_id, p.url, p.text,
                  p.source, p.source_key, p.created_at, e.listing_json
           FROM decisions d JOIN posts p ON p.id=d.post_id
           LEFT JOIN extractions e ON e.id=d.extraction_id
           WHERE d.tier='DIGEST' AND d.digest_sent_at IS NULL AND COALESCE(p.created_at, p.fetched_at) >= ?
           ORDER BY p.created_at DESC""",
        (now() - max_age_s,),
    ).fetchall()


def mark_digested(conn, decision_ids: list[int]) -> None:
    if decision_ids:
        with conn:
            conn.execute(f"UPDATE decisions SET digest_sent_at=? WHERE id IN ({','.join('?' * len(decision_ids))})",
                         [now(), *decision_ids])


# ------------------------------------------------------------ source state --

def _ensure_state(conn, source: str, key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO source_state(source, source_key) VALUES (?,?)", (source, key))


def get_state(conn, source: str, key: str) -> sqlite3.Row:
    with conn:
        _ensure_state(conn, source, key)
    return conn.execute("SELECT * FROM source_state WHERE source=? AND source_key=?", (source, key)).fetchone()


def source_paused(conn, source: str) -> str | None:
    r = conn.execute("SELECT pause_reason FROM source_state WHERE source=? AND source_key=? AND paused=1",
                     (source, WHOLE_SOURCE)).fetchone()
    return r["pause_reason"] if r else None


def pause_source(conn, source: str, reason: str) -> None:
    with conn:
        _ensure_state(conn, source, WHOLE_SOURCE)
        conn.execute("UPDATE source_state SET paused=1, pause_reason=? WHERE source=? AND source_key=?",
                     (reason, source, WHOLE_SOURCE))


def resume_source(conn, source: str) -> bool:
    with conn:
        n = conn.execute(
            """UPDATE source_state SET paused=0, pause_reason=NULL, backoff_level=0, backoff_until=NULL
               WHERE source=? AND (source_key=? OR paused=1 OR backoff_until IS NOT NULL)""",
            (source, WHOLE_SOURCE),
        ).rowcount
    return n > 0


BACKOFF_MIN = [15, 30, 60, 120]


def record_failure(conn, source: str, key: str, error: str) -> int:
    """Bump exponential backoff (15→30→60→120 min cap). Returns minutes until retry."""
    st = get_state(conn, source, key)
    level = min(st["backoff_level"], len(BACKOFF_MIN) - 1)
    minutes = BACKOFF_MIN[level]
    with conn:
        conn.execute("UPDATE source_state SET backoff_level=?, backoff_until=? WHERE source=? AND source_key=?",
                     (level + 1, now() + minutes * 60, source, key))
    return minutes


def record_success(conn, source: str, key: str, *, n_posts: int, newest: int | None, display_name: str | None) -> int:
    """Clear backoff; returns the new consecutive-empty-fetch count."""
    st = get_state(conn, source, key)
    empty = st["consecutive_empty"] + 1 if n_posts == 0 else 0
    with conn:
        conn.execute(
            """UPDATE source_state SET backoff_level=0, backoff_until=NULL, last_success_at=?, consecutive_empty=?,
                   last_seen_post_time=MAX(COALESCE(last_seen_post_time, 0), COALESCE(?, 0)),
                   display_name=COALESCE(?, display_name)
               WHERE source=? AND source_key=?""",
            (now(), empty, newest, display_name or None, source, key),
        )
    return empty


def in_backoff(conn, source: str, key: str) -> int | None:
    st = get_state(conn, source, key)
    return st["backoff_until"] if st["backoff_until"] and st["backoff_until"] > now() else None


def display_name(conn, source: str, key: str) -> str:
    r = conn.execute("SELECT display_name FROM source_state WHERE source=? AND source_key=?", (source, key)).fetchone()
    if r and r["display_name"]:
        return r["display_name"]
    return f"r/{key}" if source == "reddit" else key


def last_success(conn, source: str) -> int | None:
    r = conn.execute("SELECT MAX(last_success_at) FROM source_state WHERE source=? AND source_key<>?",
                     (source, WHOLE_SOURCE)).fetchone()
    return r[0]


def states(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source_state ORDER BY source, source_key").fetchall()


# --------------------------------------------------------------------- runs --

def start_run(conn, source: str, key: str) -> int:
    cur = conn.execute("INSERT INTO runs(source, source_key, started_at) VALUES (?,?,?)", (source, key, now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, ok: bool, seen: int = 0, new: int = 0, error: str | None = None,
               gap: bool = False) -> None:
    with conn:
        conn.execute("UPDATE runs SET finished_at=?, ok=?, posts_seen=?, posts_new=?, error=?, gap=? WHERE id=?",
                     (now(), int(ok), seen, new, error[:500] if error else None, int(gap), run_id))


def day_counts(conn, since: int) -> dict[str, dict[str, int]]:
    """Per-source counts since `since`: scanned, new, prefiltered, extracted, alerted, digest."""
    out: dict[str, dict[str, int]] = {}
    for src in ("facebook", "reddit"):
        c = {}
        c["fetches"] = conn.execute("SELECT COUNT(*) FROM runs WHERE source=? AND started_at>=? AND ok=1",
                                    (src, since)).fetchone()[0]
        c["gaps"] = conn.execute("SELECT COUNT(*) FROM runs WHERE source=? AND started_at>=? AND gap=1",
                                 (src, since)).fetchone()[0]
        c["scanned"] = conn.execute("SELECT COALESCE(SUM(posts_seen),0) FROM runs WHERE source=? AND started_at>=?",
                                    (src, since)).fetchone()[0]
        c["new"] = conn.execute("SELECT COUNT(*) FROM posts WHERE source=? AND fetched_at>=?", (src, since)).fetchone()[0]
        c["prefiltered"] = conn.execute("SELECT COUNT(*) FROM posts WHERE source=? AND fetched_at>=? AND prefilter_pass=1",
                                        (src, since)).fetchone()[0]
        c["llm"] = conn.execute(
            """SELECT COUNT(*) FROM extractions e JOIN posts p ON p.id=e.post_id
               WHERE p.source=? AND e.created_at>=? AND e.listing_json IS NOT NULL""", (src, since)).fetchone()[0]
        c["alerted"] = conn.execute(
            """SELECT COUNT(*) FROM alerts a JOIN posts p ON p.id=a.post_id
               WHERE p.source=? AND a.sent_at>=? AND a.kind='instant'""", (src, since)).fetchone()[0]
        c["digest"] = conn.execute(
            """SELECT COUNT(*) FROM decisions d JOIN posts p ON p.id=d.post_id
               WHERE p.source=? AND d.created_at>=? AND d.tier='DIGEST'""", (src, since)).fetchone()[0]
        out[src] = c
    return out


# ----------------------------------------------------------------------- kv --

def kv_get(conn, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(conn, key: str, value: str | None) -> None:
    with conn:
        if value is None:
            conn.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            conn.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))


def ts_to_dt(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
