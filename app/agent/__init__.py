"""Phase 4 — LangGraph agent orchestration. See ARCHITECTURE.md §4/§5.

Module map:
    state.py          AgentState — MessagesState + active_filters/last_result_asins/resolved_entity
    checkpointer.py    AsyncPostgresSaver factory (short-term memory, keyed by thread_id)
    store.py           AsyncPostgresStore factory (long-term memory, keyed by user_id)
    tools.py           The 6 tools from ARCHITECTURE.md §4.2 -- pure impls + LLM-facing schemas
    graph.py           The hand-rolled StateGraph: agent node + tools node + conditional edge
    usage.py           Token usage collection -> llm_usage_log
"""
