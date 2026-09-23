# Running cryptoindex

How to start the service on this machine, ingest the corpus, and use the pages. `make` (no target) lists every command with its options. This page gives the order to run them in.

`tests/test_running_doc.py` checks that every `make` command below names a real target, and that every local URL is a route the app serves. Keep each command on one line so that it can be checked.

## What it needs

- **This Mac** (Apple Silicon, 48 GB). PaddleOCR-VL parses through MLX on the GPU (D21). The 8B embedding model needs about 16 GB while loaded.
- **Docker**, for Postgres, and **uv**, for everything else.
- **A logged-in Claude subscription**, for glossing and answering in dev mode (D22, D28, D31). Both run through the Claude Agent SDK and its bundled Claude Code, which uses the login already on this machine. With `ANTHROPIC_API_KEY` set, the answer agent bills the API instead; glossing then needs `CI_LLM_BACKEND=anthropic` to do the same.

## Configure

```sh
make .env
```

This copies `.env.example`, whose comments explain each setting. The setup used here differs from the defaults in these settings:

| Setting | Value | Why |
|---|---|---|
| `CI_LLM_BACKEND` | `claude_code` | glossing on the subscription (D22, D31) |
| `CI_EMBED_MODEL` | `Qwen/Qwen3-Embedding-8B` | primary vectors |
| `CI_EMBED_MODEL_ALT` | `Qwen/Qwen3-Embedding-0.6B` | second vector set, for comparison (D27, development only) |
| `CI_SEARCH_VECTORS` | `primary` | which set searches use; change it and restart to switch |
| `CI_RERANK_MODEL` | empty, or `Qwen/Qwen3-Reranker-0.6B` to compare | optional reranker after fusion, off by default (D32); the search page can switch it off per query |

Changing an embedding model after documents are indexed means re-embedding. See `make requeue` in the `make` help.

### Run options

These are fixed for a run: change `.env`, then restart `make run`.

| Option | Setting | Effect |
|---|---|---|
| Which vectors searches use | `CI_SEARCH_VECTORS=primary` or `alt` | `alt` searches the 0.6B vectors instead of the 8B ones (D27); it needs `CI_EMBED_MODEL_ALT` |
| Reranker on | `CI_RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B`, `Qwen/Qwen3-Reranker-4B` or `BAAI/bge-reranker-v2-m3` | a cross-encoder reorders the best 30 fused hits for the search page and the agent (D32); adds about 5–8 s per search with the 0.6B |
| Reranker off | `CI_RERANK_MODEL=` (empty) | fused order only, as before D32 |

A model not yet downloaded must be fetched before the restart:

```sh
make fetch-models
make run |& tee -a data/logs/run.log
```

The search page's `/search/api/info` names the models the run uses. The page also has switches for a single query: "gloss and question channels", to leave out what the model wrote at ingestion, and "rerank", shown when a reranker is set, to see the fused order instead.

## Start

```sh
make fetch-models
make run
```

- **`make fetch-models`** downloads the pinned embedding weights once. It resumes an interrupted download.
- **`make run`** starts Postgres, migrates, and starts PaddleOCR-VL's model server. It then runs the pipeline and the web server in one process, in the foreground, logging JSON lines to the terminal (see [Logs](#logs)).
  - The model server's log is `data/logs/mlx-vlm.log`.
  - Ctrl-C stops everything, including the model server if `make run` started it.

The service is ready when this returns `{"status": "ok"}`:

```sh
curl -s http://127.0.0.1:8000/health
```

## Logs

`make run` writes one JSON object per line to the terminal (stderr). Each line has `time`, `level`, `logger`, `event` and the event's own fields. `CI_LOG_LEVEL` in `.env` sets the level. To keep a copy that you can follow from another terminal:

```sh
make run |& tee -a data/logs/run.log
tail -f data/logs/run.log | jq -R -c 'fromjson? | select(.level != "INFO" or .event == "stage_done" or .event == "answer_done")'
```

`make`, Docker and uv print lines of their own at startup, so `jq` reads raw lines and keeps those that parse as JSON (`-R`, `fromjson?`). The filter shows every warning and error, plus one line each time a document finishes a stage or a question is answered. Replace the `select(...)` with `.` to see every log line. Events worth knowing:

| Event | Meaning |
|---|---|
| `stage_done` | a document finished a stage, with counts |
| `stage_failed` | an attempt at a stage failed, with the error; `gave_up` says whether it will be retried |
| `llm_usage_limit` | glossing is waiting out a subscription usage limit |
| `agent_session` | the model and the tools an answer session was offered |
| `answer_done` | one per question: seconds, tool calls, paragraphs read, outcome |

## The pages

All the pages are served by `make run`; there is no separate UI to host.

- **Upload:** [http://127.0.0.1:8000/](http://127.0.0.1:8000/). Add PDFs with a name each, and see their stage.
- **Search:** [http://127.0.0.1:8000/search/](http://127.0.0.1:8000/search/). Hybrid search, showing which channels found each hit, with filters by kind (D24, D25).
- **Ask:** [http://127.0.0.1:8000/ask/](http://127.0.0.1:8000/ask/). A question answered by the agent, with its steps and located citations (D28–D30).

## Ingest the corpus

`docs/CORPUS.md` lists the documents, their sources, and the name each is uploaded under. That name is the document's title everywhere (D18).

The easy way is to ask Claude Code in this repository, however loosely: "add the Nova paper to the corpus", or "make sure everything in CORPUS.md is uploaded". The `corpus` skill (`.claude/skills/corpus/SKILL.md`) pins down the document, names it, adds its row, and uploads it with the commands below.

By hand: for each row, download the source if it is a URL, then upload it under its name. For example:

```sh
curl -fL -o /tmp/2019-1021.pdf https://eprint.iacr.org/2019/1021.pdf
curl -s -F file=@/tmp/2019-1021.pdf -F "name=Halo: recursive proof composition without a trusted setup (ePrint 2019/1021)" http://127.0.0.1:8000/documents
curl -s -F "file=@$HOME/code/paper/kimchi-spec.pdf" -F "name=Kimchi specification" http://127.0.0.1:8000/documents
```

- **Re-uploading is safe:** uploading the same file again returns the existing document with `"duplicate": true` and queues nothing.
- **Documents move through three stages:** `parse` → `segment` (glossing) → `embed` → `ready`. Parsing takes about 6 s a page, one document at a time. Glossing makes one call per chunk of about 14 paragraphs, and a subscription can throttle it for hours without failing. Watch the counts per stage, and the stage of each document:

```sh
curl -s http://127.0.0.1:8000/ingest/status
curl -s http://127.0.0.1:8000/documents
```

## When something goes wrong

- **A document in `failed`:** its `error` in `/documents` says why, and the `stage_failed` log lines give each attempt. Fix the cause, stop `make run`, and send it back to the stage that failed. Pass the document's `id` from `/documents` as `DOCS`; the one below is an example:

  ```sh
  make requeue STAGE=parse DOCS=8b009488-fd4d-4c76-8db7-eb913057b957
  ```

- **After a parser, prompt or model change:** send every document back to that stage, then `make run` again. Glossing replies are cached by input hash, so only what changed is redone.

  ```sh
  make requeue STAGE=segment
  ```

- **Start over:** this deletes the database and the stored PDFs, and asks first.

  ```sh
  make reset
  ```

## Checks

```sh
make check
make test
```

- **`make check`:** lint, formatting and types; this is what CI runs.
- **`make test`:** the offline test suite, against a separate `cryptoindex_test` database.
- **`make smoke`** runs the whole service with the real models on a two-page PDF. Stop `make run` first; the `make` help explains why.
