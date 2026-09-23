---
name: corpus
description: Add documents to the cryptoindex corpus, or bring the index in line with docs/CORPUS.md. Use when the human asks, however loosely, to add, upload, ingest or index a paper, spec, thesis or PDF ("add the Nova paper", "index ePrint 2021/370", "put ~/Downloads/foo.pdf in"), or asks what is in the corpus.
---

# Managing the corpus

The human describes a document loosely: a title, a nickname ("the Nova paper"), an author, an ePrint or arXiv ID, a URL, or a local path. Your job is to pin down exactly which document they mean, give it the right name, record it in `docs/CORPUS.md`, and upload it to the running service. The mechanics (health check, the upload command, following progress) are in `docs/RUNNING.md`. Use its commands as written there.

## 1. Pin down the document

- Work out which document is meant. Search the web if you need to, preferring the paper's own ePrint or arXiv page.
- When more than one document fits, show the candidates (title, authors, year, source) and ask. That covers a conference version and a full version, several revisions, or two papers with similar names. Prefer the full or extended version when the human has no preference.
- Check `docs/CORPUS.md` and `GET /documents` first. If the document is already there, say so and stop.

## 2. Get the PDF and check it

- **A URL:** download it to the scratchpad or `/tmp`, following the `curl -fL -o` example in `docs/RUNNING.md`.
- **A local path:** use it in place.
- **Check it is the right document,** using pypdfium2 (a dev dependency, which `uv run` installs):
  - the title on its first page;
  - its page count, for the table.

  ```sh
  uv run python -c "import pypdfium2 as p, sys; d = p.PdfDocument(sys.argv[1]); print(len(d)); print(d[0].get_textpage().get_text_range()[:300])" FILE.pdf
  ```

## 3. Name it

The name is permanent. It is the document's title in every citation, and it is part of the glossing input hash, so a later rename means re-glossing. Follow the existing rows of `docs/CORPUS.md`:

- the title as printed on the first page, with its capitalisation;
- then the identifier in parentheses when it has one: `(ePrint 2019/1021)`, `(arXiv 2106.00001)`;
- for a thesis or a book, the author, as in `Proof-Carrying Data (Alessandro Chiesa, MIT thesis)`.

Show the name to the human before uploading, unless they already gave it.

## 4. Record it

Add a row to the table in `docs/CORPUS.md`: the name, the source (the URL, or the local path written with `~`), and the page count. Update the date in the sentence above the table. Do not commit unless the human asks.

## 5. Upload it

- **Check the service is up** with the health check in `docs/RUNNING.md`. If it is not running, ask before starting it. When you do start it, use the `make run |& tee -a data/logs/run.log` command from the Logs section, in the background.
- **Upload with the `curl -F file=@… -F "name=…"` command** from `docs/RUNNING.md`, one document at a time. A reply with `"duplicate": true` means this exact file was already there. Nothing new was queued.
- **Report back** each document's `id` and stage. Glossing runs on the human's Claude subscription and can be slow or throttled. Offer to watch `GET /ingest/status` until the documents reach `ready`, and report any `failed`, with the `error` from `GET /documents`.

## Syncing

"Make sure everything in CORPUS.md is uploaded": compare the table's names with the names in `GET /documents`. Then do steps 2 and 5 for each row that is missing, and report the rows that were already there.

## Never

- **Never delete anything.** The API has no delete endpoint. `make reset` wipes the whole index and every stored PDF, so run it only when the human asks for exactly that.
- **Never rename an uploaded document** by uploading it again under a new name. The same file comes back as a duplicate under its old name. Ask the human instead.
