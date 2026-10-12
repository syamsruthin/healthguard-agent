# ADR-0037: Packaging and deployment: one image, one process per container, configuration guards

- **Status:** Accepted (`d88c015`, verified in `f99ce81`)
- **Area:** Operations

## Context

The system has four runtime roles (API, worker, UI, one-shot migration/seed/ingest) plus Postgres, Redis and optional Jaeger. It must run identically on a laptop and in CI, start in the right order, and refuse to run insecurely in production.

## Decision

- **One multi-stage Docker image** ([deploy/Dockerfile](../../deploy/Dockerfile)) for all four roles, selected by command:
  - builder stage: `uv sync --frozen` with dependencies installed before the project for layer caching;
  - runtime stage: `python:3.12-slim`, **non-root** `app` user, `LOG_JSON=true`, the tiktoken encoding pre-downloaded, a health check on `/health/live`.
- **One uvicorn worker per container** (`--workers 1`); scale with replicas.
- **Docker Compose** ([docker-compose.yml](../../docker-compose.yml)): `postgres` (pgvector/pg17) and `redis` (redis:8, AOF, `allkeys-lru`) with health checks; a one-shot **`migrate`** service (`alembic upgrade head && hg-seed && hg-ingest`) that `api` and `worker` wait on (`service_completed_successfully`); `ui`; `jaeger` behind the `observability` profile.
- **Configuration** via pydantic-settings ([core/config.py](../../backend/core/config.py)): every setting has a development default and an env var. **Production guards:** `ENV=production` refuses to start with the default `JWT_SECRET` or without `OPENROUTER_API_KEY`; OpenAPI docs are disabled in production.
- **Health:** `/health/live` (process up) and `/health/ready` (Postgres and Redis reachable).
- **Toolchain:** uv with a committed `uv.lock`, ruff, mypy, a Makefile for common tasks.

## Why

- **One image, many commands** means one build, one vulnerability scan and identical dependencies everywhere.
- **Non-root and slim** reduce the attack surface.
- **One process per container** because Prometheus counters are per process (multiple uvicorn workers would each expose partial counts), and the orchestrator (Compose, Kubernetes) is the right layer for scaling and restarts.
- **Migrations as a one-shot job** that gates the app avoids races where two API replicas migrate at once.
- **Fail fast on insecure configuration.** A production deployment with the default JWT secret is a vulnerability; refusing to start is the cheapest possible control.
- **Liveness vs readiness** lets an orchestrator restart a dead process without killing a live one that's waiting for Redis.

## Alternatives considered

| Option | Why not |
|---|---|
| An image per service | More builds; dependency drift between images |
| `--workers N` in uvicorn | Breaks per-process metrics; the orchestrator scales better |
| Migrate on API start-up | Races with multiple replicas |
| Kubernetes manifests / Helm | Appropriate for production; Compose keeps the demo runnable on a laptop |

## Consequences

- **Positive:** reproducible builds; correct start order; insecure configs can't reach production.
- **Negative:** the image contains all roles' dependencies (UI libraries in the API image). The worker has no health check (arq has no HTTP endpoint). The pandas sandbox's memory limit only applies on Linux, i.e. in the container.

## Interview soundbite

> "One non-root image runs the API, worker, UI and the migration job by command. Migrations run as a one-shot job that gates the app, so replicas never race. I run one uvicorn worker per container and scale with replicas, because Prometheus counters are per process, and production refuses to start with a default secret or no API key."
