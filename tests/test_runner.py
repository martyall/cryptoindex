import asyncio
import dataclasses
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import LiteralString

import psycopg
import pytest
from psycopg.rows import TupleRow

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.runner import Runner
from cryptoindex.ingest.stages import (
    DEFAULT_STAGES,
    WORK_STAGES,
    StageContext,
    StageFn,
)

CHILD = Path(__file__).with_name("runner_child.py")
STAGES_TO_READY = {Stage.PARSE: 3, Stage.SEGMENT: 2, Stage.EMBED: 1, Stage.READY: 0}

SeedFn = Callable[[list[Stage]], list[int]]  # the `seed` fixture in conftest.py


def mixed_stages(n: int) -> list[Stage]:
    return [WORK_STAGES[i % len(WORK_STAGES)] for i in range(n)]


def query(settings: Settings, sql: LiteralString) -> list[TupleRow]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(sql).fetchall()


async def wait_until(pred: Callable[[], Awaitable[bool]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not await pred():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.05)


async def run_until_idle(runner: Runner, timeout: float = 10) -> None:
    async def idle() -> bool:
        status = await runner.status()
        return all(status.stages[s.value] == 0 for s in WORK_STAGES)

    task = asyncio.create_task(runner.run())
    try:
        await wait_until(idle, timeout)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_seeded_revisions_all_reach_ready(
    settings: Settings, pool: Pool, seed: SeedFn
) -> None:
    ids = seed(mixed_stages(20))
    await run_until_idle(Runner(StageContext(pool=pool, settings=settings)))

    rows = query(settings, "SELECT stage, locked_at, attempts FROM docs.revisions")
    assert [r[0] for r in rows] == ["ready"] * len(ids)
    assert all(r[1] is None and r[2] == 0 for r in rows)
    # Each paper's revision 2 is current; exactly one current revision per paper.
    current = query(
        settings, "SELECT paper_id, revision FROM docs.revisions WHERE is_current"
    )
    assert sorted(r[1] for r in current) == [2] * (len(ids) // 2)
    events = query(settings, "SELECT count(*) FROM docs.events")
    assert events == [(len(ids),)]


async def test_enqueue_signals_a_new_revision(
    settings: Settings, pool: Pool, seed: SeedFn
) -> None:
    runner = Runner(StageContext(pool=pool, settings=settings))
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.sleep(0.2)  # startup seeding has found nothing
        (work_id,) = seed([Stage.PARSE])
        await runner.enqueue(RevisionId(work_id))

        async def ready() -> bool:
            return (await runner.status()).stages["ready"] == 1

        await wait_until(ready, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_kill_mid_run_then_restart_does_no_duplicate_work(
    settings: Settings, test_env: dict[str, str], seed: SeedFn
) -> None:
    initial = mixed_stages(20) + [Stage.READY, Stage.READY]
    ids = seed(initial)
    expected_runs = sum(STAGES_TO_READY[s] for s in initial)
    env = test_env | {"CI_STALE_LOCK_S": "0", "CI_CONCURRENCY_SEGMENT": "4"}

    def audit_count() -> int:
        return query(settings, "SELECT count(*) FROM test_audit.stage_runs")[0][0]

    first = subprocess.Popen([sys.executable, str(CHILD), "0.2"], env=env)
    deadline = time.monotonic() + 30
    while audit_count() < expected_runs // 3:
        assert first.poll() is None, "runner exited before it could be killed"
        assert time.monotonic() < deadline, "runner made no progress"
        time.sleep(0.02)
    os.kill(first.pid, signal.SIGKILL)
    first.wait()

    # Killed mid-run: work remains, and some claims were left by dead workers.
    pending = query(
        settings,
        "SELECT count(*), count(locked_at) FROM docs.revisions WHERE stage <> 'ready'",
    )[0]
    assert pending[0] > 0
    assert pending[1] > 0

    second = subprocess.run(
        [sys.executable, str(CHILD), "0.01"], env=env, timeout=60, check=True
    )
    assert second.returncode == 0

    stages = query(settings, "SELECT stage, locked_at FROM docs.revisions")
    assert stages == [("ready", None)] * len(ids)
    duplicates = query(
        settings,
        "SELECT revision_id, stage, count(*) FROM test_audit.stage_runs"
        " GROUP BY 1, 2 HAVING count(*) > 1",
    )
    assert duplicates == []
    assert audit_count() == expected_runs
    ready_events = query(
        settings, "SELECT count(*) FROM docs.events WHERE kind = 'revision_ready'"
    )
    assert ready_events == [(len(mixed_stages(20)),)]


async def test_failing_stage_retries_then_fails(
    settings: Settings, pool: Pool, seed: SeedFn
) -> None:
    async def broken(work_id: RevisionId, ctx: StageContext) -> None:
        raise RuntimeError("boom")

    async def forgetful(work_id: RevisionId, ctx: StageContext) -> None:
        return None

    stages: dict[Stage, StageFn] = dict(DEFAULT_STAGES)
    stages[Stage.PARSE] = broken
    stages[Stage.SEGMENT] = forgetful
    seed([Stage.PARSE, Stage.SEGMENT])
    ctx = StageContext(
        pool=pool, settings=dataclasses.replace(settings, max_attempts=2)
    )
    await run_until_idle(Runner(ctx, stages, retry_base_s=0))

    rows = query(
        settings,
        "SELECT stage, attempts, locked_at, last_error FROM docs.revisions ORDER BY id",
    )
    assert [r[:3] for r in rows] == [("failed", 2, None), ("failed", 2, None)]
    assert rows[0][3] == "parse: RuntimeError('boom')"
    assert "without advancing" in rows[1][3]


async def test_cancel_releases_claims(
    settings: Settings, pool: Pool, seed: SeedFn
) -> None:
    started = asyncio.Event()

    async def hang(work_id: RevisionId, ctx: StageContext) -> None:
        started.set()
        await asyncio.Event().wait()

    stages: dict[Stage, StageFn] = dict(DEFAULT_STAGES)
    stages[Stage.PARSE] = hang
    seed([Stage.PARSE])
    task = asyncio.create_task(
        Runner(StageContext(pool=pool, settings=settings), stages).run()
    )
    await asyncio.wait_for(started.wait(), 5)
    assert query(settings, "SELECT count(locked_at) FROM docs.revisions") == [(1,)]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert query(settings, "SELECT stage, locked_at FROM docs.revisions") == [
        ("parse", None)
    ]


@pytest.mark.parametrize("attempts_before, expected", [(0, "parse"), (2, "failed")])
async def test_stale_claim_counts_as_attempt(
    settings: Settings, pool: Pool, seed: SeedFn, attempts_before: int, expected: str
) -> None:
    (work_id,) = seed([Stage.PARSE])
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE docs.revisions SET locked_at = now() - interval '1 hour',"
            " attempts = %s WHERE id = %s",
            (attempts_before, work_id),
        )
    runner = Runner(StageContext(pool=pool, settings=settings))
    await runner._recover_stale_locks()
    rows = query(settings, "SELECT stage, attempts, locked_at FROM docs.revisions")
    assert rows == [(expected, attempts_before + 1, None)]
