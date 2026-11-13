"""The synthetic eval set must agree with the matcher: each row's `expect` is match(gold)."""
import pytest

from flatwatch.config import EXAMPLE_CONFIG, load_config
from flatwatch.evals import load_synthetic
from flatwatch.match import match
from flatwatch.models import Listing

CRIT = load_config(EXAMPLE_CONFIG).criteria
ROWS = load_synthetic()


def test_synthetic_set_present_and_balanced():
    tiers = {r["expect"] for r in ROWS}
    assert len(ROWS) >= 20 and tiers == {"P1", "P2", "DIGEST", "DROP"}


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_expect_matches_gold(row):
    assert match(Listing.model_validate(row["gold"]), CRIT).tier == row["expect"]
