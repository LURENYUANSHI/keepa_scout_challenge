# Diverges from candidate_package/Dockerfile.example: that reference is the
# minimal SQLite path (no separate db service, ETL runs synchronously as the
# container's CMD prefix). We use Postgres + Celery + Redis (see
# ARCHITECTURE.md §1), so this image is shared by the api/worker/beat
# services (docker-compose.yml overrides `command` per service).
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source + data
COPY app ./app
COPY data ./data

ENV PYTHONUNBUFFERED=1 \
    TZ=UTC
EXPOSE 8000

# Phase 3a: app/etl.py exists now, so ETL runs once at container start,
# matching candidate_package/Dockerfile.example's pattern and HARNESS.md
# §0's "ETL 在容器启动流程里自动执行一次". Note this is `;` not `&&` on
# purpose: ETL failing (no Keepa network reachable, keys exhausted, ...)
# must not prevent the API itself from coming up -- `docker compose up`
# should still leave :8000 healthy even on a bad ETL run, per HARNESS.md
# §0's "API 在 :8000 就绪" acceptance criterion, which doesn't hinge on ETL
# succeeding. This line only applies to the `api` service in practice --
# `worker`/`beat` (docker-compose.yml) override `command` and never hit it.
CMD ["sh", "-c", "python -m app.etl; uvicorn app.main:app --host 0.0.0.0 --port 8000"]
