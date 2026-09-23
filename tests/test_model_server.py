import socket
import sys
from pathlib import Path

import pytest

from cryptoindex.ingest.model_server import address, listening, mlx_vlm_server

# Stands in for mlx_vlm.server: accepts `--host H --port P` and listens there.
FAKE_SERVER = """
import argparse, socketserver
p = argparse.ArgumentParser()
p.add_argument("--host"); p.add_argument("--port", type=int)
a = p.parse_args()
server = socketserver.TCPServer((a.host, a.port), socketserver.BaseRequestHandler)
server.serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    return [sys.executable, str(script)]


async def test_starts_and_stops_its_own_server(
    fake_command: list[str], tmp_path: Path
) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    async with mlx_vlm_server(url, tmp_path / "log", fake_command, 20) as started:
        assert started and listening("127.0.0.1", port)
    assert not listening("127.0.0.1", port)


async def test_reuses_and_leaves_a_server_already_listening(
    fake_command: list[str], tmp_path: Path
) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    async with mlx_vlm_server(url, tmp_path / "log", fake_command, 20):
        async with mlx_vlm_server(url, tmp_path / "log2", ["false"], 20) as started:
            assert not started
        assert listening("127.0.0.1", port)  # the inner one did not stop it


async def test_a_server_that_exits_is_an_error(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{free_port()}/"
    command = [sys.executable, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(RuntimeError, match="exited with 3"):
        async with mlx_vlm_server(url, tmp_path / "log", command, 20):
            pass


def test_address_needs_host_and_port() -> None:
    assert address("http://localhost:8111/") == ("localhost", 8111)
    with pytest.raises(ValueError, match="host and a port"):
        address("http://localhost/")
