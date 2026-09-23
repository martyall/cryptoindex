# Phase 1 — Upload

Status: done (2026-09-21)

## Goal
A person uploads PDFs through a small web page, giving each a name of their choosing. Each upload becomes a document with a UUID and a revision at stage `parse`, and the running pipeline picks it up immediately. Nothing is fetched from the network (D18).

## Deliverables

1. **Migration `003_documents.sql`.**
   - `docs.papers.id` becomes `uuid DEFAULT gen_random_uuid()`, and `docs.revisions.paper_id` becomes `uuid`.
   - A new `name text NOT NULL` column holds the uploader's name, which need not be unique.
   - `title` becomes nullable. A parser may fill it in Phase 2.
   - `docs.revisions.pdf_sha256` gets a unique index across all documents, replacing the per-paper one. The same file can therefore never be indexed twice, even when two uploads race.
   - The ePrint-specific columns (`oai_datestamp`, `fetched_datestamp`, `arxiv_id`) stay nullable and unused, for a possible remote source later.
   - `DATA_MODEL.md` is updated to match.

2. **Importer** (`ingest/importer.py`). `import_document(name, stream) -> ImportResult` is the single entry point, whatever the file's origin:
   - Rejects files that are not PDFs (checked by their `%PDF-` header) or that exceed `CI_MAX_UPLOAD_MB`.
   - Streams the file into a temp file under `CI_DATA_DIR`, hashing (sha256) as it writes, so the file is read once and never held in memory.
   - If that hash already exists, deletes the temp file and returns the existing document ("already uploaded as <name>").
   - Otherwise it moves the file to `pdfs/<document uuid>/<sha256>.pdf` and, in one transaction, inserts the document and revision 1 at stage `parse`. The file is written before the commit, so a crash between the two leaves at most an orphan file and never a row without a file.
   - If two identical uploads race, the unique index rejects the second insert, and the importer returns the first document.
   - **Identity:** the document is identified by its UUID, and each of its files by its hash (one revision per file). A later corrected or published PDF can become revision 2 of the same document, keeping its name and citations (Invariant 5).

3. **API**, on the existing FastAPI app:
   - `POST /documents` takes multipart `file` and an optional `name`, which defaults to the file name without `.pdf`. It returns the document ID, name, revision ID and whether the upload was a duplicate, and signals the runner with `runner.notify()`.
   - `GET /documents` lists documents with their name, stage, and upload time.

4. **Upload page** at `GET /`. One static HTML file served by the app, with no framework and no build step:
   - pick one or more PDFs, edit each name (pre-filled from the file name), and upload;
   - a list of documents showing each one's pipeline stage, refreshed every few seconds.

   This page is a stopgap, not the planned TypeScript UI (D1).

5. **Tests** (offline):
   - importer: new document, duplicate content, non-PDF rejected, oversize rejected, two uploads with the same name produce two documents;
   - API upload through the ASGI test client;
   - an uploaded document reaches `ready` through the no-op stages without restarting the runner.

## Acceptance
- Uploading a PDF through the page, or with `curl -F file=@x.pdf -F name=... localhost:8000/documents`, creates a document with a UUID and a revision, and it reaches `ready` without restarting `make run`.
- Two different PDFs uploaded under the same name become two separate documents.
- Uploading the same PDF again creates nothing new and reports the existing document.

## Out of scope
- Network sources (ePrint, arXiv, URLs).
- Uploading a new version of an existing document. The schema supports it through revision 2, but there is no UI for it yet.
- Editing names, deleting documents, authentication.

The API binds to `127.0.0.1` only, so it is reachable from this machine and nowhere else.

## Decisions from the human (2026-09-21)
- Minimal metadata: a PDF and a name. Names need not be unique.
- Keep both identities: a UUID per document and a hash per file.
- Guard against re-indexing. An identical file is rejected with a pointer to the existing document; re-processing the same revision is already prevented by Phase 0's idempotent stages.

## As built
- **Non-blocking signal.** `Runner.notify()` enqueues from a request without waiting, so a full parse channel (bounded, capacity `CI_QUEUE_CAPACITY`) cannot stall an upload.
- **Where the size limit applies.** It is enforced by the importer, after Starlette's multipart parser has already spooled the whole request to a temp file. `CI_MAX_UPLOAD_MB` therefore stops an oversized file from being stored, not from being received. For the same reason, the importer reads the spooled copy rather than the network stream, so a file is read twice, not once as deliverable 2 says. Both are acceptable for a single local user.
- **`notify()` is fail-safe.** It never raises. A failed signal is logged and cannot stop the runner; the committed revision is seeded on the next start. It is a no-op while the runner is not running. Both paths are tested (from the Phase 1 comment review).
- **Unchanged after review.** The header comment of `003_documents.sql` restates D18 instead of pointing to it, and `001_core.sql` still labels `papers.id` as an ePrint ID. Both files have been applied, and migrations are never edited, so `DATA_MODEL.md` is the correct description.
- **Reads use the ingest role.** `GET /documents` reads with the ingest pool, like `/ingest/status`. The query role is for the retrieval code that arrives in Phase 4.
- **Stored file layout.** `pdf_path` is stored relative to `CI_DATA_DIR`, so the data directory can move. Files are created mode 0600.
- **Verified end to end** on a real `make run`:
  - the page is served;
  - a real 470 KB PDF uploaded through the page reached `ready`;
  - duplicates and non-PDFs are answered correctly;
  - Ctrl-C (SIGINT to the whole `make` process group) shuts everything down cleanly.

## Deferred
- **Phase 2 — near-duplicate detection.** The same paper uploaded as a different file (arXiv versus conference version, a PDF with a download stamp, a re-saved or annotated copy) hashes differently, so upload cannot catch it. Once text is parsed, compare it against existing documents and warn ("looks similar to <name>"), without rejecting, because it may be a revised version worth keeping.
- **UI phase — managing documents.** Uploading a new version of an existing document, renaming, and deleting. The schema already supports new versions as revision 2.
- **Phase 7 — orphan files.** A crash between storing a file and committing its rows leaves an orphan `pdfs/<uuid>/` directory. A sweep should remove directories with no matching document.
- **Phase 5 — citations by name.** Names are not unique (D18), so a citation rendered as "<name> v2, Theorem 3" can be ambiguous. The document UUID stays in `Citation.source_id`; rendering should disambiguate when two documents share a name.
- **Phase 7 — database restarts.** The connection pool does not check connections on checkout. After the database restarts under a running process (for example `make reset`), the first requests fail until the pool reconnects. Enabling the pool's connection check fixes this.
