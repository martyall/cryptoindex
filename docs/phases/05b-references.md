# Phase 5b — References

Status: planned, not scheduled (2026-09-23). The questions below are open until it is.

## Goal
The agent follows a paper's citations. Each document's bibliography is indexed as references. Each reference points at a document in the corpus when we have that document. Each passage records which references it cites. When a passage the agent reads rests on a cited work that is indexed, the agent is encouraged to consider whether that work's own statement would improve the answer, and if so to read it there. When the cited work is not indexed, the traversal ends at the bibliography entry, which is itself citable original text.

## Why
- The corpus is sealed at six documents on one theme (`docs/CORPUS.md`), and they cite each other: Halo cites Bulletproofs, and PlonK and the PCD thesis cite the cycles paper.
- Today the agent reaches another paper only by a separate topical search. Its search tool excludes bibliography entries, it cannot restrict a search to one document, and it cannot see which documents exist.
- `ARCHITECTURE.md` lists "cross-reference resolution between units" as a later slot. This is that slot between documents. It changes no boundary: a new stage, new tables, and more tools for the agent.

## What the data looks like (2026-09-23)
- **Entries:** the parser stored bibliography entries one per paragraph, as block kind `reference`:
  - PCD thesis: 136;
  - cycles paper: 114;
  - Bulletproofs: 76;
  - Halo: 38;
  - PlonK: 16;
  - Kimchi: 1. Kimchi names Halo and PlonK in its text instead.
- **Key styles:** `[AFK21]`, `[1]`, `1.`.
- **Identifiers:** some entries carry them, e.g. "IACR ePrint 2020/499".
- **In-text markers are OCR-mangled:** `[PBF $ ^{+} $]`, `$ [P^{+}91] $`. Matching a marker to its entry needs a model reading the text, not a pattern (No string munging).

## Deliverables

1. **Tables** (migration 009), written by `ci_ingest` and read by `ci_query`:
   - `docs.references`, one row per bibliography entry:
     - its revision, and the entry's paragraph ID, which is what gets cited;
     - the key, normalised by the model (`PBF+`, `1`);
     - title, authors, year, and any identifiers (ePrint, arXiv, DOI) as the model reads them;
     - `cited_paper_id`: the corpus document it names, or NULL;
     - the model, the prompt version and the input hash (Invariant 10).
   - `docs.unit_references`: unit → reference, meaning "this passage cites that entry".

   All of these fields are finding aids (Invariant 3). A citation still names only the original paragraphs: the passage, or the bibliography entry.

2. **A `references` stage** between `segment` and `embed`, with its own prompt `prompts/references-v1.md`. It runs on the same LLM interface as glossing, so both backends work (Invariant 9). For each revision:
   - it reads the bibliography paragraphs, in chunks, into structured entries;
   - it reads each unit's text together with the entry keys, and returns the keys the unit cites.

   Replies are stored per chunk as they arrive, as glossing's are, so the stage can be retried and requeued (Invariant 2). A document with no bibliography makes no calls.

3. **Linking references to documents.** The link depends on the whole corpus, not on one revision, so it is a job run whenever a revision reaches `ready`. It considers every reference whose `cited_paper_id` is NULL:
   - **When the entry and the document share an identifier, they match exactly.** Documents can carry optional identifiers, given at upload or added later: a new `docs.papers.external_ids`, since D18 made names free text.
   - **Otherwise a model judges the match.** It gets the reference and the list of corpus documents (their names and first-page text) and answers with a document or with none.

   The method used is stored with each link. `make relink` redoes all links.

4. **Tools for the agent:**
   - `search` gains a `documents` filter. Each hit lists the references its unit cites: key, title, and `document_id` when that work is indexed.
   - `get_reference(document_id, key)` returns the entry: its paragraph (citable), and the linked document if there is one.
   - `list_documents` returns names, IDs and outlines.

   Everything these tools return is recorded for the checker, as now (D29).

5. **Prompt `agent-v4`:**
   - When a point rests on a cited work that is indexed, decide whether that work's own statement would improve the answer. If so, search inside it and cite it there.
   - When the cited work is not indexed, stop there. Say it is cited but not in the corpus, if that matters to the answer.
   - Traversal is bounded by `max_turns` as now; the prompt asks for depth only where it helps.

6. **Pages:** the search page shows each hit's references, with the indexed ones marked. The Ask page's steps show the new tools.

## Acceptance
- **Offline tests,** using recorded replies:
  - bibliography extraction;
  - unit-to-reference mapping on a fixture with mangled markers;
  - linking by identifier and by model judgement;
  - `get_reference`;
  - search hits carrying their references;
  - the stage re-running with no calls on unchanged input.
- **On the corpus,** checked by hand:
  - Halo's reference to Bulletproofs links to the Bulletproofs document;
  - PlonK's and the thesis's references to the cycles paper link to it;
  - Kimchi's reference to ePrint 2020/499 stays unlinked.
- **Manual questions:**
  - A question whose best answer is in a cited paper, e.g. "how does Halo's inner product argument relate to Bulletproofs'", gets an answer that reaches the cited paper through the reference. The steps show `get_reference` or a `documents`-filtered search.
  - A question about a work that is cited but not indexed says so.

## Questions for the human
1. **What "ask" means.** I read "encouraged to ask if more detail would help" as the agent asking itself while it works: one question, one answer, as now. Asking you, as in "want me to look at what Halo cites here?", would make the Ask page a conversation, which Phase 5 left out. Which one did you mean?
2. **Cost of the unit-to-reference pass.** This is roughly one model call per glossing chunk: about 250 calls over the corpus, on your subscription, which throttled yesterday. The bibliography pass is about 20 calls. There are three options:
   - **(a) All of it on Opus**, as glossing: the best matching of mangled markers. **Recommended.**
   - **(b) The unit pass on a smaller model** (a different `CI_*` model setting for this stage): cheaper, and it is an easier task than glossing.
   - **(c) Skip the unit pass.** The agent reads markers in the text and calls `get_reference`, and search hits don't list references. This is cheapest, but references then influence only what the agent chooses to look up, not what search shows it.
3. **Identifiers at upload.** Should the upload page take optional identifiers ("ePrint 2019/1021")? Exact links need them. Without them, every link is the model's judgement.
4. **Naming.** This spec is called "5b" because Phase 6 (the HTTP API) is parked and this builds directly on Phase 5. Is that fine, or should it become Phase 6?

## Decisions to record
- **D31:** references are indexed per document and linked to corpus documents, and the agent follows them (this spec). The `ARCHITECTURE.md` "not in the minimal system" list and data-flow steps 4–5 change with it.

## Out of scope
- References to works outside the corpus beyond their entry: no fetching, no stubs (D18).
- Resolving references within a document (Section 3.2, Lemma 4).
- Citation counts, citation graphs, or ranking by them.

## Deferred
