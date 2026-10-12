# ADR-0038: Streamlit as a thin client over the public API

- **Status:** Accepted (`fffd676`)
- **Area:** UI

## Context

The project needs a usable UI for members (chat, coverage, memory controls) and admins (observability and evaluation dashboards). It is an AI-architecture project, not a front-end project, so UI effort should be proportionate. The prototype's Streamlit UI called internal Python functions directly.

## Decision

- A multipage **Streamlit** app ([frontend/](../../frontend/)) using `st.navigation`: signed-out users see only the login page; **admin pages are registered only for admins**.
- The UI is a **pure HTTP client of the public API** ([frontend/lib/api.py](../../frontend/lib/api.py)): login and refresh, conversations, the SSE chat stream, feedback, coverage, memories, admin metrics, evaluations. It imports nothing from `backend/`.
- A small, unit-tested **SSE parser** ([frontend/lib/sse.py](../../frontend/lib/sse.py)) renders `status`, `plan`, `tool`, `token`, `answer_reset`, `answer_replaced`, `citations` and `final` events live.
- Pages: Chat (persona quick prompts, live plan and tool steps, outcome / latency / cost / cache badges, sources, 👍/👎, "show how answers are produced"), My coverage, Memory, Observability (admin), Evaluations (admin). Plotly charts.
- Expired sessions return to the login page; API errors are shown inline.

## Why

- **Fast to build, good enough to demo:** Python only, with chat primitives and charts built in.
- **The API is the product boundary.** Because the UI uses only public endpoints, every UI feature is also available to other clients (CLI, a future React or mobile app), and the UI can be replaced without touching the backend.
- **Authorisation is enforced by the API, not the UI.** Hiding admin pages is a convenience; the endpoints check the role.
- **Streaming in the UI exercises the real SSE contract,** which is also covered by integration tests.

## Alternatives considered

| Option | Why not (here) |
|---|---|
| React / Next.js | Better UX and session handling; much more effort for an architecture showcase |
| Streamlit calling backend functions directly (the prototype) | Couples UI and backend; bypasses auth and rate limits |
| No UI (API + CLI only) | Harder to demonstrate streaming, citations and dashboards |

## Consequences

- **Positive:** a complete product surface with little code; a clean API boundary.
- **Negative:** Streamlit keeps the session in server memory per browser tab, so **reloading the page signs the user out**; tokens live in Streamlit session state. For production, a React front end with refresh-token handling (e.g. an httpOnly cookie through a backend-for-frontend) is the next step.

## Interview soundbite

> "The UI is Streamlit because this is an architecture project, but it's a strict client of the public API: no backend imports, streaming through the same SSE contract, and admin pages hidden in the UI while the API enforces the role. That means I can swap it for React without touching the backend."
