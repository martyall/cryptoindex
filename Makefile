UV_RUN := uv run --env-file .env

.PHONY: help db-up db-down migrate reset requeue fetch-models test smoke run check fmt \
	paddle-server \
	parse-eval parse-review parse-close-blind parse-report gloss-eval gloss-review \
	gloss-report

.DEFAULT_GOAL := help

# The menu is this file: a target's `##` annotation is its line, a `##` line
# indented with spaces (never a tab, which would make it part of the recipe)
# adds a variable or an example, and `## #` starts a section. Nothing else is
# read, so a target documents itself where it is defined.
help:
	@awk 'BEGIN { FS = ":.*## " } \
	  /^## #/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 6); next } \
	  /^[a-z][a-z-]*:.*## / { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2; next } \
	  /^ +## / { sub(/^ +## /, ""); printf "  %-18s   %s\n", "", $$0 }' $(MAKEFILE_LIST)
	@echo

.env:
	cp .env.example .env

## # Database

db-up: .env ## Start Postgres (docker compose) and wait until it is healthy
	docker compose up -d --wait

db-down: ## Stop Postgres, keeping its data volume
	docker compose down

migrate: db-up ## Apply migrations and set the role passwords from the DSNs
	$(UV_RUN) python -m cryptoindex.core.migrate

reset: .env ## Delete the database volume and CI_DATA_DIR, then migrate afresh
    ## Asks first. Uploaded PDFs go with it; .env is kept.
	$(UV_RUN) python -m cryptoindex.core.reset
	docker compose down -v
	$(MAKE) migrate

## # Pipeline

run: migrate ## Start the pipeline and the API on CI_API_PORT
    ## Upload page at /, search page at /search/ (D24), Ask page at /ask/ (D28),
    ## one process. docs/RUNNING.md: first start, ingesting the corpus.
    ## Embedding models come from .env: CI_EMBED_MODEL (primary), optional
    ## CI_EMBED_MODEL_ALT (D27), and CI_SEARCH_VECTORS=primary|alt, which
    ## picks the one searches use for the whole run: change it and restart.
    ## Optional CI_RERANK_MODEL (D32) reorders search results after fusion;
    ## empty for none. A new model: make fetch-models, then restart.
    ## With CI_PARSER=paddle (D21) this also starts PaddleOCR-VL's model
    ## server, or reuses one already listening: data/logs/mlx-vlm.log.
	$(UV_RUN) python -m cryptoindex

requeue: .env ## Send documents back to STAGE so the pipeline redoes it
    ## STAGE=parse|segment|embed, after a parser, prompt or model change.
    ## DOCS="<id> <id>"; every document when empty. Stop `make run` first.
    ##   make requeue STAGE=segment
    ##   make requeue STAGE=embed DOCS=8b009488-fd4d-4c76-8db7-eb913057b957
    ## A new CI_EMBED_MODEL(_ALT): make fetch-models, stop make run, then
    ## make requeue STAGE=embed; re-embedding redoes no parsing or glossing.
    ## Both vector sets are cleared and refilled.
	$(UV_RUN) python -m cryptoindex.ingest.requeue $(STAGE) $(DOCS)

fetch-models: .env ## Download the pinned weights of the embedding models and reranker .env names
    ## Once, into the Hugging Face cache; nothing is fetched at run time.
    ## Resumes an interrupted download.
	$(UV_RUN) python -m cryptoindex.core.fetch_models

paddle-server: .env ## Run PaddleOCR-VL's model server on its own
    ## Native, because it needs the Mac GPU (D21), in the foreground. The
    ## parser evaluation needs it; `make run` starts its own. Weights
    ## download on first use.
	$(UV_RUN) python -m cryptoindex.ingest.model_server

## # Checks

test: db-up ## Run the offline test suite against cryptoindex_test
	$(UV_RUN) pytest

smoke: db-up ## Run the service end to end in the modes we use (real models)
    ## Uploads 2 pages of PDF to a fresh cryptoindex_test on port 8001,
    ## waits for ready and searches, then again on the alternate vectors
    ## when CI_EMBED_MODEL_ALT is set (D27). Local only;
    ## your index is untouched. Stop `make run` first: the smoke service
    ## loads its own models, and two copies do not fit in memory. Not
    ## alongside `make test` either: both use cryptoindex_test.
    ##   make smoke PDF=path/to/some.pdf
	$(UV_RUN) python -m cryptoindex.evaluation.smoke $(PDF)

check: ## Lint, formatting and types, read-only: what CI runs
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check

fmt: ## Apply ruff's fixes and formatting
    ## `ruff check --fix` alone would exit non-zero on unfixable errors and
    ## skip the formatter; `make check` reports whatever is left.
	uv run ruff check --fix --exit-zero .
	uv run ruff format .

## # Evaluation (local only; these call real models)

parse-eval: .env ## Parse eval/parse-sample with both parsers (needs paddle-server)
    ## Writes review items, page images and formula counts under
    ## eval/parse-sample/local/.
	cd tools/katex-check && npm ci --no-audit --no-fund
	$(UV_RUN) python -m cryptoindex.evaluation.parse_eval

parse-review: .env ## Serve the parser review page on 127.0.0.1:8009
    ## One page at a time, every judgement saved to
    ## eval/parse-sample/scores/ as it is made: blind first, then only the
    ## disagreements with Claude's proposals.
	$(UV_RUN) python -m cryptoindex.evaluation.review_server

parse-close-blind: .env ## End the parser blind pass before every page is scored
	$(UV_RUN) python -m cryptoindex.evaluation.review_server close-blind

parse-report: .env ## Write eval/parse-report.md from the parser scores
	$(UV_RUN) python -m cryptoindex.evaluation.parse_scores

gloss-eval: .env ## Gloss the parse-eval excerpts and pick the spot-check sample
    ## Calls the configured LLM backend; replies are cached by input hash.
	$(UV_RUN) python -m cryptoindex.evaluation.gloss_eval

gloss-review: .env ## Serve the gloss review page on 127.0.0.1:8010
	$(UV_RUN) python -m cryptoindex.evaluation.gloss_review

gloss-report: .env ## Write eval/gloss-report-<prompt>.md from the gloss scores
	$(UV_RUN) python -m cryptoindex.evaluation.gloss_review report
