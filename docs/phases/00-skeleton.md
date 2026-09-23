# Phase 0 — Skeleton

Status: done (2026-09-21)

## Goal
A runnable, empty pipeline with every contract in place, so later phases only fill in stage bodies.

## Deliverables

1. **Repo layout**
   ```
   pyproject.toml   Makefile   docker-compose.yml
   db/migrations/001_core.sql  002_roles.sql
   src/cryptoindex/core/     config.py db.py llm.py embed.py citation.py events.py plugins.py
   src/cryptoindex/ingest/   runner.py stages.py (no-op bodies) harvest.py fetch.py parse.py gloss.py embed.py (stubs)
   src/cryptoindex/query/    search.py agent.py tools.py (stubs)
   src/cryptoindex/__main__.py   # starts runner + API in one loop
   tests/
   prompts/  eval/fixtures/
   ```
2. **Migrations** implementing `DATA_MODEL.md` exactly, including roles and privileges. Applied by a tiny migrator that records applied filenames in `docs.schema_migrations`.
3. **Config** (`core/config.py`): a frozen dataclass from env vars — DSNs for both roles, data dir, embed model/dims/device, LLM backend selection and endpoint, parser selection, ePrint base URL, fetch delay, user agent.
4. **Runner** (`ingest/runner.py`): bounded queues per transition, per-stage concurrency, claim with `FOR UPDATE SKIP LOCKED`, stale-lock recovery, DB re-seeding on startup, `enqueue()`, `status()`. Stage bodies are no-ops that advance `stage`.
5. **Interfaces** from `INTERFACES.md` as Protocols, with `FakeLLM`, `FakeEmbedder`, and a `StubParser` implementation.
6. **Tests** (pytest, offline, against docker Postgres):
   - privileges: `ci_query` INSERT fails.
   - migrator is idempotent.
   - runner: 20 revisions seeded across stages → all reach `ready`; kill/restart mid-run → no duplicate transitions (assert via an audit counter on the no-op stage).
   - `FakeLLM` replays by request hash and raises on an unrecorded request.
7. **Makefile**: `db-up`, `db-down`, `migrate`, `test`, `run`, `fmt`, `check` (ruff lint, ruff format check, ty; read-only).

## Out of scope
Real parsing, harvesting, glossing, embedding, retrieval, API endpoints beyond `GET /health` and `GET /ingest/status`.

## Decisions from the human (2026-09-21)
- Data directory: `CI_DATA_DIR=./data` (the `.env.example` default); can point at an external disk later.
- Postgres runs in Docker only, via `docker compose`; no native install. Any other service images also go through compose.
- Python tooling: uv. Type checker: ty, configured under `[tool.ty]`.
- Coordination through the `Makefile` and `.env` / `.env.example`.

## As built

Departures from the deliverables above, and choices the spec left open:

- **Layout.** No empty placeholder modules: `ingest/harvest.py`, `fetch.py`, `gloss.py`, `embed.py` and `query/search.py`, `agent.py`, `tools.py` are created by the phase that implements them. The no-op stage bodies live in `ingest/stages.py`. Added `core/model.py` (`Stage`, `RevisionId`), `core/migrate.py` (the migrator), and `api.py` at package top level (it composes ingest and query, so it belongs to neither). `core/plugins.py` is deferred (below).
- **Database.** Image pinned to `pgvector/pgvector:0.8.6-pg18` (Postgres 18.6). A third DSN, `CI_ADMIN_DSN` (superuser), runs migrations and test setup. Role passwords are not in any migration: the migrator sets them from `CI_INGEST_DSN` / `CI_QUERY_DSN`. `docs.schema_migrations` stores a sha256 per file and the migrator refuses to run if an applied file was edited.
- **Schema additions** beyond `DATA_MODEL.md`: `UNIQUE (paper_id, pdf_sha256)` on revisions (Phase 1's "same PDF twice creates one revision"), `created_at`/`updated_at` columns, and `ci_ingest` cannot write `docs.schema_migrations`.
- **Runner semantics.**
  - Claim = `UPDATE … SET locked_at = now()` on a `FOR UPDATE SKIP LOCKED` subselect, committed before the stage runs.
  - `advance()` only transitions a row that is claimed at the expected stage, which is what makes duplicate transitions impossible.
  - Reaching `ready` makes the revision current only if no higher-numbered revision of the paper is already ready, and writes `revision_ready`.
  - A stage that returns without advancing counts as a failure.
  - A stale claim found at startup counts as a failed attempt, so a revision that crashes the process each time ends in `failed` instead of a crash loop.
  - Cancellation (SIGINT/SIGTERM) releases in-flight claims.
  - `attempts` resets to 0 on each successful transition, so `CI_MAX_ATTEMPTS` is per stage.
- **Tests.** Run against a separate `cryptoindex_test` database, recreated each session. The kill test SIGKILLs a real subprocess mid-stage and audits committed stage executions in a test-only schema.
- **`.env`.** At the first unquoted value containing spaces, uv drops that line and every line after it, with only a warning. `CI_USER_AGENT` is therefore quoted.
- **Shutdown.** uvicorn re-raises SIGINT/SIGTERM after its own graceful shutdown. With default handlers, SIGTERM killed the process before the runner released its claims. `serve()` installs no-op handlers so the re-raise is harmless; `tests/test_shutdown.py` covers both signals.
- **Not changed after review.** A comment in `001_core.sql` refers to Phase 4. The file has already been applied, and migrations are never edited.

## Deferred

- **Phase 3 — FakeLLM recording mode.** Capturing real backend responses into `FakeLLM` fixture files. The replay format (`{"request", "completion"}`, keyed by a hash recomputed from the stored request) is fixed.
- **Phase 3 — gloss JSON validation.** Decide between a hand-written validator and pydantic, which FastAPI already brings in.
- **Phases 2–4 — backend construction.** Building the parser, LLM and embedder from config and adding them to `StageContext`. Phase 0 stages need only the pool and settings.
- **Phase 4 — `docs.meta.embed_model`.** Written when the first embeddings are stored. Migration 001 records only `schema_version` and `embed_dims`.
- **Phase 5 — sync `CitationSource.fetch`.** `docs/INTERFACES.md` makes `fetch` synchronous, but the checker runs in the async query layer and `fetch` reads the database. Phase 5 must either make it async (a contract change) or run it through `asyncio.to_thread`.
- **Phase 6 — retry endpoint.** A `failed` revision records its failing stage only as the `<stage>: ` prefix of `last_error`. `POST /ingest/retry` must decide where to restart; restarting at `parse` is always safe because stages are idempotent.
- **Phase 7 — stale claims.** They are recovered only at startup. That is enough while one process runs all stages (D6). Running stages as separate processes needs a periodic sweep.
- **Phase 8 — plugin contract (`core/plugins.py`).** `CoreAPI` needs the tool-registration and migration-hook types, which do not exist until Phases 5 and 8.
