import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.ingest.importer import (
    ImportResult,
    RejectedUploadError,
    import_document,
)

MAX = 1024


def pdf(body: str) -> bytes:
    return b"%PDF-1.7\n" + body.encode() + b"\n%%EOF\n"


async def chunked(data: bytes, size: int = 3) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def upload(pool: Pool, data_dir: Path, name: str, data: bytes) -> ImportResult:
    return await import_document(pool, data_dir, name, chunked(data), MAX)


def rows(settings: Settings) -> list[tuple]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(
            "SELECT p.name, r.revision, r.stage, r.pdf_sha256, r.pdf_path"
            " FROM docs.papers p JOIN docs.revisions r ON r.paper_id = p.id"
            " ORDER BY p.created_at, p.name"
        ).fetchall()


def stored_files(data_dir: Path) -> list[Path]:
    return sorted(p.relative_to(data_dir) for p in data_dir.rglob("*") if p.is_file())


async def test_new_document(settings: Settings, pool: Pool, tmp_path: Path) -> None:
    result = await upload(pool, tmp_path, "  Kyber spec  ", pdf("kyber"))

    assert result.name == "Kyber spec" and not result.duplicate
    [(name, revision, stage, sha, path)] = rows(settings)
    assert (name, revision, stage) == ("Kyber spec", 1, "parse")
    assert path == f"pdfs/{result.paper_id}/{sha}.pdf"
    assert (tmp_path / path).read_bytes() == pdf("kyber")
    assert stored_files(tmp_path) == [Path(path)]


async def test_same_file_twice_is_one_document(
    settings: Settings, pool: Pool, tmp_path: Path
) -> None:
    first = await upload(pool, tmp_path, "original", pdf("x"))
    second = await upload(pool, tmp_path, "renamed copy", pdf("x"))

    assert second.duplicate
    assert (second.paper_id, second.revision_id) == (first.paper_id, first.revision_id)
    assert second.name == "original"
    assert len(rows(settings)) == 1
    assert len(stored_files(tmp_path)) == 1


async def test_same_name_different_files_are_two_documents(
    settings: Settings, pool: Pool, tmp_path: Path
) -> None:
    a = await upload(pool, tmp_path, "notes", pdf("a"))
    b = await upload(pool, tmp_path, "notes", pdf("b"))

    assert a.paper_id != b.paper_id
    assert [r[0] for r in rows(settings)] == ["notes", "notes"]


async def test_concurrent_identical_uploads_make_one_document(
    settings: Settings, pool: Pool, tmp_path: Path
) -> None:
    results = await asyncio.gather(
        *(upload(pool, tmp_path, f"copy {i}", pdf("race")) for i in range(5))
    )

    assert len({r.paper_id for r in results}) == 1
    assert sum(not r.duplicate for r in results) == 1
    assert len(rows(settings)) == 1
    assert len(stored_files(tmp_path)) == 1


@pytest.mark.parametrize(
    "name, data, message",
    [
        ("doc", b"<html>not a pdf</html>", "not a PDF"),
        ("doc", b"%PD", "not a PDF"),
        ("doc", b"", "not a PDF"),
        ("doc", pdf("x" * MAX), "larger than"),
        ("   ", pdf("x"), "name must not be empty"),
    ],
)
async def test_rejected_uploads_leave_nothing(
    settings: Settings,
    pool: Pool,
    tmp_path: Path,
    name: str,
    data: bytes,
    message: str,
) -> None:
    with pytest.raises(RejectedUploadError, match=message):
        await upload(pool, tmp_path, name, data)
    assert rows(settings) == []
    assert stored_files(tmp_path) == []
