"""Runs the pipeline in its own process until no revision has pending work.

Started, and SIGKILLed mid-run, by test_runner.py. Its stages behave like the
no-op stages but record each execution in test_audit.stage_runs inside the
stage's transaction, and sleep before committing so a kill lands mid-stage.
Usage: runner_child.py <stage delay seconds>
"""

import asyncio
import sys

from cryptoindex.core import config
from cryptoindex.core.db import open_pool
from cryptoindex.core.embed import FakeEmbedder
from cryptoindex.core.llm import FakeLLM
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.parsers import StubParser
from cryptoindex.ingest.runner import Runner, pool_size
from cryptoindex.ingest.stages import WORK_STAGES, StageContext, StageFn, advance


def audited(stage: Stage, delay: float) -> StageFn:
    async def run(work_id: RevisionId, ctx: StageContext) -> None:
        async with ctx.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO test_audit.stage_runs (revision_id, stage)"
                " VALUES (%s, %s)",
                (work_id, stage.value),
            )
            await asyncio.sleep(delay)
            await advance(conn, work_id, stage)

    return run


async def main(delay: float) -> None:
    settings = config.settings
    pool = await open_pool(settings.ingest_dsn, pool_size(settings))
    runner = Runner(
        StageContext(
            pool=pool,
            settings=settings,
            parser=StubParser(),
            llm=FakeLLM({}),
            embedder=FakeEmbedder(),
        ),
        {stage: audited(stage, delay) for stage in WORK_STAGES},
    )
    task = asyncio.create_task(runner.run())
    while True:
        await asyncio.sleep(0.05)
        status = await runner.status()
        if all(status.stages[s.value] == 0 for s in WORK_STAGES):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1])))
