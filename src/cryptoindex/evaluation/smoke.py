"""`make smoke`: the service end to end in the modes we use, with the real
parser and models, against the test database and a scratch data directory
(port 8001), so the working index is untouched. Local only: it calls real
models, which CI never does.

It uploads the first two pages of PDF, waits for `ready`, and searches the
primary vectors; when CI_EMBED_MODEL_ALT is set, it restarts and searches the
alternate ones too (D27).
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import make_conninfo
from pypdf import PdfReader, PdfWriter

from cryptoindex.core import config
from cryptoindex.core.migrate import migrate

PORT = 8001
BASE = f"http://127.0.0.1:{PORT}"
PDF = Path("eval/parse-sample/local/excerpts/erickson-dp.pdf")
MIGRATIONS = Path("db/migrations")


def main() -> None:
    settings = config.settings
    env = dict(os.environ) | {"CI_API_PORT": str(PORT)}
    for var in ("CI_ADMIN_DSN", "CI_INGEST_DSN", "CI_QUERY_DSN"):
        env[var] = make_conninfo(env[var], dbname="cryptoindex_test")
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS cryptoindex_test WITH (FORCE)")
        conn.execute("CREATE DATABASE cryptoindex_test")
    migrate(
        env["CI_ADMIN_DSN"], [env["CI_INGEST_DSN"], env["CI_QUERY_DSN"]], MIGRATIONS
    )
    with tempfile.TemporaryDirectory(prefix="ci-smoke-") as scratch:
        env["CI_DATA_DIR"] = scratch
        pdf = Path(scratch) / "smoke.pdf"
        writer = PdfWriter()
        for page in PdfReader(Path(sys.argv[1]) if sys.argv[1:] else PDF).pages[:2]:
            writer.add_page(page)
        writer.write(pdf)

        results = [("primary", _run(env, pdf, upload=True))]
        if settings.embed_model_alt:
            results.append(
                ("alt", _run(env | {"CI_SEARCH_VECTORS": "alt"}, pdf, upload=False))
            )
    for mode, error in results:
        print(f"{'PASS' if error is None else 'FAIL'}  searching {mode}  {error or ''}")
    if any(error for _, error in results):
        sys.exit(1)


def _run(env: dict[str, str], pdf: Path, upload: bool) -> str | None:
    """Returns what went wrong, or None."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "cryptoindex"], env=env, start_new_session=True
    )
    try:
        with httpx.Client(base_url=BASE, timeout=600) as client:
            _wait(lambda: _up(client), 600, "the service did not start")
            if upload:
                with pdf.open("rb") as f:
                    client.post(
                        "/documents", files={"file": f}, data={"name": "smoke"}
                    ).raise_for_status()
            _wait(lambda: _ready(client), 1800, "the document did not reach ready")
            info = client.get("/search/api/info").json()
            if info["vectors"] != env.get("CI_SEARCH_VECTORS", "primary"):
                return f"searching {info['vectors']} vectors"
            hits = client.get("/search/api/search", params={"q": "recurrence"}).json()
            if not hits:
                return "no search results"
            first = hits[0]["gloss"][:60]
            print(f"ok    {info['model']}: {len(hits)} hits, first {first}")
            return None
    except (TimeoutError, httpx.HTTPError) as e:
        return str(e)
    finally:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=60)


def _up(client: httpx.Client) -> bool:
    try:
        return client.get("/health").status_code == 200
    except httpx.TransportError:
        return False


def _ready(client: httpx.Client) -> bool:
    (doc,) = client.get("/documents").json()
    if doc["stage"] == "failed":
        raise TimeoutError(f"the document failed: {doc['error']}")
    return doc["stage"] == "ready"


def _wait(done: Callable[[], bool], timeout_s: float, message: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not done():
        if time.monotonic() > deadline:
            raise TimeoutError(message)
        time.sleep(2)


if __name__ == "__main__":
    main()
