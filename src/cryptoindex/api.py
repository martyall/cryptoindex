import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
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


def create_app(runner: Runner, pool: Pool, settings: Settings) -> FastAPI:
    app = FastAPI(title="cryptoindex", version="0.1.0")

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
        """Store the PDF and start it through the pipeline. `name` defaults to
        the file name without `.pdf`. An identical file already stored is not
        stored again: the response is 200 with `duplicate: true` and the
        existing document."""
        if name is None or not name.strip():
            name = (file.filename or "").removesuffix(".pdf").removesuffix(".PDF")
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
        """Newest first, each with the stage of its latest revision."""
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT p.id, p.name, p.created_at, r.revision, r.stage,"
                "       r.last_error"
                " FROM docs.papers p"
                " JOIN LATERAL ("
                "   SELECT revision, stage, last_error FROM docs.revisions"
                "   WHERE paper_id = p.id ORDER BY revision DESC LIMIT 1"
                " ) r ON true"
                " ORDER BY p.created_at DESC, p.name"
            )
            return [DocumentSummary(*row) for row in await cur.fetchall()]

    return app


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(1 << 20):
        yield chunk
