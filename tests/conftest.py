import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from cryptoindex.core.config import Settings, load_settings
from cryptoindex.core.db import Pool, open_pool
from cryptoindex.core.migrate import migrate
from cryptoindex.core.model import Stage
from cryptoindex.ingest.runner import pool_size

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
TEST_DB = "cryptoindex_test"
DSN_VARS = ("CI_ADMIN_DSN", "CI_INGEST_DSN", "CI_QUERY_DSN")


@pytest.fixture(scope="session")
def test_env() -> dict[str, str]:
    """The process environment with every DSN pointed at the test database."""
    if "CI_ADMIN_DSN" not in os.environ:
        pytest.exit("database tests need the .env settings; run them with `make test`")
    env = dict(os.environ)
    for var in DSN_VARS:
        env[var] = make_conninfo(env[var], dbname=TEST_DB)
    return env


@pytest.fixture(scope="session")
def database(test_env: dict[str, str]) -> Settings:
    """A freshly created and migrated test database, once per session."""
    with psycopg.connect(os.environ["CI_ADMIN_DSN"], autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS cryptoindex_test WITH (FORCE)")
        conn.execute("CREATE DATABASE cryptoindex_test")
    settings = load_settings(test_env)
    migrate(settings.admin_dsn, [settings.ingest_dsn, settings.query_dsn], MIGRATIONS)
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        # Test-only record of every committed stage execution; see runner_child.py.
        conn.execute(
            "CREATE SCHEMA test_audit;"
            " CREATE TABLE test_audit.stage_runs ("
            "   revision_id bigint NOT NULL, stage text NOT NULL);"
            " GRANT USAGE ON SCHEMA test_audit TO ci_ingest;"
            " GRANT SELECT, INSERT ON test_audit.stage_runs TO ci_ingest;"
        )
    return settings


@pytest.fixture
def settings(database: Settings) -> Iterator[Settings]:
    """The test database with pipeline and audit tables emptied, and no
    embedding model recorded, before each test."""
    with psycopg.connect(database.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE docs.events, docs.unit_questions, docs.units,"
            " docs.segment_chunks, docs.paragraphs, docs.revisions, docs.papers,"
            " test_audit.stage_runs;"
            " DELETE FROM docs.meta WHERE key LIKE 'embed_model%'"
        )
    yield database


@pytest.fixture
async def pool(settings: Settings) -> AsyncIterator[Pool]:
    p = await open_pool(settings.ingest_dsn, pool_size(settings))
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def query_pool(settings: Settings) -> AsyncIterator[Pool]:
    """The read-only ci_query role, as search uses it (Invariant 7)."""
    p = await open_pool(settings.query_dsn, 2)
    try:
        yield p
    finally:
        await p.close()


SeedFn = Callable[[list[Stage]], list[int]]


@pytest.fixture
def seed(settings: Settings) -> SeedFn:
    """Insert revisions at the given stages, two per paper (revision 1 and 2),
    and return their IDs in the same order."""

    def insert(stages: list[Stage]) -> list[int]:
        ids: list[int] = []
        with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
            for i, stage in enumerate(stages):
                if i % 2 == 0:
                    paper = conn.execute(
                        "INSERT INTO docs.papers (name) VALUES (%s) RETURNING id",
                        (f"Paper {i // 2}",),
                    ).fetchone()
                    assert paper is not None
                    paper_id = paper[0]
                row = conn.execute(
                    "INSERT INTO docs.revisions"
                    " (paper_id, revision, source_kind, pdf_sha256, stage)"
                    " VALUES (%s, %s, 'pdf', %s, %s) RETURNING id",
                    (paper_id, i % 2 + 1, f"sha-{i}", stage.value),
                ).fetchone()
                assert row is not None
                ids.append(row[0])
        return ids

    return insert
