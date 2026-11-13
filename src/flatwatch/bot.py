"""Telegram commands and 👍/👎 feedback (PLAN.md §4.7). Only the configured chat is obeyed."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

from . import store
from .models import Listing
from .notify import ago, esc, money

if TYPE_CHECKING:
    from .scheduler import Runner

IST = ZoneInfo("Asia/Kolkata")
HELP = ("/status: sources, today's counts, LLM spend\n/pause [hours]: stop fetching (default until /resume)\n"
        "/resume [facebook|reddit]: resume fetching, or clear a source paused by a checkpoint\n"
        "/digest: send the digest now\n/last [n]: last n instant alerts")


async def handle_update(r: "Runner", u: dict) -> None:
    if cq := u.get("callback_query"):
        await _feedback(r, cq)
        return
    msg = u.get("message") or {}
    if (msg.get("chat") or {}).get("id") != r.tg.chat_id:
        if msg:
            logger.warning("ignoring message from unknown chat {}", (msg.get("chat") or {}).get("id"))
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    reply = await COMMANDS.get(cmd, _help)(r, args)
    if reply:
        await r.notifier.send(reply)


async def _help(r, args) -> str:
    return HELP


async def _status(r: "Runner", args) -> str:
    conn = r.conn
    lines = ["<b>flatwatch status</b>"]
    from .scheduler import global_pause_until

    if (p := global_pause_until(conn)) is not None:
        lines.append("⏸ paused " + ("until /resume" if p == float("inf") else
                                     f"until {datetime.fromtimestamp(p, IST):%H:%M}"))
    for src in ("facebook", "reddit"):
        keys = getattr(r.cfg.sources, src)
        if not keys:
            continue
        if reason := store.source_paused(conn, src):
            lines.append(f"⛔ {src}: PAUSED ({esc(reason)})")
            continue
        lines.append(f"<b>{src}</b>")
        for k in keys:
            st = store.get_state(conn, src, k)
            last = st["last_success_at"]
            when = ago(store.ts_to_dt(last)) if last else "never"
            extra = ""
            if st["backoff_until"] and st["backoff_until"] > store.now():
                extra = f" · backoff {(st['backoff_until'] - store.now()) // 60} min"
            lines.append(f"  {esc(store.display_name(conn, src, k))}: {when}{extra}")
    midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    lines.append("")
    lines.append(r.summary_text(int(midnight.timestamp()), "Today").replace("<b>Today</b>", "<b>Today</b>"))
    if r.notifier.outbox:
        lines.append(f"{len(r.notifier.outbox)} message(s) waiting to send")
    lines.append(f"up {int((time.time() - r.started) // 3600)} h {int((time.time() - r.started) % 3600 // 60)} min")
    return "\n".join(lines)


async def _pause(r: "Runner", args) -> str:
    if args:
        try:
            hours = float(args[0])
        except ValueError:
            return "usage: /pause [hours]"
        store.kv_set(r.conn, "paused_until", str(time.time() + hours * 3600))
        return f"⏸ paused for {hours:g} h"
    store.kv_set(r.conn, "paused_until", "inf")
    return "⏸ paused until /resume"


async def _resume(r: "Runner", args) -> str:
    if args and args[0].lower() in ("facebook", "reddit"):
        src = args[0].lower()
        store.resume_source(r.conn, src)
        return f"▶️ {src} resumed. Next cycle within ~20 min."
    store.kv_set(r.conn, "paused_until", None)
    return "▶️ resumed"


async def _digest(r: "Runner", args) -> str | None:
    await r.send_digest("Digest (on request)", force=True)
    return None


async def _last(r: "Runner", args) -> str:
    n = 5
    if args and args[0].isdigit():
        n = max(1, min(20, int(args[0])))
    rows = store.last_alerts(r.conn, n)
    if not rows:
        return "No alerts yet."
    out = [f"<b>Last {len(rows)} alerts</b>"]
    for row in rows:
        when = datetime.fromtimestamp(row["sent_at"], IST).strftime("%d %b %H:%M")
        head = row["tier"] or "?"
        if row["listing_json"]:
            lst = Listing.model_validate_json(row["listing_json"])
            head += f" · {esc(lst.one_line)} · {esc(money(lst.rent_inr) or '')}"
        link = f' <a href="{esc(row["url"])}">open</a>' if row["url"] else ""
        out.append(f"• {when}: {head}{link}")
    return "\n".join(out)


COMMANDS = {"/status": _status, "/pause": _pause, "/resume": _resume, "/digest": _digest, "/last": _last,
            "/help": _help, "/start": _help}


async def _feedback(r: "Runner", cq: dict) -> None:
    data = cq.get("data") or ""
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    if chat != r.tg.chat_id or not data.startswith("fb|"):
        return
    try:
        _, alert_id, val = data.split("|")
        alert_id, relevant = int(alert_id), val == "1"
    except ValueError:
        return
    store.insert_feedback(r.conn, alert_id, relevant)
    try:
        await r.tg.answer_callback_query(cq["id"], "Thanks, noted 👍" if relevant else "Noted 👎")
        a = store.get_alert(r.conn, alert_id)
        post = store.get_post(r.conn, a["post_id"]) if a else None
        rows = [[{"text": "Open post", "url": post["url"]}]] if post and post["url"] else []
        rows.append([{"text": ("✅ marked relevant" if relevant else "❌ marked not relevant"),
                      "callback_data": "noop"}])
        await r.tg.edit_message_reply_markup(cq["message"]["message_id"], {"inline_keyboard": rows})
    except Exception as e:  # best effort UI update; the feedback row is already stored
        logger.warning("feedback UI update failed: {}", e)
