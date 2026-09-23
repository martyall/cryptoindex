# Decision log

One entry per settled choice. To change a decision, add a new entry that supersedes the old one; never edit or delete old entries. All entries dated 2026-09-21 unless noted.

---

### D1. Language: Python for everything server-side
The parsing, embedding, and model tooling (PaddleOCR, Marker, sentence-transformers, MLX) are Python-first. Retrieval stays in Python because query embedding must use the same model as ingestion. The UI will be TypeScript, talking to Python over HTTP.

### D2. Storage: Postgres with pgvector, `halfvec`, HNSW
One database for metadata, text, and vectors; joins, filters, and transactions in SQL; roles enforce the read/write split. Corpus size (tens of thousands of papers, a few hundred thousand units) is far below pgvector's limits. `halfvec` halves memory at no measurable retrieval cost.

### D3. Embeddings: Qwen3-Embedding, local, 1024 dimensions
Leading open model, Apache-2.0, covers text, LaTeX tolerably, and code. Start with 0.6B; compare with 8B truncated to 1024 dims on the evaluation set. Vectors are derived data; switching models is a re-embed job, not a redesign.

### D4. Base unit is the paragraph; argument units are LLM-segmented spans
Crypto papers put the reasoning in prose and the theorem block is often just the punchline. Indexing formal blocks alone yields context-stripped fragments. Paragraphs get stable IDs; an LLM groups them into self-contained argument units; formal blocks are anchors within units.

### D5. Glossing uses a strong API model, run once, stored permanently
Glossing is translation of dense notation into correct English, not just compression. It runs once per unit, its errors are invisible (unfindable units), so quality is worth paying for. Use batch processing and prompt caching. Store model, prompt version, and input hash with every gloss; regenerate only when those change.

### D6. Pipeline: one process, stages joined by bounded channels carrying work IDs
Simplest to run locally, with natural backpressure. Crash safety comes from Invariant 1 (commit before signal, re-seed from DB on startup), not from the transport. Stages are functions `(work_id) -> None` so they can later be wrapped in a DB-polling loop and run as separate processes without changing stage code.

### D7. Parser call runs out-of-process
Native parser code can crash the host process. PaddleOCR-VL's model runs in a separate MLX-VLM server; Marker is invoked as a subprocess.

### D8. Parser choice (Marker vs PaddleOCR-VL) is decided by evaluation, not benchmarks
Public benchmarks (olmOCR-Bench arXiv math, OmniDocBench) put them within a few points. The deciding test is pseudocode/security-game fidelity on `eval/parse-sample/`. Default to Marker until Phase 2 decides.

### D9. LLM backends: Anthropic native SDK + OpenAI message format for local servers; no gateway
Native SDK gives prompt caching, batch API, and native tool use. Local servers (mlx_lm, llama.cpp, Ollama, LM Studio) all speak the OpenAI format on localhost; no OpenAI account is involved. Two backends behind a three-method interface (messages, tool calls, JSON) don't justify LiteLLM or another gateway.

### D10. Agent: hand-written tool-use loop, not an agent SDK, not MCP-internal
The Claude Agent SDK is tied to Claude models and bundles file/shell tools we'd disable; MCP inside our own process is an unnecessary hop. A ~100-line loop works with both backends and gives full control over streaming and citation checking. MCP remains an optional thin adapter over the same functions for external clients.

### D11. Citation checking is mechanical and server-side
The model returns structured claims with verbatim quotes and source IDs. Code verifies the source was retrieved in-session and the quote appears in stored text after normalization. Failing claims are dropped. Prompt instructions alone are insufficient.

### D12. Citation format is source-agnostic from day one
`(source_type, source_id, version, locator, quote)`. The core ships the `paper` source. Plugins register others (`code`). Unknown source types fail closed.

### D13. Plugin architecture: own schema, one-way references, events, versioned core contract
The code layer (and later standards, test vectors, formal models) are optional plugins. Uninstalling a plugin is `DROP SCHEMA`. The core never references plugin tables.

### D14. Code plugin indexes release tags only (deferred)
Security reasoning is per-release; explicit release membership avoids the non-linear-branch problem of commit ranges. Commit-level precision, when needed, is obtained on demand via git. Not in scope until Phase 8+.

### D15. Full-text search uses the `simple` configuration
English stemming mangles citations and terms of art ("IND-CCA2", "LWE", "42 U.S.C."). Exact matching is the safer default for this corpus.

### D16. Prompts are versioned files under `prompts/`
See Invariant 10.

### D17. Deferred: reranker, section summaries, cross-refs, notation extraction, HyDE
All are additive. HyDE is lowest priority: in cryptography, a hypothetical answer can bias retrieval toward what the model expects rather than what the papers say.

