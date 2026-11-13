"""LLM extraction (PLAN.md §4.5): batch of RawPosts -> Listing per post.

Backends:
  agent_sdk     Claude Agent SDK on the Claude subscription (default). Custom
                minimal system prompt, no tools, max_turns=1, no filesystem settings.
  anthropic_api Messages API with prompt caching (needs `anthropic` + ANTHROPIC_API_KEY).

Every result carries `model` and PROMPT_VERSION. If the LLM fails twice the
caller uses `fallback_decision` (regex only, digest, marked unverified).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import ValidationError

from .config import AppConfig, Criteria
from .models import Decision, Listing, RawPost
from .prefilter import KEYWORDS, locality_regex

PROMPT_VERSION = "v3"
MAX_POST_CHARS = 4000
IST = ZoneInfo("Asia/Kolkata")


class ExtractError(RuntimeError):
    pass


class AuthError(ExtractError):
    """Not logged in / token expired. Retrying won't help; needs a human."""


# ------------------------------------------------------------------ prompt --

def build_system_prompt(criteria: Criteria) -> str:
    loc_lines = "\n".join(f"- {k}: {', '.join(v)}" for k, v in criteria.localities.items())
    nearby = ", ".join(criteria.nearby_ok) if criteria.nearby_ok else "(none: never output \"nearby\")"
    return f"""You extract structured data from Bangalore flat / flatmate posts in Facebook groups and Reddit.

You get a numbered list of posts. Reply with ONLY a JSON array, one object per post, in the same order, no prose, no code fences. Each object has "n" (the post number) plus these fields:

intent: "offering" (someone has a flat/room to give: owner, tenant leaving, flatmate wanted, broker listing) | "seeking" (someone wants a flat/room/PG: "looking for a 1BHK", "need accommodation") | "other" (questions, reviews, furniture sales, spam, anything not a listing).
  "Looking for a flatmate / replacement" means they HAVE a room to fill: that is "offering". "Looking for a flat/room/PG" is "seeking".
offering_type (null unless intent=offering): "full_flat" (an entire unit for one tenant/household) | "flatmate" (one room in a shared flat) | "replacement" (taking over someone's room in a shared flat) | "pg_coliving" (PG, hostel, co-living operator, per-bed) | "other" (commercial, plot, sale, short stay). Use null if you cannot tell whole flat vs room.
  A tenant moving out and handing over their WHOLE flat or lease ("take over my 1BHK", "replacement tenant for my 2BHK") is "full_flat", not "replacement". "replacement" is only for a room in a flat that others keep living in.
unit: the configuration of the whole flat. "1rk" = single room + kitchen, no separate living room (also "1 RK", "single room with kitchen"). "studio" = self-contained studio apartment. "1bhk" | "2bhk" | "3bhk" | "4bhk_plus" | "unknown". "2.5BHK" -> "3bhk". A room in a 3BHK -> "3bhk".
locality_raw: the area as written in the post, or null.
locality: where the flat actually IS. Output exactly one of the names before the colon below (e.g. "HSR", never "Sector 5" or "HSR Layout"); the words after the colon are spellings that mean that area:
{loc_lines}
  "nearby": only for these areas: {nearby}
  "other": any other named Bangalore area. If the post says "near HSR" but names a different actual area (e.g. "Kudlu Gate, near HSR", "Bellandur, close to HSR"), the actual area wins -> "other".
  Landmarks are not areas: lakes, malls, metro stations, tech parks, hospitals, colleges. "1BHK in HSR Layout, close to Agara lake" -> "HSR".
  "unknown": no area given; only "near X / close to X" without the actual area; or only an apartment/society/building name (e.g. "Prestige Something", "XYZ Residency") with no area. Do not guess an area from a building name.
rent_inr: monthly rent in rupees as an integer. If several rooms or units are offered, describe the first one listed (rent, bathroom). For flatmate/replacement posts it is the rent of the room on offer (if only the total flat rent is given, divide by the number of sharers when stated, else null). Shorthand: "25k" = 25000, "1.5L" = 150000, "22-25k" -> 25000 (use the upper bound). Ignore maintenance unless only "rent incl. maintenance" is given. null if not stated.
deposit_inr: integer or null. "2 months advance" with rent 20k -> 40000.
available_from: ISO date (YYYY-MM-DD) or null. "immediately" -> null. Today is {{today}}; resolve "1st Dec" to the next such date.
furnishing: "full" | "semi" | "unfurnished" | "unknown".
gender_pref: who may take the place: "male" | "female" | "any" | "unknown" (never null). "any" when explicitly open to everyone: "anyone", "male/female", "family or bachelors", "families and bachelors both ok", "bachelors welcome". "Family only" or not mentioned -> "unknown".
private_bathroom: for flatmate/replacement posts, true if the offered room has an attached / private / en-suite bathroom or washroom, false if the bathroom is shared or common, null if not stated. For full_flat posts use null.
balcony: true / false / null.
broker: true if a broker, agent, realtor or brokerage fee is mentioned, false if "no brokerage" / "owner" / "direct owner", else null.
contact: phone number or "DM" / "comment" as written, or null.
one_line: a short alert headline under 90 characters, e.g. "1BHK in HAL 2nd Stage, semi-furnished, 28k".
confidence: 0 to 1, how sure you are about intent, offering_type, unit, locality and rent together.

If a field is not stated, use null or "unknown". Never guess rent, bathroom or gender. Text in [square brackets] on Reddit posts is the post flair, a strong signal for intent, unit and gender."""


