# Phase 5 — Agent and citation checker

Status: ready (reviewed 2026-09-22)

## Goal
Ask a question in plain words; an agent searches the index, reads the relevant passages of the original documents, and answers in prose, citing its main points where a person can find them: document, page, section, and the numbered block (Theorem 3.1, Definition 7.5). Every citation is checked before the answer is shown, and a question the documents cannot answer gets a plain "not found", not a guess (ARCHITECTURE data flow 7; D28, D29, D30).

## What Phase 4 hands over
- Hybrid search over paragraphs, glosses and questions, with kinds (D25), on the read-only role, in the main process with the embedding model already loaded.
- For every paragraph: its document, PDF page, section path and, where a unit is anchored there, its label. That is enough for a human-findable citation.
- Two indexed documents to try it on: Halo and the Kimchi specification; three more downloaded.
- A dev-mode route to Claude on the subscription (D22), now through the Agent SDK too (D28).

## Deliverables

1. **The answer handler interface** (`cryptoindex.query.answer`), per D28:
   ```python
   class AnswerHandler(Protocol):
       async def answer(self, question: str, tools: Tools) -> AsyncIterator[AgentEvent]
   ```
   Events: `tool_call` (which tool, with what arguments), `tool_result` (what came back, summarized), `answer` (the model's structured answer), then, from the checker, `final`: the answer with each citation rendered or marked removed.

2. **Tools**, read-only, on `ci_query`, the same functions whatever the handler:
   - `search(q, kinds?)`: the Phase 4 hybrid search; returns units with their citation and a short excerpt.
   - `get_unit(unit_id)`: a unit's full original paragraphs.
   - `get_paragraphs(document, first, last)`: a range of paragraphs, to read around a hit.
   - `get_document(document)`: its name, and its outline (the section paths), to orient.

   Every paragraph a tool returns is recorded for the session: that is what "retrieved in-session" means to the checker (D29).

3. **The Claude Agent SDK handler.** Our tools registered as in-process functions, built-in tools disabled, settings sources off, the final answer as structured output in the answer schema (below), `max_turns` bounded. Model and route come from settings: dev mode on the subscription (no key) or the API (with a key).

4. **The answer and the citation checker** (D29, D30, D12), server-side, after the handler and outside it:
   - The model returns `{answer, citations: [{marker, paragraph_id}]}`: prose whose main points carry markers like `[1]`, and one citation per marker. Not every sentence needs one, and there are no quotes (D30).
   - A citation stays only if its paragraph was returned by a tool in this session; otherwise it is removed and its marker shown as removed. The answer itself is never dropped.
   - Nothing generated at ingestion (glosses, questions) can be cited (Invariant 3): citations name paragraphs of the original text.

5. **Human-findable citations.** Each citation that stays is rendered from stored data only: document name, PDF page (1-based), section path, and the paragraph's block label or its unit's anchor label when there is one. For example: *Kimchi specification, p. 42, §7 Polynomial commitment, Definition 7.5*.

6. **Prompt** `prompts/agent-v1.md` (Invariant 10): answer only from what the tools return; cite the main points, preferring the anchored block (the theorem, the definition); say plainly when the documents do not answer the question rather than guess.

7. **An Ask page** in the main process, beside the search page: a question box, the steps as they happen (streamed as server-sent events), and the answer with its citations. It is a QA tool like the search page; the full HTTP API stays Phase 6.

## Acceptance
- Offline tests with a scripted handler (no model): a citation of a paragraph no tool returned is removed and marked; the answer is never dropped; a citation renders to document, page, section and label.
- The tools are tested against the test database like search is.
- Manual run: about ten questions you write about Halo and the Kimchi specification, including one they cannot answer, answered on your subscription; you confirm that the cited points are right and findable, and that the unanswerable one says so.

## Decisions from the human (2026-09-22)
1. **Page numbers:** the PDF's page (1-based), which is what a viewer shows. The numbers printed on the page are deferred: viewers number pages their own way anyway, and many documents print none.
2. **Quotes:** none; a citation is a location (D30).
3. **Model:** Claude Opus 5.5 for the agent, set by its own setting, `CI_AGENT_MODEL`; glossing stays on Opus 5 (`CI_LLM_MODEL`). The model ID is to be confirmed with one small call, with the human's go-ahead, before the manual run.

## Out of scope
- A local-model handler (D28 defers it).
- The HTTP API proper, SSE for clients other than the Ask page, and the MCP adapter (Phase 6).
- Multi-turn conversation: one question, one answer.
- Uploading the other three documents is not needed for acceptance, but nothing stops it.

## Deferred
- **Showing a cited paragraph's stored text** beside the answer, for judging answer quality once answers are evaluated (D30).
- **Printed page numbers.** PaddleOCR-VL reads them (its `number` blocks), and the parser discards them as page furniture. Keeping them per page would need a parser change and a re-parse; many documents print none.
