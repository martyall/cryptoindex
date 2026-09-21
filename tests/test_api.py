from collections.abc import Callable

import httpx

from cryptoindex.api import create_app
from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.model import Stage
from cryptoindex.ingest.runner import Runner
from cryptoindex.ingest.stages import StageContext


async def test_health_and_ingest_status(
    settings: Settings, pool: Pool, seed: Callable[[list[Stage]], list[int]]
) -> None:
    seed([Stage.PARSE, Stage.PARSE, Stage.READY])
    app = create_app(Runner(StageContext(pool=pool, settings=settings)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        status = (await client.get("/ingest/status")).json()
    assert status["stages"] == {
        "parse": 2,
        "segment": 0,
        "embed": 0,
        "ready": 1,
        "failed": 0,
    }
    assert status["locked"] == 0