### D18. Documents are uploaded by a person; nothing is scraped (for now)
A person uploads PDFs and gives each a name of their choosing. The ingestion pipeline makes no network requests to ePrint, arXiv, or anywhere else. Documents are keyed by UUID, so names need not be unique, and no external identifier (ePrint ID, arXiv ID) is required. Every import goes through one function taking a name and a file stream, so a remote source (OAI-PMH harvest plus PDF fetch) can be added later as another caller. This supersedes the harvest-and-fetch scope of roadmap Phase 1; data flow steps 1–2 in `ARCHITECTURE.md` describe that later remote source.

### D19. The corpus is mixed; evaluation sets come from material the human can judge (2026-09-21)
The index will hold both cryptography research papers and standard mathematics and CS material (textbooks, lecture notes). The parser, gloss, and retrieval evaluations (`EVALUATION.md`) are built from material the human reviewer can judge: undergraduate mathematics, algorithms texts, and introductory cryptography. Research papers get indirect assurance only: their layout can be checked visually, and every citation is still verified against the stored text (Invariant 4), but whether a gloss of a research-level proof is faithful cannot be judged by the reviewer. Consequently the design stays general, not crypto-specific: formal-block kinds are the union of mathematical and cryptographic ones, and the glossing prompt is written for mathematical and CS text.

### D20. Evaluation samples may be downloaded once from named sources (2026-09-21)
A narrow exception to D18, approved by the human. Evaluation material may be downloaded once, one request per file, from the author's or institution's official URL, recorded with its URL, license, and sha256 in the evaluation set's `ids.txt`. The PDFs are never committed to git. The pipeline itself still makes no network requests.

### D21. Parser: PaddleOCR-VL (supersedes D8's default of Marker) (2026-09-21)
Chosen by the human after the Phase 2 evaluation (`eval/parse-report.md`: 84 of 100 excerpt pages scored blind).
- **For:** PaddleOCR-VL won every pseudocode and game-box excerpt, the fidelity D8 made the deciding test (Morin 2.00 vs 1.25, Joy of Cryptography 1.42 vs 0.67, Goldwasser–Bellare 1.83 vs 1.25, Erickson 1.50 vs 1.33). It writes inline math as LaTeX: it recognized 2,854 formulas where Marker recognized 689, because Marker writes most inline math as plain text.
- **The text-and-formula excerpts favoured Marker, but that comparison was confounded.** The review page showed 132 PaddleOCR-VL blocks as raw LaTeX source (114 equations it failed to delimit, 18 pseudocode blocks the page did not render), and no Marker blocks. Marker's plain-text math was not flagged at all. The reviewer could not read raw LaTeX and scored it lower. So those scores are not taken as evidence against PaddleOCR-VL.
- **Diagrams were not a criterion.** Neither parser reproduces them reliably, and text and formulas matter most.
- **Known weaknesses, accepted:**
  - some display equations are not delimited as math (flagged `malformed_math`);
  - stray tokens ("i.", "i.e.") and occasional invented words;
  - no heading levels, so section paths are at most `title > section`;
  - it needs the MLX-VLM server running natively (`make paddle-server`).

### D22. Glossing runs on Claude Opus 5 through an API key, or through Claude Code as a dev mode (2026-09-21)
Approved by the human. Extends D9 with a third backend; the two D9 backends stay as they are (Invariant 9).
- **`anthropic` (the supported option):** Claude Opus 5 through the native SDK, with `ANTHROPIC_API_KEY` in `.env`. Billed per token to Console credits; batches and prompt caching as in D5.
- **`claude_code` (dev mode):** the same model, reached by running the human's own logged-in Claude Code in headless mode (`claude -p` with `--json-schema`), so the calls count against their Claude subscription instead of API credits. It is for personal, local use only: Anthropic does not allow products to offer claude.ai login to other people, so a deployment for anyone else uses `anthropic`.
  - The child process is isolated: an empty working directory, a replacement system prompt, no tools, no settings, MCP servers, or skills, and no session saved. `ANTHROPIC_API_KEY` is removed from its environment, or Claude Code would bill the API instead of the subscription. `--bare` cannot be used, because it refuses subscription login.
  - It supports single-turn JSON requests only (what glossing needs); the agent (Phase 5) uses `anthropic` or `openai_format`.
  - Subscription usage limits apply. A call refused for a usage limit is not counted as a failed attempt; the stage waits and retries.

