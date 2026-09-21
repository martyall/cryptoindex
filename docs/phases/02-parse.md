# Phase 2 — Parse

Status: in progress

## Goal
Uploaded PDFs become stable paragraphs, read from each parser's structured output, and the parser is chosen by evaluation (D8).

## Deliverables

1. **Parser adapters** (`ingest/parsers.py`), implementing the `Parser` protocol in two steps:
   - `run(pdf) -> bytes` returns the parser's own structured JSON. It is blocking and out-of-process (D7).
   - `read(raw) -> ParsedDocument` is pure, so improving a mapping never requires re-running a parser.

   The two adapters:
   - **`MarkerParser`** runs Marker 2.0.0's Python API through `tools/marker_parse.py`, inside its own uv tool environment, mirroring Marker's own `convert_single`. It needs llama.cpp's `llama-server`.
   - **`PaddleVLParser`** runs PaddleOCR 3.7's `PaddleOCRVL` pipeline through `tools/paddle_vl_parse.py`, inside its own uv tool environment. Layout detection runs on the CPU, and the vision-language model on the MLX-VLM server (`make paddle-server`, bound to 127.0.0.1).

2. **Document model** (`ingest/document.py`):
   - Each parser's JSON is validated against a pydantic schema. Marker's mirrors its own `JSONOutput` model.
   - Every block type maps to a block kind through an explicit table: 31 Marker types (group containers are flattened) and 25 PaddleOCR-VL labels, taken from the installed packages. An unmapped type is an error.
   - Marker's block HTML goes through an HTML parser, and PaddleOCR-VL's Markdown through markdown-it with its math plugin, so formulas are found by real parsers.
   - Pages come from the structure of the output, and sections from Marker's `section_hierarchy`.
   - An equation block the parser failed to delimit is flagged as `malformed_math`.

3. **Paragraphs** (`ingest/paragraphs.py`):
   - Every block except headings, page furniture and empty figures becomes a paragraph, in reading order.
   - Each paragraph carries its page and bounding box (migration 004), its section path, and a `content_hash` over whitespace-normalized text.
   - `block_kind` holds only what a parser reports: equation, algorithm, code, table, list item, caption, footnote, figure.

4. **Parse stage:**
   1. Load the revision.
   2. Run the parser, or read its cached raw JSON from `parsed/<parser>-<version>/<sha256>.json`. Output the reader rejects is never cached.
   3. In one transaction: upsert paragraphs by `(revision_id, position)`, guarded by `content_hash`; set the parser identity; fill an empty title from the first heading; `advance()`.

   Re-parsing an unchanged revision keeps every paragraph ID (Invariant 5).

5. **Formula render check.** Every formula a reader found is rendered with KaTeX 0.18.7 (`tools/katex-check`, pinned with a lockfile). An undelimited equation block counts as a failure without rendering.

6. **Parser evaluation.** `make parse-eval` runs locally only:
   - It cuts each excerpt from its source, checking the source's sha256 first, and runs both parsers.
   - It reports the formula failure rate per parser.
   - It writes a review page (below).
   - Results: `eval/parse-report.md` and `eval/parse-sample/parse-scores.csv`.

   **Review page.** A static local page, never published, because some sources do not permit redistribution. For each excerpt page it shows the page image, then Marker's and PaddleOCR-VL's blocks side by side, rendered with KaTeX from the local install.
   - Claude proposes a 0/1/2 score per page and parser, with a note, in `eval/parse-sample/proposed-scores.json`.
   - The human accepts or changes each score, using keyboard shortcuts.
   - Choices stay in the browser's local storage until the human exports `parse-scores.csv`.

7. **Near-duplicate warning** (deferred from Phase 1). After parsing, the revision records the other document whose paragraph hashes it shares most, and the shared fraction (migration 004). `GET /documents` and the upload page show "looks similar to <name>". It is only a warning, never a rejection.

8. **Tests** (offline). The real parsers never run in `make test` or CI.
   - Both readers are tested against committed real output: `eval/fixtures/parsers/`, three pages of Erickson's *Algorithms*, CC BY 4.0.
   - Adapters are tested against stand-in tool scripts.
   - The parse stage is tested with a fake parser, including ID stability across a re-parse and across a parser version change.

