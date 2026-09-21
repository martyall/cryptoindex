# Evaluation

"Done" for Phases 2–5 means measurably better on these sets, not "it runs". The sets live under `eval/` and are committed (PDFs excluded from git if licenses require; store IDs and hashes instead).

## 1. Parser sample — `eval/parse-sample/`

About 10 excerpts from freely available textbooks and lecture notes that the reviewer can judge (D19), chosen for difficulty, not typicality:
- side-by-side boxed security games or oracles (*The Joy of Cryptography*, Goldwasser–Bellare);
- boxed pseudocode (algorithms texts);
- commutative diagrams (category theory);
- dense derivations, matrices, and theorem/proof runs (algebra, calculus);
- at least one older born-digital PDF.

`ids.txt` lists each source's name, SHA-256, license, and URL, and each excerpt's page range. The PDFs are not in git (D20). The Phase 2 spec has the exact plan.

**Automatic metric:** formula render failure rate — every extracted `$…$`/`$$…$$` is rendered with KaTeX; count failures per parser.

**Manual metric:** for each pseudocode box, score 0/1/2 (scrambled / partially intact / faithful) recorded in `parse-scores.csv` with parser, paper, box, score, note. Decision rule: prefer the parser with the higher mean box score; formula failure rate breaks ties.

## 2. Gloss spot-check — `eval/gloss-sample/`

50–100 units drawn from the parser sample after segmentation, stratified: theorems, definitions, security games, algorithm steps, assumptions, and plain expository argument spans.

Reviewer marks each gloss: faithful / overstated / wrong / unclear, and whether the segmentation boundary was sensible. Prompt is frozen when ≥90% faithful and no "wrong" on theorems/definitions. Results in `gloss-review.csv`.

## 3. Retrieval questions — `eval/questions.jsonl`

30–50 questions written by someone who knows the corpus. Each line:

```json
{"q": "...", "expected": [{"paper": "2024/1234", "locator": "Theorem 3"}], "kind": "lookup|assumption|near-miss|negative", "notes": "..."}
```

- **lookup:** "which paper proves X secure under Y?"
- **assumption:** "what does Theorem 4.1 of 2023/0456 assume?"
- **near-miss:** same result under different assumptions, or a lemma vs its corollary — the expected answer must beat the distractor.
- **negative:** nothing in the corpus answers it; expected is empty; the system should abstain.

**Metrics (Phase 4):** recall@10 and MRR of the expected locator's unit/paragraph, reported per retrieval channel and for the fused result. **Metrics (Phase 5):** citation precision (fraction of emitted citations that survive the checker and are judged relevant by the reviewer), abstention correctness on negatives.

## 4. Fixtures for offline tests — `eval/fixtures/`

Two or three short parsed papers (paragraph JSON), a recorded set of LLM responses for the fake backend, and recorded OAI-PMH XML. Kept small; used by `make test`.
