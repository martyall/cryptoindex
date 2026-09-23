import shutil
from pathlib import Path
from typing import LiteralString

import psycopg
import pytest
from psycopg import errors

from cryptoindex.core.config import Settings
from cryptoindex.core.migrate import MigrationError, migrate

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def test_query_role_reads_but_cannot_write(settings: Settings) -> None:
    with psycopg.connect(settings.query_dsn) as conn:
        conn.execute("SELECT count(*) FROM docs.papers")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("INSERT INTO docs.papers (name) VALUES ('x')")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE docs.meta SET value = 'x'",
        "DELETE FROM docs.events",
        "CREATE TABLE docs.intruder (id int)",
    ],
)
def test_query_role_cannot_modify(settings: Settings, statement: LiteralString) -> None:
    with psycopg.connect(settings.query_dsn) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)


def test_ingest_role_writes_data_but_not_schema(settings: Settings) -> None:
    with psycopg.connect(settings.ingest_dsn) as conn:
        conn.execute("INSERT INTO docs.papers (name) VALUES ('x')")
        conn.rollback()
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE docs.intruder (id int)")
        conn.rollback()
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO docs.schema_migrations (filename, sha256)"
                " VALUES ('x', 'x')"
            )


def test_migrate_is_idempotent(settings: Settings) -> None:
    assert migrate(settings.admin_dsn, [], MIGRATIONS) == []
    with psycopg.connect(settings.admin_dsn) as conn:
        rows = conn.execute(
            "SELECT filename FROM docs.schema_migrations ORDER BY 1"
        ).fetchall()
    assert rows == [(p.name,) for p in sorted(MIGRATIONS.glob("*.sql"))]


def test_file_is_unique_across_documents(settings: Settings) -> None:
    with psycopg.connect(settings.ingest_dsn) as conn:
        a, b = (
            conn.execute(
                "INSERT INTO docs.papers (name) VALUES ('same name') RETURNING id"
            ).fetchone()
            for _ in range(2)
        )
        assert a is not None and b is not None and a != b
        insert = (
            "INSERT INTO docs.revisions (paper_id, revision, source_kind, pdf_sha256)"
            " VALUES (%s, 1, 'pdf', 'abc')"
        )
        conn.execute(insert, (a[0],))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(insert, (b[0],))


def test_migrate_refuses_an_edited_migration(
    settings: Settings, tmp_path: Path
) -> None:
    for path in MIGRATIONS.glob("*.sql"):
        shutil.copy(path, tmp_path)
    edited = tmp_path / "001_core.sql"
    edited.write_text(edited.read_text() + "\n-- edited\n")
    with pytest.raises(MigrationError, match="001_core.sql changed"):
        migrate(settings.admin_dsn, [], tmp_path)
