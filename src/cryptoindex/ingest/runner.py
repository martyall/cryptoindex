import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from cryptoindex.core.config import Settings
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.stages import (
    DEFAULT_STAGES,
    NEXT_STAGE,
    WORK_STAGES,
    StageContext,
    StageFn,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Status:
    stages: dict[str, int]  # revisions per stage, from the database
    locked: int  # rows with a claim set, including stale ones from a dead process
    queued: dict[str, int]  # work IDs waiting in this process's channels


def pool_size(s: Settings) -> int:
    """One connection per worker, plus two shared by seeding and the API;
    further callers wait for a free connection."""
    return s.concurrency_parse + s.concurrency_segment + s.concurrency_embed + 2


class Runner:
    """Runs the ingestion stages over bounded channels of revision IDs.

    The channels are only a signal (Invariant 1): a worker claims the row in
    the database before running a stage, and skips any ID whose row is no
    longer at that stage or is claimed elsewhere. Duplicate or stale IDs in a
    channel are therefore harmless, and startup re-seeds the channels from
    `docs.revisions`.
    """

    def __init__(
        self,
        ctx: StageContext,
        stages: Mapping[Stage, StageFn] = DEFAULT_STAGES,
        retry_base_s: float = 1.0,
    ) -> None:
        s = ctx.settings
        self._ctx = ctx
        self._stages = stages
        self._retry_base_s = retry_base_s
        self._concurrency = {
            Stage.PARSE: s.concurrency_parse,
            Stage.SEGMENT: s.concurrency_segment,
            Stage.EMBED: s.concurrency_embed,
        }
        self._queues: dict[Stage, asyncio.Queue[RevisionId]] = {
            stage: asyncio.Queue(maxsize=s.queue_capacity) for stage in WORK_STAGES
        }
        self._tg: asyncio.TaskGroup | None = None

    async def run(self) -> None:
        """Run until cancelled; cancellation releases in-flight claims.

        Stale claims are recovered before seeding because seeding skips claimed
        rows, so a claim younger than `stale_lock_s` stays unprocessed until a
        later start. A database error outside a stage body (claiming, recording
        a failure) escapes as an ExceptionGroup and stops the runner.
        """
        await self._recover_stale_locks()
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                for stage in WORK_STAGES:
                    for _ in range(self._concurrency[stage]):
                        tg.create_task(self._worker(stage))
                tg.create_task(self._seed())
        finally:
            self._tg = None

    async def enqueue(self, work_id: RevisionId) -> None:
        """Signal pending work for a revision. Its stage is read from the
        database; revisions at `ready` or `failed` are ignored. Waits while the
        stage's channel is full."""
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT stage FROM docs.revisions WHERE id = %s", (work_id,)
            )
            row = await cur.fetchone()
        if row is not None and (stage := Stage(row[0])) in self._queues:
            await self._queues[stage].put(work_id)

    def notify(self, work_id: RevisionId) -> None:
        """`enqueue` without waiting, for request handlers: a full channel must
        not stall the request. A no-op when the runner is not running, and
        never raises; either way the committed revision is seeded on the next
        start."""
        if self._tg is not None:
            self._tg.create_task(self._enqueue_or_log(work_id))

    async def _enqueue_or_log(self, work_id: RevisionId) -> None:
        # Runs in the runner's TaskGroup, where an exception would stop every
        # worker; a failed signal only delays this revision until next start.
        try:
            await self.enqueue(work_id)
        except Exception:
            log.exception("notify_failed work_id=%d", work_id)

    async def status(self) -> Status:
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT stage, count(*), count(locked_at)"
                " FROM docs.revisions GROUP BY stage"
            )
            rows = await cur.fetchall()
        return Status(
            stages={stage.value: 0 for stage in Stage} | {r[0]: r[1] for r in rows},
            locked=sum(r[2] for r in rows),
            queued={stage.value: q.qsize() for stage, q in self._queues.items()},
        )

    async def _recover_stale_locks(self) -> None:
        # A claim that outlived the timeout belonged to a worker that died
        # mid-stage. It counts as a failed attempt, so a revision that crashes
        # the process every time ends in `failed` instead of a crash loop.
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE docs.revisions"
                " SET locked_at = NULL, attempts = attempts + 1, updated_at = now(),"
                "     last_error = stage || ': claim went stale',"
                "     stage = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE stage END"
                " WHERE locked_at < now() - make_interval(secs => %s)"
                " RETURNING id, stage",
                (self._ctx.settings.max_attempts, self._ctx.settings.stale_lock_s),
            )
            for work_id, stage in await cur.fetchall():
                log.warning("stale_claim_released work_id=%d stage=%s", work_id, stage)

    async def _seed(self) -> None:
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, stage FROM docs.revisions"
                " WHERE stage = ANY(%s) AND locked_at IS NULL ORDER BY id",
                ([stage.value for stage in WORK_STAGES],),
            )
            rows = await cur.fetchall()
        log.info("seeding revisions=%d", len(rows))
        for work_id, stage in rows:
            await self._queues[Stage(stage)].put(RevisionId(work_id))

    async def _worker(self, stage: Stage) -> None:
        queue = self._queues[stage]
        while True:
            work_id = await queue.get()
            try:
                await self._process(stage, work_id)
            finally:
                queue.task_done()

    async def _process(self, stage: Stage, work_id: RevisionId) -> None:
        if not await self._claim(stage, work_id):
            return
        try:
            await self._stages[stage](work_id, self._ctx)
            if await self._still_claimed(stage, work_id):
                raise RuntimeError("stage returned without advancing the revision")
        except asyncio.CancelledError:
            await asyncio.shield(self._release(stage, work_id))
            raise
        except Exception as exc:
            await self._record_failure(stage, work_id, exc)
            return
        # Invariant 2: the transition is committed; only now signal downstream.
        next_stage = NEXT_STAGE[stage]
        if next_stage in self._queues:
            await self._queues[next_stage].put(work_id)

    async def _claim(self, stage: Stage, work_id: RevisionId) -> bool:
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE docs.revisions SET locked_at = now()"
                " WHERE id = ("
                "   SELECT id FROM docs.revisions"
                "   WHERE id = %s AND stage = %s AND locked_at IS NULL"
                "   FOR UPDATE SKIP LOCKED)"
                " RETURNING id",
                (work_id, stage.value),
            )
            return await cur.fetchone() is not None

    async def _still_claimed(self, stage: Stage, work_id: RevisionId) -> bool:
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM docs.revisions"
                " WHERE id = %s AND stage = %s AND locked_at IS NOT NULL",
                (work_id, stage.value),
            )
            return await cur.fetchone() is not None

    async def _release(self, stage: Stage, work_id: RevisionId) -> None:
        async with self._ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE docs.revisions SET locked_at = NULL"
                " WHERE id = %s AND stage = %s",
                (work_id, stage.value),
            )
        log.info("claim_released work_id=%d stage=%s", work_id, stage)

    async def _record_failure(
        self, stage: Stage, work_id: RevisionId, exc: Exception
    ) -> None:
        async with self._ctx.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE docs.revisions"
                " SET attempts = attempts + 1, last_error = %s, locked_at = NULL,"
                "     updated_at = now(),"
                "     stage = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE stage END"
                " WHERE id = %s AND stage = %s AND locked_at IS NOT NULL"
                " RETURNING stage, attempts",
                (
                    f"{stage}: {exc!r}",
                    self._ctx.settings.max_attempts,
                    work_id,
                    stage.value,
                ),
            )
            row = await cur.fetchone()
        if row is None:
            log.error(
                "failure_unrecorded work_id=%d stage=%s error=%r", work_id, stage, exc
            )
            return
        new_stage, attempts = row
        log.warning(
            "stage_failed work_id=%d stage=%s attempts=%d gave_up=%s error=%r",
            work_id,
            stage,
            attempts,
            new_stage == Stage.FAILED,
            exc,
        )
        if new_stage != Stage.FAILED and self._tg is not None:
            delay = self._retry_base_s * 2 ** (attempts - 1)
            self._tg.create_task(self._retry_later(stage, work_id, delay))

    async def _retry_later(
        self, stage: Stage, work_id: RevisionId, delay: float
    ) -> None:
        # A separate task, so a worker never blocks putting into its own full queue.
        await asyncio.sleep(delay)
        await self._queues[stage].put(work_id)