## Acceptance (from ROADMAP)
- Both parsers run on the sample PDFs, and a report records the formula render failure rate and the manual scores.
- The parser choice is recorded in `DECISIONS.md`.
- Re-parsing an unchanged revision changes no paragraph IDs.

## Out of scope
- arXiv LaTeX/HTML sources. There are none under D18.
- Formal-block detection (Theorem N, Definition N, …). Neither parser reports it, and recognizing it from prose is text classification: it belongs to Phase 3's anchor detection, done by the LLM segmentation.
- `latex_norm`, which is used only by Phase 4's trigram search. It is defined there, with a real LaTeX parser.
- Segmentation and glossing (Phase 3). Embeddings (Phase 4).

## Evaluation sample (D19, D20)
Nine excerpts of 10–12 pages each, one per source, listed with URL, license, sha256 and page range in `eval/parse-sample/ids.toml`. The material is chosen so the human can judge fidelity, and it replaces the research-paper sample in `EVALUATION.md` §1.

| Excerpts | From | Stresses |
|---|---|---|
| 3 | Judson, Hefferon, *Active Calculus* | formulas, derivations, matrices, theorem/proof runs |
| 1 | Leinster, *Basic Category Theory* | commutative and labelled-arrow diagrams |
| 3 | Erickson, Morin, *Mathematics for Computer Science* | pseudocode, recurrences, proofs |
| 2 | Rosulek, *The Joy of Cryptography*; Goldwasser–Bellare | side-by-side game boxes, experiments, older PDF |

Sources and everything derived from them live in the git-ignored `eval/parse-sample/local/`.

## Decisions from the human (2026-09-21)
- **Evaluation sample:** as above. Claude downloads the sources once (D20).
- **Model downloads:** approved. Parser model weights may be downloaded once from Hugging Face, with versions pinned.
- **PaddleOCR-VL:** approved to run natively, as it needs Apple's GPU, which Docker cannot reach. CI never runs either real parser.
- **llama.cpp:** approved to install natively with Homebrew (0.4.1), because Marker 2.0 runs its OCR and equation models through `llama-server`.
- **Licenses:** acceptable. Marker 2.0.0 is Apache-2.0 on PyPI (1.x was GPL-3.0); the terms of its model weights have not been re-checked.
- **Nothing is trained or fine-tuned.** The phase chooses between two off-the-shelf parsers, and possibly their settings.
- **No string munging** (CLAUDE.md). See the audit below.

## Audit (2026-09-21)
The first version of this phase parsed each parser's Markdown with line regexes, recognized "Theorem N" by regex, split Marker's output on its page-separator lines, and found formulas by matching `$`. The human ordered an audit of the whole ingestion pipeline for this pattern. Fixed:
- **Parsing now reads each parser's structured JSON** (deliverables 1–3). The regex splitter (`ec6219e`) was replaced in `413692e`.
- **`failed_stage` column:** the stage a revision failed in is its own column. It used to be a prefix of `last_error` that a retry would have had to parse back out.
- **Upload validation** reads the page tree with pypdf, not the first five bytes.
- **Default names** come from `PurePath`, in one place on the server.
- **`make reset`** reads `CI_DATA_DIR` through the app's config and compares resolved paths, not `sed` output and `case` patterns.
- **The import-boundary test** resolves relative imports with importlib. It had ignored them.
- **The evaluation sample** moved from a pipe-delimited `ids.txt` to TOML, and the formula metric from `$` matching to parser-found math.

Kept on inspection:
- whitespace normalization before hashing;
- listing our own migration and fixture files by extension;
- DSNs parsed by psycopg;
- config values parsed with `int()` and `float()`.

## Deferred
- **Phase 3 — formal-block anchors.** Theorem, lemma, definition, proof, algorithm and game labels come from the LLM segmentation, not from text patterns.
- **Phase 4 — `latex_norm`.** Normalize LaTeX with a real LaTeX parser (e.g. pylatexenc) for trigram search. The column exists and stays NULL until then.
- **Section depth with PaddleOCR-VL.** It reports no heading levels, only title versus section, so its section paths are at most `title > section`. If PaddleOCR-VL is chosen, deeper paths need a source other than the heading text.
