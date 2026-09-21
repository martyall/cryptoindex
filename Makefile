UV_RUN := uv run --env-file .env

.PHONY: db-up db-down migrate reset test run check fmt

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
	@dir=$$(sed -n 's/^CI_DATA_DIR=//p' .env); \
	case "$$dir" in ""|/|/*/..|..|../*) echo "refusing: CI_DATA_DIR='$$dir'"; exit 1;; esac; \
	printf "Delete the database and everything in '%s'? [y/N] " "$$dir"; read ans; \
	[ "$$ans" = y ] || { echo aborted; exit 1; }; \
	docker compose down -v && rm -rf "$$dir"
	$(MAKE) migrate

# Uses a separate cryptoindex_test database, recreated on every run.
test: db-up
	$(UV_RUN) pytest

run: migrate
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
