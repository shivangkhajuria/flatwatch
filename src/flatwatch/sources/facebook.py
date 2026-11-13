"""Facebook group fetcher: GraphQL interception (primary), HTML-embedded Relay
data (first page), DOM (fallback).

Read-only: the only actions are page.goto(feed URL) and mouse-wheel scrolling.
Never click; full text comes from the network payloads, not "See more".
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from loguru import logger
from playwright.async_api import BrowserContext, Response

from ..browser import BlockedError  # noqa: F401  (re-exported)
from ..config import WORK_DIR
from ..models import RawPost

FIXTURE_DIR = WORK_DIR / "fixtures" / "fb"
# Live polling: keep scrolling until at least MIN_POSTS are
# loaded or the last post seen in this group is reached, never more than MAX_SCROLLS. In normal
# traffic that is 0–3 scrolls; it only goes further during a posting burst.
MAX_SCROLLS = 5
MIN_POSTS = 15
SKIP_KEYS = {"attached_story", "feedback", "comment_list_renderer", "comet_ufi_summary_and_actions_renderer"}
PERMALINK_RE = re.compile(r"/(posts|permalink)/|story_fbid=")


# ---------------------------------------------------------------- parsing ---

def iter_json_objects(body: str) -> Iterator[Any]:
    """Yield every JSON value in a body that may hold several, newline separated,
    optionally prefixed with Facebook's `for (;;);` guard."""
    body = body.strip()
    if body.startswith("for (;;);"):
        body = body[len("for (;;);"):]
    dec = json.JSONDecoder()
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(body, i)
        except json.JSONDecodeError:
            nl = body.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        yield obj


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _apply_chunk(root: Any, path: list, data: Any) -> bool:
    if not path:
        return False
    cur = root
    for k in path[:-1]:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return False
    last = path[-1]
    if isinstance(cur, list) and isinstance(last, int):
        if last < len(cur):
            if isinstance(cur[last], dict) and isinstance(data, dict):
                _deep_merge(cur[last], data)
                return True
            return False
        if last == len(cur):
            cur.append(data)
            return True
        return False
    if isinstance(cur, dict):
        if isinstance(cur.get(last), dict) and isinstance(data, dict):
            _deep_merge(cur[last], data)
        else:
            cur[last] = data
        return True
    return False


def merge_stream(results: Iterable[Any]) -> list[Any]:
    """Fold Relay @defer/@stream chunks ({label, path, data}) into their base
    result. Chunks that can't be placed are kept so their stories still parse."""
    trees: list[Any] = []
    for r in results:
        if not isinstance(r, dict) or "data" not in r:
            continue
        if r.get("path") is not None and trees:
            if any(_apply_chunk(t, r["path"], r["data"]) for t in reversed(trees)):
                continue
        trees.append(r["data"])
    return trees


def _collect_images(x: Any, out: list[str]) -> None:
    if isinstance(x, dict):
        for k in ("photo_image", "image", "large_share_image", "viewer_image"):
            v = x.get(k)
            if isinstance(v, dict) and isinstance(v.get("uri"), str) and v["uri"] not in out:
                out.append(v["uri"])
        for v in x.values():
            _collect_images(v, out)
    elif isinstance(x, list):
        for v in x:
            _collect_images(v, out)


def _scan_story(node: dict, pid: str, acc: dict) -> None:
    stack: list[Any] = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(reversed(x))
            continue
        if not isinstance(x, dict):
            continue
        if x is not node and x.get("__typename") == "Story" and x.get("post_id") not in (None, pid):
            continue  # a different story nested inside (e.g. a share)
        children = []
        for k, v in x.items():
            if k == "attached_story" and isinstance(v, dict) and isinstance(v.get("permalink_url"), str):
                acc.setdefault("shared_from", v["permalink_url"])
            if k in SKIP_KEYS:
                continue
            if k == "creation_time" and isinstance(v, int):
                acc.setdefault("creation_time", v)
            elif k == "message" and isinstance(v, dict) and isinstance(v.get("text"), str):
                if len(v["text"]) > len(acc.get("text", "")):
                    acc["text"] = v["text"]
            elif k == "actors" and isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("name"):
                acc.setdefault("author", v[0]["name"])
            elif k in ("url", "wwwURL", "permalink_url") and isinstance(v, str) and PERMALINK_RE.search(v):
                acc.setdefault("url", v)
            elif k == "attachments":
                _collect_images(v, acc.setdefault("images", []))
            if isinstance(v, (dict, list)):
                children.append(v)
        stack.extend(reversed(children))


