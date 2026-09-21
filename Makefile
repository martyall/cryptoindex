UV_RUN := uv run --env-file .env

.PHONY: db-up db-down migrate test run check fmt

.env:
	cp .env.example .env

db-up: .env
	docker compose up -d --wait

# Keeps the data volume; `docker compose down -v` also deletes it.
db-down:
	docker compose down

migrate: .env
	$(UV_RUN) python -m cryptoindex.core.migrate

# Uses a separate cryptoindex_test database, recreated on every run.
test: db-up
	$(UV_RUN) pytest

run: db-up
	$(UV_RUN) python -m cryptoindex

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
