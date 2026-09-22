import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from cryptoindex.core.events import EventKind, write_event
from cryptoindex.core.llm import BatchLLM, Completion, LLMRequest
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.core.prompts import load_prompt
from cryptoindex.ingest.gloss import (
    GLOSS_PROMPT,
    SegmentReply,
    SourceParagraph,
    UnitReply,
    chunks_of,
    gloss_request,
    input_hash,
    merge,
    quality_flags,
    validate_reply,
)
from cryptoindex.ingest.stages import StageContext, TransitionConflictError, advance

log = logging.getLogger(__name__)


async def segment_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Group the revision's paragraphs into glossed argument units and advance
    it to `embed`.

    Idempotent: each chunk's validated reply is stored under its input hash
    (docs.segment_chunks) as soon as it arrives, before the stage's own
    transaction, so unchanged input, or a retry after a later call failed,
    makes no call for that chunk. With a BatchLLM and CI_GLOSS_BATCH, two or
    more missing chunks go as one batch (D5), which arrives, or fails, whole.
    Units are matched to existing ones by span and anchor (Invariant 5);
    units that no longer exist are deleted with a `unit_changed` event each.
    Units, questions, paragraph block labels (from the units' anchors), the
    removal of replies for chunks that no longer exist, and the transition
    commit in one transaction. Raises the backend's errors, pydantic's
    ValidationError or InvalidReplyError for a reply that does not fit its
    chunk (after storing the replies that did), and TransitionConflictError
    if the revision is gone or no longer claimed.
    """
    title, paragraphs = await _load(ctx, work_id)
    prompt = load_prompt(GLOSS_PROMPT)
    chunks = chunks_of(paragraphs)
    hashes = [input_hash(c, title, prompt.version, ctx.llm.model) for c in chunks]

    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT input_hash, gloss_model, response FROM docs.segment_chunks"
            " WHERE revision_id = %s AND input_hash = ANY(%s)",
            (work_id, hashes),
        )
        stored = {h: (model, response) for h, model, response in await cur.fetchall()}

    replies: dict[int, tuple[str, Sequence[UnitReply]]] = {
        i: (stored[h][0], validate_reply(stored[h][1], chunks[i]))
        for i, h in enumerate(hashes)
        if h in stored
    }
    missing = [i for i in range(len(chunks)) if i not in replies]
    if missing:
        log.info(
            "segment_calls work_id=%d chunks=%d missing=%d backend=%s",
            work_id,
            len(chunks),
            len(missing),
            ctx.llm.name,
        )
    requests = {i: gloss_request(chunks[i], title, prompt) for i in missing}
    failure: Exception | None = None
    async for i, completion in _completions(ctx, requests):
        try:
            units = validate_reply(completion.parsed, chunks[i])
        except ValueError as e:  # includes pydantic's ValidationError
            failure = failure or e
            continue
        replies[i] = (completion.model, units)
        await _store_reply(ctx, work_id, hashes[i], prompt.version, completion, units)
    if failure is not None:
        raise failure

    text = {p.position: p.text for p in paragraphs}
    model_of = {u.key: replies[i][0] for i in replies for u in replies[i][1]}
    units = merge([(chunks[i], replies[i][1]) for i in range(len(chunks))])
    chunk_of = {u.key: hashes[i] for i in replies for u in replies[i][1]}

    async with ctx.pool.connection() as conn, conn.transaction():
        await _store_units(
            conn,
            work_id,
            [
                _Row(u, model_of[u.key], chunk_of[u.key], quality_flags(u, text))
                for u in units
            ],
            prompt.version,
        )
        await conn.execute(
            "DELETE FROM docs.segment_chunks"
            " WHERE revision_id = %s AND NOT input_hash = ANY(%s)",
            (work_id, hashes),
        )
        await advance(conn, work_id, Stage.SEGMENT)
    log.info(
        "stage_done work_id=%d stage=segment chunks=%d calls=%d units=%d flagged=%d",
        work_id,
        len(chunks),
        len(missing),
        len(units),
        sum(1 for u in units if quality_flags(u, text)),
    )


async def _load(
    ctx: StageContext, work_id: RevisionId
) -> tuple[str, list[SourceParagraph]]:
    async with ctx.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT p.name FROM docs.revisions r"
            " JOIN docs.papers p ON p.id = r.paper_id WHERE r.id = %s",
            (work_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise TransitionConflictError(f"revision {work_id} disappeared")
        cur = await conn.execute(
            "SELECT position, section_path, text, content_hash, block_kind"
            " FROM docs.paragraphs WHERE revision_id = %s ORDER BY position",
            (work_id,),
        )
        paragraphs = [
            SourceParagraph(pos, tuple(path), text, content_hash, kind)
            for pos, path, text, content_hash, kind in await cur.fetchall()
        ]
    return row[0], paragraphs


async def _completions(
    ctx: StageContext, requests: Mapping[int, LLMRequest]
) -> AsyncIterator[tuple[int, Completion]]:
    """Each chunk's completion, by chunk index, as it arrives."""
    llm = ctx.llm
    if len(requests) > 1 and ctx.settings.gloss_batch and isinstance(llm, BatchLLM):
        completions = await llm.batch(list(requests.values()))
        for i, completion in zip(requests, completions, strict=True):
            yield i, completion
        return
    for i, r in requests.items():
        yield (
            i,
            await llm.complete(
                r.system,
                r.messages,
                json_schema=r.json_schema,
                cache_prefix=r.cache_prefix,
            ),
        )


async def _store_reply(
    ctx: StageContext,
    work_id: RevisionId,
    hash_: str,
    prompt_version: str,
    completion: Completion,
    units: Sequence[UnitReply],
) -> None:
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO docs.segment_chunks"
            " (revision_id, input_hash, gloss_model, prompt_version, response)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                work_id,
                hash_,
                completion.model,
                prompt_version,
                Jsonb(SegmentReply(units=tuple(units)).model_dump(mode="json")),
            ),
        )


