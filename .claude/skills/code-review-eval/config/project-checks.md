# Project-specific check commands

Read this before Step 3 (side-effects / static analysis) and Step 6 (coverage). These are the exact commands `code-review-eval` should run in this repo — do not invent alternatives.

## Stack summary

This is a timeboxed (~4h) take-home challenge: FastAPI + async SQLAlchemy + SQLite/Postgres +
Keepa API + an LLM provider. See `candidate_package/CHALLENGE.md` for the full spec.

- **Language**: Python 3.11+ only. UI is raw JSON/minimal HTML — no frontend build, no TS/JS pipeline.
- **Source**: `app/` (`app/main.py` = FastAPI app, `app/etl.py` = ETL entrypoint — per `candidate_package/Dockerfile.example`)
- **Tests**: `tests/` (pytest); the challenge also requires standalone **acceptance scripts** for the
  `/chat` multi-turn scenario and `/refresh` resume-after-kill scenario — check for these under
  `scripts/` or `tests/acceptance/` and run them too if present
- **Package manager**: `pip` with `requirements.txt` (no `pyproject.toml`/poetry unless the author added one — check first)
- **Migrations**: none required by the spec; if Alembic (or hand-rolled `CREATE TABLE IF NOT EXISTS`)
  was added, adjust this section

## Lint / type check

Only run these if the corresponding tool is actually configured in this repo (check for `ruff`/`mypy`
in `requirements.txt` or a config file first — the challenge does not mandate either):

```
ruff check app tests        # if ruff is present
mypy app                    # if mypy is present
```

If neither is configured, skip this step and note it in the report rather than inventing a command.

## Tests (scoped to changes, fast feedback)

```
pytest <changed test files> -x -q
```

Then a full run for regression:

```
pytest -q
```

- Prefer `pytest-asyncio` for async endpoint/ETL tests if it's in `requirements.txt`
- Mock outbound Keepa/LLM HTTP calls in unit tests (`respx`/`unittest.mock`) — don't burn real Keepa
  tokens or LLM cost in the test suite; real-call smoke tests belong in the acceptance scripts instead

## Acceptance scripts (required by the challenge, not optional)

The challenge explicitly requires one-command acceptance scripts covering:
1. `/chat` multi-turn scenario (context carries across turns)
2. `/refresh` resume-after-kill (interrupt mid-run, restart, verify it resumes instead of re-pulling
   completed ASINs)

If these exist, run them as part of review. If a change touches chat session state or the refresh
job, treat a missing/stale acceptance script for that area as a coverage gap, not just missing unit tests.

## Coverage

```
pytest --cov=app --cov-report=term-missing <changed test files>
```

- Read the `Missing` column to find uncovered lines **in the changed files**
- Ignore coverage gaps in files not touched by this change
- The challenge explicitly says test *coverage depth* is not graded ("几个 sanity test 就够了") —
  don't over-invest here relative to the 4h budget; a few sanity tests per endpoint is enough

## What NOT to run

- ❌ `npx tsc` / `vitest` / `jest` — no TS/JS build pipeline
- ❌ `go vet` / `cargo check` — wrong language
- ❌ Anything that calls the real Keepa API or a paid LLM API from an automated review pass — costs
  real tokens/money; only the author's manual acceptance-script runs should hit real APIs
- ❌ `docker compose up` as part of routine review — that's a separate manual/CI check, not this skill's job

## Missing tool fallback

If a tool above is not installed in the current environment:

1. Report it to the user explicitly: `⚠️ pytest not found — test check skipped`
2. **Do not** pretend the check passed
3. Downgrade the corresponding sub-risk to "unverified" and surface it in the final report
