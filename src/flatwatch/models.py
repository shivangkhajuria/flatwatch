"""Shared Pydantic models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class RawPost(BaseModel):
    source: Literal["facebook", "reddit"]
    source_key: str                 # normalised group URL / subreddit name
    post_id: str
    author: str | None = None
    text: str = ""
    url: str | None = None
    image_urls: list[str] = []
    created_at: datetime | None = None   # timezone-aware (UTC)
    shared_from: str | None = None       # permalink of the original when this is a reshare (its text isn't in the feed payload)
    via: Literal["graphql", "html", "dom", "json"] = "graphql"

    @property
    def id(self) -> str:
        return f"{self.source}:{self.post_id}"


class Listing(BaseModel):
    """What the LLM extracts from one post (PLAN.md §4.5)."""

    intent: Literal["offering", "seeking", "other"]
    offering_type: Literal["full_flat", "flatmate", "replacement", "pg_coliving", "other"] | None = None
    unit: Literal["1rk", "studio", "1bhk", "2bhk", "3bhk", "4bhk_plus", "unknown"] = "unknown"
    # 1rk = single room + kitchen (excluded); studio = self-contained open-plan flat (allowed)
    locality_raw: str | None = None
    locality: str = "unknown"              # a key of criteria.localities, or nearby | other | unknown
    rent_inr: int | None = None            # monthly; for flatmate posts = rent of the room on offer
    deposit_inr: int | None = None
    available_from: date | None = None
    furnishing: Literal["full", "semi", "unfurnished", "unknown"] = "unknown"
    gender_pref: Literal["male", "female", "any", "unknown"] = "unknown"
    private_bathroom: bool | None = None   # attached/private/en-suite washroom for the offered room
    balcony: bool | None = None
    broker: bool | None = None
    contact: str | None = None
    one_line: str = Field("", max_length=200)  # ≤ 90 chars asked; tolerate overshoot
    confidence: float = Field(0.5, ge=0, le=1)


Tier = Literal["P1", "P2", "DIGEST", "DROP"]


class Decision(BaseModel):
    tier: Tier
    reasons: list[str] = []
    tags: list[str] = []
    unverified: bool = False   # regex-only fallback path (LLM unavailable)
