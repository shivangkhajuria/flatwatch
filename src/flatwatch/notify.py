"""Telegram message formatting (PLAN.md §4.7) and a notifier that survives outages.

Notifier.send() tries immediately; on a transient failure (network down, 429, 5xx)
the message goes to an in-memory outbox that a background task retries every
minute, in order. An optional on_sent(message) callback lets callers store the
Telegram message id once it is finally delivered.
"""

from __future__ import annotations

import asyncio
import html
import json
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from loguru import logger

from .models import Decision, Listing, RawPost
from .telegram import Telegram, TelegramError

IST = ZoneInfo("Asia/Kolkata")
MAX_LEN = 4000   # Telegram limit is 4096


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def money(n: int | None) -> str | None:
    if n is None:
        return None
    if n >= 100000:
        v = n / 100000
        return f"₹{v:.1f}L".replace(".0L", "L")
    if n >= 1000:
        v = n / 1000
        return f"₹{v:.1f}k".replace(".0k", "k")
    return f"₹{n}"


def ago(created: datetime | None, now: datetime | None = None) -> str:
    if created is None:
        return "time unknown"
    mins = int(((now or datetime.now(timezone.utc)) - created).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


UNIT_LABEL = {"1rk": "1RK", "studio": "Studio", "1bhk": "1BHK", "2bhk": "2BHK", "3bhk": "3BHK",
              "4bhk_plus": "4BHK+", "unknown": "?BHK"}
FURNISH = {"full": "fully furnished", "semi": "semi-furnished", "unfurnished": "unfurnished"}


def _unit(listing: Listing) -> str:
    u = UNIT_LABEL[listing.unit]
    if listing.offering_type in ("flatmate", "replacement"):
        return f"Room in {u}"
    return u


def _locality(listing: Listing) -> str:
    """Show the area once: 'HSR Layout Sector 1' (raw already names it), 'Indiranagar (HAL 2nd Stage)'
    (raw is a sub-area), or the raw text for non-target areas."""
    raw = (listing.locality_raw or "").strip()
    if listing.locality in ("other", "unknown", "nearby"):
        return esc(raw or listing.locality)
    if not raw or raw.lower() == listing.locality.lower():
        return esc(listing.locality)
    if listing.locality.lower().replace(" ", "") in raw.lower().replace(" ", ""):
        return esc(raw)
    return f"{esc(listing.locality)} ({esc(raw)})"


TIER_DOT = {"P1": "🟢", "P2": "🔵", "DIGEST": "🟡", "DROP": "⚪"}
TIER_NAME = {"P1": "P1 whole flat", "P2": "P2 room", "DIGEST": "digest", "DROP": "drop"}
EXCERPT_CHARS = 120


def excerpt(text: str, n: int = EXCERPT_CHARS) -> str:
    """First n chars of the post itself, unicode-bold folded to plain, whitespace collapsed."""
    t = " ".join(unicodedata.normalize("NFKC", text or "").split())
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return cut + " …"


def detail_line(listing: Listing, tags: list[str]) -> str:
    """Line 2: what you get."""
    bits = []
    room = listing.offering_type in ("flatmate", "replacement")
    if room:
        if listing.private_bathroom:
            bits.append("🚿 private bathroom")
        elif listing.private_bathroom is False:
            bits.append("shared bathroom")
    if listing.furnishing in FURNISH:
        bits.append(FURNISH[listing.furnishing].capitalize() if not bits else FURNISH[listing.furnishing])
    if listing.deposit_inr:
        bits.append(f"deposit {money(listing.deposit_inr)}")
    if listing.available_from:
        bits.append(f"from {listing.available_from.day} {listing.available_from:%b}")
    if "balcony" in tags:
        bits.append("🌿 balcony")
    return " · ".join(esc(b) for b in bits)


def who_line(listing: Listing) -> str:
    """Line 3: who it's for, brokerage, contact."""
    bits = []
    g = {"male": "👨 men only", "female": "👩 women only", "any": "👥 open to anyone"}.get(listing.gender_pref)
    if g:
        bits.append(g)
    if listing.broker is True:
        bits.append("🏷 broker")
    elif listing.broker is False:
        bits.append("✅ no brokerage")
    if listing.contact and any(c.isdigit() for c in listing.contact):
        bits.append(f"📞 {listing.contact}")
    return " · ".join(esc(b) for b in bits)


def alert_text(post: RawPost, listing: Listing, decision: Decision, group_name: str,
               now: datetime | None = None) -> str:
    head = f"{TIER_DOT[decision.tier]} {_unit(listing)} · {_locality(listing)} · {money(listing.rent_inr) or 'rent ?'}"
    lines = [f"<b>{head}</b>"]   # parts are escaped individually
    for extra in (detail_line(listing, decision.tags), who_line(listing)):
        if extra:
            lines.append(extra)
    body = excerpt(post.text) or esc(listing.one_line)
    if body:
        lines += ["", f"<i>{esc(body)}</i>"]     # blank line: facts above, post text below
    lines.append(f"{esc(group_name)} · {ago(post.created_at, now)} · {TIER_NAME[decision.tier]}")
    return "\n".join(lines)


def alert_markup(url: str | None, alert_id: int) -> dict:
    row = [{"text": "👍 relevant", "callback_data": f"fb|{alert_id}|1"},
           {"text": "👎 not", "callback_data": f"fb|{alert_id}|0"}]
    rows = [[{"text": "Open post", "url": url}]] if url else []
    return {"inline_keyboard": rows + [row]}


def with_also_in(text: str, names: list[str]) -> str:
    base = text.split("\n↪ also in:")[0]
    return base + "\n↪ also in: " + esc(", ".join(names)) if names else base


def digest_messages(rows: list, group_names: dict[str, str], title: str) -> list[str]:
    """rows: store.pending_digest rows. Returns one or more ≤4000-char HTML messages."""
    if not rows:
        return []
    items = []
    for r in rows:
        tags = json.loads(r["tags"] or "[]")
        reasons = json.loads(r["reasons"] or "[]")
        created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc) if r["created_at"] else None
        if r["listing_json"]:
            lst = Listing.model_validate_json(r["listing_json"])
            head = f"{_unit(lst)} · {_locality(lst)} · {esc(money(lst.rent_inr) or 'rent ?')}"
            if "balcony" in tags:
                head += " · 🌿"
            summary = esc(lst.one_line)
        else:
            head = "unverified (LLM unavailable)"
            summary = esc(" ".join((r["text"] or "").split())[:100])
        why = esc("; ".join(reasons))
        link = f'<a href="{esc(r["url"])}">open</a>' if r["url"] else ""
        items.append(f"• <b>{head}</b>\n  {summary}\n  <i>{why}</i> · {esc(group_names.get(r['post_id'], ''))} · "
                     f"{ago(created)} {link}")
    # balcony first (bonus), then newest
    items.sort(key=lambda s: "🌿" not in s.split("\n")[0])
    msgs, cur = [], f"<b>{esc(title)}</b> ({len(items)})\n"
    for it in items:
        if len(cur) + len(it) + 2 > MAX_LEN:
            msgs.append(cur)
            cur = ""
        cur += "\n" + it + "\n"
    msgs.append(cur)
    return msgs


