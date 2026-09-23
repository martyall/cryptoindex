# Phase 6 — HTTP API

Status: parked (2026-09-22): no UI or outside client needs it yet; the draft stands for when one does

## Goal
One documented HTTP contract that a browser UI (TypeScript, D1) can be built against: ingest control, pipeline status, search, and the agent streaming its steps and its cited answer. The QA pages become clients of it.

## What Phase 5 hands over
- One process, one event loop: the pipeline, and FastAPI (`cryptoindex.api.create_app`) on `CI_API_HOST`, by default `127.0.0.1`.
- Endpoints that already exist, grown phase by phase without a common shape:
  - `/health`, `/ingest/status`;
  - `POST /documents` (upload) and `GET /documents`;
  - the search page's `/search/api/search` and `/search/api/info`;
  - the Ask page's `/ask/api/ask?q=`, server-sent events of `AgentEvent`s, then `done`.
- The answer path (D28–D30):
  - `ask()` streams the events `tool_call`, `tool_result`, `answer`, `error`, then `final`, which carries the checked, located citations;
  - it logs `answer_done` for every question;
  - a closed connection stops the agent within about 5 s.
- `requeue()` (`make requeue`), which sends documents back to a stage. It is synchronous, and it refuses documents a worker has claimed (`ClaimedError`).

## Deliverables

1. **One API under `/api`**, with a pydantic model for every request and response, so that the OpenAPI document is exact. Each endpoint uses the role that matches its side (Invariant 7): ingest control on `ci_ingest`, search and ask on `ci_query`.
   - `GET /api/documents` and `GET /api/documents/{id}`: name, revisions, stage, last error, and counts of paragraphs and units.
   - `POST /api/documents`: upload, as now.
   - `GET /api/status`: counts per stage, and what is running.
   - `POST /api/documents/{id}/retry`: sends a `failed` revision back to its failed stage.
   - `POST /api/requeue` with `{stage, documents?}`: the same function as `make requeue`, including re-glossing after a prompt change (stage `segment`). It runs through `asyncio.to_thread`, and a claimed document gets a 409.
   - `GET /api/search`: the Phase 4 search, with channels, kinds and documents as parameters.
   - `GET /api/ask?q=`: server-sent events. Each event's `data` is one of the event models, which are named in the OpenAPI components because OpenAPI cannot describe a stream's items. The stream ends with a `done` event.
   - `GET /api/paragraphs/{id}`: a cited paragraph's stored text and location. This is Phase 5's deferred "show a cited paragraph", and the UI will need it.

2. **The QA pages use the API.** The search and Ask pages call `/api/search` and `/api/ask`, and their own `/search/api/*` and `/ask/api/*` routes go. The old upload endpoints go too, and the upload page and `make smoke` move to `/api/documents`.

3. **Stopping on disconnect is tested.** A scripted handler that waits forever is closed when its client goes. Nothing new is built for this; it pins the behaviour Phase 5 measured.

4. **The spec is exported and checked.**
   - `make openapi` writes `openapi.json` at the repository root.
   - A test fails when the committed file differs from what the app produces, so the spec cannot drift.
   - `make openapi-check` runs `openapi-typescript` on it in a Node container, so the project still needs only Docker and uv.

5. **Concurrency.** An agent request never blocks the other endpoints. A test holds a scripted agent mid-answer and calls `/api/status` and `/api/search` while it waits. Tools take a query connection only for each call, so the query pool (4) bounds concurrent tool calls, not concurrent questions.

## Acceptance (from ROADMAP)
- Every endpoint is covered by an offline test. A long-running agent request does not block the status endpoints.
- `openapi.json` is committed, and `openapi-typescript` generates types from it without errors.

## Questions for the human
1. **The MCP adapter** (the roadmap marks it optional): I recommend deferring it. It would expose the four agent tools to outside MCP clients, such as Claude Code itself, which is cheap because the tools already exist. Nothing needs it yet.
2. **Ask over GET or POST:** I recommend GET. The browser's EventSource only makes GET requests, and one question fits in a query string. POST with fetch-based streaming would suit multi-turn conversation later, which is out of scope.
3. **Authentication:** none. The API binds to localhost, and D22's dev mode is only for personal use. A deployment for other people would need auth, and the `anthropic` backend (D22).
4. **Node in a container for the type check:** is that acceptable, or should `openapi-typescript` stay a manual step?

## Out of scope
- The browser UI itself.
- Multi-turn conversation, and a local-model handler (D28).
- Authentication and rate limits (question 3).

## Deferred