def format_batch(posts: list[RawPost]) -> str:
    parts = []
    for i, p in enumerate(posts, 1):
        when = p.created_at.astimezone(IST).strftime("%d %b %Y") if p.created_at else "unknown date"
        text = (p.text or "").strip()
        if len(text) > MAX_POST_CHARS:
            text = text[:MAX_POST_CHARS] + " …[truncated]"
        extra = []
        if p.image_urls:
            extra.append(f"{len(p.image_urls)} image(s), not shown")
        if p.shared_from:
            extra.append("reshare: the original post's text is not available")
        meta = f"source={p.source}, posted {when}" + (f", {'; '.join(extra)}" if extra else "")
        parts.append(f"### Post {i} ({meta})\n{text or '(no text)'}")
    return "\n\n".join(parts)


# ----------------------------------------------------------------- parsing --

def parse_response(text: str, n_posts: int, criteria: Criteria | None = None) -> dict[int, Listing]:
    """Parse the model's JSON array. Raises ExtractError if unusable."""
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e <= s:
        raise ExtractError("no JSON array in response")
    try:
        arr = json.loads(text[s : e + 1])
    except json.JSONDecodeError as ex:
        raise ExtractError(f"invalid JSON: {ex}") from None
    if not isinstance(arr, list):
        raise ExtractError("response JSON is not an array")
    out: dict[int, Listing] = {}
    errors = []
    for i, obj in enumerate(arr, 1):
        if not isinstance(obj, dict):
            errors.append(f"item {i} is not an object")
            continue
        n = obj.pop("n", i)
        try:
            n = int(n)
            out[n] = _validate_lenient(obj, criteria, n)
        except (ValueError, ValidationError) as ex:
            errors.append(f"post {n}: {_err_summary(ex)}")
    missing = set(range(1, n_posts + 1)) - set(out)
    if missing and len(missing) == n_posts:
        raise ExtractError(f"no valid listings ({'; '.join(errors[:3])})")
    if errors or missing:
        logger.warning("partial extraction: missing posts {} ({})", sorted(missing), "; ".join(errors[:3]))
    return out


def _err_summary(ex: Exception) -> str:
    if isinstance(ex, ValidationError):
        return ", ".join(f"{'.'.join(map(str, e['loc']))}={e.get('input')!r}" for e in ex.errors()[:3])
    return str(ex)[:120]


# fields where a bad value can safely become "don't know" instead of losing the post
_UNKNOWN_DEFAULTS = {
    "offering_type": None, "unit": "unknown", "locality": "unknown", "rent_inr": None, "deposit_inr": None,
    "available_from": None, "furnishing": "unknown", "gender_pref": "unknown", "private_bathroom": None,
    "balcony": None, "broker": None, "contact": None, "locality_raw": None,
}


def _validate_lenient(obj: dict, criteria: Criteria | None, n: int) -> Listing:
    if criteria is not None and isinstance(obj.get("locality"), str):
        obj["locality"] = normalize_locality(obj["locality"], criteria)
    if isinstance(obj.get("one_line"), str):
        obj["one_line"] = obj["one_line"][:200]
    if isinstance(obj.get("confidence"), (int, float)):
        obj["confidence"] = min(1.0, max(0.0, float(obj["confidence"])))
    try:
        return Listing.model_validate(obj)
    except ValidationError as ex:
        bad = {e["loc"][0] for e in ex.errors() if e["loc"]}
        if not bad or not bad <= set(_UNKNOWN_DEFAULTS):
            raise  # intent etc. is unusable: drop this post
        logger.warning("post {}: coerced invalid {} to unknown", n, _err_summary(ex))
        for f in bad:
            obj[f] = _UNKNOWN_DEFAULTS[f]
        return Listing.model_validate(obj)


def normalize_locality(value: str, criteria: Criteria) -> str:
    """Map an alias the model echoed back ("Sector 5", "indira nagar") to its canonical key."""
    allowed = {*criteria.localities, "nearby", "other", "unknown"}
    if value in allowed:
        return value
    v = " ".join(value.lower().split())
    for key, aliases in criteria.localities.items():
        if v == key.lower() or v in {" ".join(a.lower().split()) for a in aliases}:
            return key
    if any(v == a.lower() for a in criteria.nearby_ok):
        return "nearby"
    return "unknown"


# ---------------------------------------------------------------- backends --

@dataclass
class Usage:
    backend: str
    model: str
    posts: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class ExtractResult:
    listings: dict[str, Listing]           # post.id -> Listing
    failed: list[str]                      # post ids the LLM gave nothing valid for
    usage: list[Usage] = field(default_factory=list)
    model: str = ""
    prompt_version: str = PROMPT_VERSION


