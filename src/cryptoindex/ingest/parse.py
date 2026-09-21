import asyncio
import logging
from pathlib import Path

from psycopg import AsyncConnection

from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.paragraphs import Paragraph, paragraphs_from
from cryptoindex.ingest.stages import StageContext, TransitionConflictError, advance

log = logging.getLogger(__name__)


async def parse_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Parse the revision's PDF into paragraphs and advance it to `segment`.

    Idempotent: the parser's raw output is cached per parser version and
    reused, so a retry never re-runs the parser; paragraphs are matched by
    position and content hash, so unchanged ones keep their IDs (Invariant 5).
    Paragraphs, parser identity, an empty title, and the transition commit
    together. The parser is blocking and runs in a thread (it is itself
    out-of-process, D7).
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

    raw = await _raw_output(ctx, ctx.settings.data_dir / pdf_path, sha256)
    document = ctx.parser.read(raw)
    paragraphs = paragraphs_from(document)

    async with ctx.pool.connection() as conn, conn.transaction():
        await _store_paragraphs(conn, work_id, paragraphs)
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
        "stage_done work_id=%d stage=parse paragraphs=%d pages=%d parser=%s",
        work_id,
        len(paragraphs),
        document.page_count,
        ctx.parser.name,
    )


async def _raw_output(ctx: StageContext, pdf: Path, sha256: str) -> bytes:
    parser = ctx.parser
    cached = (
        ctx.settings.data_dir / "parsed" / f"{parser.name}-{parser.version}"
    ) / f"{sha256}.json"
    if cached.exists():
        return await asyncio.to_thread(cached.read_bytes)
    raw = await asyncio.to_thread(parser.run, pdf)
    parser.read(raw)  # never cache output the reader rejects
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(cached.name + ".partial")
    await asyncio.to_thread(partial.write_bytes, raw)
    partial.replace(cached)
    return raw


async def _store_paragraphs(
    conn: AsyncConnection, work_id: RevisionId, paragraphs: list[Paragraph]
) -> None:
    # Invariant 5: a paragraph whose (position, content_hash) is unchanged is
    # updated in place and keeps its ID (and embedding); any other row at that
    # position is replaced, and rows past the new end are removed.
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
                    p.section_path,
                    p.text,
                    p.content_hash,
                    p.block_kind,
                )
                for p in paragraphs
            ],
        )
