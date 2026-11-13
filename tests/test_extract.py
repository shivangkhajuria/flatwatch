import asyncio
import json
from datetime import datetime, timezone

import pytest

from flatwatch import extract
from flatwatch.config import EXAMPLE_CONFIG, load_config
from flatwatch.extract import (
    ExtractError,
    Usage,
    build_system_prompt,
    extract_batch,
    fallback_decision,
    format_batch,
    parse_response,
)
from flatwatch.models import RawPost

CFG = load_config(EXAMPLE_CONFIG)


def P(pid, text, **kw):
    return RawPost(source="facebook", source_key="g", post_id=pid, text=text,
                   created_at=datetime(2026, 11, 12, 10, tzinfo=timezone.utc), **kw)


GOOD = {"intent": "offering", "offering_type": "full_flat", "unit": "1bhk", "locality": "HSR",
        "rent_inr": 25000, "one_line": "1BHK HSR 25k", "confidence": 0.9}


def test_prompt_contains_aliases_and_rules():
    s = build_system_prompt(CFG.criteria)
    assert "HAL 2nd Stage" in s and "Embassy Golf Links" in s
    assert "1rk" in s and "studio" in s
    assert 'never output "nearby"' in s   # nearby_ok is empty


def test_format_batch_numbers_and_flags():
    out = format_batch([P("1", "a 1BHK", image_urls=["x"]), P("2", "", shared_from="https://fb/p/1")])
    assert "### Post 1" in out and "1 image(s)" in out
    assert "### Post 2" in out and "reshare" in out and "(no text)" in out


def test_parse_with_prose_and_fences():
    text = "Here you go:\n```json\n" + json.dumps([{"n": 1, **GOOD}]) + "\n```"
    assert parse_response(text, 1)[1].unit == "1bhk"


def test_parse_partial_keeps_valid():
    bad = {"n": 2, "intent": "maybe"}
    out = parse_response(json.dumps([{"n": 1, **GOOD}, bad]), 2)
    assert set(out) == {1}


@pytest.mark.parametrize("text", ["no json here", "[not json]", json.dumps([{"n": 1, "intent": "x"}])])
def test_parse_unusable_raises(text):
    with pytest.raises(ExtractError):
        parse_response(text, 1)


def _fake(responses):
    calls = []

    async def fake_call(system, user, model):
        calls.append(user)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r, Usage("agent_sdk", model, 0, 100, 50, 0.001)

    return fake_call, calls


def test_extract_batch_maps_ids_and_records_usage(monkeypatch):
    fake, calls = _fake([json.dumps([{"n": 1, **GOOD}, {"n": 2, **GOOD, "intent": "seeking", "offering_type": None}])])
    monkeypatch.setattr(extract, "_call_agent_sdk", fake)
    res = asyncio.run(extract_batch([P("a", "x"), P("b", "y")], CFG))
    assert res.listings["facebook:a"].intent == "offering"
    assert res.listings["facebook:b"].intent == "seeking"
    assert res.failed == [] and res.prompt_version == extract.PROMPT_VERSION
    assert res.usage[0].posts == 2 and len(calls) == 1


def test_extract_batch_retries_once_then_raises(monkeypatch):
    fake, calls = _fake(["garbage", "still garbage"])
    monkeypatch.setattr(extract, "_call_agent_sdk", fake)
    with pytest.raises(ExtractError, match="twice"):
        asyncio.run(extract_batch([P("a", "x")], CFG))
    assert len(calls) == 2


def test_extract_batch_recovers_on_retry(monkeypatch):
    fake, calls = _fake([ExtractError("boom"), json.dumps([{"n": 1, **GOOD}])])
    monkeypatch.setattr(extract, "_call_agent_sdk", fake)
    res = asyncio.run(extract_batch([P("a", "x")], CFG))
    assert "facebook:a" in res.listings and len(res.usage) == 1


def test_agent_sdk_refuses_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    with pytest.raises(ExtractError, match="ANTHROPIC_API_KEY"):
        asyncio.run(extract._call_agent_sdk("s", "u", "m"))


@pytest.mark.parametrize("text,tier", [
    ("1BHK available in HSR Layout from Dec, 25k", "DIGEST"),
    ("Flatmate needed for 2BHK in Indiranagar, attached bath, 18k", "DIGEST"),
    ("Flatmate needed for 2BHK in Indiranagar, attached bath", "DROP"),     # no rent -> never sent
    ("Looking for a 1BHK in HSR Layout, budget 25k", "DROP"),     # seeking
    ("1BHK available in Whitefield", "DROP"),                      # no target locality
    ("Domlur is lovely this time of year", "DROP"),                # no listing keyword
])
def test_fallback(text, tier):
    d = fallback_decision(P("1", text), CFG.criteria)
    assert d.tier == tier and d.unverified
    if tier == "DIGEST":
        assert "unverified" in d.tags


def test_auth_error_not_retried(monkeypatch):
    calls = []

    async def fake(system, user, model):
        calls.append(1)
        raise extract.AuthError("Claude login problem")

    monkeypatch.setattr(extract, "_call_agent_sdk", fake)
    with pytest.raises(extract.AuthError):
        asyncio.run(extract_batch([P("a", "x")], CFG))
    assert len(calls) == 1


@pytest.mark.parametrize("raw,canon", [
    ("Sector 5", "HSR"), ("hsr  layout", "HSR"), ("Indira Nagar", "Indiranagar"), ("HAL 2nd Stage", "Indiranagar"),
    ("EGL", "Domlur"), ("HSR", "HSR"), ("other", "other"), ("Koramangala", "unknown"),
])
def test_normalize_locality(raw, canon):
    assert extract.normalize_locality(raw, CFG.criteria) == canon


def test_alias_locality_no_longer_drops_post():
    obj = {"n": 1, **GOOD, "locality": "Sector 5"}
    assert parse_response(json.dumps([obj]), 1, CFG.criteria)[1].locality == "HSR"


def test_unknown_area_and_bad_enum_coerced_not_dropped():
    obj = {"n": 1, **GOOD, "locality": "Koramangala", "unit": "2.5bhk", "available_from": "next month"}
    lst = parse_response(json.dumps([obj]), 1, CFG.criteria)[1]
    assert (lst.locality, lst.unit, lst.available_from) == ("unknown", "unknown", None)
    assert lst.rent_inr == 25000  # good fields kept


def test_bad_intent_still_drops_post():
    obj = {"n": 1, **GOOD, "intent": "selling"}
    with pytest.raises(ExtractError):
        parse_response(json.dumps([obj]), 1, CFG.criteria)
