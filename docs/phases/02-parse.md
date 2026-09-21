# Phase 2 — Parse

Status: draft, awaiting review

## Goal
Uploaded PDFs become stable paragraphs (Markdown with LaTeX math, section paths, formal-block labels), and the parser is chosen by evaluation (D8).

## Deliverables

1. **Parser adapters** (`ingest/parsers/`), implementing the `Parser` protocol. Both run out-of-process (D7).
   - **`MarkerParser`** calls Marker's CLI as a subprocess. Marker is installed as an isolated uv tool (`uvx --from marker-pdf==<pinned> marker_single`), so torch and its model stack never enter the project's environment.
   - **`PaddleVLParser`** is a client of a local PaddleOCR-VL server (MLX-VLM, `CI_PADDLE_VLM_URL`).
   - Each reports its name and a version (the pinned package and model versions) into `docs.revisions.parser` / `parser_version`.
   - The output is saved to `CI_DATA_DIR/parsed/<sha256>.md` (DATA_MODEL), so re-segmenting never re-parses.

2. **Markdown → paragraphs** (`ingest/paragraphs.py`). A pure function, and most of this phase's unit tests:
   - Splits into paragraphs at block boundaries, never inside display math, code, or a table.
   - Records each paragraph's `section_path` from the heading hierarchy, e.g. `4 Security > 4.1 Unforgeability`.
   - Detects formal blocks into `block_kind` / `block_label`. The kinds are the union of mathematical and cryptographic ones (D19): theorem, lemma, proposition, corollary, definition, proof, example, exercise, remark, algorithm, game.
   - Computes `content_hash` over whitespace-normalized text, and `latex_norm` for trigram search.

3. **Parse stage** replaces the no-op:
   1. Load the revision.
   2. Run the parser in a thread or subprocess.
   3. Write the Markdown file.
   4. In one transaction: upsert paragraphs by `(revision_id, position)` guarded by `content_hash`, fill `papers.title` from the first heading if it is still empty, and `advance()`.

   Re-parsing an unchanged revision must keep every paragraph ID (Invariant 5).

4. **Formula render check.** Every `$…$` / `$$…$$` span is rendered with KaTeX, the renderer the UI will use, and failures are counted. KaTeX is JavaScript, so it runs from a small pinned Node tool in `tools/katex-check/`, with a committed lockfile.

5. **Parser evaluation** on the sample below:
   - both parsers run on every excerpt;
   - `eval/parse-report.md` gives the formula failure rate per parser;
   - `parse-scores.csv` holds the box and diagram scores (0/1/2).

   A `make` target runs this locally only.

6. **Near-duplicate warning** (deferred from Phase 1). After parsing, compare the document's set of paragraph `content_hash` values with those of existing documents. If the overlap is high, `GET /documents` and the upload page show "looks similar to <name>". It is only a warning, never a rejection.

7. **Tests** (offline). The real parsers never run in `make test` or CI:
   - paragraph splitting, section paths, block detection and hashing, on committed Markdown fixtures that include real parser output from the sample;
   - the parse stage using a fake parser that returns a fixture;
   - paragraph ID stability across a re-parse.

## Acceptance (from ROADMAP)
- Both parsers run on the sample PDFs, and a report records the formula render failure rate and the manual pseudocode-box scores.
- The parser choice is recorded in `DECISIONS.md`.
- Re-parsing an unchanged revision changes no paragraph IDs.

## Out of scope
- arXiv LaTeX/HTML sources. There are none under D18.
- Segmentation and glossing (Phase 3).
- Embeddings (Phase 4).

## Evaluation sample (D19, D20)
About 10 excerpts of 10–20 pages each, cut from nine freely available textbooks and lecture notes listed with URL, license and sha256 in `eval/parse-sample/ids.txt`. The material is chosen so the human can judge fidelity, and it replaces the research-paper sample in `EVALUATION.md` §1:

| Excerpts | From | Stresses |
|---|---|---|
| 3 | Judson, Hefferon, *Active Calculus* | formulas, aligned derivations, matrices, theorem/proof runs |
| 1–2 | Leinster, *Basic Category Theory* | commutative diagrams (2D layout) |
| 3–4 | Erickson, Morin, *Mathematics for Computer Science* | boxed pseudocode, indentation, numbered steps |
| 2–3 | Rosulek, *The Joy of Cryptography*; Goldwasser–Bellare | side-by-side game boxes, oracles, older PDF encoding |

Page ranges are chosen at the start of the phase by looking for the hard cases and are added to `ids.txt`. Scoring:
- Claude proposes a 0/1/2 score per box and per diagram, with a note, by comparing page images with each parser's output.
- The human confirms or corrects each score.
- The results go in `parse-scores.csv`.

## Decisions from the human (2026-09-21)
- **Evaluation sample:** as above. Claude downloads the sources once (D20).
- **Model downloads:** approved. Parser model weights may be downloaded once from Hugging Face, with versions pinned.
- **PaddleOCR-VL:** approved to run natively, as it needs Apple's GPU, which Docker cannot reach. CI never runs either real parser; only the fakes and committed parser output are tested there.
- **Marker's license:** GPL-3.0 code and non-commercial model weights are acceptable.
- **Nothing is trained or fine-tuned.** The phase chooses between two off-the-shelf parsers, and possibly their settings.

## Deferred
(Add items discovered during this phase that belong to later phases.)
