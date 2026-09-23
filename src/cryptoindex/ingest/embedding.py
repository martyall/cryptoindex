import asyncio
import logging
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from cryptoindex.core.embed import (
    ALT,
    PRIMARY,
    Embedder,
    EmbedModelMismatchError,
    VectorSet,
    check_embed_model,
)
from cryptoindex.core.latex import latex_norm
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.stages import StageContext, advance

log = logging.getLogger(__name__)

Vector = npt.NDArray[np.float32]


async def embed_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Embed the revision's paragraphs, glosses and questions that have no
    vector yet, in each configured vector set (the primary one, and the
    alternate one when there is an alternate model, D27), fill `latex_norm`
    for the paragraphs the primary set lacked, and advance the revision to
    `ready`.

    Idempotent: only rows without a vector are embedded, so re-running on
    unchanged input embeds nothing (Invariant 2). The vectors, latex_norm,
    each set's model in `docs.meta` (recorded by its first embedding), and
    the transition commit in one transaction. Embedding runs in a thread.
    Raises EmbedModelMismatchError if `docs.meta` records a different model
    for a set (Invariant 6), and TransitionConflictError if the revision is
    gone or no longer claimed.
    """
    sets = [(PRIMARY, ctx.embedder)]
    if ctx.embedder_alt is not None:
        sets.append((ALT, ctx.embedder_alt))
    pending = [await _embed(ctx, work_id, vs, embedder) for vs, embedder in sets]

    async with ctx.pool.connection() as conn, conn.transaction():
        for p in pending:
            await _record_model(conn, p.vectors, p.model)
            await _store(conn, p)
        async with conn.cursor() as cur:
            await cur.executemany(
                "UPDATE docs.paragraphs SET latex_norm = %s WHERE id = %s",
                [(latex_norm(text, kind), pid) for pid, text, kind in pending[0].latex],
            )
        await advance(conn, work_id, Stage.EMBED)
    for p in pending:
        log.info(
            "stage_done",
            extra={
                "work_id": work_id,
                "stage": "embed",
                "vectors": p.vectors.name,
                "paragraphs": len(p.paragraphs),
                "glosses": len(p.glosses),
                "questions": len(p.questions),
            },
        )


@dataclass(frozen=True, slots=True)
class _Pending:
    vectors: VectorSet
    model: str
    paragraphs: list[tuple[int, Vector]]
    glosses: list[tuple[int, Vector]]
    questions: list[tuple[int, Vector]]
    # the paragraphs this set lacked; the stage fills latex_norm from
    # PRIMARY's only
    latex: list[tuple[int, str, str | None]]


async def _embed(
    ctx: StageContext, work_id: RevisionId, vs: VectorSet, embedder: Embedder
) -> _Pending:
    """Vectors for the rows of `work_id` that `vs` lacks, computed but not yet
    stored."""
    async with ctx.pool.connection() as conn:
        await check_embed_model(conn, embedder.model, vs)
        paragraphs = await _missing(
            conn,
            sql.SQL(
                "SELECT id, text, block_kind FROM docs.paragraphs"
                " WHERE revision_id = %s AND {col} IS NULL ORDER BY position"
            ).format(col=sql.Identifier(vs.paragraph)),
            work_id,
        )
        glosses = await _missing(
            conn,
            sql.SQL(
                "SELECT id, gloss FROM docs.units"
                " WHERE revision_id = %s AND {col} IS NULL ORDER BY id"
            ).format(col=sql.Identifier(vs.gloss)),
            work_id,
        )
        questions = await _missing(
            conn,
            sql.SQL(
                "SELECT q.id, q.question FROM docs.unit_questions q"
                " JOIN docs.units u ON u.id = q.unit_id"
                " WHERE u.revision_id = %s AND q.{col} IS NULL ORDER BY q.id"
            ).format(col=sql.Identifier(vs.question)),
            work_id,
        )
    texts = [row[1] for row in paragraphs + glosses + questions]
    vectors = await asyncio.to_thread(embedder.embed_documents, texts)
    it = iter(vectors)
    return _Pending(
        vectors=vs,
        model=embedder.model,
        paragraphs=[(row[0], next(it)) for row in paragraphs],
        glosses=[(row[0], next(it)) for row in glosses],
        questions=[(row[0], next(it)) for row in questions],
        latex=[(row[0], row[1], row[2]) for row in paragraphs],
    )


async def _missing(
    conn: AsyncConnection, query: sql.Composed, work_id: RevisionId
) -> list[TupleRow]:
    cur = await conn.execute(query, (work_id,))
    return await cur.fetchall()


async def _store(conn: AsyncConnection, p: _Pending) -> None:
    vs = p.vectors
    async with conn.cursor() as cur:
        for table, column, rows in (
            ("paragraphs", vs.paragraph, p.paragraphs),
            ("units", vs.gloss, p.glosses),
            ("unit_questions", vs.question, p.questions),
        ):
            await cur.executemany(
                sql.SQL("UPDATE {table} SET {col} = %s::halfvec WHERE id = %s").format(
                    table=sql.Identifier("docs", table), col=sql.Identifier(column)
                ),
                [(vec, row_id) for row_id, vec in rows],
            )


async def _record_model(conn: AsyncConnection, vs: VectorSet, model: str) -> None:
    # Checked again inside the transaction: two revisions embedded at once
    # under different models must not both succeed.
    await conn.execute(
        "INSERT INTO docs.meta (key, value) VALUES (%s, %s)"
        " ON CONFLICT (key) DO NOTHING",
        (vs.meta_key, model),
    )
    cur = await conn.execute(
        "SELECT value FROM docs.meta WHERE key = %s FOR SHARE", (vs.meta_key,)
    )
    row = await cur.fetchone()
    if row is None or row[0] != model:
        raise EmbedModelMismatchError(
            f"the {vs.name} vectors were embedded with"
            f" {row[0] if row else None}, not {model}"
        )
