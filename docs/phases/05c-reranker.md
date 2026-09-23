# Phase 5c: Reranker

Status: built on `feature-reranker` (2026-09-23), off by default. Whether to use it is open.

## Goal
A cross-encoder reorders what hybrid search finds (D32). Unlike the embedding channels, it reads the question and each passage together. The hope is that the six passages the agent sees per search are better ones, so that it needs fewer searches.

## What was built
- **`cryptoindex.core.rerank`:** the `Reranker` protocol, a `CrossEncoderReranker` on sentence-transformers (float16 on MPS, loaded on first use, one lock per model), a `FakeReranker` for tests, and pinned revisions for the three candidates.
- **`search(…, reranker=)`:** it rescores the best `RERANK_CANDIDATES` (30) fused hits, then returns the best `k`. Each hit keeps its fused `score` and gains a `rerank_score`. Scoring runs in a thread, after the database connection has been returned.
  - The reranker reads `rerank_text(hit)`: the document's name, the gloss, then the paragraphs. The gloss comes first because it is plain words where the paragraphs are LaTeX, and because it then survives the cut at 1024 tokens.
  - A model trained with instructions (Qwen3-Reranker) gets the search's query instruction. One without (bge) gets the bare query.
- **Setting and download:** `CI_RERANK_MODEL`, and `make fetch-models` downloads that model.
- **Search page:** a "rerank" switch; each hit shows the rerank score beside the fused one.
- **The agent:** its `search` tool uses the reranker whenever one is configured.

## First comparison (2026-09-23, Qwen3-Reranker-0.6B, 8 queries)
- **Latency:** fused search takes 0.22–0.44 s; with reranking, 4.6–8.5 s. The agent makes several searches per question, so this adds tens of seconds to an answer.
- **Order:** mixed.
  - Better: it raised Halo's Definition 6 for `\langle \mathbf{a}, \mathbf{G} \rangle`, and Bulletproofs' Theorem 1 and Protocol 2 for the opening-proof question.
  - Worse: for "compliance predicate in proof-carrying data", it moved the passage that introduces the predicate (PCD thesis ¶60) below a later discussion of variants (¶500).
  - Neither order was good for "definition of an accumulation scheme": the corpus does not contain that definition.

## To decide
1. **Evaluation:** a labelled set, with the passages that should come back for each question, from the Phase 5 acceptance questions. Then compare recall@6 with and without the reranker, and the agent's searches per question.
2. **Latency:** try fewer candidates (20), shorter inputs (512 tokens), and bge-reranker-v2-m3, which is an encoder and may be faster. The 4B model is likely too slow at 30 candidates.
3. **Keep, tune or drop it:** decide once the numbers exist.

## Deferred
