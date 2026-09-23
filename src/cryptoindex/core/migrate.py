import hashlib
import logging
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from cryptoindex.core import config
from cryptoindex.core.logs import configure as configure_logging

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path("db/migrations")


class MigrationError(RuntimeError):
    pass


def migrate(admin_dsn: str, role_dsns: list[str], migrations_dir: Path) -> list[str]:
    """Apply pending migrations in filename order, one transaction each, then
    set each role's password from its DSN. Returns the filenames applied.

    Raises MigrationError if an applied migration file has since been edited,
    or a role DSN lacks a user or password.
    """
    applied_now: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS docs")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS docs.schema_migrations ("
            " filename text PRIMARY KEY,"
            " sha256 text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = dict(
            conn.execute(
                "SELECT filename, sha256 FROM docs.schema_migrations"
            ).fetchall()
        )
        for path in sorted(migrations_dir.glob("*.sql")):
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if path.name in applied:
                if applied[path.name] != digest:
                    raise MigrationError(
                        f"{path.name} changed after it was applied; "
                        "add a new migration instead"
                    )
                continue
            with conn.transaction():
                conn.execute(body)
                conn.execute(
                    "INSERT INTO docs.schema_migrations (filename, sha256)"
                    " VALUES (%s, %s)",
                    (path.name, digest),
                )
            log.info("migration_applied", extra={"file": path.name})
            applied_now.append(path.name)

        for dsn in role_dsns:
            params = conninfo_to_dict(dsn)
            user, password = params.get("user"), params.get("password")
            if not user or not password:
                raise MigrationError(f"DSN for role {user!r} has no user or password")
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(str(user)), sql.Literal(str(password))
                )
            )
    return applied_now


def main() -> None:
    configure_logging(config.settings.log_level)
    s = config.settings
    try:
        applied = migrate(s.admin_dsn, [s.ingest_dsn, s.query_dsn], MIGRATIONS_DIR)
    except MigrationError as exc:
        sys.exit(f"migrate: {exc}")
    log.info("migrate_done", extra={"applied": len(applied)})


if __name__ == "__main__":
    main()
