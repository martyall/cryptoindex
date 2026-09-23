import asyncio
import logging
from pathlib import Path

from psycopg import AsyncConnection

from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.document import ParsedDocument
from cryptoindex.ingest.paragraphs import Paragraph, paragraphs_from
from cryptoindex.ingest.parsers import ParserError
from cryptoindex.ingest.stages import StageContext, TransitionConflictError, advance

log = logging.getLogger(__name__)


async def parse_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Parse the revision's PDF into paragraphs and advance it to `segment`.

    Idempotent. The parser's raw output is cached per parser name and version
    once its reader accepts it, so after one successful run a retry reads the
    cache instead of re-running the parser. Paragraphs are matched by position
    and content hash, so unchanged ones keep their IDs (Invariant 5).
    Paragraphs, the similarity warning, parser name and version, the paper's
    title (only if unset), and the transition commit in one transaction. The
    parser and its reader run in a thread (the parser is itself
    out-of-process, D7). Raises ParserError if the revision has no stored PDF
    or the parser fails, pydantic's
    ValidationError or UnknownBlockError if its output is rejected, and
    TransitionConflictError if the revision is gone or no longer claimed.
    """
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT pdf_path, pdf_sha256, paper_id FROM docs.revisions WHERE id = %s",
            (work_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise TransitionConflictError(f"revision {work_id} disappeared")
    pdf_path, sha256, paper_id = row
    if pdf_path is None:
        raise ParserError(f"revision {work_id} has no stored PDF")

    document = await _parsed(ctx, ctx.settings.data_dir / pdf_path, sha256)
    paragraphs = paragraphs_from(document)

    async with ctx.pool.connection() as conn, conn.transaction():
        await _store_paragraphs(conn, work_id, paragraphs)
        await _record_similarity(conn, work_id)
        await conn.execute(
            "UPDATE docs.revisions SET parser = %s, parser_version = %s WHERE id = %s",
            (ctx.parser.name, ctx.parser.version, work_id),
        )
        if document.title:
            await conn.execute(
                "UPDATE docs.papers SET title = %s WHERE id = %s AND title IS NULL",
                (document.title, paper_id),
            )
        await advance(conn, work_id, Stage.PARSE)
    log.info(
        "stage_done",
        extra={
            "work_id": work_id,
            "stage": "parse",
            "paragraphs": len(paragraphs),
            "pages": document.page_count,
            "parser": ctx.parser.name,
        },
    )


async def _parsed(ctx: StageContext, pdf: Path, sha256: str) -> ParsedDocument:
    parser = ctx.parser
    cached = (
        ctx.settings.data_dir / "parsed" / f"{parser.name}-{parser.version}"
    ) / f"{sha256}.json"
    if cached.exists():
        raw = await asyncio.to_thread(cached.read_bytes)
        return await asyncio.to_thread(parser.read, raw)
    raw = await asyncio.to_thread(parser.run, pdf)
    document = await asyncio.to_thread(parser.read, raw)  # raises before caching
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(cached.name + ".partial")
    await asyncio.to_thread(partial.write_bytes, raw)
    partial.replace(cached)
    return document


# Paragraphs shorter than this ("Proof.", "Exercises", a lone equation) are
# shared by unrelated documents and would make them look alike.
SIMILARITY_MIN_CHARS = 80


async def _record_similarity(conn: AsyncConnection, work_id: RevisionId) -> None:
    """Record on the revision the other document sharing the largest fraction
    of its paragraphs of at least SIMILARITY_MIN_CHARS (by content hash), or
    NULL if none shares any."""
    await conn.execute(
        "WITH mine AS ("
        "   SELECT DISTINCT content_hash FROM docs.paragraphs"
        "   WHERE revision_id = %(id)s AND length(text) >= %(min)s),"
        " best AS ("
        "   SELECT r.paper_id, count(DISTINCT p.content_hash) AS shared"
        "   FROM docs.paragraphs p JOIN docs.revisions r ON r.id = p.revision_id"
        "   WHERE p.content_hash IN (SELECT content_hash FROM mine)"
        "     AND r.paper_id <> (SELECT paper_id FROM docs.revisions WHERE id = %(id)s)"
        "   GROUP BY r.paper_id ORDER BY shared DESC, r.paper_id LIMIT 1)"
        " UPDATE docs.revisions SET"
        "   similar_paper_id = (SELECT paper_id FROM best),"
        "   similarity = (SELECT shared::real / (SELECT count(*) FROM mine) FROM best)"
        " WHERE id = %(id)s",
        {"id": work_id, "min": SIMILARITY_MIN_CHARS},
    )


async def _store_paragraphs(
    conn: AsyncConnection, work_id: RevisionId, paragraphs: list[Paragraph]
) -> None:
    # Invariant 5: a paragraph whose (position, content_hash) is unchanged is
    # updated in place and keeps its ID, so anything keyed by it survives; any
    # other row at that position is replaced, and rows past the new end are
    # removed.
    cur = await conn.execute(
        "SELECT position, content_hash FROM docs.paragraphs WHERE revision_id = %s",
        (work_id,),
    )
    existing = dict(await cur.fetchall())
    keep = {
        p.position for p in paragraphs if existing.get(p.position) == p.content_hash
    }
    drop = [pos for pos in existing if pos not in keep]
    if drop:
        await conn.execute(
            "DELETE FROM docs.paragraphs WHERE revision_id = %s AND position = ANY(%s)",
            (work_id, drop),
        )
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO docs.paragraphs (revision_id, position, page, bbox,"
            "  section_path, text, content_hash, block_kind)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (revision_id, position) DO UPDATE SET"
            "  page = EXCLUDED.page, bbox = EXCLUDED.bbox,"
            "  section_path = EXCLUDED.section_path, text = EXCLUDED.text,"
            "  block_kind = EXCLUDED.block_kind",
            [
                (
                    work_id,
                    p.position,
                    p.page,
                    list(p.bbox),
                    list(p.section_path),
                    p.text,
                    p.content_hash,
                    p.block_kind,
                )
                for p in paragraphs
            ],
        )
