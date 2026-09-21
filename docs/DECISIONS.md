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
