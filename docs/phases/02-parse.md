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
   - Detects formal blocks (Theorem, Lemma, Definition, Proof, Algorithm, Game, …) into `block_kind` / `block_label`.
   - Computes `content_hash` over whitespace-normalized text, and `latex_norm` for trigram search.

3. **Parse stage** replaces the no-op:
   1. Load the revision.
   2. Run the parser in a thread or subprocess.
   3. Write the Markdown file.
   4. In one transaction: upsert paragraphs by `(revision_id, position)` guarded by `content_hash`, fill `papers.title` from the first heading if it is still empty, and `advance()`.

   Re-parsing an unchanged revision must keep every paragraph ID (Invariant 5).

4. **Formula render check.** Every `$…$` / `$$…$$` span is rendered with KaTeX, the renderer the UI will use, and failures are counted. KaTeX is JavaScript, so it runs from a small pinned Node tool in `tools/katex-check/`, with a committed lockfile.

5. **Parser evaluation** on `eval/parse-sample/`, per `EVALUATION.md` §1:
   - both parsers run on every sample PDF;
   - `eval/parse-report.md` gives the formula failure rate per parser;
   - `parse-scores.csv` holds the manual pseudocode-box scores (0/1/2).

   Because documents are uploads (D18), `ids.txt` lists each sample's name and sha256 rather than ePrint IDs.

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

## Open questions for the human
1. **The evaluation sample.** `EVALUATION.md` asks for 20–30 PDFs chosen for difficulty:
   - at least 10 with boxed security games or oracles;
   - at least 5 with heavy custom notation;
   - at least 5 with parameter or benchmark tables;
   - a few older born-digital PDFs.

   Can you supply these? Scoring the pseudocode boxes 0/1/2 is manual and has to be done by you, a person who can judge fidelity. That is probably one to two hours.
2. **Model downloads.** On first run, Marker and PaddleOCR-VL each download several GB of model weights from Hugging Face. That is network access, though not scraping. Is it OK, once, with the versions pinned?
3. **PaddleOCR-VL can't run in Docker here.** It needs MLX, meaning Apple's Metal GPU, which Docker on macOS cannot reach. It would run as a native process started from the Makefile (`uvx mlx-vlm …`). Is that an acceptable exception to Docker-only? If not, the evaluation drops to Marker alone, which changes the roadmap and D8.
4. **Marker's license.** The code is GPL-3.0, and the model weights are free for personal and research use but restricted commercially. That is fine for a personal index. Flag it if this might become a product.

## Deferred
(Add items discovered during this phase that belong to later phases.)
