"""HARNESS.md §7.1's lookup_asin row: ordinal resolution ("the second one"
-> 1-based index 1), out-of-range ordinal -> a clear error object (never an
`IndexError`/crash), pronoun resolution, and explicit-asin lookups.

`resolve_reference` is pure (no DB) and covers the reference-resolution
logic in isolation; `lookup_asin_impl` covers the DB round-trip on top of
it, seeded with a couple of hand-picked `Asin` rows.
"""
import pytest

from app.agent.tools import lookup_asin_impl, resolve_reference
from app.models.asin import Asin

pytestmark = pytest.mark.asyncio

_LAST_RESULTS = ["B0000000A1", "B0000000A2", "B0000000A3"]


# --- resolve_reference (pure) -------------------------------------------


def test_explicit_asin_wins_over_reference():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS,
        resolved_entity=None,
        asin="b0000000a9",
        reference={"ordinal": 1},
    )
    assert result == {"asin": "B0000000A9"}  # normalized upper, explicit wins


def test_ordinal_second_one_is_1based_index_1():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS, resolved_entity=None, reference={"ordinal": 2}
    )
    assert result == {"asin": "B0000000A2"}


def test_ordinal_first_is_index_0():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS, resolved_entity=None, reference={"ordinal": 1}
    )
    assert result == {"asin": "B0000000A1"}


def test_ordinal_out_of_range_returns_error_not_crash():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS, resolved_entity=None, reference={"ordinal": 10}
    )
    assert "error" in result
    assert "out of range" in result["error"].lower()


def test_ordinal_zero_or_negative_is_out_of_range_not_crash():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS, resolved_entity=None, reference={"ordinal": 0}
    )
    assert "error" in result
    result_neg = resolve_reference(
        last_result_asins=_LAST_RESULTS, resolved_entity=None, reference={"ordinal": -1}
    )
    assert "error" in result_neg


def test_ordinal_out_of_range_on_empty_results_does_not_crash():
    result = resolve_reference(
        last_result_asins=[], resolved_entity=None, reference={"ordinal": 1}
    )
    assert "error" in result


def test_pronoun_resolves_to_resolved_entity():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS,
        resolved_entity="B0000000A2",
        reference={"pronoun": True},
    )
    assert result == {"asin": "B0000000A2"}


def test_pronoun_falls_back_to_single_result_when_no_resolved_entity():
    result = resolve_reference(
        last_result_asins=["B0000000A1"], resolved_entity=None, reference={"pronoun": True}
    )
    assert result == {"asin": "B0000000A1"}


def test_pronoun_with_no_context_and_multiple_results_is_an_error():
    result = resolve_reference(
        last_result_asins=_LAST_RESULTS, resolved_entity=None, reference={"pronoun": True}
    )
    assert "error" in result


def test_no_asin_and_no_reference_is_an_error():
    result = resolve_reference(last_result_asins=_LAST_RESULTS, resolved_entity=None)
    assert "error" in result


# --- lookup_asin_impl (DB round-trip) -----------------------------------


async def test_lookup_asin_impl_returns_detail_and_resolved_entity(db_session):
    db_session.add(
        Asin(asin="B0000000A2", title="Widget", eligible=True, computed_roi_pct=42.0)
    )
    await db_session.flush()

    result = await lookup_asin_impl(
        db_session,
        last_result_asins=_LAST_RESULTS,
        resolved_entity=None,
        reference={"ordinal": 2},
    )
    assert result["resolved_entity"] == "B0000000A2"
    assert result["detail"]["asin"] == "B0000000A2"
    assert result["detail"]["computed_roi_pct"] == 42.0


async def test_lookup_asin_impl_unknown_asin_in_catalog_is_an_error(db_session):
    result = await lookup_asin_impl(
        db_session, last_result_asins=[], resolved_entity=None, asin="B0DOESNOTEXIST"
    )
    assert "error" in result
    assert "not found" in result["error"].lower()


async def test_lookup_asin_impl_out_of_range_ordinal_never_hits_db(db_session):
    result = await lookup_asin_impl(
        db_session,
        last_result_asins=_LAST_RESULTS,
        resolved_entity=None,
        reference={"ordinal": 99},
    )
    assert "error" in result
    assert "resolved_entity" not in result
