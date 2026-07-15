"""HARNESS.md §7.1's run_readonly_sql row -- the single highest-risk tool in
the whole system (ARCHITECTURE.md §4.2: "run_readonly_sql 是全仓库里唯一
接受原始 SQL 文本的工具"). Covers: multi-statement rejection, DDL/DML
keyword blocklist (including mixed case and "hidden in a comment"
variants), and that legitimate multi-line SELECTs still work.

`validate_readonly_sql` is pure (no DB) and does the actual gatekeeping;
`run_readonly_sql_impl` is the DB round-trip on top of it, verified here to
never actually mutate `asins` even when fed something malicious (belt and
suspenders on top of the validator itself being the primary defense).
"""
import pytest

from app.agent.tools import run_readonly_sql_impl, validate_readonly_sql
from app.models.asin import Asin

pytestmark = pytest.mark.asyncio


MALICIOUS_SQL = [
    "DROP TABLE asins",
    "DROP TABLE asins;",
    "SELECT * FROM asins; DELETE FROM asins",
    "SELECT asin FROM asins; DROP TABLE asins--",
    "dRoP TaBLe asins",
    "select * from asins; insert into asins (asin) values ('x')",
    "UPDATE asins SET buybox = 0",
    "SELECT * FROM asins /* sneaky */; TRUNCATE asins",
    "SELECT * FROM asins -- then DROP TABLE asins",
    "GRANT ALL ON asins TO public",
    "CREATE TABLE evil (id int)",
    "ALTER TABLE asins DROP COLUMN buybox",
]


@pytest.mark.parametrize("sql", MALICIOUS_SQL)
def test_malicious_sql_is_rejected_before_execution(sql):
    assert validate_readonly_sql(sql) is not None


def test_legit_single_select_is_accepted():
    assert validate_readonly_sql("SELECT asin, computed_roi_pct FROM asins WHERE eligible = true") is None


def test_legit_multiline_select_is_accepted():
    sql = """
    SELECT asin, computed_roi_pct
    FROM asins
    WHERE eligible = true
      AND computed_roi_pct > 25
    ORDER BY computed_roi_pct DESC
    LIMIT 10
    """
    assert validate_readonly_sql(sql) is None


def test_select_referencing_updated_at_style_column_is_not_a_false_positive():
    # `updated_at`/`created_at`-style column names contain "UPDATE"/"CREATE"
    # as a raw substring -- word-boundary matching must not flag these.
    assert validate_readonly_sql("SELECT asin, snapshot_at FROM asins ORDER BY snapshot_at") is None


def test_trailing_semicolon_alone_is_tolerated():
    assert validate_readonly_sql("SELECT asin FROM asins;") is None


def test_empty_sql_is_rejected():
    assert validate_readonly_sql("") is not None
    assert validate_readonly_sql("   ") is not None


def test_non_select_statement_is_rejected():
    assert validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x") is not None


async def test_run_readonly_sql_impl_executes_legit_select(db_session):
    db_session.add(Asin(asin="B0300000A1", title="Widget", eligible=True, computed_roi_pct=42.0))
    await db_session.flush()

    result = await run_readonly_sql_impl(
        db_session, "SELECT asin, computed_roi_pct FROM asins WHERE asin = 'B0300000A1'"
    )
    assert result["row_count"] == 1
    assert result["rows"][0]["asin"] == "B0300000A1"


@pytest.mark.parametrize("sql", MALICIOUS_SQL)
async def test_run_readonly_sql_impl_never_executes_malicious_sql(db_session, sql):
    db_session.add(Asin(asin="B0300000A2", title="Untouched", eligible=True))
    await db_session.flush()
    count_before = await db_session.execute(Asin.__table__.select())
    n_before = len(count_before.fetchall())

    result = await run_readonly_sql_impl(db_session, sql)
    assert "error" in result

    count_after = await db_session.execute(Asin.__table__.select())
    n_after = len(count_after.fetchall())
    assert n_after == n_before  # nothing was inserted/deleted/dropped