@dataclass(frozen=True, slots=True)
class _Row:
    unit: UnitReply
    model: str
    input_hash: str
    flags: list[str]


async def _store_units(
    conn: AsyncConnection, work_id: RevisionId, rows: list[_Row], prompt_version: str
) -> None:
    cur = await conn.execute(
        "SELECT u.id, u.first_pos, u.last_pos, u.anchor_label,"
        "  coalesce(array_agg(q.question ORDER BY q.id)"
        "           FILTER (WHERE q.id IS NOT NULL), '{}')"
        " FROM docs.units u LEFT JOIN docs.unit_questions q ON q.unit_id = u.id"
        " WHERE u.revision_id = %s GROUP BY u.id",
        (work_id,),
    )
    existing = {
        (first, last, anchor): (unit_id, tuple(questions))
        for unit_id, first, last, anchor, questions in await cur.fetchall()
    }
    wanted = {r.unit.key for r in rows}
    for key, (unit_id, _) in existing.items():
        if key not in wanted:
            await conn.execute("DELETE FROM docs.units WHERE id = %s", (unit_id,))
            await write_event(
                conn,
                EventKind.UNIT_CHANGED,
                {
                    "unit_id": unit_id,
                    "revision_id": work_id,
                    "first_pos": key[0],
                    "last_pos": key[1],
                    "anchor_label": key[2],
                    "change": "deleted",
                },
            )
    for r in rows:
        u = r.unit
        values = (
            u.anchor_pos,
            u.anchor_kind,
            u.gloss,
            list(u.key_terms),
            r.flags,
            r.model,
            prompt_version,
            r.input_hash,
        )
        if u.key in existing:
            unit_id, questions = existing[u.key]
            # Phase 4 re-embeds a changed gloss; `gloss` in the CASE is the
            # value before this update.
            await conn.execute(
                "UPDATE docs.units SET anchor_pos = %s, anchor_kind = %s,"
                "  gloss = %s, terms = %s,"
                "  flags = %s, gloss_model = %s, prompt_version = %s,"
                "  input_hash = %s,"
                "  emb_gloss = CASE WHEN gloss = %s THEN emb_gloss END"
                " WHERE id = %s",
                (*values, u.gloss, unit_id),
            )
            if questions == u.questions:
                continue
            await conn.execute(
                "DELETE FROM docs.unit_questions WHERE unit_id = %s", (unit_id,)
            )
        else:
            cur = await conn.execute(
                "INSERT INTO docs.units (revision_id, first_pos, last_pos,"
                "  anchor_label, anchor_pos, anchor_kind, gloss, terms, flags,"
                "  gloss_model, prompt_version, input_hash)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " RETURNING id",
                (work_id, u.first_pos, u.last_pos, u.anchor_label, *values),
            )
            inserted = await cur.fetchone()
            assert inserted is not None
            unit_id = inserted[0]
        async with conn.cursor() as qcur:
            await qcur.executemany(
                "INSERT INTO docs.unit_questions (unit_id, question) VALUES (%s, %s)",
                [(unit_id, q) for q in u.questions],
            )
    await conn.execute(
        "UPDATE docs.paragraphs SET block_label = NULL"
        " WHERE revision_id = %s AND block_label IS NOT NULL",
        (work_id,),
    )
    async with conn.cursor() as lcur:
        await lcur.executemany(
            "UPDATE docs.paragraphs SET block_label = %s"
            " WHERE revision_id = %s AND position = %s",
            [
                (r.unit.anchor_label, work_id, r.unit.anchor_pos)
                for r in rows
                if r.unit.anchor_pos is not None
            ],
        )
