import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from cryptoindex.core.config import Settings
from cryptoindex.ingest.document import (
    Block,
    ParsedDocument,
    read_marker,
    read_paddle,
)

TOOLS = Path(__file__).resolve().parents[3] / "tools"

MARKER_VERSION = "2.0.0"
MARKER_COMMAND = (
    "uvx",
    "--python",
    "3.12",
    "--from",
    f"marker-pdf=={MARKER_VERSION}",
    "python",
    str(TOOLS / "marker_parse.py"),
)
# What marker/scripts/convert_single.py sets before importing Marker.
MARKER_ENV = {
    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
    "GRPC_VERBOSITY": "ERROR",
    "GLOG_minloglevel": "2",
}

PADDLE_VERSION = "3.7.0"
PADDLEPADDLE_VERSION = "3.3.1"
MLX_VLM_VERSION = "0.7.2"  # the server `make paddle-server` runs
PADDLE_MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
PADDLE_MODEL_NAME = "PaddleOCR-VL-1.6"
PADDLE_COMMAND = (
    "uvx",
    "--python",
    "3.12",
    "--from",
    f"paddleocr[doc-parser]=={PADDLE_VERSION}",
    "--with",
    f"paddlepaddle=={PADDLEPADDLE_VERSION}",
    "python",
    str(TOOLS / "paddle_vl_parse.py"),
)
# Skip the model-host connectivity check once models are cached.
PADDLE_ENV = {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"}


class ParserError(RuntimeError):
    pass


class Parser(Protocol):
    """A PDF parser, in two steps so its output can be cached and re-read.

    `run` is blocking and runs the actual parser out-of-process (D7), so a
    native crash cannot take down the pipeline; it returns the parser's own
    structured output (JSON bytes) and raises ParserError on failure. `read` is
    pure and blocking: it maps that output to a ParsedDocument and raises
    (pydantic's ValidationError, UnknownBlockError) on output it does not
    accept. Cached output is keyed by `name` and `version`, so `version` must
    change whenever `run`'s output could.
    """

    name: str
    version: str

    def run(self, pdf_path: Path) -> bytes: ...

    def read(self, raw: bytes) -> ParsedDocument: ...


class StubParser:
    """Returns one text block per call, whatever the PDF; for tests and the
    runner's no-op checks."""

    name = "stub"
    version = "0"

    def run(self, pdf_path: Path) -> bytes:
        return b"{}"

    def read(self, raw: bytes) -> ParsedDocument:
        return ParsedDocument(
            page_count=1,
            blocks=(Block("text", 0, (0.0, 0.0, 1.0, 1.0), "Stub paragraph."),),
        )


class MarkerParser:
    """Marker through its Python API (tools/marker_parse.py) in its own uv tool
    environment, so torch and its models never enter the project's
    environment. Needs llama.cpp's `llama-server` on the PATH. Only
    marker-pdf is pinned; uvx resolves its other dependencies (surya, …) on
    install, so a fresh install can change output without changing `version`."""

    name = "marker"
    version = MARKER_VERSION

    def __init__(
        self, command: Sequence[str] = MARKER_COMMAND, timeout_s: float = 3600
    ) -> None:
        self._command = list(command)
        self._timeout_s = timeout_s

    def run(self, pdf_path: Path) -> bytes:
        return _run_to_json(
            lambda out: [*self._command, str(pdf_path), str(out)],
            self._timeout_s,
            MARKER_ENV,
        )

    def read(self, raw: bytes) -> ParsedDocument:
        return read_marker(raw)


class PaddleVLParser:
    """PaddleOCR-VL: layout detection in PaddlePaddle on the CPU and the
    vision-language model in a separate MLX-VLM server (D7), which must be
    running at `server_url` (`make paddle-server`). The pipeline runs in its own
    uv tool environment through tools/paddle_vl_parse.py."""

    name = "paddle"
    version = (
        f"{PADDLE_VERSION}+paddle{PADDLEPADDLE_VERSION}"
        f"+mlxvlm{MLX_VLM_VERSION}+{PADDLE_MODEL_NAME}"
    )

    def __init__(
        self,
        server_url: str,
        command: Sequence[str] = PADDLE_COMMAND,
        timeout_s: float = 3600,
    ) -> None:
        self._server_url = server_url
        self._command = list(command)
        self._timeout_s = timeout_s

    def run(self, pdf_path: Path) -> bytes:
        return _run_to_json(
            lambda out: [
                *self._command,
                str(pdf_path),
                str(out),
                self._server_url,
                PADDLE_MODEL,
            ],
            self._timeout_s,
            PADDLE_ENV,
        )

    def read(self, raw: bytes) -> ParsedDocument:
        return read_paddle(raw)


def build_parser(settings: Settings) -> Parser:
    """The configured real parser. PaddleVLParser needs `make paddle-server`
    running only when `run` is called."""
    match settings.parser:
        case "marker":
            return MarkerParser()
        case "paddle":
            return PaddleVLParser(settings.paddle_vlm_url)


def _run_to_json(
    args_for: "_ArgsFor", timeout_s: float, env: Mapping[str, str]
) -> bytes:
    """Run a tool script that writes its JSON result to the path it is given."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.json"
        args = args_for(out)
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=os.environ | dict(env),
            )
        except subprocess.CalledProcessError as exc:
            tail = "\n".join((exc.stderr or "").strip().splitlines()[-5:])
            raise ParserError(f"{args[0]} exited {exc.returncode}: {tail}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ParserError(f"{args[0]} timed out after {timeout_s:.0f}s") from exc
        except FileNotFoundError as exc:
            raise ParserError(f"{args[0]} not found") from exc
        if not out.exists():
            raise ParserError(f"{args[0]} exited cleanly but wrote no result")
        return out.read_bytes()


class _ArgsFor(Protocol):
    def __call__(self, out: Path) -> list[str]: ...
