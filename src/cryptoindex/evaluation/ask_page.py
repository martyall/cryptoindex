"""The Ask page (Phase 5), served by `make run` at /ask/: a question, the
agent's steps as it works, and the answer with its citations located (D28-D30).
A QA tool like the search page; the HTTP API proper is Phase 6."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cryptoindex.core.db import Pool
from cryptoindex.core.embed import PRIMARY, Embedder, VectorSet
from cryptoindex.evaluation.search_page import KATEX_DIR
from cryptoindex.query.answer import AnswerHandler, Tools, ask

PAGE = Path(__file__).with_name("ask_page.html")


def mount_ask(
    app: FastAPI,
    handler: AnswerHandler,
    pool: Pool,
    embedder: Embedder,
    vectors: VectorSet = PRIMARY,
    katex_dir: Path = KATEX_DIR,
) -> None:
    """Serve the Ask page at /ask/ on `app`. Each question gets its own Tools
    session on `pool`, which must be the ci_query role (Invariant 7), with
    `embedder` the model behind `vectors` (Invariant 6, D27)."""
    router = APIRouter(prefix="/ask")

    @router.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(PAGE)

    @router.get("/api/ask")
    async def run(q: str) -> StreamingResponse:
        """Server-sent events, one per AgentEvent, then `done`."""
        return StreamingResponse(
            _events(handler, Tools(pool, embedder, vectors), q),
            media_type="text/event-stream",
        )

    app.include_router(router)
    app.mount("/ask/katex", StaticFiles(directory=katex_dir, check_dir=False))


async def _events(handler: AnswerHandler, tools: Tools, q: str) -> AsyncIterator[str]:
    async for event in ask(handler, tools, q):
        yield f"data: {json.dumps({'kind': event.kind, **event.data})}\n\n"
    # EventSource reconnects when a stream ends; `done` tells the page to stop.
    yield "event: done\ndata: {}\n\n"
