import asyncio
import contextlib
import signal
from collections.abc import Mapping

import uvicorn

from cryptoindex.api import create_app
from cryptoindex.core import config
from cryptoindex.core.backends import build_llm
from cryptoindex.core.config import Settings
from cryptoindex.core.db import open_pool
from cryptoindex.core.embed import (
    ALT,
    PRIMARY,
    VECTOR_SETS,
    build_embedder,
    check_embed_model,
)
from cryptoindex.core.logs import configure as configure_logging
from cryptoindex.core.model import Stage
from cryptoindex.ingest.model_server import mlx_vlm_server
from cryptoindex.ingest.parsers import build_parser
from cryptoindex.ingest.pipeline import DEFAULT_STAGES
from cryptoindex.ingest.runner import Runner, pool_size
from cryptoindex.ingest.stages import StageContext, StageFn

QUERY_POOL_SIZE = 4


async def serve(
    settings: Settings, stages: Mapping[Stage, StageFn] = DEFAULT_STAGES
) -> None:
    """Pipeline and HTTP API on one event loop, until SIGINT or SIGTERM. Returns
    normally after the runner has released its in-flight claims. With the
    PaddleOCR-VL parser, its model server runs for the same span (started
    here unless one is already listening)."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with contextlib.AsyncExitStack() as stack:
        if settings.parser == "paddle":
            await stack.enter_async_context(
                mlx_vlm_server(
                    settings.paddle_vlm_url, settings.data_dir / "logs" / "mlx-vlm.log"
                )
            )
        await _serve(settings, stages)


async def _serve(settings: Settings, stages: Mapping[Stage, StageFn]) -> None:
    pool = await open_pool(settings.ingest_dsn, pool_size(settings))
    query_pool = await open_pool(settings.query_dsn, QUERY_POOL_SIZE)
    try:
        embedder = build_embedder(settings, settings.embed_model)
        embedder_alt = (
            build_embedder(settings, settings.embed_model_alt)
            if settings.embed_model_alt
            else None
        )
        async with pool.connection() as conn:  # Invariant 6, per set (D27)
            await check_embed_model(conn, embedder.model, PRIMARY)
            if embedder_alt is not None:
                await check_embed_model(conn, embedder_alt.model, ALT)
        # The embed stage and searches share these embedders: a model is the
        # largest thing in memory, and this process loads each once.
        ctx = StageContext(
            pool=pool,
            settings=settings,
            parser=build_parser(settings),
            llm=build_llm(settings),
            embedder=embedder,
            embedder_alt=embedder_alt,
        )
        searching = VECTOR_SETS[settings.search_vectors]
        search_embedder = embedder if searching is PRIMARY else embedder_alt
        assert search_embedder is not None  # config requires the alt model
        runner = Runner(ctx, stages)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    runner, pool, settings, query_pool, search_embedder, searching
                ),
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
        await query_pool.close()
        await pool.close()


def main() -> None:
    settings = config.settings
    configure_logging(settings.log_level)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