def extract_stories(trees: Iterable[Any]) -> dict[str, dict]:
    """Walk payload trees for Story nodes; merge every partial copy per post_id."""
    found: dict[str, dict] = {}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            pid = x.get("post_id")
            if x.get("__typename") == "Story" and isinstance(pid, (str, int)):
                _scan_story(x, str(pid), found.setdefault(str(pid), {}))
            for k, v in x.items():
                if k not in ("attached_story",):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for t in trees:
        walk(t)
    return found


def bbox_results(blob: Any) -> list[dict]:
    """Relay results embedded in the page HTML: every `__bbox.result` with data."""
    out: list[dict] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            bb = x.get("__bbox")
            if isinstance(bb, dict) and isinstance(bb.get("result"), dict) and "data" in bb["result"]:
                out.append(bb["result"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(blob)
    return out


def stories_to_posts(stories: dict[str, dict], source_key: str, via: str) -> list[RawPost]:
    posts = []
    for pid, s in stories.items():
        if not (s.get("text") or s.get("images") or s.get("creation_time")):
            continue  # stub reference, nothing useful
        ts = s.get("creation_time")
        posts.append(
            RawPost(
                source="facebook",
                source_key=source_key,
                post_id=pid,
                author=s.get("author"),
                text=s.get("text", ""),
                url=s.get("url") or f"{source_key}posts/{pid}/",
                image_urls=s.get("images", []),
                created_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                shared_from=s.get("shared_from"),
                via=via,  # type: ignore[arg-type]
            )
        )
    return posts


def parse_payloads(gql_bodies: list[str], html_blobs: list[str], source_key: str) -> list[RawPost]:
    gql_results = [o for b in gql_bodies for o in iter_json_objects(b)]
    html_results = []
    for blob in html_blobs:
        try:
            html_results.extend(bbox_results(json.loads(blob)))
        except json.JSONDecodeError:
            continue
    by_id: dict[str, RawPost] = {}
    # HTML first, then GraphQL overrides (GraphQL tends to be more complete).
    for via, results in (("html", html_results), ("graphql", gql_results)):
        for p in stories_to_posts(extract_stories(merge_stream(results)), source_key, via):
            old = by_id.get(p.post_id)
            if old is None or len(p.text) >= len(old.text):
                if old is not None:
                    p.created_at = p.created_at or old.created_at
                    p.author = p.author or old.author
                    p.image_urls = p.image_urls or old.image_urls
                    p.shared_from = p.shared_from or old.shared_from
                by_id[p.post_id] = p
    return sorted(by_id.values(), key=lambda p: p.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


# ---------------------------------------------------------------- fetching ---

def group_slug(url: str) -> str:
    m = re.search(r"/groups/([^/?#]+)", url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)


def feed_url(group_url: str) -> str:
    return f"{group_url}?sorting_setting=CHRONOLOGICAL"


def clean_title(title: str) -> str:
    title = re.sub(r"^\(\d+\+?\)\s*", "", title)
    return re.sub(r"\s*\|\s*Facebook\s*$", "", title).strip()


DOM_JS = """
() => Array.from(document.querySelectorAll('[role="feed"] [role="article"]'))
  .filter(a => !a.parentElement.closest('[role="article"]'))
  .map(a => {
    const link = Array.from(a.querySelectorAll('a[href]'))
      .map(l => l.href).find(h => /\\/(posts|permalink)\\/|story_fbid=/.test(h));
    return {href: link || null, text: a.innerText.slice(0, 4000)};
  })
"""


async def fetch_group(
    ctx: BrowserContext,
    group_url: str,
    *,
    save_fixtures: bool = True,
    detect_block=None,
    known_until: datetime | None = None,
    max_scrolls: int = MAX_SCROLLS,
    scroll_wait_ms: tuple[int, int] = (3000, 6000),
    min_posts: int = MIN_POSTS,
) -> tuple[str, list[RawPost], dict]:
    """Open one group feed, scroll ≤ max_scrolls times, return (title, posts, stats).

    Stops scrolling at the first of: the loaded posts reach back to `known_until`
    (nothing in between can be missed), at least `min_posts` are loaded, the feed
    stops yielding new posts, or `max_scrolls`. stats["gap"] is True when posts were
    loaded but none reached back to `known_until`: some posts may have been missed.

    known_until: newest post time already seen in this group. Scrolling stops as soon
    as the loaded posts reach back to it (usually zero scrolls in steady state)."""
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    gql_bodies: list[str] = []
    pending: list[asyncio.Task] = []

    async def grab(resp: Response) -> None:
        try:
            gql_bodies.append(await resp.text())
        except Exception as e:  # body evicted, redirect, etc.
            logger.debug("graphql body unavailable: {}", type(e).__name__)

    def on_response(resp: Response) -> None:
        if "/api/graphql" in resp.url and resp.request.method == "POST":
            pending.append(asyncio.ensure_future(grab(resp)))

    page.on("response", on_response)
    try:
        await page.goto(feed_url(group_url), wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.uniform(4000, 7000))
        if detect_block and (reason := await detect_block(page)):
            raise BlockedError(reason)

        html_blobs = [
            t for t in await page.locator('script[type="application/json"]').all_text_contents() if "post_id" in t
        ]
        deep = max_scrolls > MAX_SCROLLS          # backtest only
        scrolls, stalled, last_count = 0, 0, -1
        for _ in range(max_scrolls):
            if pending:
                await asyncio.gather(*pending)
            so_far = parse_payloads([b for b in gql_bodies if "post_id" in b], html_blobs, group_url)
            times = [p.created_at for p in so_far if p.created_at]
            if known_until is not None and times and min(times) <= known_until:
                break   # reached posts we've already seen: nothing in between is missing
            if min_posts and not deep and len(so_far) >= min_posts:
                break   # loaded enough for one visit
            stalled = stalled + 1 if len(so_far) == last_count else 0
            last_count = len(so_far)
            if stalled >= 3:
                break   # feed isn't loading more
            if deep and scrolls and scrolls % 5 == 0 and detect_block:
                if reason := await detect_block(page):
                    raise BlockedError(reason)
            await page.mouse.wheel(0, random.randint(1800, 2600))
            await page.wait_for_timeout(random.uniform(*scroll_wait_ms))
            scrolls += 1
        if detect_block and (reason := await detect_block(page)):
            raise BlockedError(reason)
        if pending:
            await asyncio.gather(*pending)
        title = clean_title(await page.title())
        dom_items = await page.evaluate(DOM_JS)
    finally:
        page.remove_listener("response", on_response)

    feed_bodies = [b for b in gql_bodies if "post_id" in b]
    posts = parse_payloads(feed_bodies, html_blobs, group_url)
    stats = {
        "graphql_responses": len(gql_bodies),
        "graphql_with_posts": len(feed_bodies),
        "html_blobs_with_posts": len(html_blobs),
        "dom_articles": len(dom_items),
        "dom_permalinks": sum(1 for d in dom_items if d["href"]),
        "scrolls": scrolls,
    }
    times = [p.created_at for p in posts if p.created_at]
    stats["gap"] = bool(known_until is not None and times and min(times) > known_until)

    if not posts and dom_items:
        logger.warning("no posts from payloads; falling back to DOM ({} articles)", len(dom_items))
        for d in dom_items:
            if not d["href"]:
                continue
            m = re.search(r"/(?:posts|permalink)/(\d+)|story_fbid=(\d+)", d["href"])
            if m:
                posts.append(RawPost(source="facebook", source_key=group_url, post_id=m.group(1) or m.group(2),
                                     text=d["text"], url=d["href"].split("?")[0], via="dom"))

    if save_fixtures:
        out = FIXTURE_DIR / group_slug(group_url) / time.strftime("%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        for i, b in enumerate(feed_bodies):
            (out / f"gql_{i:03}.txt").write_text(b)
        for i, b in enumerate(html_blobs):
            (out / f"html_{i:03}.json").write_text(b)
        (out / "dom.json").write_text(json.dumps(dom_items, ensure_ascii=False, indent=1))
        (out / "meta.json").write_text(json.dumps({"url": group_url, "title": title, **stats}, indent=1))
        stats["fixture_dir"] = str(out.relative_to(WORK_DIR))
    return title, posts, stats
