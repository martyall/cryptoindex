# Interfaces

Signatures are illustrative Python; the shapes are the contract.

## Stage function

```python
async def stage(work_id: int, ctx: StageContext) -> None
```
- `work_id` is a `docs.revisions.id`. The stage loads whatever it needs from the DB.
- Must be idempotent. Must commit output and the stage transition in one transaction, then return. The runner signals the next channel after return.
- Raise to signal failure; the runner records `last_error`, increments `attempts`.

## Pipeline runner

- One bounded `asyncio.Queue[int]` per stage transition; capacity is configuration.
- Per-stage concurrency is configuration (parse: 1, gloss: N, embed: 1).
- On startup: mark stale locks, re-seed each queue from `docs.revisions` by stage.
- Exposes `enqueue(work_id)` and a status snapshot (counts per stage) for the API.

## Parser

```python
class Parser(Protocol):
    name: str
    version: str
    def parse(self, pdf_path: Path) -> str  # Markdown with LaTeX math
```
Runs out-of-process. Implementations: `MarkerParser`, `PaddleVLParser` (client of an MLX-VLM server), `ArxivSourceParser`. A separate pure function turns Markdown into paragraphs with section paths and block detection.

## LLM backend

```python
class LLM(Protocol):
    name: str
    async def complete(self, system: str, messages: list[Message],
                       tools: list[ToolSpec] | None = None,
                       json_schema: dict | None = None,
                       cache_prefix: bool = False) -> Completion
```
`Completion` holds text, parsed JSON (if a schema was given), and tool calls. Implementations: `AnthropicLLM` (native SDK; supports batch submission via a separate `batch()` method used only by glossing), `OpenAIFormatLLM` (local servers), `FakeLLM` (replays recorded responses keyed by request hash; used in tests).

## Embedder

```python
class Embedder(Protocol):
    model: str
    dims: int
    def embed_documents(self, texts: list[str]) -> np.ndarray
    def embed_queries(self, texts: list[str], instruction: str) -> np.ndarray
```
Qwen3 requires the instruction prefix on queries only. Vectors are L2-normalized. `FakeEmbedder` (deterministic hash-based vectors) for tests.

## Retrieval

```python
async def search(q: str, filters: Filters, k: int = 20) -> list[Hit]
```
`Hit` = paragraph or unit reference, score, channel breakdown (which retrievers matched), and the expanded unit span. Filters: paper IDs, subjects, year range, `current_only` (default true).

## Citation

```python
@dataclass(frozen=True)
class Citation:
    source_type: str   # 'paper' (core); plugins add more
    source_id: str     # paper: paper_id
    version: str       # paper: revision number
    locator: str       # paper: 'p<position>' or block label, e.g. 'Theorem 3'
    quote: str         # verbatim
```
```python
class CitationSource(Protocol):
    source_type: str
    def fetch(self, source_id: str, version: str, locator: str) -> str
    def render(self, c: Citation) -> str   # 'ePrint 2024/1234 v2, Theorem 3'
```
Checker: quote must appear in `fetch(...)` after normalization (whitespace, LaTeX spacing, Unicode dashes/quotes); source must have been returned by a tool call in the same session; unknown `source_type` ⇒ unverifiable ⇒ dropped.

## Agent

```python
async def answer(question: str, llm: LLM, tools: ToolSet) -> AsyncIterator[AgentEvent]
```
Events: `tool_call`, `tool_result`, `claims` (structured), `verification` (per-claim pass/fail), `final` (answer or abstention with nearest units). Tools: `search`, `get_unit`, `get_paragraphs`, `get_paper`. Output schema for claims: `[{text, citations: [Citation]}]`.

## Plugin registration (core contract v1)

Entry point group `cryptoindex.plugins`; a plugin exposes `register(core: CoreAPI)` where `CoreAPI` offers: migrations hook (plugin's own schema), `add_citation_source`, `add_tools`, `subscribe(event_kind, handler)`, `embedding_model()`, and read access to `docs.*` via the query role.

## HTTP API (Phase 6)

- `POST /ingest/papers` (IDs or category+range) · `POST /ingest/retry` · `POST /ingest/regloss` (prompt version) · `GET /ingest/status` · `GET /ingest/events` (SSE)
- `GET /search?q=&…` · `GET /papers/{id}` · `GET /units/{id}` · `GET /paragraphs?revision=&from=&to=`
- `POST /agent/answer` (SSE stream of `AgentEvent`)
- `GET /openapi.json`
