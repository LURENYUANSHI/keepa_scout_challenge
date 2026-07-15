"""`AgentState` — the custom StateGraph state schema from ARCHITECTURE.md §3.3/§4.

Extends LangGraph's built-in `MessagesState` (which gives us `messages:
Annotated[list[AnyMessage], add_messages]` for free) with the three
short-term fields that used to live in a `chat_sessions` DB column in an
earlier design and now live purely in checkpointed graph state (see
ARCHITECTURE.md §2's "关键约束" bullet on `chat_sessions`):

    active_filters      structured filter/sort/limit state accumulated by
                         build_filter_sql across turns (see HARNESS.md §7.2
                         scenario A/D: accumulation + threshold replacement)
    last_result_asins    the ASIN list from the most recent
                         build_filter_sql/run_readonly_sql result, used by
                         lookup_asin to resolve "the second one"/"it"
    resolved_entity      the ASIN lookup_asin most recently resolved to,
                         used to keep resolving "it"/"that ASIN" across
                         several follow-up turns (HARNESS.md §7.2 scenario B)

These three fields intentionally use the *default* LangGraph reducer
(last-write-wins, not a merge/append reducer) -- app/agent/graph.py's tools
node computes the full next value itself (merging old + new filters, or
clearing all three for reset_topic) before returning it, so a plain
overwrite is exactly what's wanted here. `messages` is the only field that
needs the special `add_messages` append-reducer, which MessagesState
already wires up.
"""
from langgraph.graph import MessagesState


class AgentState(MessagesState):
    active_filters: dict
    last_result_asins: list[str]
    resolved_entity: str | None
