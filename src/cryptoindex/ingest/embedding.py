import asyncio
import logging

from psycopg import AsyncConnection

from cryptoindex.core.embed import EmbedModelMismatchError, check_embed_model
from cryptoindex.core.latex import latex_norm
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.stages import StageContext, advance

log = logging.getLogger(__name__)


async def embed_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Embed the revision's paragraphs, glosses and questions that have no
    vector yet, fill `latex_norm` for those paragraphs, and advance it to
    `ready`.

    Idempotent: only rows without a vector are embedded, so re-running on
    unchanged input embeds nothing (Invariant 2). The vectors, latex_norm,
    the index's embedding model in `docs.meta` (recorded by the first
    embedding), and the transition commit in one transaction. Embedding runs
    in a thread. Raises EmbedModelMismatchError if `docs.meta` records a
    different model (Invariant 6), and TransitionConflictError if the
    revision is gone or no longer claimed.
    """
    embedder = ctx.embedder
    async with ctx.pool.connection() as conn:
        await check_embed_model(conn, embedder.model)
        paragraphs = await (
            await conn.execute(
                "SELECT id, text, block_kind FROM docs.paragraphs"
                " WHERE revision_id = %s AND emb IS NULL ORDER BY position",
                (work_id,),
            )
        ).fetchall()
        glosses = await (
            await conn.execute(
                "SELECT id, gloss FROM docs.units"
                " WHERE revision_id = %s AND emb_gloss IS NULL ORDER BY id",
                (work_id,),
            )
        ).fetchall()
        questions = await (
            await conn.execute(
                "SELECT q.id, q.question FROM docs.unit_questions q"
                " JOIN docs.units u ON u.id = q.unit_id"
                " WHERE u.revision_id = %s AND q.emb IS NULL ORDER BY q.id",
                (work_id,),
            )
        ).fetchall()

    texts = [t for _, t, _ in paragraphs] + [t for _, t in glosses]
    texts += [t for _, t in questions]
    vectors = await asyncio.to_thread(embedder.embed_documents, texts)
    para_vecs = vectors[: len(paragraphs)]
    gloss_vecs = vectors[len(paragraphs) : len(paragraphs) + len(glosses)]
    question_vecs = vectors[len(paragraphs) + len(glosses) :]

    async with ctx.pool.connection() as conn, conn.transaction():
        await _record_model(conn, embedder.model)
        async with conn.cursor() as cur:
            await cur.executemany(
                "UPDATE docs.paragraphs SET emb = %s::halfvec, latex_norm = %s"
                " WHERE id = %s",
                [
                    (vec, latex_norm(text, kind), pid)
                    for (pid, text, kind), vec in zip(
                        paragraphs, para_vecs, strict=True
                    )
                ],
            )
            await cur.executemany(
                "UPDATE docs.units SET emb_gloss = %s::halfvec WHERE id = %s",
                [(vec, uid) for (uid, _), vec in zip(glosses, gloss_vecs, strict=True)],
            )
            await cur.executemany(
                "UPDATE docs.unit_questions SET emb = %s::halfvec WHERE id = %s",
                [
                    (vec, qid)
                    for (qid, _), vec in zip(questions, question_vecs, strict=True)
                ],
            )
        await advance(conn, work_id, Stage.EMBED)
    log.info(
        "stage_done work_id=%d stage=embed paragraphs=%d glosses=%d questions=%d",
        work_id,
        len(paragraphs),
        len(glosses),
        len(questions),
    )


async def _record_model(conn: AsyncConnection, model: str) -> None:
    # Checked again inside the transaction: two revisions embedded at once
    # under different models must not both succeed.
    await conn.execute(
        "INSERT INTO docs.meta (key, value) VALUES ('embed_model', %s)"
        " ON CONFLICT (key) DO NOTHING",
        (model,),
    )
    cur = await conn.execute(
        "SELECT value FROM docs.meta WHERE key = 'embed_model' FOR SHARE"
    )
    row = await cur.fetchone()
    if row is None or row[0] != model:
        raise EmbedModelMismatchError(
            f"the index was embedded with {row[0] if row else None}, not {model}"
        )
