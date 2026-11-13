import pytest

from flatwatch.config import EXAMPLE_CONFIG, load_config
from flatwatch.models import RawPost
from flatwatch.prefilter import prefilter

CRIT = load_config(EXAMPLE_CONFIG).criteria


def P(text, images=()):
    return RawPost(source="facebook", source_key="g", post_id="1", text=text, image_urls=list(images))


@pytest.mark.parametrize("text", [
    "Spacious 1BHK available, 25k", "2 bhk flat for rent", "3BHK room on rent", "Studio apartment for rent",
    "Looking for a flatmate", "roommate needed", "Replacement needed for my room", "Room available from Dec",
    "Vacant room in a gated society", "TO-LET board seen near 12th main", "to let: furnished house",
    "Available from 1st December, call", "Room with attached bathroom", "private washroom, 18k",
])
def test_keywords_pass(text):
    assert prefilter(P(text), CRIT)[0]


@pytest.mark.parametrize("text", [
    "House near HAL 2nd Stage, 28k rent", "Anyone near Domlur want to share?", "indira nagar place, dm",
    "Embassy Golf Links side, family house", "hsr   layout house for rent, 3 rooms",
])
def test_locality_alias_passes(text):
    ok, reason = prefilter(P(text), CRIT)
    assert ok and reason.startswith(("locality", "keyword"))


@pytest.mark.parametrize("text", [
    "Selling my projector, 1 month old", "Anyone know a good electrician in Whitefield?",
    "Happy Diwali everyone in the group!",
])
def test_unrelated_fails(text):
    assert not prefilter(P(text), CRIT)[0]


def test_short_without_image_fails_with_image_passes_to_keyword_check():
    assert prefilter(P("DM"), CRIT) == (False, "too short, no image")
    assert prefilter(P("1BHK"), CRIT)[0] is False  # 4 chars, no image
    assert prefilter(P("1BHK", images=["x.jpg"]), CRIT)[0] is True


def test_alias_word_boundary():
    # "HSR" must not match inside another word
    assert not prefilter(P("Our HSRP number plates are ready, call us"), CRIT)[0]


@pytest.mark.parametrize("text", ["1RK - Brookefield - 13k", "1 rk near socials, 12k", "Accomodation sharing near Budigere"])
def test_pf2_additions(text):
    assert prefilter(P(text), CRIT)[0]
