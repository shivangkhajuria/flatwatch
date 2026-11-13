"""Pure rule-based matcher (PLAN.md §2, §4.6). No LLM, no I/O.

    match(listing, criteria) -> Decision(tier=P1|P2|DIGEST|DROP, reasons, tags)

Order: hard excludes -> locality -> tier shape -> rent -> P2 extras.
Anything plausible but uncertain collects a digest reason; any digest reason
downgrades an otherwise-instant match to DIGEST.
"""

from __future__ import annotations

from .config import Criteria
from .models import Decision, Listing


def _drop(reason: str, tags: list[str]) -> Decision:
    return Decision(tier="DROP", reasons=[reason], tags=tags)


def _tags(listing: Listing, criteria: Criteria) -> list[str]:
    tags = []
    if listing.balcony and "balcony" in criteria.bonus:
        tags.append("balcony")
    if listing.broker and criteria.flag_brokers:
        tags.append("broker")
    return tags


def match(listing: Listing, criteria: Criteria) -> Decision:
    tags = _tags(listing, criteria)
    ex = criteria.exclude

    # 1. hard excludes
    if listing.intent != "offering":
        return _drop(f"intent={listing.intent}", tags)
    if listing.intent in ex.intent:
        return _drop(f"excluded intent {listing.intent}", tags)
    if listing.offering_type in ex.offering_type:
        return _drop(f"excluded offering_type {listing.offering_type}", tags)
    if listing.unit in ex.unit:
        return _drop(f"excluded unit {listing.unit}", tags)
    if listing.gender_pref in ex.gender_pref:
        return _drop(f"excluded gender_pref {listing.gender_pref}", tags)

    digest: list[str] = []

    # 2. locality
    if listing.locality in criteria.localities:
        pass
    elif listing.locality == "nearby" and criteria.nearby_ok:
        digest.append("nearby locality")
        tags.append("nearby")
    elif listing.locality == "unknown":
        digest.append("locality unknown")
    else:
        return _drop(f"locality {listing.locality}", tags)

    # 3. tier shape
    p1, p2 = criteria.tiers.P1, criteria.tiers.P2
    ot, unit = listing.offering_type, listing.unit
    if ot in p1.offering_type:
        tier, max_rent = "P1", p1.max_rent_inr
        if unit not in p1.unit:
            if unit == "unknown":
                digest.append("unit unknown")
            else:
                return _drop(f"P1 needs {'/'.join(p1.unit)}, got {unit}", tags)
    elif ot in p2.offering_type:
        tier, max_rent = "P2", p2.max_rent_inr
        if unit not in p2.unit:
            if unit == "unknown":
                digest.append("unit unknown")
            else:
                return _drop(f"P2 needs {'/'.join(p2.unit)}, got {unit}", tags)
    elif ot is None:
        # offering, but whole-flat vs room is unclear
        tier, max_rent = "DIGEST", max(p1.max_rent_inr, p2.max_rent_inr)
        digest.append("offering_type unknown")
    else:
        return _drop(f"offering_type {ot}", tags)

    # 4. rent
    rent = listing.rent_inr
    if rent is None:
        if criteria.rent_unknown == "drop":
            return _drop("rent unknown", tags)
        digest.append("rent unknown")
    elif rent > max_rent:
        ceiling = max_rent * (100 + criteria.over_budget_digest_pct) / 100
        if rent > ceiling:
            return _drop(f"rent {rent} > {int(ceiling)}", tags)
        digest.append(f"rent {rent} over budget {max_rent}")
        tags.append("over budget")

    # 5. P2 extras (gender + private bathroom)
    if tier == "P2":
        g = listing.gender_pref
        if g == "unknown":
            digest.append("gender unknown")
        elif g not in p2.gender_pref:
            return _drop(f"gender {g}", tags)
        if p2.private_bathroom:
            if listing.private_bathroom is None:
                digest.append("private bathroom unknown")
            elif listing.private_bathroom is False:
                return _drop("shared bathroom", tags)

    if digest or tier == "DIGEST":
        return Decision(tier="DIGEST", reasons=digest, tags=tags)
    return Decision(tier=tier, reasons=[f"{tier} match"], tags=tags)
