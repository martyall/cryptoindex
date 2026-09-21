import asyncio
import dataclasses
import io
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter

from cryptoindex.api import create_app
from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.model import Stage
from cryptoindex.ingest.parsers import StubParser
from cryptoindex.ingest.runner import Runner
from cryptoindex.ingest.stages import StageContext


def _one_page_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


PDF = _one_page_pdf()


@pytest.fixture
def runner(settings: Settings, pool: Pool, tmp_path: Path) -> Runner:
    ctx = StageContext(
        pool=pool,
        settings=dataclasses.replace(settings, data_dir=tmp_path),
        parser=StubParser(),
    )
    return Runner(ctx)


@pytest.fixture
async def client(runner: Runner, pool: Pool) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runner, pool, runner._ctx.settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_upload_page_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="files"' in response.text


async def test_health_and_ingest_status(
    client: httpx.AsyncClient, seed: Callable[[list[Stage]], list[int]]
) -> None:
    seed([Stage.PARSE, Stage.PARSE, Stage.READY])
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


async def test_upload_defaults_name_and_reports_duplicates(
    client: httpx.AsyncClient,
) -> None:
    first = await client.post("/documents", files={"file": ("Kyber.Pdf", PDF)})
    assert first.status_code == 201
    assert first.json()["name"] == "Kyber" and not first.json()["duplicate"]

    again = await client.post(
        "/documents", files={"file": ("other.pdf", PDF)}, data={"name": "Copy"}
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] and again.json()["name"] == "Kyber"
    assert again.json()["paper_id"] == first.json()["paper_id"]

    listed = (await client.get("/documents")).json()
    assert [(d["name"], d["revision"], d["stage"]) for d in listed] == [
        ("Kyber", 1, "parse")
    ]


async def test_upload_rejects_non_pdf(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/documents", files={"file": ("x.pdf", b"not a pdf")}, data={"name": "x"}
    )
    assert response.status_code == 400
    assert "not a readable PDF" in response.json()["detail"]


async def test_uploaded_document_reaches_ready_without_restart(
    client: httpx.AsyncClient, runner: Runner, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cryptoindex.ingest.runner")
    task = asyncio.create_task(runner.run())
    try:
        # Upload only after startup seeding, so notify() must deliver it.
        for _ in range(100):
            if "seeding revisions=0" in caplog.messages:
                break
            await asyncio.sleep(0.02)
        assert "seeding revisions=0" in caplog.messages
        response = await client.post("/documents", files={"file": ("a.pdf", PDF)})
        assert response.status_code == 201
        for _ in range(100):
            listed = (await client.get("/documents")).json()
            if listed[0]["stage"] == "ready":
                break
            await asyncio.sleep(0.05)
        assert listed[0]["stage"] == "ready"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
