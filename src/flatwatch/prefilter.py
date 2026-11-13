"""Cheap high-recall keyword prefilter (PLAN.md §4.4). Decides which posts
are worth an LLM call; the target is ~70-80% fewer calls with no real listing lost."""

from __future__ import annotations

import re
from functools import lru_cache

from .config import Criteria
from .models import RawPost

PREFILTER_VERSION = "pf2"

KEYWORDS = re.compile(
    r"1\s*bhk|2\s*bhk|3\s*bhk|studio|flat ?mate|room ?mate|replacement|room available|vacan|to.?let"
    r"|available from|attached (bath|wash)|private (bath|wash)"
    # pf2: 1RK posts and "accommodation sharing" one-liners were missed in the first eval
    r"|1\s*rk|accomm?odation|sharing",
    re.IGNORECASE,
)
MIN_CHARS = 15


@lru_cache(maxsize=8)
def _locality_re(aliases: tuple[str, ...]) -> re.Pattern:
    alts = sorted({re.escape(a) for a in aliases}, key=len, reverse=True)
    # \b at both ends, and spaces inside an alias match any run of whitespace
    # re.escape turns " " into "\ "; let any whitespace run match
    pats = [a.replace("\\ ", " ").replace(" ", r"\s+") for a in alts]
    return re.compile(r"\b(?:" + "|".join(pats) + r")\b", re.IGNORECASE)


def locality_regex(criteria: Criteria) -> re.Pattern:
    aliases = [a for names in criteria.localities.values() for a in names]
    aliases += list(criteria.localities) + list(criteria.nearby_ok)
    return _locality_re(tuple(aliases))


def prefilter(post: RawPost, criteria: Criteria) -> tuple[bool, str]:
    """Return (pass, reason)."""
    text = post.text or ""
    if len(text.strip()) < MIN_CHARS and not post.image_urls:
        return False, "too short, no image"
    if m := KEYWORDS.search(text):
        return True, f"keyword {m.group(0)!r}"
    if m := locality_regex(criteria).search(text):
        return True, f"locality {m.group(0)!r}"
    return False, "no keyword or locality"