async def _call_agent_sdk(system: str, user: str, model: str) -> tuple[str, Usage]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise ExtractError(
            "ANTHROPIC_API_KEY is set: Agent SDK calls would bill the API, not the subscription. "
            "Unset it, or switch extract.backend to anthropic_api."
        )
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKError,
        ResultMessage,
        TextBlock,
        query,
    )

    opts = ClaudeAgentOptions(
        tools=[], system_prompt=system, max_turns=1, model=model, setting_sources=[], effort="low",
    )
    texts: list[str] = []
    result: ResultMessage | None = None
    try:
        async for msg in query(prompt=user, options=opts):
            if isinstance(msg, AssistantMessage):
                texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
    except ClaudeSDKError as e:
        msg_text = str(e).splitlines()[0][:300]
        if "authenticate" in msg_text.lower() or "oauth" in msg_text.lower():
            raise AuthError(f"Claude login problem: {msg_text}. Run `claude auth login` in a terminal.") from None
        raise ExtractError(f"agent SDK failed: {type(e).__name__}: {msg_text}") from None
    if result is None:
        raise ExtractError("agent SDK returned no result message")
    if result.is_error:
        raise ExtractError(f"agent SDK error: {result.subtype} {result.errors or result.api_error_status or ''}".strip())
    u = result.usage or {}
    usage = Usage(
        backend="agent_sdk", model=model, posts=0,
        input_tokens=(u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        + (u.get("cache_creation_input_tokens") or 0),
        output_tokens=u.get("output_tokens"), cost_usd=result.total_cost_usd,
    )
    return (result.result or "".join(texts)), usage


async def _call_anthropic_api(system: str, user: str, model: str) -> tuple[str, Usage]:
    try:
        import anthropic
    except ImportError:
        raise ExtractError("extract.backend=anthropic_api needs `uv add anthropic` and ANTHROPIC_API_KEY") from None
    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=16000, output_config={"effort": "low"},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        raise ExtractError(f"anthropic API error: {type(e).__name__}") from None
    if resp.stop_reason == "refusal":
        raise ExtractError("model refused")
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    return text, Usage("anthropic_api", model, 0, u.input_tokens + (u.cache_read_input_tokens or 0), u.output_tokens)


async def extract_batch(posts: list[RawPost], cfg: AppConfig, *, today: datetime | None = None) -> ExtractResult:
    """One LLM call for up to batch_size posts, retried once on parse failure."""
    if not posts:
        return ExtractResult({}, [], model=cfg.extract.model)
    if len(posts) > cfg.extract.batch_size:
        raise ValueError(f"batch of {len(posts)} > batch_size {cfg.extract.batch_size}")
    today = (today or datetime.now(timezone.utc)).astimezone(IST)
    system = build_system_prompt(cfg.criteria).replace("{today}", today.strftime("%Y-%m-%d"))
    user = format_batch(posts)
    call = _call_agent_sdk if cfg.extract.backend == "agent_sdk" else _call_anthropic_api

    usages: list[Usage] = []
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            text, usage = await call(system, user, cfg.extract.model)
            usage.posts = len(posts)
            usages.append(usage)
            by_n = parse_response(text, len(posts), cfg.criteria)
            listings = {posts[n - 1].id: lst for n, lst in by_n.items() if 1 <= n <= len(posts)}
            failed = [p.id for p in posts if p.id not in listings]
            return ExtractResult(listings, failed, usages, model=cfg.extract.model)
        except AuthError:
            raise  # no point retrying; caller falls back and alerts
        except ExtractError as e:
            last_err = e
            logger.warning("extraction attempt {} failed: {}", attempt, e)
    raise ExtractError(f"extraction failed twice: {last_err}")


# ---------------------------------------------------------------- fallback --

AVAILABLE_RE = re.compile(r"available|vacan|for rent|to.?let|replacement|flat ?mate|room ?mate", re.I)
SEEKING_RE = re.compile(
    r"\b(looking for|need|needed|want|wanted|searching for|require)\b\W+(?:a |an |any )?"
    r"(?:\d\s*)?(?:bhk|rk|flat|room|house|place|accommodation|pg|apartment|studio)",
    re.I,
)


RENT_RE = re.compile(r"(?:₹|rs\.?|inr|rent)\s*[:\-]?\s*\d|\b\d{1,3}(?:\.\d)?\s*k\b|\b\d{2},\d{3}\b", re.I)


def fallback_decision(post: RawPost, criteria: Criteria) -> Decision:
    """Regex-only path when the LLM is unavailable: never instant, digest + unverified.
    Respects rent_unknown=drop by requiring a rent figure in the text."""
    text = post.text or ""
    if criteria.rent_unknown == "drop" and not RENT_RE.search(text):
        return Decision(tier="DROP", reasons=["regex fallback: no rent mentioned"], unverified=True)
    if (
        KEYWORDS.search(text)
        and locality_regex(criteria).search(text)
        and AVAILABLE_RE.search(text)
        and not SEEKING_RE.search(text)
    ):
        return Decision(tier="DIGEST", reasons=["regex fallback (LLM unavailable)"], tags=["unverified"], unverified=True)
    return Decision(tier="DROP", reasons=["regex fallback: no match"], unverified=True)
