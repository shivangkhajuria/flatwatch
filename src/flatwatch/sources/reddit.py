"""Reddit fetcher: logged-in `.json` via same-origin fetch (primary), shreddit
DOM (fallback).

Read-only: page.goto(/r/<sub>/new/) plus one same-origin GET from inside the
page. Never clicks, votes, comments or joins.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone  # noqa: F401
from typing import Any

from loguru import logger
from playwright.async_api import BrowserContext

from ..browser import BlockedError
from ..config import WORK_DIR
from ..models import RawPost

FIXTURE_DIR = WORK_DIR / "fixtures" / "reddit"
JSON_RETRY_S = 24 * 3600
_json_blocked_at: float | None = None   # .json returned 403/HTML: use the DOM for a day before retrying
BASE = "https://www.reddit.com"

FETCH_JS = """
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
    return {status: r.status, ct: r.headers.get('content-type') || '', body: await r.text()};
  } catch (e) {
    return {status: 0, ct: '', body: String(e)};
  }
}
"""

DOM_JS = """
() => Array.from(document.querySelectorAll('shreddit-post')).map(p => {
  const body = p.querySelector('[slot="text-body"]');
  const flair = p.querySelector('[slot="post-flair"]');
  const imgs = Array.from(p.querySelectorAll('[slot="post-media-container"] img'))
    .map(i => i.currentSrc || i.src).filter(Boolean);
  return {
    id: p.getAttribute('id'),
    permalink: p.getAttribute('permalink'),
    title: p.getAttribute('post-title'),
    author: p.getAttribute('author'),
    created: p.getAttribute('created-timestamp'),
    post_type: p.getAttribute('post-type'),
    content_href: p.getAttribute('content-href'),
    stickied: p.hasAttribute('stickied') || p.getAttribute('is-pinned') === 'true',
    body: body ? body.innerText : '',
    flair: flair ? flair.innerText.trim() : '',
    images: imgs,
  };
})
"""


def _join(title: str | None, body: str | None, flair: str | None = None) -> str:
    """Title, then `[flair]` (e.g. "[Looking for a flatmate (Male)]"), then body."""
    title, body, flair = (title or "").strip(), (body or "").strip(), (flair or "").strip()
    head = f"{title}\n[{flair}]" if title and flair else title or (f"[{flair}]" if flair else "")
    return f"{head}\n\n{body}" if head and body else head or body


def parse_listing_json(data: Any, sub: str) -> list[RawPost]:
    """Parse a /r/<sub>/new.json Listing."""
    posts = []
    children = (data or {}).get("data", {}).get("children", []) if isinstance(data, dict) else []
    for ch in children:
        d = ch.get("data") or {}
        if ch.get("kind") != "t3" or not d.get("id") or d.get("stickied"):
            continue
        images: list[str] = []
        for img in (d.get("preview") or {}).get("images", []):
            src = (img.get("source") or {}).get("url")
            if src:
                images.append(src)
        for item in (d.get("media_metadata") or {}).values():
            src = (item.get("s") or {}).get("u")
            if src and src not in images:
                images.append(src)
        url = d.get("url") or ""
        if not images and isinstance(url, str) and url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            images.append(url)
        posts.append(
            RawPost(
                source="reddit",
                source_key=sub,
                post_id=d["id"],
                author=d.get("author"),
                text=_join(d.get("title"), d.get("selftext"), d.get("link_flair_text")),
                url=BASE + d["permalink"] if d.get("permalink") else None,
                image_urls=images,
                created_at=datetime.fromtimestamp(d["created_utc"], tz=timezone.utc) if d.get("created_utc") else None,
                via="json",
            )
        )
    return _newest_first(posts)


def parse_dom_items(items: list[dict], sub: str) -> list[RawPost]:
    posts = []
    for it in items:
        pid = (it.get("id") or "").removeprefix("t3_")
        if not pid or it.get("stickied"):
            continue
        created = None
        if it.get("created"):
            try:
                created = datetime.fromisoformat(it["created"].replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                pass
        posts.append(
            RawPost(
                source="reddit",
                source_key=sub,
                post_id=pid,
                author=it.get("author"),
                text=_join(it.get("title"), it.get("body"), it.get("flair")),
                url=BASE + it["permalink"] if it.get("permalink") else None,
                image_urls=it.get("images") or [],
                created_at=created,
                via="dom",
            )
        )
    return _newest_first(posts)


def _newest_first(posts: list[RawPost]) -> list[RawPost]:
    return sorted(posts, key=lambda p: p.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


async def fetch_sub(
    ctx: BrowserContext,
    sub: str,
    *,
    save_fixtures: bool = True,
    detect_block=None,
    force_dom: bool = False,
    max_scrolls: int = 0,
    until: "datetime | None" = None,
    scroll_wait_ms: tuple[int, int] = (6000, 10000),
) -> tuple[str, list[RawPost], dict]:
    """Default (live bot): no scrolling. max_scrolls > 0 (backtest only): scroll until posts
    are older than `until`, the feed stops growing, or the cap is hit."""
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(f"{BASE}/r/{sub}/new/", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(random.uniform(3000, 5000))
    # The feed renders in chunks; reading too early once gave 3 posts instead of 27.
    # Wait (≤15 s) until the shreddit-post count stops growing for ~2 s. Read-only: no clicks, no scrolling.
    last, stable_since, deadline = -1, time.monotonic(), time.monotonic() + 15
    while time.monotonic() < deadline:
        n = await page.locator("shreddit-post").count()
        if n != last:
            last, stable_since = n, time.monotonic()
        elif n > 0 and time.monotonic() - stable_since >= 2:
            break
        await page.wait_for_timeout(500)
    if detect_block and (reason := await detect_block(page)):
        raise BlockedError(reason)
    title = (await page.title()).strip()

    stats: dict[str, Any] = {}
    raw_json: str | None = None
    posts: list[RawPost] = []
    global _json_blocked_at
    if _json_blocked_at and time.time() - _json_blocked_at < JSON_RETRY_S:
        force_dom = True
        stats["json"] = "skipped (403 earlier)"
    if not force_dom:
        res = await page.evaluate(FETCH_JS, f"/r/{sub}/new.json?limit=25&raw_json=1")
        stats["json_status"] = res["status"]
        if res["status"] == 429:
            raise BlockedError("reddit .json returned 429 (rate limited)")
        if res["status"] == 200 and "json" in res["ct"]:
            raw_json = res["body"]
            try:
                posts = parse_listing_json(json.loads(raw_json), sub)
            except json.JSONDecodeError:
                stats["json_error"] = "invalid JSON"
        else:
            stats["json_ct"] = res["ct"].split(";")[0]
            if res["status"] in (401, 403):
                _json_blocked_at = time.time()
            stats["_json_error_body"] = res["body"][:2000]
            logger.warning("r/{} .json unavailable (HTTP {}, {}); using DOM", sub, res["status"], stats["json_ct"])

    scrolls = 0
    if max_scrolls:
        stalled, last_n = 0, await page.locator("shreddit-post").count()
        while scrolls < max_scrolls:
            cur = parse_dom_items(await page.evaluate(DOM_JS), sub)
            times = [p.created_at for p in cur if p.created_at]
            if until is not None and times and min(times) <= until:
                break
            if scrolls and scrolls % 5 == 0 and detect_block and (reason := await detect_block(page)):
                raise BlockedError(reason)
            await page.mouse.wheel(0, random.randint(1800, 2600))
            await page.wait_for_timeout(random.uniform(*scroll_wait_ms))
            scrolls += 1
            n = await page.locator("shreddit-post").count()
            stalled = stalled + 1 if n == last_n else 0
            last_n = n
            if stalled >= 3:
                break
        stats["scrolls"] = scrolls
        posts = []   # the DOM now holds far more than .json's 25; use it

    dom_items = await page.evaluate(DOM_JS)
    feed_html = await page.evaluate(
        "() => Array.from(document.querySelectorAll('shreddit-post')).map(p => p.outerHTML).join('\\n<!-- next -->\\n')"
    )
    stats["dom_posts"] = len(dom_items)
    if not posts or max_scrolls:
        posts = parse_dom_items(dom_items, sub)
    stats["via"] = posts[0].via if posts else "none"

    json_err = stats.pop("_json_error_body", None)
    if json_err is not None:
        stats["_json_error_body"] = json_err  # kept only for the fixture write below
    if save_fixtures:
        out = FIXTURE_DIR / sub / time.strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        if raw_json:
            (out / "new.json").write_text(raw_json)
        (out / "dom.json").write_text(json.dumps(dom_items, ensure_ascii=False, indent=1))
        (out / "feed.html").write_text(feed_html)
        if "_json_error_body" in stats:
            (out / "json_error.html").write_text(stats["_json_error_body"])
        stats.pop("_json_error_body", None)
        (out / "meta.json").write_text(json.dumps({"sub": sub, "title": title, **stats}, indent=1))
        stats["fixture_dir"] = str(out.relative_to(WORK_DIR))
    stats.pop("_json_error_body", None)
    return title, posts, stats
