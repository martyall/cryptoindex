# Roadmap

Each phase ends with something runnable and testable. Detailed specs go in `docs/phases/NN-name.md`, written just before the phase starts. Status: `todo` · `in progress` · `done`.

| # | Phase | Status |
|---|---|---|
| 0 | Skeleton | done |
| 1 | Upload | done |
| 2 | Parse | done |
| 3 | Segment and gloss | done (Anthropic manual run pending an API key) |
| 4 | Embed and retrieve | done |
| 5 | Agent and citation checker | done (manual run of ten questions pending) |
| 6 | HTTP API | todo |
| 7 | Operations and backfill | todo |
| 8+ | Plugins (code first) | todo |

---

## Phase 0 — Skeleton

**Goal:** a runnable empty pipeline and the contracts everything else plugs into.

**In scope:** repo layout, `pyproject.toml`, config from env, docker-compose Postgres, migrations `001_core.sql` and `002_roles.sql` per `DATA_MODEL.md`, the pipeline runner (stages, bounded channels, DB re-seeding on startup), the stage function interface, the fake LLM backend, embedder and parser interfaces (stub implementations), test harness, `Makefile`.

**Out of scope:** any real parsing, glossing, embedding, or retrieval.

**Acceptance:**
- `make db-up && make migrate` creates the schema; both roles work with the intended privileges (a test asserts `ci_query` cannot INSERT).
- `make test` passes offline.
- A test inserts revisions in various stages, starts the runner, kills it mid-run, restarts it, and shows every revision reaches `ready` via no-op stages with no duplicate work.

## Phase 1 — Upload

**Goal:** a person uploads named PDFs; each becomes a document and a revision that the running pipeline picks up (D18).

**In scope:** migration keying documents by UUID with a user-chosen, non-unique name; one importer function (name + file stream), which keeps its own copy of each PDF under `CI_DATA_DIR`; `POST /documents` and `GET /documents`; a single static upload page served by the app.

**Acceptance:**
- An uploaded PDF becomes a document and a revision and reaches `ready` without restarting the process.
- Same name twice gives two documents; the same file twice gives one.

A remote source (OAI-PMH harvest, polite PDF fetch, arXiv cross-match) is deferred until it is wanted; it becomes another caller of the importer.

## Phase 2 — Parse

**Goal:** PDFs become stable paragraphs; the parser is chosen.

**In scope:** Marker and PaddleOCR-VL adapters (out-of-process), each parser's structured output → typed blocks → paragraphs with page, box, section path and content hash, formula render check (KaTeX pass/fail rate), the parser evaluation on `eval/parse-sample/`, near-duplicate warning (deferred from Phase 1). No arXiv sources (D18).

**Acceptance:**
- Both parsers run on the evaluation sample (textbook excerpts, D19); a report records formula render failure rate and the human's manual scores per `EVALUATION.md`.
- Parser chosen and recorded in `DECISIONS.md`.
- Re-parsing an unchanged revision changes no paragraph IDs.

## Phase 3 — Segment and gloss

**Goal:** argument units with glosses, terms, and questions.

**In scope:** segmentation+glossing prompt (`prompts/gloss-v1.md`, then `gloss-v2.md`), per-section batched calls with paper context, Anthropic batch API + prompt caching, local backend fallback, the Claude Code dev-mode backend (D22), JSON validation against the section, input hashing, anchors (label, position, kind) filling `paragraphs.block_label`, gloss quality flags, the 50–100 unit spot-check.

**Acceptance:**
- Spot-check set glossed and reviewed; prompt frozen with reviewer approval (frozen at gloss-v2, D23).
- Re-running on unchanged input makes zero LLM calls.
- Works end-to-end with the fake backend in tests and with both real backends manually.

## Phase 4 — Embed and retrieve

**Goal:** searchable index, checked by hand (D24).

**In scope:** local embedding (Qwen3-Embedding, 0.6B and 8B cut to 1024 dimensions), model registry in `docs.meta`, HNSW indexes, hybrid search (paragraph/gloss/question vectors + full-text + trigram on normalized LaTeX) with RRF, expansion to enclosing unit, search by block and anchor kind (D25), a search QA page in the main process (D24), `make requeue` to redo a stage, a second vector set for comparing models (D27, development only), `make smoke`. The retrieval evaluation harness waits for real questions (D24).

**Acceptance:**
- The nine evaluation excerpts are indexed end to end, and the human has tried searches of each kind on the search page (lookup, assumption, near-miss, and one the excerpts cannot answer) and judged the results usable.
- Every hit shows which channels found it; a search with the gloss and question channels switched off can be compared by hand.
- Query layer refuses to start when `docs.meta` model differs from configured model.

## Phase 5 — Agent and citation checker

**Goal:** answers whose main points cite where a person can find them (document, page, section, numbered block), or a plain "not found".

**In scope:** an answer-handler interface with a Claude Agent SDK handler (D28), read-only tools (`search`, `get_unit`, `get_paragraphs`, `get_document`), a prose answer citing its main points, the server-side citation checker (in-session source, D29, D30), human-findable citation rendering, an Ask page streaming the steps, agent prompts `prompts/agent-v1.md` to `agent-v3.md`. See `docs/phases/05-agent-citations.md`.

**Acceptance:**
- Tests with a scripted handler: a citation of a paragraph not retrieved in-session is removed and marked; the answer is never dropped.
- Manual run on about ten of the human's questions about the indexed documents produces cited points the human confirms are right and findable, and says so where the documents cannot answer.

## Phase 6 — HTTP API

**Goal:** the UI's contract.

**In scope:** FastAPI on the same event loop; endpoints for ingest control (enqueue papers, retry failed, re-gloss with new prompt version), pipeline status, search, agent (SSE streaming of steps and final claims), OpenAPI spec exported; optional MCP adapter.

**Acceptance:**
- All endpoints covered by tests; a long-running agent request does not block status endpoints.
- `openapi.json` committed; `openapi-typescript` generates types without errors.

## Phase 7 — Operations and backfill

**Goal:** the full archive, kept current.

**In scope:** backfill plan (categories/years first), throughput measurement, optional remote-GPU parsing with pinned model versions, retry policy and `failed` triage, daily harvest schedule, status reporting, disk layout for PDFs.

**Acceptance:**
- Chosen scope backfilled; pipeline stage counts reported; daily incremental run completes unattended.

## Phase 8+ — Plugins

Code plugin per the earlier design: own schema, release-tag snapshots, tree-sitter symbols, glosses embedded with the core's text model, `spec_links` with a verification lifecycle, findings, release review reports. Needs Phase 3's anchors (algorithm steps, checks, assumptions) to be good link targets; refine those first if they aren't.
