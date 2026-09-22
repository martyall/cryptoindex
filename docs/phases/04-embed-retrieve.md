# Phase 4 — Embed and retrieve

Status: draft, for the human's review

## Goal
Make the index searchable and measure how well search finds the right passage, with and without the gloss and question channels (D3, D15, Invariant 6).

## What Phase 3 hands over
- Paragraphs with `block_kind` and, where a unit is anchored, `block_label` (`Theorem 21.5`).
- Units with a gloss, key terms, 3–5 questions, an anchor, and quality flags. The prompt is frozen at gloss-v2 (D23).
- Three limits that retrieval will expose:
  - **garbled source text** in about a fifth of sampled units (D21's parser noise);
  - **units too narrow to stand alone** in about a quarter;
  - **key terms often not in the text** (notation written differently, or synonyms).

  Whether these hurt finding is one of the things the evaluation below should show.

## Deliverables

1. **Embedder.** `Qwen3Embedder` behind the existing `Embedder` protocol: Qwen3-Embedding-0.6B (D3) through sentence-transformers on `mps`, run with `asyncio.to_thread`. The query instruction prefix is applied to queries only. The model name and revision are pinned, and the weights are downloaded once, like the parser and local LLM models.

2. **Embed stage**, replacing the placeholder:
   - It embeds paragraph text, glosses, and questions whose vector is NULL. A changed gloss already clears its vector (Phase 3), so re-running embeds only what changed (Invariant 2).
   - Vectors and the transition to `ready` commit together.
   - The first embedding writes `embed_model` to `docs.meta`. A run whose configured model differs from the recorded one refuses to start, not silently mixing vectors (Invariant 6). Changing models is a re-embed job with an `embed_model_changed` event; the job itself is Phase 7's.

3. **Normalized LaTeX for trigram search** (`paragraphs.latex_norm`, deferred from Phase 2).
   - It is written with a real LaTeX parser. pylatexenc's LaTeX walker is the candidate; it is to be confirmed at phase start (No string munging).
   - Normalization drops spacing and style commands and canonicalizes whitespace, so `\mathbb{Z}_2[x]` and `\mathbb Z_{2} [x]` compare equal.
   - Math the parser fails on is stored un-normalized and flagged, never guessed.

4. **Search** in `cryptoindex.query`, on the `ci_query` role only; it imports nothing from `ingest` (Invariant 7).
   - Five channels:
     - paragraph vectors;
     - gloss vectors;
     - question vectors;
     - full text with the `simple` configuration (D15);
     - trigram similarity on `latex_norm`.
   - The channels are fused with Reciprocal Rank Fusion. Every hit records which channels found it.
   - A matching paragraph expands to its enclosing unit or units. A matching gloss or question expands to its unit's paragraphs.
   - Filters: document IDs, and current revisions only by default.

5. **Retrieval evaluation harness** (EVALUATION.md §3), `make retrieval-eval`.
   - **Corpus:** the nine Phase 2 excerpts, uploaded through the normal pipeline and glossed with gloss-v2. The questions must be ones the human can judge (D19).
   - **Questions:** `eval/questions.jsonl`, at least 30, of the four kinds (lookup, assumption, near-miss, negative), each naming the expected document and locator (`Theorem 21.5` or a paragraph position).
   - **Metrics:** recall@10 and MRR, per channel, for the fused result, and for the fused result without the gloss and question channels.

6. **Embedding model comparison:** 0.6B against 8B truncated to 1024 dimensions, on the same questions. The choice is recorded in `DECISIONS.md`.

## Acceptance (from ROADMAP)
- At least 30 questions run through the harness, with recall@10 reported with and without the gloss and question channels.
- The embedding model choice is recorded in `DECISIONS.md`.
- The query layer refuses to start when the model recorded in `docs.meta` differs from the configured one.

## Decisions for the human before starting
1. **Who writes the questions?** EVALUATION.md says someone who knows the corpus. Two options:
   - you write them, which takes the most of your time;
   - Claude drafts about 45 from the excerpts, and you keep, edit, or reject each one on a review page like the spot-check.

   The recommendation is the second, with the expected locators checked against the text by code.
2. **Excerpt titles.** The parse stage stores an excerpt's first heading ("Exercises") as its title. For the evaluation corpus the uploader's name (the source's name) should win. Options:
   - prefer the uploader's name everywhere;
   - only keep a parsed title when it is not a generic heading. This needs a rule, so it is not recommended.
3. **Downloads:** the 0.6B and 8B embedding weights, about 1.2 GB and 16 GB, fetched once from Hugging Face at pinned revisions.

## Out of scope
- Reranker, HyDE, section summaries (D17).
- The agent and citation checker (Phase 5); the HTTP search endpoint (Phase 6).
- Repairing parser noise; re-segmenting narrow units. Both are measured here, not fixed.

## Deferred
(Add items discovered during this phase that belong to later phases.)
