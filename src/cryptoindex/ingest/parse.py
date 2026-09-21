import asyncio
import logging
import re
from pathlib import Path

from psycopg import AsyncConnection

from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.paragraphs import Paragraph, split_paragraphs
from cryptoindex.ingest.stages import StageContext, TransitionConflictError, advance

log = logging.getLogger(__name__)

_FIRST_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


async def parse_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Parse the revision's PDF into paragraphs and advance it to `segment`.

    Idempotent: the parser's Markdown is cached per parser version and reused,
    so a retry never re-parses; paragraphs are matched by position and
    content hash, so unchanged ones keep their IDs (Invariant 5). Paragraphs,
    parser identity, an empty title, and the transition commit together.
    The parser is blocking and runs in a thread (it is itself out-of-process,
    D7).
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

    markdown = await _markdown(ctx, ctx.settings.data_dir / pdf_path, sha256)
    paragraphs = split_paragraphs(markdown)
    heading = _FIRST_HEADING.search(markdown)

    async with ctx.pool.connection() as conn, conn.transaction():
        await _store_paragraphs(conn, work_id, paragraphs)
        await conn.execute(
            "UPDATE docs.revisions SET parser = %s, parser_version = %s WHERE id = %s",
            (ctx.parser.name, ctx.parser.version, work_id),
        )
        if heading:
            await conn.execute(
                "UPDATE docs.papers SET title = %s WHERE id = %s AND title IS NULL",
                (heading.group(1).strip("*_ "), paper_id),
            )
        await advance(conn, work_id, Stage.PARSE)
    log.info(
        "stage_done work_id=%d stage=parse paragraphs=%d parser=%s",
        work_id,
        len(paragraphs),
        ctx.parser.name,
    )


async def _markdown(ctx: StageContext, pdf: Path, sha256: str) -> str:
    parser = ctx.parser
    cached = (
        ctx.settings.data_dir / "parsed" / f"{parser.name}-{parser.version}"
    ) / f"{sha256}.md"
    if cached.exists():
        return await asyncio.to_thread(cached.read_text)
    markdown = await asyncio.to_thread(parser.parse, pdf)
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_suffix(".md.partial")
    await asyncio.to_thread(partial.write_text, markdown)
    partial.replace(cached)
    return markdown


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
            "INSERT INTO docs.paragraphs (revision_id, position, section_path, text,"
            "  content_hash, block_kind, block_label, latex_norm)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (revision_id, position) DO UPDATE SET"
            "  section_path = EXCLUDED.section_path, text = EXCLUDED.text,"
            "  block_kind = EXCLUDED.block_kind, block_label = EXCLUDED.block_label,"
            "  latex_norm = EXCLUDED.latex_norm",
            [
                (
                    work_id,
                    p.position,
                    p.section_path,
                    p.text,
                    p.content_hash,
                    p.block_kind,
                    p.block_label,
                    p.latex_norm,
                )
                for p in paragraphs
            ],
        )