### D23. The glossing prompt is frozen at gloss-v2, judged on readable source text (2026-09-21)
Approved by the human after two blind spot-check passes (`eval/gloss-report-gloss-v1.md`, `eval/gloss-report-gloss-v2.md`). The roadmap expected v1 to be frozen.
- **gloss-v1:** 49 of 66 faithful (74%). Both failure modes were unit boundaries: the reviewer marked units too narrow to stand alone as "overstated", and most "unclear" units mixed several points. gloss-v2 adds rules for sizing units and asks each gloss to name what its unit belongs to.
- **gloss-v2:** 57 of 64 faithful (89%), none overstated or wrong, no wrong theorem or definition gloss. Of the 50 units whose parsed source text the reviewer judged readable, 46 were faithful (92%).
- **The ≥90% criterion (EVALUATION.md §2) is applied to units with readable source text.** A gloss cannot be better than the text it was given, and repairing parser noise in stored text is out of scope (Phase 3 spec); 14 of the 64 sampled units had garbled source text (D21's known weaknesses).
- Still open: 16 of 64 units were judged too narrow. That costs context at retrieval time, not gloss accuracy, so it is measured with Phase 4's retrieval questions instead of by further prompt tuning.

### D24. Retrieval is checked by manual QA until real search questions exist (2026-09-21)
Decided by the human. Nobody writes a question set in advance (EVALUATION.md §3); a question set would be invented rather than drawn from real use. Until real questions exist, search is checked by the human trying queries on a local search page that shows, for each hit, which channels found it and the passage it expands to.
- Phase 4's acceptance changes accordingly: no recall@10 or MRR, no measured comparison with and without the gloss channels.
- D3's comparison of Qwen3-Embedding 0.6B against 8B is postponed with it, since it needs the same questions; the index starts on 0.6B as D3 says. The retrieval harness is built once questions collected from real use exist.

### D25. Kinds are an open vocabulary, indexed and searchable; the prompt moves to gloss-v3 (2026-09-22)
Decided by the human after searching the first indexed paper, where the bibliography outranked the passage that answered the question. It supersedes D23's freeze of gloss-v2 (the freeze stands for what it measured; this adds a field).
- **References are a block kind of their own.** PaddleOCR-VL's `reference_content` and Marker's `Bibliography`/`Reference` map to `reference` instead of plain text, so the label comes from the parser, not from reading the text.
- **References are stored and embedded, but not glossed** (`gloss.UNGLOSSED`) and not part of any unit; a section is broken where one sits, so no unit spans a bibliography. A reference a search matches is returned as a hit on its own.
- **A unit's anchor also records the document's own word for it** (`units.anchor_term`: `lemma`, `protocol`, `hybrid`, …), copied from the label and checked against it, beside the coarse `anchor_kind`. gloss-v3 asks for it. The vocabulary is open: a document that labels blocks "Claim" or "Construction" is searchable by those words without a code change.
- **Both are indexed** (migration 007), and `search()` takes `kinds` to keep and `exclude` to drop, matching a paragraph's structural kind, a unit's anchor kind, or its anchor term. Only `reference` is excluded by default. The QA page offers the kinds present in the results, rather than a fixed set of filters.

### D26. gloss-v4: a footnote or caption need not belong to a unit (2026-09-22)
Approved by the human while indexing the Kimchi specification, which has 200 footnotes against Halo's 4; almost all are pointers at a source file (`Kimchi.Gate.VarBaseMul.secant_add`), and a handful carry real content. Extends D25's treatment of minor blocks; D23's freeze is superseded again for the same reason.
- **Footnotes and captions are still sent to the model** (`gloss.OPTIONAL`), so an argument that uses one can include it, and a caption can join the unit discussing its figure. Only the coverage rule changes: a reply is no longer rejected for leaving them out.
- **They are not what a section breaks on.** Unlike a bibliography, they sit inside the text; breaking there would cut arguments in half and cost more calls, not fewer.
- Skipping them by length or by reading them was rejected: that is a heuristic over text.

### D27. A second embedding model's vectors, for comparison during development (2026-09-22)
Decided by the human, to compare Qwen3-Embedding 0.6B and 8B on real searches (D3's comparison, which D24 postponed) without re-embedding each time. Amends Invariant 6 for the development period, and is to be removed once the choice is made.
- **Two sets of vector columns:** the primary set (`paragraphs.emb`, `units.emb_gloss`, `unit_questions.emb`) and an alternate set (`emb_alt`, `emb_gloss_alt`, `emb_alt`), named by role, not by model. `docs.meta` records the model behind each (`embed_model`, `embed_model_alt`). Invariant 6's guarantee holds per set: a set holds one model's vectors, and a search embeds its query with that set's model and compares only that set.
- **The embed stage fills both** when `CI_EMBED_MODEL_ALT` is set.
- **Which set searches use is global, fixed at startup** (`CI_SEARCH_VECTORS=primary|alt`), never chosen per query, so there is no doubt which model produced a result. Comparing is a restart.
- **Removal:** a migration dropping the three `_alt` columns and their indexes, and deleting `embed_model_alt` from `docs.meta`; `CI_EMBED_MODEL_ALT` and `CI_SEARCH_VECTORS` leave the settings.

### D28. Answers come from an answer handler; the first is the Claude Agent SDK (2026-09-22)
Decided by the human when planning Phase 5. Supersedes D10's hand-written tool-use loop, and defers Invariant 9 for the agent (not for glossing).
- **The agent is a handler behind one interface:** a question in; a stream of steps, then claims with citations, out. The tools it may call, the citation checker, and the HTTP layer sit outside the handler, so a handler can be replaced without touching them.
- **The first handler is the Claude Agent SDK.** Writing our own loop would rewrite what the SDK already does: run the loop, call tools, keep the session. Our tools are registered with it as in-process functions; its built-in file and shell tools are disabled; the final answer comes back in our claims schema as structured output. Checked on 2026-09-22: with no `ANTHROPIC_API_KEY`, it runs on the human's Claude Code subscription (dev mode, as D22), and with a key, on the API.
- **The model never writes SQL.** It calls our tools, which run fixed queries on the read-only role (Invariant 7).
- **Invariant 9 is deferred for the agent:** a local-model handler (a loop of our own over the OpenAI format) can be added later behind the same interface; nothing now depends on its absence.

### D29. Citations mark the main points; the checker removes bad citations, not claims (2026-09-22)
Decided by the human when reviewing the Phase 5 spec. Supersedes D11's strictness (every claim cited with a verbatim quote, failing claims dropped); D11's core stands: citations are checked mechanically, server-side, before an answer is shown (Invariant 4).
- **An answer is prose; the agent cites its main points**, not every sentence.
- **A citation is a paragraph ID**, and optionally a verbatim quote. What a reader sees (document, page, section, block label) is read from the stored paragraph, so the model cannot invent a location.
- **The checker's rules:** a cited paragraph must be one a tool returned in this session, which stops invented or guessed IDs; a quote, if given, must appear in that paragraph's stored text after normalization. A citation failing the first rule is removed; a quote failing the second is removed and the citation kept. Either way the answer stays, with the removals marked, and nothing is dropped silently.
- **No forced abstention.** An answer with no citations is allowed; the prompt tells the agent to say plainly when the documents do not answer the question.
- **What this cannot catch:** a real paragraph cited for a point it does not make. Only a reader can judge that; the optional quote is what helps them.

### D30. A citation is a location; the agent gives no quotes (2026-09-22)
Decided by the human when reviewing the Phase 5 spec. Supersedes D29's optional quotes; the rest of D29 stands.
- **A citation's job is to let a person find the passage:** document, page, section, and the block label where there is one, all read from the stored paragraph the agent cited.
- **The agent gives no quotes.** A model rarely reproduces text exactly, least of all LaTeX and OCR noise, so checking quotes would mostly remove correct citations.
- **The checker's one rule:** a cited paragraph must be one a tool returned in the session; a citation that fails it is removed and its marker shown as removed.
- **Showing the cited paragraph's text** beside the answer is deferred, as a way to judge answer quality when answers are evaluated.

### D31. Dev-mode glossing goes through the Claude Agent SDK, not the `claude` CLI (2026-09-23)
Decided by the human: the server must not depend on an installed Claude Code CLI. This supersedes D22's mechanism, the `claude -p` subprocess; D22's intent and rules stand (subscription billing, personal use only, isolation, usage-limit waits).
- The `claude_code` backend calls the SDK's one-shot `query()` with structured output, as the answer agent (D28) uses the SDK. Both run the CLI bundled with the SDK, pinned by `uv.lock`, so one Claude Code version serves the whole process, and `CI_CLAUDE_BIN` is gone.
- The isolation is the same, set through SDK options:
  - no tools, no settings sources, only our (empty) MCP config;
  - an empty working directory;
  - no slash commands and no saved session (`extra_args`);
  - the API credentials blanked in its environment, so it bills the subscription.

### D32. An optional cross-encoder reranker after fusion, off by default (2026-09-23)
Requested by the human. This lifts D17's deferral of the reranker; the rest of D17 stands.
- **Setting:** `CI_RERANK_MODEL` names a cross-encoder with a pinned revision. The candidates are Qwen3-Reranker 0.6B and 4B (the embedder's family) and bge-reranker-v2-m3. When it is empty, search is unchanged.
- **What it does:** it rescores the best 30 fused hits against the question and returns the best `k` by its score. It reads the document's name, then the gloss, then the original paragraphs, up to 1024 tokens. Hits are only reordered: a citation still names original paragraphs (Invariant 3).
- **Where it applies:** the same search serves the agent's tool and the search page. The page can switch the reranker off for one query, to compare by hand (D24).
- **Status:** development only, like D27, until it is shown to help. With the 0.6B model, the first comparison on this corpus found a mixed ordering and 4.5–8 s added to each search (`docs/phases/05c-reranker.md`).
