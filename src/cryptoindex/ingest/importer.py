import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from psycopg import errors

from cryptoindex.core.db import Pool
from cryptoindex.core.model import RevisionId

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


class RejectedUploadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImportResult:
    paper_id: uuid.UUID
    name: str
    revision_id: RevisionId
    duplicate: bool  # True: this file was already imported, as `name`


async def import_document(
    pool: Pool,
    data_dir: Path,
    name: str,
    chunks: AsyncIterator[bytes],
    max_bytes: int,
) -> ImportResult:
    """Store a PDF as a new document with revision 1 at stage `parse`, or
    return the existing document if this exact file was imported before.

    Commits before returning; the caller then signals the runner with
    `revision_id` (Invariant 2). Raises RejectedUploadError for an empty name,
    a file that is not a PDF, or one larger than `max_bytes`; nothing is kept.
    """
    name = name.strip()
    if not name:
        raise RejectedUploadError("name must not be empty")

    tmp_dir = data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, suffix=".pdf")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        sha256, size = await _receive(chunks, tmp, max_bytes)
        existing = await _find_by_hash(pool, sha256)
        if existing is not None:
            return existing
        paper_id = uuid.uuid4()
        rel_path = Path("pdfs") / str(paper_id) / f"{sha256}.pdf"
        final = data_dir / rel_path
        final.parent.mkdir(parents=True)
        tmp.replace(final)
        try:
            revision_id = await _insert(pool, paper_id, name, sha256, rel_path)
        except errors.UniqueViolation:
            # An identical upload committed between our lookup and insert.
            shutil.rmtree(final.parent)
            existing = await _find_by_hash(pool, sha256)
            if existing is None:
                raise
            return existing
        except BaseException:
            shutil.rmtree(final.parent, ignore_errors=True)
            raise
    finally:
        tmp.unlink(missing_ok=True)

    log.info(
        "document_imported work_id=%d paper_id=%s bytes=%d", revision_id, paper_id, size
    )
    return ImportResult(paper_id, name, revision_id, duplicate=False)


async def _receive(
    chunks: AsyncIterator[bytes], dest: Path, max_bytes: int
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    head = b""
    with dest.open("wb") as f:
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise RejectedUploadError(f"file is larger than {max_bytes} bytes")
            if len(head) < len(PDF_MAGIC):
                head += chunk[: len(PDF_MAGIC) - len(head)]
                if not PDF_MAGIC.startswith(head):
                    raise RejectedUploadError("file is not a PDF")
            digest.update(chunk)
            await asyncio.to_thread(f.write, chunk)
    if head != PDF_MAGIC:
        raise RejectedUploadError("file is not a PDF")
    return digest.hexdigest(), size


async def _find_by_hash(pool: Pool, sha256: str) -> ImportResult | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT p.id, p.name, r.id FROM docs.revisions r"
            " JOIN docs.papers p ON p.id = r.paper_id WHERE r.pdf_sha256 = %s",
            (sha256,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return ImportResult(row[0], row[1], RevisionId(row[2]), duplicate=True)


async def _insert(
    pool: Pool, paper_id: uuid.UUID, name: str, sha256: str, rel_path: Path
) -> RevisionId:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO docs.papers (id, name) VALUES (%s, %s)", (paper_id, name)
        )
        cur = await conn.execute(
            "INSERT INTO docs.revisions"
            " (paper_id, revision, source_kind, pdf_sha256, pdf_path)"
            " VALUES (%s, 1, 'pdf', %s, %s) RETURNING id",
            (paper_id, sha256, str(rel_path)),
        )
        row = await cur.fetchone()
    assert row is not None
    return RevisionId(row[0])
