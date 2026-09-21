import asyncio
import logging
import signal
from collections.abc import Mapping

import uvicorn

from cryptoindex.api import create_app
from cryptoindex.core import config
from cryptoindex.core.config import Settings
from cryptoindex.core.db import open_pool
from cryptoindex.core.model import Stage
from cryptoindex.ingest.runner import Runner, pool_size
from cryptoindex.ingest.stages import DEFAULT_STAGES, StageContext, StageFn


async def serve(
    settings: Settings, stages: Mapping[Stage, StageFn] = DEFAULT_STAGES
) -> None:
    """Pipeline and HTTP API on one event loop, until SIGINT or SIGTERM. Returns
    normally after the runner has released its in-flight claims."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pool = await open_pool(settings.ingest_dsn, pool_size(settings))
    try:
        runner = Runner(StageContext(pool=pool, settings=settings), stages)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(runner),
                host=settings.api_host,
                port=settings.api_port,
                log_config=None,
            )
        )
        # uvicorn shuts down on these signals, then restores the previous
        # handlers and re-raises the signal. With the defaults, SIGTERM would
        # kill the process before the runner is cancelled, leaving claims
        # locked. No-op handlers make the re-raise harmless.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: None)
        async with asyncio.TaskGroup() as tg:
            runner_task = tg.create_task(runner.run())
            await server.serve()
            runner_task.cancel()
    finally:
        await pool.close()


def main() -> None:
    settings = config.settings
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
