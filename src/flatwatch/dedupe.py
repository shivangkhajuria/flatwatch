"""Cross-post detection (PLAN.md §4.3).

A new post is a duplicate of an original seen in the last 7 days when either
  (a) normalised text token_set_ratio >= 90, or
  (b) same Indian mobile number and same locality hint,
AND, for both rules, the same "numbers signature" (rents, deposits, BHK counts).

The numbers check is a deviation from PLAN.md, prompted by real data: one broker
posted six different Kudlu Gate units from one template, differing only in
rent/deposit. Without it all six would collapse into one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .config import Criteria
from .prefilter import locality_regex

WINDOW_S = 7 * 86400
FUZZ_THRESHOLD = 90
MIN_FUZZ_CHARS = 40   # very short texts ("1BHK available") match everything

PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{2,3})+|\d{1,6}(?:\.\d+)?)\s*(k|l|lakh|lakhs|lac|bhk|rk)?\b", re.I)


@dataclass(frozen=True)
class Fingerprint:
    norm: str
    phone: str | None
    locality: str | None
    numbers: str


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")   # folds 𝗕𝗢𝗟𝗗 unicode letters to ASCII
    text = URL_RE.sub(" ", text.lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def phone_of(text: str) -> str | None:
    m = PHONE_RE.search(unicodedata.normalize("NFKC", text or ""))
    return m.group(1) + m.group(2) if m else None


def numbers_signature(text: str) -> str:
    text = PHONE_RE.sub(" ", URL_RE.sub(" ", unicodedata.normalize("NFKC", text or "").lower()))
    out = set()
    for num, unit in NUM_RE.findall(text):
        n = num.replace(",", "")
        unit = (unit or "").lower()
        if unit in ("lakh", "lakhs", "lac"):
            unit = "l"
        # only money / configuration numbers; ignore "Sector 2", "3 months", "2nd floor"
        if unit or float(n) >= 1000:
            out.add(n + unit)
    return ",".join(sorted(out))


def locality_hint(text: str, criteria: Criteria) -> str | None:
    """Canonical locality key of the first alias found in the text, for rule (b)."""
    m = locality_regex(criteria).search(unicodedata.normalize("NFKC", text or ""))
    if not m:
        return None
    hit = " ".join(m.group(0).lower().split())
    for key, aliases in criteria.localities.items():
        if hit == key.lower() or hit in {" ".join(a.lower().split()) for a in aliases}:
            return key
    return hit


def fingerprint(text: str, criteria: Criteria) -> Fingerprint:
    return Fingerprint(normalize(text), phone_of(text), locality_hint(text, criteria), numbers_signature(text))


def is_duplicate(a: Fingerprint, b: Fingerprint) -> bool:
    if a.numbers != b.numbers:
        return False
    if a.phone and a.phone == b.phone and a.locality and a.locality == b.locality:
        return True
    if min(len(a.norm), len(b.norm)) >= MIN_FUZZ_CHARS:
        return fuzz.token_set_ratio(a.norm, b.norm) >= FUZZ_THRESHOLD
    return False


def find_original(fp: Fingerprint, candidates: list[tuple[str, Fingerprint]]) -> str | None:
    for cid, cfp in candidates:
        if is_duplicate(fp, cfp):
            return cid
    return None
