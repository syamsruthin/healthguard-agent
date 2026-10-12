.PHONY: help install infra up down migrate seed ingest data api worker ui lint format typecheck test test-integration test-live eval eval-estimate golden models-check chat diagrams

help:  ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install all dependencies (incl. dev)
	uv sync --all-groups

infra:  ## Start Postgres + Redis only
	docker compose up -d postgres redis

up:  ## Build and start the full stack
	docker compose up -d --build

down:  ## Stop the stack
	docker compose down

migrate:  ## Apply database migrations
	uv run alembic upgrade head

seed:  ## Generate + load synthetic insurance data, corpus and demo users
	uv run hg-seed

ingest:  ## Chunk + embed the policy corpus into pgvector
	uv run hg-ingest

data: migrate seed ingest  ## migrate + seed + ingest

api:  ## Run the API with reload
	uv run uvicorn backend.main:app --reload --port 8000

worker:  ## Run the background worker
	uv run arq backend.workers.settings.WorkerSettings

ui:  ## Run the Streamlit UI
	uv run streamlit run frontend/app.py

lint:  ## Ruff lint
	uv run ruff check backend frontend tests

format:  ## Ruff format + fix
	uv run ruff format backend frontend tests && uv run ruff check --fix backend frontend tests

typecheck:  ## mypy
	uv run mypy backend

test:  ## Unit tests (no network, no infra)
	uv run pytest -m "not integration and not live"

test-integration:  ## Integration tests (needs `make infra data`)
	uv run pytest -m integration

golden:  ## Rebuild the golden evaluation dataset from the seeded database
	uv run hg-build-golden

test-live:  ## One real turn per persona (spends OpenRouter credit, ~$0.20)
	HG_LIVE=1 uv run pytest -m live

eval-estimate:  ## Print the plan + cost estimate for an eval run (LIMIT=n JUDGES=n), no model calls
	uv run hg-eval --suite golden --dry-run --limit $(or $(LIMIT),10) --judges $(or $(JUDGES),0)

eval:  ## Offline RAGAS evaluation with the judge jury (spends credit; LIMIT=n, default 10)
	uv run hg-eval --suite golden --limit $(or $(LIMIT),10) --judges $(or $(JUDGES),0)

chat:  ## Run agent turns in the terminal: make chat AS=maria.garcia@demo.healthguard.dev Q="..."
	uv run hg-chat --as $(AS) "$(Q)"

models-check:  ## Verify configured OpenRouter model slugs exist
	uv run hg-models-check

diagrams:  ## Render Mermaid architecture diagrams to SVG
	./docs/render_diagrams.sh
