# Data model

Schema `docs`. Migrations in `db/migrations/` are the precise spec; this document explains meaning. Plugins use their own schemas and may reference `docs.*` one way only.

## Roles

- `ci_ingest`: SELECT/INSERT/UPDATE/DELETE on `docs.*`, sequences.
- `ci_query`: SELECT only on `docs.*`.

## `docs.meta`
Key/value settings. Required keys: `embed_model`, `embed_dims`, `schema_version`. The query layer checks `embed_model` against its configuration at startup.

## `docs.papers`
One row per uploaded document (D18). `id` is a UUID; `name` is chosen by the uploader and need not be unique. Optional fields: title (a parser may fill it), authors[], abstract, subjects[], `license` (if known, must be respected). `arxiv_id`, `oai_datestamp` and `fetched_datestamp` are reserved for a future remote source and unused.

## `docs.revisions`
One row per distinct PDF (or source bundle) of a paper. Pipeline state lives here.

| column | meaning |
|---|---|
| `paper_id`, `revision` | unique; revision increments per new hash |
| `source_kind` | `pdf` · `arxiv_latex` · `arxiv_html` |
| `pdf_sha256`, `pdf_path` | file identity and location under `CI_DATA_DIR`; `pdf_sha256` is unique across all documents, so a file is indexed once |
| `parser`, `parser_version` | what produced the paragraphs |
| `stage` | `parse` → `segment` → `embed` → `ready`, or `failed` |
| `is_current` | exactly one current revision per paper (partial unique index) |
| `attempts`, `locked_at`, `last_error` | retry/claim bookkeeping |
| `failed_stage` | the stage the revision was in when it went to `failed`, where a retry resumes (migration 004's comments describe the box as feeding the review page and the column as replacing a `last_error` prefix; this table is the correct description) |
| `similar_paper_id`, `similarity` | near-duplicate warning: the document whose paragraphs this revision shares most, and the shared fraction |

**Stage state machine.** A stage worker claims a row (`FOR UPDATE SKIP LOCKED`, sets `locked_at`), does its work in a transaction, sets the next stage, clears `locked_at`, commits, then signals. Errors increment `attempts` and record `last_error`; after N attempts the row goes to `failed`. Locks older than a timeout are treated as stale on startup.

## `docs.paragraphs`
Base layer. One row per paragraph of a revision.

- `position` (0-based within revision, reading order), `page` (0-based, for citation locators) and `bbox` (where the paragraph sits on that page) from the parser's layout, `section_path` (`text[]`, the headings it sits under, outermost first, e.g. `{4 Security, 4.1 Unforgeability}`; migration 005), `text` (PaddleOCR-VL: its Markdown; Marker: plain text with `$`-delimited math; equations: LaTeX, raw if the parser failed to delimit them; tables: the parser's HTML), `content_hash`, `block_kind` (nullable; only what a parser reports: `equation`, `algorithm`, `code`, `table`, `list_item`, `caption`, `footnote`, `figure`), `block_label` (nullable; a formal-block label such as `Theorem 3`, written by the segment stage from validated anchors only).
- `tsv` generated with the `simple` config; `latex_norm` (normalized LaTeX for trigram matching, filled in Phase 4 with a real LaTeX parser); `emb halfvec(1024)`.
- Stability: re-parsing an unchanged revision must reproduce identical `(position, content_hash)` pairs and therefore keep IDs.

## `docs.units`
Argument units: spans `[first_pos, last_pos]` of paragraphs within a revision.

- `anchor_label`, `anchor_pos` (both null or both set; migration 006) — the formal block that anchors the unit, if any, and the paragraph where its label appears verbatim. The same label is written to that paragraph's `block_label`.
- `gloss`, `terms[]`, `gloss_model` (the model that produced it, which a refusal fallback can change), `prompt_version`, `input_hash` (of the chunk it came from, below), `flags[]` (mechanical quality checks for review: `gloss_empty`, `gloss_long`, `term_absent`, `question_count`, `question_restates_gloss`), `emb_gloss halfvec(1024)`, cleared when the gloss changes.
- Units may overlap. On re-segmentation, new units are matched to old by `(first_pos, last_pos, anchor_label)`; unmatched old units are deleted and a `unit_changed` event is written for each.

## `docs.segment_chunks`
One validated LLM reply per chunk: the paragraphs of one section, or a piece of a long section, sent in one call. Keyed by `(revision_id, input_hash)`, where `input_hash` = hash(document title, section path, each paragraph's position, content hash and block kind, prompt version, requested model). Written as each reply arrives (a batch arrives whole), before the segment stage's own transaction, like the parser's raw-output cache, so a retry never re-asks for a chunk that already succeeded. Unchanged input therefore makes no LLM call. Rows for chunks that no longer exist are deleted when the stage commits.

## `docs.unit_questions`
3–5 doc2query questions per unit, each with `emb halfvec(1024)`.

## `docs.events`
Append-only outbox: `kind` (`revision_ready`, `unit_changed`, `paper_revised`, `embed_model_changed`), `payload jsonb`, `created_at`. Plugins consume by cursor. Optionally mirrored via `NOTIFY docs_events`.

## Indexes
- HNSW (`halfvec_cosine_ops`) on `paragraphs.emb`, `units.emb_gloss`, `unit_questions.emb`.
- GIN on `paragraphs.tsv`; GIN trigram on `paragraphs.latex_norm` (`pg_trgm`).
- Partial index on `revisions(stage)` where not `ready`/`failed`.

## Files on disk
`CI_DATA_DIR/pdfs/<paper_id>/<sha256>.pdf`, plus `parsed/<parser>-<version>/<sha256>.json` (the parser's raw structured output, kept per parser version so a retry or re-segmentation never re-parses, and a parser change never reuses stale output).

## Identity summary
- Paragraph: `(revision_id, position)` with `content_hash` guard.
- Unit: `(revision_id, first_pos, last_pos, anchor_label)`.
- Citation locator for papers: `paper_id`, `revision`, `position` (paragraph), optional `block_label`, page if known.
