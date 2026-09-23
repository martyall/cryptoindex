"""PaddleOCR-VL's MLX-VLM model server, run alongside the pipeline (D21).

It runs natively, not in Docker, because it needs the Mac GPU. `make run`
starts it with the process when CI_PARSER=paddle; `make paddle-server` runs it
on its own (for the parser evaluation).
"""

import asyncio
import logging
import os
import signal
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptoindex.core import config
from cryptoindex.core.logs import configure as configure_logging
from cryptoindex.ingest.parsers import MLX_VLM_MAX_NUM_SEQS, MLX_VLM_VERSION

log = logging.getLogger(__name__)

MLX_VLM_COMMAND = (
    "uvx",
    "--python",
    "3.12",
    "--from",
    f"mlx-vlm=={MLX_VLM_VERSION}",
    "mlx_vlm.server",
    "--max-num-seqs",
    str(MLX_VLM_MAX_NUM_SEQS),
)


def address(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.hostname is None or parts.port is None:
        raise ValueError(f"model server URL {url!r} needs a host and a port")
    return parts.hostname, parts.port


def listening(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=0.5).close()
    except OSError:
        return False
    return True


@asynccontextmanager
async def mlx_vlm_server(
    url: str,
    log_path: Path,
    command: Sequence[str] = MLX_VLM_COMMAND,
    startup_timeout_s: float = 600,
) -> AsyncIterator[bool]:
    """Ensure an MLX-VLM server is listening at `url` for the duration.

    Yields True if it started the server, which it stops on exit (SIGTERM to
    its process group, SIGKILL after 10 s), or False if one was already
    listening, which it leaves alone: a reused server must have been started
    with the same command (`make paddle-server`), MLX_VLM_MAX_NUM_SEQS
    included. The first start installs the tool, hence the long timeout;
    model weights download later, on the first request.
    Raises RuntimeError if the server exits or is not listening in time.
    """
    host, port = address(url)
    if await asyncio.to_thread(listening, host, port):
        log.info("model_server_reused", extra={"url": url})
        yield False
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as output:
        proc = await asyncio.create_subprocess_exec(
            *command,
            "--host",
            host,
            "--port",
            str(port),
            stdout=output,
            stderr=output,
            start_new_session=True,  # its own group: stopped by us, not by Ctrl-C
        )
    log.info(
        "model_server_starting",
        extra={"url": url, "pid": proc.pid, "log_path": str(log_path)},
    )
    try:
        await _wait_until_listening(proc, host, port, startup_timeout_s, log_path)
        log.info("model_server_ready", extra={"url": url})
        yield True
    finally:
        await _stop(proc)


async def _wait_until_listening(
    proc: asyncio.subprocess.Process,
    host: str,
    port: int,
    timeout_s: float,
    log_path: Path,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not await asyncio.to_thread(listening, host, port):
        if proc.returncode is not None:
            raise RuntimeError(
                f"model server exited with {proc.returncode}; see {log_path}"
            )
        if loop.time() > deadline:
            raise RuntimeError(
                f"model server not listening after {timeout_s:.0f}s; see {log_path}"
            )
        await asyncio.sleep(0.5)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
    log.info("model_server_stopped", extra={"pid": proc.pid})


async def _serve_forever() -> None:
    settings = config.settings
    log_path = settings.data_dir / "logs" / "mlx-vlm.log"
    async with mlx_vlm_server(settings.paddle_vlm_url, log_path) as started:
        if not started:
            print(f"a model server is already listening at {settings.paddle_vlm_url}")
            return
        print(f"model server at {settings.paddle_vlm_url}; Ctrl-C to stop")
        await asyncio.Event().wait()


def main() -> None:
    configure_logging(config.settings.log_level)
    try:
        asyncio.run(_serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
