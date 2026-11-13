"""Exhaustive matcher tests against the criteria in the example config."""

import pytest

from flatwatch.config import EXAMPLE_CONFIG, load_config
from flatwatch.match import match
from flatwatch.models import Listing

CRIT = load_config(EXAMPLE_CONFIG).criteria


def L(**kw):
    base = dict(intent="offering", offering_type="full_flat", unit="1bhk", locality="Indiranagar",
                rent_inr=25000, gender_pref="unknown", private_bathroom=None)
    base.update(kw)
    return Listing(**base)


def P2(**kw):
    base = dict(offering_type="flatmate", unit="2bhk", gender_pref="male", private_bathroom=True, rent_inr=18000)
    base.update(kw)
    return L(**base)


# ---------------------------------------------------------------- P1 -------
@pytest.mark.parametrize("unit", ["1bhk", "studio"])
@pytest.mark.parametrize("loc", ["HSR", "Indiranagar", "Domlur"])
def test_p1_happy(unit, loc):
    assert match(L(unit=unit, locality=loc), CRIT).tier == "P1"


def test_p1_gender_and_bathroom_irrelevant():
    assert match(L(gender_pref="unknown", private_bathroom=None), CRIT).tier == "P1"
    assert match(L(gender_pref="male", private_bathroom=False), CRIT).tier == "P1"
    assert match(L(gender_pref="any"), CRIT).tier == "P1"


@pytest.mark.parametrize("unit", ["2bhk", "3bhk", "4bhk_plus"])
def test_p1_wrong_unit_dropped(unit):
    assert match(L(unit=unit), CRIT).tier == "DROP"


def test_p1_unit_unknown_digest():
    d = match(L(unit="unknown"), CRIT)
    assert d.tier == "DIGEST" and "unit unknown" in d.reasons


# ---------------------------------------------------------------- P2 -------
@pytest.mark.parametrize("ot", ["flatmate", "replacement"])
@pytest.mark.parametrize("unit", ["2bhk", "3bhk"])
@pytest.mark.parametrize("g", ["male", "any"])
def test_p2_happy(ot, unit, g):
    assert match(P2(offering_type=ot, unit=unit, gender_pref=g), CRIT).tier == "P2"


def test_p2_gender_unknown_digest():
    d = match(P2(gender_pref="unknown"), CRIT)
    assert d.tier == "DIGEST" and "gender unknown" in d.reasons


def test_p2_bathroom_unknown_digest():
    d = match(P2(private_bathroom=None), CRIT)
    assert d.tier == "DIGEST" and "private bathroom unknown" in d.reasons


def test_p2_shared_bathroom_dropped():
    assert match(P2(private_bathroom=False), CRIT).tier == "DROP"


@pytest.mark.parametrize("unit", ["1bhk", "studio", "4bhk_plus"])
def test_p2_wrong_unit_dropped(unit):
    assert match(P2(unit=unit), CRIT).tier == "DROP"


def test_p2_unit_unknown_digest():
    assert match(P2(unit="unknown"), CRIT).tier == "DIGEST"


# ---------------------------------------------------------- excludes -------
@pytest.mark.parametrize("intent", ["seeking", "other"])
def test_non_offering_dropped(intent):
    assert match(L(intent=intent), CRIT).tier == "DROP"
    assert match(P2(intent=intent), CRIT).tier == "DROP"


def test_1rk_dropped_even_cheap_and_local():
    assert match(L(unit="1rk", rent_inr=12000), CRIT).tier == "DROP"


def test_pg_coliving_dropped():
    assert match(L(offering_type="pg_coliving", unit="unknown"), CRIT).tier == "DROP"


def test_female_dropped_for_both_tiers():
    assert match(L(gender_pref="female"), CRIT).tier == "DROP"
    assert match(P2(gender_pref="female"), CRIT).tier == "DROP"


def test_offering_type_other_dropped():
    assert match(L(offering_type="other"), CRIT).tier == "DROP"


def test_offering_type_none_digest():
    d = match(L(offering_type=None), CRIT)
    assert d.tier == "DIGEST" and "offering_type unknown" in d.reasons


# ---------------------------------------------------------- locality -------
def test_other_locality_dropped():
    assert match(L(locality="other"), CRIT).tier == "DROP"


def test_unknown_locality_digest():
    d = match(L(locality="unknown"), CRIT)
    assert d.tier == "DIGEST" and "locality unknown" in d.reasons


def test_nearby_dropped_when_nearby_ok_empty():
    assert CRIT.nearby_ok == []
    assert match(L(locality="nearby"), CRIT).tier == "DROP"


def test_nearby_digest_when_enabled():
    crit = CRIT.model_copy(update={"nearby_ok": ["Koramangala"]})
    d = match(L(locality="nearby"), crit)
    assert d.tier == "DIGEST" and "nearby" in d.tags


# -------------------------------------------------------------- rent -------
@pytest.mark.parametrize("rent,tier", [
    (8000, "P1"), (19999, "P1"), (20000, "P1"), (30000, "P1"),   # cheaper is fine
    (30001, "DIGEST"), (34500, "DIGEST"),                         # within 15%
    (34501, "DROP"), (60000, "DROP"),
])
def test_rent_bands_p1(rent, tier):
    assert match(L(rent_inr=rent), CRIT).tier == tier


@pytest.mark.parametrize("rent,tier", [(15000, "P2"), (30000, "P2"), (32000, "DIGEST"), (35000, "DROP")])
def test_rent_bands_p2(rent, tier):
    assert match(P2(rent_inr=rent), CRIT).tier == tier


def test_over_budget_tagged():
    d = match(L(rent_inr=32000), CRIT)
    assert "over budget" in d.tags


def test_rent_unknown_dropped_by_default():
    # decided 13 Nov 2026: posts that don't mention rent are never sent
    assert CRIT.rent_unknown == "drop"
    assert match(L(rent_inr=None), CRIT).tier == "DROP"
    assert match(P2(rent_inr=None), CRIT).tier == "DROP"


def test_rent_unknown_digest_policy_still_supported():
    crit = CRIT.model_copy(update={"rent_unknown": "digest"})
    d = match(L(rent_inr=None), crit)
    assert d.tier == "DIGEST" and "rent unknown" in d.reasons


# -------------------------------------------------------------- tags -------
def test_balcony_and_broker_tags_never_change_tier():
    d = match(L(balcony=True, broker=True), CRIT)
    assert d.tier == "P1" and d.tags == ["balcony", "broker"]
    assert match(L(balcony=False, broker=False), CRIT).tags == []


def test_broker_not_tagged_when_flag_off():
    crit = CRIT.model_copy(update={"flag_brokers": False})
    assert "broker" not in match(L(broker=True), crit).tags


def test_multiple_uncertainties_all_listed():
    d = match(P2(gender_pref="unknown", private_bathroom=None, locality="unknown"), CRIT)
    assert d.tier == "DIGEST"
    assert set(d.reasons) == {"gender unknown", "private bathroom unknown", "locality unknown"}


def test_drop_beats_digest():
    # unknown locality would digest, but 2BHK whole flat is a hard drop
    assert match(L(locality="unknown", unit="2bhk"), CRIT).tier == "DROP"


def test_pure_function_does_not_mutate():
    lst = L()
    before = lst.model_dump()
    match(lst, CRIT)
    assert lst.model_dump() == before