# ---------------------------------------------------------------- notifier --

OnSent = Callable[[dict], Awaitable[None] | None]


@dataclass
class _Item:
    kind: str                     # send | edit
    text: str
    markup: dict | None = None
    message_id: int | None = None
    on_sent: OnSent | None = None
    attempts: int = 0
    silent: bool = False


@dataclass
class Notifier:
    tg: Telegram
    outbox: deque = field(default_factory=deque)

    async def _do(self, it: _Item) -> dict:
        if it.kind == "edit":
            return await self.tg.edit_message_text(it.message_id, it.text, reply_markup=it.markup)
        return await self.tg.send_message(it.text, reply_markup=it.markup, silent=it.silent)

    async def _deliver(self, it: _Item) -> dict | None:
        res = await self._do(it)
        if it.on_sent:
            r = it.on_sent(res)
            if asyncio.iscoroutine(r):
                await r
        return res

    async def send(self, text: str, markup: dict | None = None, on_sent: OnSent | None = None,
                   *, silent: bool = False) -> dict | None:
        """silent=True: delivered without a notification sound (status messages)."""
        return await self._submit(_Item("send", text, markup, on_sent=on_sent, silent=silent))

    async def edit(self, message_id: int, text: str, markup: dict | None = None) -> dict | None:
        return await self._submit(_Item("edit", text, markup, message_id=message_id))

    async def _submit(self, it: _Item) -> dict | None:
        if self.outbox:                       # keep order: don't jump the queue
            self.outbox.append(it)
            return None
        try:
            return await self._deliver(it)
        except TelegramError as e:
            if e.network:
                logger.warning("telegram unavailable ({}); queued", e)
                self.outbox.append(it)
            elif "message is not modified" in str(e):
                pass
            else:
                logger.error("telegram rejected message: {}", e)
            return None

    async def flush(self) -> int:
        """Try to deliver queued items in order. Returns how many were sent."""
        sent = 0
        while self.outbox:
            it = self.outbox[0]
            try:
                await self._deliver(it)
            except TelegramError as e:
                if e.network:
                    it.attempts += 1
                    return sent
                logger.error("dropping undeliverable queued message: {}", e)
            self.outbox.popleft()
            sent += 1
        return sent

    async def run_flusher(self, interval: float = 60) -> None:
        while True:
            await asyncio.sleep(interval)
            if self.outbox:
                n = await self.flush()
                if n:
                    logger.info("delivered {} queued telegram messages ({} left)", n, len(self.outbox))
