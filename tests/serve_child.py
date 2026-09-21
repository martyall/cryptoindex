"""Runs the real entry point, `serve()`, with a parse stage that never finishes,
so a signal always arrives while a claim is held. Used by test_shutdown.py."""

import asyncio

from cryptoindex.__main__ import serve
from cryptoindex.core import config
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.stages import DEFAULT_STAGES, StageContext


async def hang(work_id: RevisionId, ctx: StageContext) -> None:
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(serve(config.settings, dict(DEFAULT_STAGES) | {Stage.PARSE: hang}))
