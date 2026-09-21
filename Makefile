UV_RUN := uv run --env-file .env

.PHONY: db-up db-down migrate reset test run check fmt paddle-server parse-eval \
	parse-review parse-reconcile parse-report

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

run: migrate
	$(UV_RUN) python -m cryptoindex

# PaddleOCR-VL's model server, native because it needs the Mac GPU (Phase 2
# decision). Runs in the foreground; the parse evaluation and CI_PARSER=paddle
# need it. Model weights download on first use.
paddle-server:
	uvx --python 3.12 --from mlx-vlm==0.7.2 mlx_vlm.server --host 127.0.0.1 --port 8111

# Runs both real parsers on eval/parse-sample and writes a review page under
# eval/parse-sample/local/. Local only; needs `make paddle-server` running.
parse-eval: .env
	cd tools/katex-check && npm ci --no-audit --no-fund
	$(UV_RUN) python -m cryptoindex.evaluation.parse_eval

# Serves the review page on this machine only (Ctrl-C to stop).
parse-review:
	@echo "review page: http://127.0.0.1:8009/review.html"
	uv run python -m http.server 8009 --bind 127.0.0.1 --directory eval/parse-sample/local

# After the blind pass: compare your exported scores with Claude's proposals
# and rewrite the review page with only the disagreements.
parse-reconcile: .env
	$(UV_RUN) python -m cryptoindex.evaluation.parse_scores reconcile "$(SCORES)"

# Final scores (blind, overridden by reconciled) and formula counts, to
# eval/parse-report.md. RECONCILED is optional if nothing needed reconciling.
parse-report: .env
	$(UV_RUN) python -m cryptoindex.evaluation.parse_scores report "$(RECONCILED)"

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
