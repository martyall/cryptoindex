import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from psycopg import AsyncConnection

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.events import EventKind, write_event
from cryptoindex.core.model import RevisionId, Stage

log = logging.getLogger(__name__)

NEXT_STAGE: Mapping[Stage, Stage] = {
    Stage.PARSE: Stage.SEGMENT,
    Stage.SEGMENT: Stage.EMBED,
    Stage.EMBED: Stage.READY,
}
WORK_STAGES: tuple[Stage, ...] = tuple(NEXT_STAGE)


@dataclass(frozen=True, slots=True)
class StageContext:
    pool: Pool  # ci_ingest
    settings: Settings


StageFn = Callable[[RevisionId, StageContext], Awaitable[None]]
"""A pipeline stage. It loads what it needs by work ID, must be idempotent, and
commits its output together with `advance()` in one transaction before
returning (Invariant 2). Raising, or returning without advancing, is a failed
attempt; cancellation releases the claim without counting one."""


class TransitionConflictError(RuntimeError):
    pass


async def advance(conn: AsyncConnection, work_id: RevisionId, stage: Stage) -> None:
    """Move a claimed revision from `stage` to the next one inside the caller's
    transaction, resetting `attempts` (so the attempt limit is per stage).
    Reaching `ready` writes `revision_ready` and makes the revision current
    unless a newer one is already ready.

    Raises TransitionConflictError if the row is not claimed at `stage`, so a
    second transition of the same revision fails and its transaction rolls
    back. Claims carry no owner, so which of two such workers wins is
    unspecified.
    """
    to = NEXT_STAGE[stage]
    cur = await conn.execute(
        "UPDATE docs.revisions"
        " SET stage = %s, locked_at = NULL, attempts = 0, last_error = NULL,"
        "     updated_at = now()"
        " WHERE id = %s AND stage = %s AND locked_at IS NOT NULL",
        (to.value, work_id, stage.value),
    )
    if cur.rowcount != 1:
        raise TransitionConflictError(
            f"revision {work_id} is not claimed at stage {stage}"
        )
    if to is Stage.READY:
        await _make_ready(conn, work_id)


async def _make_ready(conn: AsyncConnection, work_id: RevisionId) -> None:
    cur = await conn.execute(
        "SELECT r.paper_id, r.revision, NOT EXISTS ("
        "   SELECT 1 FROM docs.revisions o"
        "   WHERE o.paper_id = r.paper_id AND o.stage = 'ready'"
        "     AND o.revision > r.revision)"
        " FROM docs.revisions r WHERE r.id = %s",
        (work_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise TransitionConflictError(f"revision {work_id} disappeared")
    paper_id, revision, newest = row
    if newest:
        # Two statements: the partial unique index on is_current is checked
        # per row, so flipping both rows in one UPDATE can transiently collide.
        await conn.execute(
            "UPDATE docs.revisions SET is_current = false"
            " WHERE paper_id = %s AND is_current AND id <> %s",
            (paper_id, work_id),
        )
        await conn.execute(
            "UPDATE docs.revisions SET is_current = true WHERE id = %s", (work_id,)
        )
    await write_event(
        conn,
        EventKind.REVISION_READY,
        {
            "revision_id": work_id,
            "paper_id": str(paper_id),
            "revision": revision,
            "is_current": bool(newest),
        },
    )


async def _noop(stage: Stage, work_id: RevisionId, ctx: StageContext) -> None:
    async with ctx.pool.connection() as conn, conn.transaction():
        await advance(conn, work_id, stage)
    log.info("stage_done work_id=%d stage=%s noop=true", work_id, stage)


async def parse_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Placeholder: only commits the transition."""
    await _noop(Stage.PARSE, work_id, ctx)


async def segment_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Placeholder: only commits the transition."""
    await _noop(Stage.SEGMENT, work_id, ctx)


async def embed_stage(work_id: RevisionId, ctx: StageContext) -> None:
    """Placeholder: only commits the transition."""
    await _noop(Stage.EMBED, work_id, ctx)


DEFAULT_STAGES: Mapping[Stage, StageFn] = {
    Stage.PARSE: parse_stage,
    Stage.SEGMENT: segment_stage,
    Stage.EMBED: embed_stage,
}
