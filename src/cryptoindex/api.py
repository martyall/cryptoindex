import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.embed import Embedder
from cryptoindex.evaluation.search_page import mount_search
from cryptoindex.ingest.importer import (
    ImportResult,
    RejectedUploadError,
    import_document,
)
from cryptoindex.ingest.runner import Runner, Status

UPLOAD_PAGE = Path(__file__).with_name("static") / "upload.html"


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    id: uuid.UUID
    name: str
    uploaded_at: datetime
    revision: int
    stage: str
    error: str | None
    similar_to: (
        str | None
    )  # a near-duplicate's name, if similarity >= SIMILARITY_WARNING
    similarity: float | None


# Share of a document's paragraphs found in another one before the page
# warns "looks similar to …". A warning only; nothing is rejected.
SIMILARITY_WARNING = 0.5


def create_app(
    runner: Runner,
    pool: Pool,
    settings: Settings,
    query_pool: Pool,
    embedder: Embedder,
) -> FastAPI:
    """`pool` is the ci_ingest role, used for uploads and status;
    `query_pool` is ci_query, used for search (Invariant 7). `embedder` is
    the pipeline's own, so the model is loaded once in this process."""
    app = FastAPI(title="cryptoindex", version="0.1.0")
    mount_search(app, query_pool, embedder)

    @app.get("/", include_in_schema=False)
    async def upload_page() -> FileResponse:
        return FileResponse(UPLOAD_PAGE)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ingest/status")
    async def ingest_status() -> Status:
        return await runner.status()

    @app.post("/documents", status_code=status.HTTP_201_CREATED)
    async def upload_document(
        file: UploadFile,
        response: Response,
        name: Annotated[str | None, Form()] = None,
    ) -> ImportResult:
        """Store the PDF and signal the pipeline. `name` defaults to the file
        name without its `.pdf` suffix (any case).

        201: new document. 200 with `duplicate: true`: this exact file is
        already stored; the existing document is returned and not re-queued,
        even if it failed. 400: blank name, not a PDF, or over CI_MAX_UPLOAD_MB.
        """
        if name is None or not name.strip():
            filename = PurePath(file.filename or "")
            name = filename.stem if filename.suffix.lower() == ".pdf" else filename.name
        try:
            result = await import_document(
                pool,
                settings.data_dir,
                name,
                _chunks(file),
                settings.max_upload_mb * 1024 * 1024,
            )
        except RejectedUploadError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if result.duplicate:
            response.status_code = status.HTTP_200_OK
        else:
            runner.notify(result.revision_id)
        return result

    @app.get("/documents")
    async def list_documents() -> list[DocumentSummary]:
        """Newest first, each with the stage of its latest revision and, if at
        least SIMILARITY_WARNING of its paragraphs appear in another document,
        that document's name."""
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT p.id, p.name, p.created_at, r.revision, r.stage,"
                "       r.last_error, s.name, r.similarity"
                " FROM docs.papers p"
                " JOIN LATERAL ("
                "   SELECT revision, stage, last_error, similar_paper_id, similarity"
                "   FROM docs.revisions"
                "   WHERE paper_id = p.id ORDER BY revision DESC LIMIT 1"
                " ) r ON true"
                " LEFT JOIN docs.papers s"
                "   ON s.id = r.similar_paper_id AND r.similarity >= %s"
                " ORDER BY p.created_at DESC, p.name",
                (SIMILARITY_WARNING,),
            )
            return [DocumentSummary(*row) for row in await cur.fetchall()]

    return app


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(1 << 20):
        yield chunk
