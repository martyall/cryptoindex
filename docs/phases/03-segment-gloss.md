# Phase 3 — Segment and gloss

Status: ready (reviewed 2026-09-21)

## Goal
Group each revision's paragraphs into argument units, and give each unit a plain-English gloss, key terms, the questions it answers, and its formal-block anchor (D4, D5). These are for finding, never for citing (Invariant 3).

## What Phase 2 hands over
- Paragraphs in reading order, with `page`, `bbox`, `block_kind` (equation, algorithm, table, caption, footnote, …), and a `section_path` that is at most `title > section`, because PaddleOCR-VL reports no heading levels (D21).
- **Noise from PaddleOCR-VL:** undelimited display equations (stored as raw LaTeX), stray "i." / "i.e." tokens, and the occasional invented word.
- **No formal-block labels.** Recognizing "Theorem 3.1", "Definition 2", "Game G₀" moved here from Phase 2, because it is classification of prose, not parsing.

## Deliverables

1. **Prompt** `prompts/gloss-v1.md`, versioned (Invariant 10). Per section, the model receives:
   - the paper's title and the section path;
   - the section's paragraphs, numbered by position, with their block kinds.

   It returns units, each with:
   - `first_pos` and `last_pos`, a span of consecutive paragraphs making one self-contained point;
   - `anchor_label`, the formal block it anchors on, if any, e.g. `Theorem 3.1`;
   - a 1–3 sentence gloss;
   - key terms;
   - 3–5 questions the unit answers.

   The prompt is written for mathematical and CS text, crypto included (D19), and tells the model to read through the parser noise listed above.

2. **Structured output.** A JSON schema for the reply, validated with pydantic. That settles Phase 0's deferred question: pydantic is already used for every other structured input. Validation also checks the response against the section it was given:
   - spans are inside the section and in order;
   - every paragraph belongs to at least one unit;
   - an `anchor_label` names a block actually present in its span.

   A reply that fails validation is a failed attempt, never partly stored.

3. **Both backends** behind the `LLM` protocol (D9, Invariant 9):
   - `AnthropicLLM`, the native SDK with prompt caching, including a batch path for bulk glossing (D5);
   - `OpenAIFormatLLM`, for local servers, with JSON-mode or schema fallback where a server lacks tool use.

   `FakeLLM` replays recorded responses in tests. The recording format was fixed in Phase 0; recording itself is added here.

4. **Segment stage** replaces the no-op:
   - It batches one call per section. Long sections are split at paragraph boundaries, with overlap so a unit is never cut.
   - `input_hash` = hash(paragraph content hashes in the section + section path + prompt version + model). Unchanged input makes zero LLM calls (roadmap acceptance).
   - On re-segmentation, new units are matched to old ones by `(first_pos, last_pos, anchor_label)`. Unmatched old units are deleted, with a `unit_changed` event each (DATA_MODEL).
   - Units and questions commit together with the transition (Invariant 2).

5. **Gloss quality checks.** These are mechanical, and each one only flags units for review; none rewrites anything:
   - an empty or over-long gloss;
   - a key term that appears nowhere in the unit's paragraphs;
   - a question that restates the gloss.

6. **Spot-check** (EVALUATION.md §2). 50–100 units from the Phase 2 evaluation sample, which the human can judge (D19). They are stratified by block kind and anchor: theorem, definition, algorithm, game, plain argument.
   - The review uses the same kind of local page as Phase 2: one unit at a time, every judgement saved as it is made, blind before Claude's proposals.
   - The page lessons from Phase 2 apply: all math rendered, including inside algorithms, and a reviewer who cannot read raw LaTeX is never shown raw LaTeX.
   - The human marks each unit faithful, overstated, wrong or unclear, and says whether its boundaries are sensible.
   - The prompt is frozen at v1 when at least 90% are faithful and no theorem or definition gloss is "wrong".

## Acceptance (from ROADMAP)
- The spot-check set is glossed and reviewed, and the prompt is frozen at v1 with reviewer approval.
- Re-running on unchanged input makes zero LLM calls.
- It works end to end with the fake backend in tests, and with both real backends manually.

## Out of scope
- Embeddings (Phase 4).
- Repairing parser noise in stored text. Citations quote what is stored (Invariant 3).
- Section summaries and cross-references (D17).

## Decisions from the human (2026-09-21)
- **Glossing model: Claude Opus 5** (`claude-opus-5`, 1M-token context), through the Anthropic API.
  - The key is `ANTHROPIC_API_KEY` in `.env`, which the config already reads. Set `CI_LLM_BACKEND=anthropic` and `CI_LLM_MODEL=claude-opus-5`.
  - Pricing as checked on 2026-09-21: $5 input / $25 output per million tokens, half that through the Batches API, cached reads at about a tenth of input.
  - Rough estimate, to be measured with token counting at phase start: about $3 for the evaluation sample at full price (about $1.50 through batches), and about $0.70 for a 30-page paper (about $0.35 through batches). Adaptive thinking is on by default and billed as output, so either could plausibly double.
  - At phase start, confirm the context window from the Models API (`max_input_tokens`). Enable the server-side refusal fallback recommended for Opus 5, and tell the human.

- **Local backend for the manual acceptance run:** llama.cpp's `llama-server`, already installed natively for Marker, which serves the OpenAI message format that `OpenAIFormatLLM` targets. The model is an open instruct model that fits in 48 GB of unified memory. It is chosen and pinned at phase start, and its weights are downloaded once, as for the parser models.
- **Anchors also label paragraphs.** `paragraphs.block_label` is filled from the segmentation's validated anchors only, so a citation can say "Theorem 3.1" instead of "p42". It is written with the units, in the same transaction.

## Deferred
(Add items discovered during this phase that belong to later phases.)
