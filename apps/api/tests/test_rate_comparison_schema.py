import pytest
from pydantic import ValidationError

from app.schemas import RateComparisonRequest


def test_accepts_rating_alone():
    req = RateComparisonRequest(rating=4)
    assert req.rating == 4
    assert req.preferredProviderId is None


def test_accepts_preferred_provider_alone():
    req = RateComparisonRequest(preferredProviderId="grok")
    assert req.preferredProviderId == "grok"
    assert req.rating is None


def test_accepts_both_together():
    req = RateComparisonRequest(rating=5, preferredProviderId="gemma")
    assert req.rating == 5
    assert req.preferredProviderId == "gemma"


def test_rejects_rating_above_5():
    with pytest.raises(ValidationError):
        RateComparisonRequest(rating=6)


def test_rejects_rating_below_1():
    with pytest.raises(ValidationError):
        RateComparisonRequest(rating=0)


def test_accepts_neither_at_the_schema_level():
    # The "at least one required" rule is enforced by the router, not
    # the schema -- both fields are legitimately optional individually.
    req = RateComparisonRequest()
    assert req.rating is None
    assert req.preferredProviderId is None
