"""HARNESS.md §7.1's update_preferences row: `budget_per_unit` is REPLACE
semantics (last value wins, never averaged/appended), `exclude_asin` is
APPEND semantics (read-modify-write, second call doesn't clobber the
first).

Uses `langgraph.store.memory.InMemoryStore` -- any `BaseStore`-conformant
implementation works here since `update_preferences_impl`/`get_preferences`
only call `aget`/`aput`, so this avoids needing a real Postgres
`AsyncPostgresStore` connection for a pure read-modify-write logic test.
"""
import pytest
from langgraph.store.memory import InMemoryStore

from app.agent.tools import get_preferences, update_preferences_impl

pytestmark = pytest.mark.asyncio


async def test_budget_per_unit_is_replace_not_additive():
    store = InMemoryStore()
    await update_preferences_impl(store, "user-1", budget_per_unit=20)
    await update_preferences_impl(store, "user-1", budget_per_unit=50)

    prefs = await get_preferences(store, "user-1")
    assert prefs["budget_per_unit"] == 50  # not 70, not [20, 50]


async def test_exclude_asin_appends_does_not_clobber_prior_exclusions():
    store = InMemoryStore()
    await update_preferences_impl(store, "user-1", exclude_asin="B00AAAAAAA")
    await update_preferences_impl(store, "user-1", exclude_asin="B00BBBBBBB")

    prefs = await get_preferences(store, "user-1")
    assert prefs["excluded_asins"] == ["B00AAAAAAA", "B00BBBBBBB"]


async def test_exclude_asin_is_idempotent_no_duplicate_on_repeat():
    store = InMemoryStore()
    await update_preferences_impl(store, "user-1", exclude_asin="B00AAAAAAA")
    await update_preferences_impl(store, "user-1", exclude_asin="B00AAAAAAA")

    prefs = await get_preferences(store, "user-1")
    assert prefs["excluded_asins"] == ["B00AAAAAAA"]


async def test_notes_accumulate():
    store = InMemoryStore()
    await update_preferences_impl(store, "user-1", note="likes electronics")
    await update_preferences_impl(store, "user-1", note="prefers fast turnover")

    prefs = await get_preferences(store, "user-1")
    assert prefs["notes"] == ["likes electronics", "prefers fast turnover"]


async def test_preferences_are_scoped_per_user():
    store = InMemoryStore()
    await update_preferences_impl(store, "user-1", budget_per_unit=20)
    await update_preferences_impl(store, "user-2", budget_per_unit=999)

    prefs_1 = await get_preferences(store, "user-1")
    prefs_2 = await get_preferences(store, "user-2")
    assert prefs_1["budget_per_unit"] == 20
    assert prefs_2["budget_per_unit"] == 999


async def test_get_preferences_defaults_when_nothing_stored_yet():
    store = InMemoryStore()
    prefs = await get_preferences(store, "brand-new-user")
    assert prefs == {"budget_per_unit": None, "excluded_asins": [], "notes": []}


async def test_a_call_that_touches_only_one_field_leaves_the_others_alone():
    store = InMemoryStore()
    await update_preferences_impl(store, "user-1", budget_per_unit=20, exclude_asin="B00AAAAAAA")
    await update_preferences_impl(store, "user-1", note="just a note")

    prefs = await get_preferences(store, "user-1")
    assert prefs["budget_per_unit"] == 20
    assert prefs["excluded_asins"] == ["B00AAAAAAA"]
    assert prefs["notes"] == ["just a note"]
