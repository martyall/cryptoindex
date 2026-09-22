UV_RUN := uv run --env-file .env

.PHONY: db-up db-down migrate reset test run check fmt paddle-server parse-eval \
	parse-review parse-close-blind parse-report gloss-eval gloss-review gloss-report \
	search

.env:
	cp .env.example .env

db-up: .env
	docker compose up -d --wait

# Keeps the data volume; `docker compose down -v` also deletes it.
db-down:
	docker compose down

migrate: db-up
	$(UV_RUN) python -m cryptoindex.core.migrate

# Deletes the database volume and CI_DATA_DIR (uploaded PDFs), then starts over
# with an empty, migrated database. Asks first; .env is kept.
reset: .env
	$(UV_RUN) python -m cryptoindex.core.reset
	docker compose down -v
	$(MAKE) migrate

# Uses a separate cryptoindex_test database, recreated on every run.
test: db-up
	$(UV_RUN) pytest

# With CI_PARSER=paddle (the default, D21) this also starts PaddleOCR-VL's model
# server, or reuses one already listening; its log is data/logs/mlx-vlm.log.
run: migrate
	$(UV_RUN) python -m cryptoindex

# PaddleOCR-VL's model server on CI_PADDLE_VLM_URL, native because it needs the
# Mac GPU (D21), in the foreground. The parse evaluation needs it; `make run`
# starts its own when CI_PARSER=paddle. Weights download on first use.
paddle-server: .env
	$(UV_RUN) python -m cryptoindex.ingest.model_server

# Runs both real parsers on eval/parse-sample and writes the review items, page
# images and formula counts under eval/parse-sample/local/. Local only; needs
# `make paddle-server` running.
parse-eval: .env
	cd tools/katex-check && npm ci --no-audit --no-fund
	$(UV_RUN) python -m cryptoindex.evaluation.parse_eval

# The review page, one page at a time, every judgement saved to
# eval/parse-sample/scores/ as it is made: blind first, then only the
# disagreements with Claude's proposals. Binds 127.0.0.1; Ctrl-C to stop.
parse-review: .env
	$(UV_RUN) python -m cryptoindex.evaluation.review_server

# Ends the blind pass before every page is scored; `make parse-review` then
# reconciles the scored pages, and the report lists what was left unscored.
parse-close-blind: .env
	$(UV_RUN) python -m cryptoindex.evaluation.review_server close-blind

# Final scores (blind, overridden by reconciled) and formula counts, to
# eval/parse-report.md.
parse-report: .env
	$(UV_RUN) python -m cryptoindex.evaluation.parse_scores

# Phase 3 spot-check: gloss-eval glosses the parse-eval excerpts with the
# configured backend (calls cached); gloss-review serves the blind review
# page; gloss-report writes the report.
gloss-eval: .env
	$(UV_RUN) python -m cryptoindex.evaluation.gloss_eval

gloss-review: .env
	$(UV_RUN) python -m cryptoindex.evaluation.gloss_review

gloss-report: .env
	$(UV_RUN) python -m cryptoindex.evaluation.gloss_review report

# Manual QA of search over the index (D24), on the read-only role.
search: .env
	$(UV_RUN) python -m cryptoindex.evaluation.search_page

# Read-only: safe for CI and pre-commit.
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check

# `ruff check --fix` would exit non-zero on unfixable errors and skip the
# formatter; `make check` reports whatever is left.
fmt:
	uv run ruff check --fix --exit-zero .
	uv run ruff format .
