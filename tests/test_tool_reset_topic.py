"""HARNESS.md §7.1's reset_topic row: clears the short-term graph-state
fields (`active_filters`/`last_result_asins`/`resolved_entity`) but must
NEVER touch the Store-backed long-term preferences.

`reset_topic_impl` itself takes no store/session at all -- by construction
it's incapable of touching the Store (see app/agent/tools.py). This test
still exercises that guarantee end-to-end: set preferences via
`update_preferences_impl` on a Store, call `reset_topic_impl`, and confirm
the Store is untouched.
"""
import pytest
from langgraph.store.memory import InMemoryStore

from app.agent.tools import get_preferences, reset_topic_impl, update_preferences_impl

pytestmark = pytest.mark.asyncio


def test_reset_topic_clears_all_three_short_term_fields():
    result = reset_topic_impl()
    assert result == {
        "active_filters": {},
        "last_result_asins": [],
        "resolved_entity": None,
    }


def test_reset_topic_takes_no_arguments():
    # Signature check -- HARNESS.md/ARCHITECTURE.md §4.2: reset_topic has
    # no parameters at all.
    import inspect

    sig = inspect.signature(reset_topic_impl)
    assert len(sig.parameters) == 0


async def test_reset_topic_does_not_touch_store_backed_preferences():
    store = InMemoryStore()
    await update_preferences_impl(
        store, "user-1", budget_per_unit=20, exclude_asin="B00AAAAAAA", note="a note"
    )
    before = await get_preferences(store, "user-1")

    reset_topic_impl()  # no store/session param to even pass -- can't touch it

    after = await get_preferences(store, "user-1")
    assert after == before
    assert after["budget_per_unit"] == 20
    assert after["excluded_asins"] == ["B00AAAAAAA"]
