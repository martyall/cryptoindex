# Phase 4 — Embed and retrieve

Status: draft, for the human's review

## Goal
Make the index searchable, and let the human check by hand how well search finds the right passage, with and without the gloss and question channels (D3, D15, D24, Invariant 6).

## What Phase 3 hands over
- Paragraphs with `block_kind` and, where a unit is anchored, `block_label` (`Theorem 21.5`).
- Units with a gloss, key terms, 3–5 questions, an anchor, and quality flags. The prompt is frozen at gloss-v2 (D23).
- Three limits that retrieval will expose:
  - **garbled source text** in about a fifth of sampled units (D21's parser noise);
  - **units too narrow to stand alone** in about a quarter;
  - **key terms often not in the text** (notation written differently, or synonyms).

  Whether these hurt finding is one of the things manual QA should look at.

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
   - Filters: document IDs, kinds to keep and kinds to drop (D25), and current revisions only by default.

5. **Search page for manual QA** (D24), `make search` on 127.0.0.1, in the style of the review pages.
   - A query box, and per hit: the document, the locator (block label or paragraph), the rendered passage with all math rendered, the unit it expands to, and which channels found it with their ranks.
   - A switch to leave out the gloss and question channels, so their effect can be seen by hand, and the kinds present in the results as filters (D25, D26).
   - It calls the same `cryptoindex.query` functions the API and agent will use. The page itself is a QA tool, not the browser UI (which is out of scope).

## Acceptance (from ROADMAP, as changed by D24)
- The nine evaluation excerpts are indexed end to end, and the human has tried searches of each kind on the search page (lookup, assumption, near-miss, and one the excerpts cannot answer) and judged the results usable.
- Every hit shows which channels found it; a search with the gloss and question channels switched off can be compared by hand.
- The query layer refuses to start when the model recorded in `docs.meta` differs from the configured one.

## Decisions from the human (2026-09-21)
- **Document titles:** the name given at upload is the document's title everywhere, including the glossing prompt's context and its input hash. A title the parser reads (the first heading) is kept only as stored information: for chapters, notes and excerpts the first heading is often "Exercises" or "Introduction".

## Before starting
- **Download:** the Qwen3-Embedding-0.6B weights, about 1.2 GB, fetched once from Hugging Face at a pinned revision.

## Out of scope
- Reranker, HyDE, section summaries (D17).
- The agent and citation checker (Phase 5); the HTTP search endpoint (Phase 6).
- Repairing parser noise; re-segmenting narrow units. Manual QA may show their effect; neither is fixed here.

## Found during the phase
- **Nothing could redo a finished stage.** A prompt, parser or embedding-model change meant `make reset` and uploading every document again, re-parsing for nothing; Phase 3 and Phase 4 hit this four times. `make requeue STAGE=… DOCS=…` now sends revisions back to a stage (pulled forward from roadmap Phase 7, which keeps the HTTP endpoint). The caches make the rest cheap: the stored PDF, the parser's raw output, and each chunk's reply are all reused, so only what changed is redone. A requeue to `embed` clears vectors, since the embed stage only fills missing ones.
- **MLX-VLM 0.7.2's continuous batching corrupted PaddleOCR-VL's output.** PaddleX sends many blocks to the model server at once; when the server decoded several together, the first tokens of each changed. Display formulas lost their opening `\[`, so PaddleX left them undelimited with a stray `\]`: 32 of Halo's 56 display equations could not be rendered and were missing from notation search. The same 16 formula crops sent one at a time all began with `\[`; sent together, 2 of 16 did. The server now runs with `--max-num-seqs 1` (one sequence at a time), which gives the sequential output for concurrent requests; the parser version records the setting, so documents are parsed again. Some of the parser noise attributed to PaddleOCR-VL in Phase 2 and Phase 3 (D21, D23) may have had the same cause.
  - After the fix, Halo re-parsed in the same time (about 3 minutes); 55 of its 57 display equations render and are in notation search (24 of 56 before).
  - **Of the two that still fail, one is PaddleX's post-processing:** when it turns `\[…\]` into `$$…$$` it deletes every `$` in the formula, including a `\$` in sampling notation (`\xleftarrow{\$}`), which crypto papers use often. That is upstream behaviour, recorded here, not worked around. The other begins mid-expression: recognition noise.

## Deferred
- **The retrieval evaluation harness** (EVALUATION.md §3) and **the 0.6B vs 8B comparison** (D3), until questions collected from real use exist (D24).
