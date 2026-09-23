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
from cryptoindex.evaluation.search_page import KATEX_DIR, paragraph_html
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


def cited_places(citations: object) -> list[dict[str, object]]:
    """The citations grouped by what the reader sees: markers citing
    different paragraphs of the same block share one line. Removed citations
    keep a line each, with the reason."""
    markers_at: dict[str, list[str]] = {}
    removed: list[dict[str, object]] = []
    for c in citations if isinstance(citations, list) else []:
        if c["rendered"] is None:
            removed.append({"markers": [c["marker"]], "removed": c["removed"]})
        elif c["marker"] not in markers_at.setdefault(c["rendered"], []):
            markers_at[c["rendered"]].append(c["marker"])
    kept: list[dict[str, object]] = [
        {"markers": m, "rendered": where} for where, m in markers_at.items()
    ]
    return kept + removed


async def _events(handler: AnswerHandler, tools: Tools, q: str) -> AsyncIterator[str]:
    async for event in ask(handler, tools, q):
        data: dict[str, object] = {"kind": event.kind, **event.data}
        if event.kind == "final":
            # Model output: rendered with raw HTML escaped, math left for KaTeX.
            data["answer_html"] = paragraph_html(str(event.data["answer"]), None)
            data["cited"] = cited_places(event.data["citations"])
        yield f"data: {json.dumps(data)}\n\n"
    # EventSource reconnects when a stream ends; `done` tells the page to stop.
    yield "event: done\ndata: {}\n\n"
