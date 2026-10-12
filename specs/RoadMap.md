# Roadmap & Implementation Milestones

## Milestone 1: Environment & Synthetic Data (Day 1)
- [x] Initialize `uv` project and directory structure.
- [x] **SQL**: Create `setup_data.py` to populate `insurance.db` with 50 rows of plan data.
- [x] **CSV**: Populate `providers.csv` with 500 rows, ensuring diverse `city` and `state` distribution.
- [x] **Text**: Create 3 markdown files with 2000+ words of insurance policy jargon.
- **Commit**: `feat: init environment and synthetic data generation`

## Milestone 2: The Modular Tooling Layer (Day 2)
- [x] **Execution Engines** (`core/tools.py`): Build data-source-agnostic `SQLExecutionEngine` and `PythonExecutionEngine` classes that accept schema config at init time. These engines are reusable for any SQLite DB or CSV/DataFrame.
- [x] **Data Source Configs** (`core/data_sources.py`): Define `PLAN_BENEFITS_SOURCE` (SQL) and `PROVIDERS_SOURCE` (CSV) with schema, paths, and system prompts.
- [x] **Agent Wrappers** (`agents/sql_agent.py`, `agents/csv_agent.py`): Thin wrappers that instantiate the engines with the appropriate data source config.
- [x] **Hybrid RAG**: Implement ChromaDB indexing + BM25 search. Create a reranker logic to merge results.
- **Test Case**: `assert python_tool("Find PCPs in Seattle, WA")` returns the correct filtered DataFrame.
- **Commit**: `feat: core tools for sql, csv, and hybrid rag`

## Milestone 3: Agentic Logic & Tracing (Day 3)
- [x] ~~**Query Re-writer**: Implement a rule-based step to de-alias user input (e.g., "Texas" -> "TX").~~ (Replaced by LLM-based rewriter in Milestone 6)
- [x] **Router**: Implement the LLM routing logic based on the TechSpecs prompt.
- [x] **Trace Logger**: Create the `Logger` class to handle async JSONL writes and Markdown file generation in `backend/logs/`.
- [x] ~~**Memory**: Implement `WindowBufferMemory` backed by SQLite (`memory.db`).~~
- [x] **Orchestrator**: Full pipeline (Rewrite → Route → Execute → Synthesize) with tracing.
- **Commit**: `feat: agent orchestration and tracing system`

## Milestone 4: API & UI Integration (Day 4)
- [x] **FastAPI**: Wired `/chat` endpoint to orchestrator pipeline, added `/history` GET/DELETE, RAG warm-up on startup.
- [x] **Streamlit**: Built premium UI with:
    - Chat Interface with quick-start suggestion buttons.
    - Sidebar displaying **Live Trace** with trace history expanders.
    - Toggle for "System Reasoning" visibility with color-coded tool badges.
    - Backend health indicator and session management (New/Clear).
- **Commit**: `feat: fastapi backend and streamlit frontend`

## Milestone 5: Quality Assurance (Day 5)
- [x] **Test**: Complex query: "I need a Dermatologist in Austin, TX under a plan with a deductible less than $1000." — ✅ Triggers MULTI_TOOL → SQL_TOOL + CSV_TOOL. Answer includes both Platinum plans (<$1000 deductible) and Austin dermatologists.
- [x] **Log Validation**: ✅ Trace MD shows 4-step report: User Query → Router Decision (MULTI_TOOL reasoning) → Tool Execution (SQL + CSV code/results) → Synthesis. JSONL has 5 structured entries.
- [x] **Extensibility**: ✅ Validated with `pharmacies.csv` (PythonExecutionEngine) and `claims.db` (SQLExecutionEngine) — zero engine code changes, only config + prompt.
- **Test Script**: `backend/scripts/test_qa.py` — 5/5 tests pass.
- **Commit**: `docs: final documentation and demo-ready state`

## Milestone 6: LangGraph Refactor (Day 6)
- [x] **LangGraph StateGraph**: Replaced the custom `process_query()` orchestrator with a native LangGraph `StateGraph` containing 4 nodes: `rewrite → route → execute → synthesize`.
- [x] **Memory → LangGraph Checkpointer**: Removed custom `WindowBufferMemory` class (`core/memory.py`). Replaced with `langgraph-checkpoint-sqlite` `SqliteSaver` — conversation persistence is now automatic via `thread_id` on graph invocation.
- [x] **State Schema**: Defined `HealthGuardState(TypedDict)` with `messages` (LangGraph `add_messages`), routing decisions, tool results, and trace data — all flowing through the graph.
- [x] **Dependency Update**: Added `langgraph-checkpoint-sqlite` to `pyproject.toml`.
- [x] **FastAPI Integration**: Updated `/chat`, `/history`, and `/history/{id}` endpoints to use the compiled LangGraph graph and checkpointer.
- [x] **No custom agents**: Removed all custom agent/orchestrator classes. The graph IS the orchestrator.
- [x] **LLM Query Rewriter**: Replaced the rule-based `rewrite_query()` with an LLM-powered rewriter that also performs conversational memory coreference resolution (resolves pronouns like 'those' based on chat history).
- **Commit**: `refactor: migrate to native LangGraph StateGraph with SqliteSaver checkpointer`

