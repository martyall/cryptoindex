# cryptoindex

Citation-grounded retrieval and answering over mathematics, computer science and cryptography documents that you upload (D18, D19), running locally. An agent answers questions from the index with citations a person can find (D28–D30). An optional code-analysis plugin is planned for later.

To run it, see `docs/RUNNING.md`. The design documents in `docs/` drive the build; `docs/ROADMAP.md` says which phases are done.

## Reading order

| Document | Purpose | Read when |
|---|---|---|
| `CLAUDE.md` | How a coding agent should work in this repo | Every session |
| `docs/RUNNING.md` | Starting the service, ingesting the corpus, the pages | Before running it |
| `docs/CORPUS.md` | The documents to index | Before ingesting |
| `docs/ARCHITECTURE.md` | The system and its invariants | Every session |
| `docs/DECISIONS.md` | Settled choices and why | Before changing anything structural |
| `docs/ROADMAP.md` | Phases, scope, acceptance criteria | Start of each phase |
| `docs/phases/NN-*.md` | Detailed spec for the current phase | During that phase |
| `docs/DATA_MODEL.md` | Database contract | Phases 0+ |
| `docs/INTERFACES.md` | Internal contracts (stages, parser, LLM, embedder, citations, API) | Phases 0+ |
| `docs/EVALUATION.md` | Evaluation sets and how "done" is measured | Phases 2+ |

## Layout

```
cryptoindex/
  CLAUDE.md
  README.md
  docs/            design documents
  docs/phases/     one detailed spec per phase, written just before it starts
  eval/            evaluation data (sample PDFs, parsed excerpts, review scores)
  prompts/         versioned prompt files (glossing, segmentation, agent)
```

Source code lives under `src/cryptoindex/`, migrations under `db/migrations/`, tests under `tests/`.
