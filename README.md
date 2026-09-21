# cryptoindex

Citation-grounded retrieval over cryptography papers (primarily the IACR ePrint archive), running locally, with an optional code-analysis plugin planned for later.

The design documents in `docs/` drive the build. See `docs/ROADMAP.md` for which phases are done, and `CLAUDE.md` for the `make` commands.

## Reading order

| Document | Purpose | Read when |
|---|---|---|
| `CLAUDE.md` | How a coding agent should work in this repo | Every session |
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
  eval/            evaluation data (sample PDFs, questions) — populated in Phase 2+
  prompts/         versioned prompt files (glossing, segmentation, agent)
```

Source code lives under `src/cryptoindex/`, migrations under `db/migrations/`, tests under `tests/`.