## Milestone 7: Fully Agentic Architecture (Day 7)
- [x] **Conditional Routing** (`add_conditional_edges`): Replaced the single generic `execute` node with dedicated `execute_sql`, `execute_csv`, `execute_rag`, and `execute_multi` nodes. The `route` node dispatches via `add_conditional_edges` based on `route_decision`.
- [x] **Self-Correction Retry Loop**: If SQL/Python code generation fails, the graph loops back to the same execute node (max 2 retries) with the error message injected as context for the LLM to self-correct.
- [x] **Answer Validation Node**: After synthesis, an LLM `validate` node checks if the answer fully addresses the user's question. Returns PASS/FAIL.
- [x] **ReAct Loop**: If validation returns FAIL and `react_count < 2`, the graph loops back to `route` to try a different approach (e.g., different tool, refined query). This is the Reason+Act iterative pattern.
- [x] **Streaming Endpoint**: Added `POST /chat/stream` SSE endpoint using LangGraph's `astream_events`. Frontend receives real-time progress events (node starts, tool results, final answer).
- **Commit**: `feat: fully agentic graph with retry, validation, ReAct, and streaming`

## Milestone 8: LLM Abstraction Layer (Day 7)
- [x] **Provider-Agnostic LLM Module** (`core/llm.py`): Created a single `llm_completion()` function that all LLM calls go through. No file directly imports Groq/OpenAI/etc.
- [x] **Config-Driven Provider**: `LLM_PROVIDER` env var switches between `groq`, `openai`, `ollama`, or any OpenAI-compatible endpoint. Default: `groq`.
- [x] **Decoupled All Consumers**: Removed direct `from groq import Groq` from `orchestrator.py`, `router.py`, and `tools.py`. All now call `llm_completion()`.
- [x] **Per-Node Tracing**: Enhanced trace logging to capture input/output/reasoning/duration at every graph node.
- **Commit**: `refactor: extract LLM abstraction layer for provider-agnostic inference`

## Milestone 9: Strict Guardrails Framework (Day 8)
- [x] **Input Guardrails**: Implemented `query_validator` module in `rewrite_node` to reject out-of-scope, non-enterprise queries before tool execution.
- [x] **Context Guardrails**: Added `context_validator` in `synthesize_node` to ensure retrieved data is sufficient before calling the LLM.
- [x] **Output Guardrails**: Added `response_validator` in `validate_node` to detect hallucinations and prevent the use of external/pretrained knowledge.
- [x] **Graph Bypass Logic**: Updated `orchestrator.py` to route `FAIL_GUARDRAIL` states directly to `finalize` to skip unneeded processing.
- [x] **Standardized Fallbacks**: Unified fallback responses for out-of-scope, no-data, and insufficient-context scenarios.
- [x] **Memory-Aware Validation**: Integrated conversation history into the guardrail contexts so valid follow-up answers are not falsely flagged as hallucinations.
- [x] **Robust Logic Derivation**: Upgraded context and output guardrails to analyze executed SQL/Python code and user queries, eliminating false-positive hallucination flags on strictly filtered data.
- **Commit**: `feat: implement strict 3-layer input, context, and output guardrails`
## Milestone 10: Production Upgrade (OpenRouter · Postgres · Redis · evaluations)
Status and measured results: [docs/IMPLEMENTATION_STATUS.md](../docs/IMPLEMENTATION_STATUS.md). Architecture: [docs/architecture.md](../docs/architecture.md).
- [x] **Foundation**: pydantic settings, structlog with PII masking, Prometheus + OpenTelemetry, Docker Compose, CI.
- [x] **Database**: Postgres 17 + pgvector, Alembic, read-only agent role, Row-Level Security on member data.
- [x] **Synthetic data**: 40 plans, 1.5k members in families, 14k adjudicated claims, 5k providers, 60-document corpus, 6 personas + admin.
- [x] **LLM layer**: OpenRouter per-role models with fallbacks, prompt caching, exact cache, usage and cost accounting.
- [x] **RAG**: markdown-aware chunking, idempotent ingest, pgvector + full-text hybrid retrieval with RRF.
- [x] **Hardened tools**: sqlglot SQL guard + RLS execution, pandas process sandbox.
- [x] **Agent**: plan → parallel execute → stream synthesis → verify (one rewrite, one replan), plan sanitising, error-coded fallbacks.
- [x] **API**: JWT with refresh rotation, conversations, SSE chat, feedback, coverage, admin metrics, rate limits.
- [x] **Memory**: token-budgeted window, rolling summary, privacy-filtered long-term memories.
- [x] **Worker**: arq tasks for titles, summaries, memories, online LLM-judge jury, offline evals.
- [x] **UI**: multipage Streamlit with streaming chat, coverage, memory controls and admin dashboards.
- [x] **Evaluation**: golden set from the database, `hg-eval` with deterministic checks + RAGAS judge jury + quality gate.
- [x] **Tests**: ~250 unit / integration tests, a live persona suite, combined coverage gate.
- [x] **Router**: the TypeSafe Jev Router as the default for planning, code and synthesis, with fixed-model fallbacks.
- [x] **Engineering review**: validated-only exact cache, turn deadline and owned locks, PII-safe logs, cache-first answers, gated background calls, spend guards, judge cost, admin-only traces, shared codegen loop.
- [ ] **Evaluation on the router**: re-run the golden suite and meet the RAGAS gate.
- **Commits**: one per phase on `claude/vigilant-wozniak-f6rbb3`.
