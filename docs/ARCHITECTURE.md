# Architecture

## Purpose

A local service that indexes cryptography papers (IACR ePrint, with arXiv sources where available) so that a user, or an agent, can ask questions and receive answers grounded in verified citations to the papers. Later, an optional plugin will link code implementations to the papers they claim to implement.

Constraints: runs on one machine (MacBook Pro, M5 Pro, 48 GB unified memory). Everything on the query path runs locally. Offline batch jobs (glossing) may call an external API.

## Components

One Python process, one Postgres database, one browser UI (out of scope for now).

```
                ┌────────────────────── Python process ──────────────────────┐
  ePrint ──────▶│ Ingestion pipeline (stateful, role ci_ingest)              │
  arXiv         │   harvest → fetch → parse → segment+gloss → embed          │
                │   stages joined by bounded channels carrying work IDs      │
                │                                                            │
                │ Query layer (stateless, role ci_query)                     │
                │   retrieval · citation checker · agent tool-use loop       │
                │                                                            │
                │ HTTP API (FastAPI) — ingestion control, search, agent      │
                │ MCP adapter (optional, thin) over the same query functions │
                └──────────────────────────┬─────────────────────────────────┘
                                           │
                               Postgres + pgvector (schema `docs`)
                                           │
                             plugins: own schema, one-way refs to docs.*
```

LLM backends (behind one small interface): Anthropic via the native SDK; local models via the OpenAI message format (mlx_lm.server, llama.cpp, Ollama, LM Studio). Embeddings: Qwen3-Embedding, always local.

## Data flow

1. **Harvest.** OAI-PMH metadata → `docs.papers`. Metadata is CC0; per-paper full-text licenses are stored and respected.
2. **Fetch.** PDFs downloaded politely (delay, user agent). A new file hash creates a new `docs.revisions` row in stage `parse`.
3. **Parse.** PDF → Markdown with LaTeX math (Marker or PaddleOCR-VL) → `docs.paragraphs` with stable positions and content hashes. Where an arXiv LaTeX source exists, it is used instead of the PDF.
4. **Segment and gloss.** A strong LLM groups a section's paragraphs into *argument units* (spans of consecutive paragraphs making one self-contained point), writes a 1–3 sentence plain-English gloss, key terms, and 3–5 questions each unit answers → `docs.units`, `docs.unit_questions`. Formal blocks (Theorem, Definition, Algorithm, Game) are recorded as anchors within units, not as the units themselves.
5. **Embed.** Local vectors for paragraphs, glosses, and questions. The revision becomes `ready` and current; a `revision_ready` event is written.
6. **Retrieve.** Hybrid search (paragraph vectors, gloss vectors, question vectors, full-text with the `simple` config, trigram on normalized LaTeX) fused with Reciprocal Rank Fusion; matches expand to their enclosing unit.
7. **Answer.** The agent loop calls retrieval tools, reads originals, and returns structured claims with verbatim quotes. The citation checker verifies every quote against stored text; unverifiable claims are dropped. Abstaining is a valid answer.

## Invariants (never broken, in any phase)

1. **The database is the source of truth.** Channels carry work IDs only. Restarting the process loses no committed work; on startup, channels are re-seeded from the database.
2. **Every stage is idempotent** and commits its output and stage transition before signaling downstream. Re-running a stage on unchanged input does no new work (content and input hashes).
3. **Glosses, terms, and questions are for finding, never for citing.** Citations point only at stored original text (paragraphs / anchored formal blocks).
4. **The citation checker runs server-side**, in Python, before any answer leaves the query layer.
5. **Paragraph IDs are stable across re-processing** of the same revision (matched by position and content hash). Units are matched to prior units by span and anchor; changed units emit events rather than silently breaking dependents.
6. **One embedding model per index.** Its name and dimension are recorded in `docs.meta`; the query layer refuses to start on a mismatch. Embeddings are regenerable; glosses are permanent.
7. **Ingestion writes, queries read.** Enforced by database roles.
8. **Plugins reference the core one way only.** The core exposes a versioned contract (IDs, unit schema, citation interface, embedding registry, events, tool registration) and never knows a plugin exists.
9. **Both LLM backends stay supported.** Any LLM-dependent feature works with Anthropic and with a local OpenAI-format server.
10. **Prompts are versioned files.** Prompt version participates in the input hash of everything it produces.

## Not in the minimal system (slots in later without changing boundaries)

Reranker, section-level summaries, cross-reference resolution between units, notation extraction, HyDE, the browser UI, the code plugin.
