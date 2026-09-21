from fastapi import FastAPI

from cryptoindex.ingest.runner import Runner, Status


def create_app(runner: Runner) -> FastAPI:
    app = FastAPI(title="cryptoindex", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ingest/status")
    async def ingest_status() -> Status:
        return await runner.status()

    return app
