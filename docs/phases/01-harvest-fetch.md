# Phase 1 — Harvest and fetch

Status: draft, awaiting review

## Goal
Papers and their PDFs flow in; each distinct PDF becomes a revision at stage `parse`, and the runner picks it up.

## Deliverables

1. **HTTP client** (`ingest/http.py`). One `httpx.AsyncClient` configured from settings: `CI_USER_AGENT`, a minimum delay of `CI_FETCH_DELAY_S` between requests to the same host, and retry with exponential backoff on connection errors, 429, and 5xx, honouring `Retry-After`. The harvester, fetcher, and arXiv matcher all use it. `httpx` moves from a dev to a runtime dependency.

2. **Harvester** (`ingest/harvest.py`). OAI-PMH `ListRecords` against `CI_EPRINT_BASE`:
   - Parses records into `docs.papers` (title, authors, abstract, subjects, license, `oai_datestamp`), upserting by ePrint ID. A record whose datestamp is unchanged is a no-op.
   - Follows `resumptionToken`. Tokens expire, so harvesting resumes from a datestamp high-water mark stored in `docs.meta` (`oai_harvested_until`), committed after each page.
   - Category filter (see open questions for how ePrint exposes categories).
   - Deleted records (`status="deleted"`) are logged and left in place. Removing papers is out of scope.

3. **Fetcher** (`ingest/fetch.py`). For papers where `fetched_datestamp` is null or older than `oai_datestamp`:
   - Streams the PDF to a temp file, computes the sha256, and moves it to `CI_DATA_DIR/pdfs/<paper_id with / → _>/<sha256>.pdf`.
   - In one transaction: inserts a revision (`revision` = previous + 1, stage `parse`) unless `(paper_id, pdf_sha256)` already exists, sets `fetched_datestamp`, and writes `paper_revised` when a second or later revision appears. Then enqueues the revision.
   - A changed PDF adds a revision and leaves earlier revisions and their files untouched.

4. **arXiv cross-match** (`ingest/arxiv.py`). Looks up each paper's normalized title (case, whitespace, LaTeX, punctuation) via the arXiv API and records `arxiv_id` only on an unambiguous exact normalized match. Downloading arXiv sources is Phase 2.

5. **How work reaches the runner.** Harvest and fetch run as tasks in the main process, started by `python -m cryptoindex.ingest harvest|fetch|match` (also `make harvest` / `make fetch`) connecting as `ci_ingest`. New revisions must be picked up by an already-running `make run`. Proposed: the runner re-seeds the `parse` channel from the database every `CI_RESEED_S` seconds. This keeps the database as the only channel between processes (Invariant 1) and needs no new mechanism. The alternative, `LISTEN/NOTIFY`, is faster but adds a second signalling path.

6. **Fixtures and tests** (offline, `httpx.MockTransport` serving files under `eval/fixtures/`):
   - Recorded OAI-PMH pages for one category and one month, including a resumption token and a changed datestamp.
   - A few small PDFs, including two different PDFs for the same paper.
   - Recorded arXiv API responses: one match, one near-miss title, and one ambiguous result.
   - Fixtures are recorded once with `make record-fixtures`, which is the only command that touches the network, and then committed.

## Acceptance (from ROADMAP)
- Harvesting one category for one month populates `docs.papers` with correct metadata and licenses, tested against recorded OAI responses.
- Re-running the harvest is a no-op except for changed datestamps.
- Fetching the same PDF twice creates one revision. A changed PDF creates a second revision and leaves the first intact.

## Out of scope
Parsing, arXiv source download, backfill beyond the test month, scheduled harvesting (Phase 7), API endpoints for ingest control (Phase 6).

## Open questions for the human
1. **Network access to record fixtures.** Recording fixtures needs one-time requests to `eprint.iacr.org` and `export.arxiv.org`. OK to make them, politely and rate-limited, when this phase starts?
2. **Which category and month** should the acceptance harvest and fixtures use?
3. **Unverified facts about ePrint's OAI-PMH feed**, to be checked on the first recording before the harvester is written:
   - whether categories are exposed as OAI sets or only inside the record metadata, which decides whether the filter is server-side or client-side;
   - where the per-paper license appears, and what it looks like when absent;
   - how the PDF URL is derived for a given version.
4. **Licenses.** Phase 1 only stores them. Should any license stop the PDF being fetched at all, or does enforcement only matter for what is shown or quoted later?
5. **Work reaching the runner.** Periodic re-seed (recommended) or `LISTEN/NOTIFY`?

## Deferred
(Add items discovered during this phase that belong to later phases.)
