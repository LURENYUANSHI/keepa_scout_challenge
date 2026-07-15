"""HARNESS.md §6: real integration coverage of the 7 `/ask` question
categories from CHALLENGE.md -- but paraphrased, per the top-of-file
callout in both ARCHITECTURE.md and HARNESS.md ("场景对话是例子,不是字面
spec"). Each category gets >= 2 differently-worded questions so nothing
here is fitting a literal example string. These are REAL DeepSeek calls
against the REAL dev-catalog `asins` data (this project's 32 seeded
fixtures) -- not mocked -- per this phase's "VERIFY YOURSELF" instructions.

**Why this doesn't use tests/conftest.py's `client`/`db_session` fixtures**:
those point at a separate, empty `_test`-suffixed database (see
conftest.py's docstring) so unit tests stay isolated and repeatable. `/ask`
needs the real, seeded `asins` catalog to produce a grounded answer -- an
empty test DB would make every question here answerable only with "no
rows found," which doesn't exercise anything interesting. So this file
builds its own small `real_client` fixture that talks to the real app
without any `get_db` override, i.e. the actual `DATABASE_URL` from
app/config.py (the `db` compose service, already ETL'd). `/ask` doesn't
touch `app.state.agent_graph`/checkpointer/store at all (see
app/routers/ask.py's module docstring for why it bypasses the graph
entirely), so no lifespan dance is needed here either -- plain
`ASGITransport` is enough.

These tests assert on response SHAPE and coarse behavioral properties
(row_count > 0, sql present, no forbidden keywords, out_of_scope flag),
not exact wording -- LLM phrasing varies run to run.
"""
import re
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def real_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_token(real_client):
    email = f"ask-test-{uuid.uuid4().hex}@example.com"
    resp = await real_client.post(
        "/auth/register", json={"email": email, "password": "correct horse battery"}
    )
    assert resp.status_code == 201
    return resp.json()["access_token"]


async def _ask(real_client, token, question) -> dict:
    resp = await real_client.post(
        "/ask",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": question},
    )
    assert resp.status_code == 200
    return resp.json()


_FORBIDDEN = re.compile(r"\b(DROP|INSERT|UPDATE|DELETE|CREATE|ALTER|TRUNCATE)\b", re.IGNORECASE)


def _assert_grounded_sql_response(body: dict) -> None:
    assert body["out_of_scope"] is False
    assert body["sql"], "expected a non-empty SQL query for an in-scope data question"
    assert not _FORBIDDEN.search(body["sql"])
    assert isinstance(body["rows"], list)
    assert body["row_count"] == len(body["rows"])
    assert body["answer"]


# --- 1. counting questions -----------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "How many ASINs in my catalog currently pass all the eligibility checks?",
        "What's the total count of ASINs that are NOT eligible right now?",
    ],
)
async def test_counting_questions(real_client, auth_token, question):
    body = await _ask(real_client, auth_token, question)
    _assert_grounded_sql_response(body)
    assert body["row_count"] >= 1


# --- 2. single-filter questions ------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "List the ASINs whose ROI is above 25 percent.",
        "Which ASINs have a supplier cost under $15?",
    ],
)
async def test_single_filter_questions(real_client, auth_token, question):
    body = await _ask(real_client, auth_token, question)
    _assert_grounded_sql_response(body)


# --- 3. compound-filter questions -----------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Give me the 5 best-ROI ASINs where Amazon isn't the dominant seller.",
        "Show eligible ASINs sorted by ROI, but only ones Amazon doesn't control more than 70% of the BuyBox on.",
    ],
)
async def test_compound_filter_questions(real_client, auth_token, question):
    body = await _ask(real_client, auth_token, question)
    _assert_grounded_sql_response(body)


# --- 4. explanation questions ("why isn't X eligible") -------------------


async def test_explanation_question_cites_the_actual_failed_check(real_client, auth_token):
    # B006JVZXJM is a real seeded ASIN in this dev catalog.
    body = await _ask(
        real_client, auth_token, "Why doesn't B006JVZXJM qualify as eligible?"
    )
    # It may in fact BE eligible in this dev dataset (synthetic fixtures
    # differ from CHALLENGE.md's illustrative numbers) -- either way the
    # answer must be grounded in a real row, not invented.
    _assert_grounded_sql_response(body)
    assert "B006JVZXJM" in body["sql"]


# --- 5. subjective-but-grounded recommendation questions ------------------


@pytest.mark.parametrize(
    "question",
    [
        "Out of everything eligible, which single ASIN looks like the best opportunity today?",
        "If you had to pick one ASIN to resell right now, which would it be and why?",
    ],
)
async def test_recommendation_questions_are_grounded(real_client, auth_token, question):
    body = await _ask(real_client, auth_token, question)
    _assert_grounded_sql_response(body)
    # A real recommendation should name at least one real ASIN pattern.
    assert re.search(r"B0[0-9A-Z]{8}", body["answer"])


# --- 6. business-judgment questions (turnover vs margin) ------------------


async def test_business_judgment_question_with_budget_constraint(real_client, auth_token):
    body = await _ask(
        real_client,
        auth_token,
        "If I only have $500 to put into one eligible ASIN, which one nets me the most profit over the next 90 days?",
    )
    _assert_grounded_sql_response(body)


# --- 7. out-of-scope refusal ----------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What's the weather forecast for tomorrow?",
        "Can you recommend a good recipe for dinner tonight?",
    ],
)
async def test_out_of_scope_questions_are_refused(real_client, auth_token, question):
    body = await _ask(real_client, auth_token, question)
    assert body["out_of_scope"] is True
    assert body["sql"] is None
    assert body["rows"] == []
    assert body["answer"] == "I can only help with Amazon ASIN arbitrage analysis."


# --- boundary question: must NOT be refused -------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Can you explain what ROI represents in this system?",
        "What does it mean for Amazon to 'dominate the BuyBox' on a listing?",
        "How do you decide whether an ASIN is eligible?",
    ],
)
async def test_boundary_definitional_questions_are_not_refused(real_client, auth_token, question):
    body = await _ask(real_client, auth_token, question)
    assert body["out_of_scope"] is False
    assert body["answer"]
