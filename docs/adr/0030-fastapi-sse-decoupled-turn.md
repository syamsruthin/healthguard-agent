# ADR-0030: FastAPI + Server-Sent Events, with the turn decoupled from the HTTP connection

- **Status:** Accepted (`ea1eaeb`)
- **Area:** API / runtime

## Context

A turn takes 5–25 seconds. Members should see progress ("Looking up the information…", the plan, each tool step) and the answer token by token. Two failure modes matter:

1. If the browser disconnects mid-turn, the answer, its trace and its cost must **still be saved**. Money was spent and the member may come back.
2. The middleware stack must not **buffer** the stream.

## Decision

- **FastAPI** with async SQLAlchemy and asyncpg; routers under `/api/v1`; one error shape `{"error": {code, message, request_id}}`; `X-Request-ID` on every response.
- **Streaming via SSE** (`sse-starlette`) at `POST /conversations/{id}/messages/stream`, plus a non-streaming `POST …/messages` that runs the same pipeline.
- **Two phases** ([services/chat_service.py](../../backend/services/chat_service.py)):
  - **HTTP phase** (`prepare_turn`, errors become real status codes): input checks (empty, > 2,000 chars) → ownership (404) → **turn lock** ([ADR-0031](0031-turn-lock-and-deadline.md)) → persist the user message → return `200 text/event-stream`.
  - **Stream phase** (`execute_turn`) runs in a **background `asyncio.Task`** that pushes events into an `asyncio.Queue`; the SSE response only drains the queue.
- **Event protocol:** `meta`, `status`, `plan`, `tool`, `token`, `answer_reset`, `answer_replaced`, `citations`, `error`, `final`. Graph nodes emit custom events through LangGraph's stream writer; `run_graph` uses `stream_mode=["custom", "updates"]` ([agents/runner.py](../../backend/agents/runner.py)).
- **Persistence happens in the task**, in one transaction (assistant message + trace + LLM-call rows), then `final` is sent, then the semantic-cache store and follow-up jobs.
- Background tasks are held in a set (so they aren't garbage-collected) and **drained on shutdown** (30 s grace).
- **Pure ASGI middleware** for request ids, security headers and a 64 KB body cap, because Starlette's `BaseHTTPMiddleware` buffers responses and breaks SSE.
- `ping=15` keep-alives and `X-Accel-Buffering: no` so proxies don't buffer.

## Why

- **Decoupling makes persistence independent of the client.** Cancelling the HTTP response does not cancel the task, so a closed tab never loses an answer, a trace or a cost record.
- **Errors before the stream are real HTTP errors** (409 turn in progress, 429 rate limited, 404 not yours). After the stream starts, errors are `error` events followed by `final`, so the client always gets a terminal event.
- **SSE over WebSockets:** one-way server → client is all a turn needs; SSE works over plain HTTP, through proxies, with `curl -N`, and reconnects are simple.
- **`answer_reset` / `answer_replaced`** let the UI handle the verifier's rewrite and replan honestly: the streamed draft is visibly reset or replaced.
- **Same pipeline for streaming, non-streaming, the CLI and the eval runner,** so evaluation measures exactly what members get.

## Alternatives considered

| Option | Why not |
|---|---|
| WebSockets | Bidirectional and stateful; more infrastructure for a one-way stream |
| Long polling | Higher latency to first token; more requests |
| Run the graph inside the response generator | Client disconnect cancels the generator, losing persistence |
| A job queue for every turn | Extra hop and latency; the worker would need to stream back through Redis pub/sub |
| `BaseHTTPMiddleware` | Buffers the body, which breaks SSE |

## Consequences

- **Positive:** live progress, no lost answers, one pipeline everywhere.
- **Negative:** in-flight turns live in the API process; a hard crash loses the in-flight turn (the lock then expires on its own). Graceful shutdown drains them.
- **Scaling:** one uvicorn worker per container, scale by replicas ([ADR-0037](0037-packaging-and-deployment.md)); SSE connections are long-lived, so size load balancer timeouts accordingly.

## Interview soundbite

> "The HTTP handler only validates, takes the conversation lock and saves the user message. The turn itself runs in a background task that feeds a queue the SSE response drains. So a closed browser tab never loses the answer, its trace or its cost, and pre-stream failures are still proper HTTP status codes. I wrote the middleware as pure ASGI because BaseHTTPMiddleware buffers and breaks streaming."
