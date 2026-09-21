import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from cryptoindex.core.config import Settings

MARKER_VERSION = "2.0.0"
MARKER_COMMAND = (
    "uvx",
    "--python",
    "3.12",
    "--from",
    f"marker-pdf=={MARKER_VERSION}",
    "marker_single",
)


class ParserError(RuntimeError):
    pass


class Parser(Protocol):
    """PDF to Markdown with LaTeX math. Blocking, and runs the actual parser
    out-of-process (D7) so a native crash cannot take down the pipeline.
    Raises ParserError when the parser fails or crashes; the stage records a
    failed attempt. `version` names everything that changes the output, since
    cached output is keyed by it."""

    name: str
    version: str

    def parse(self, pdf_path: Path) -> str: ...


class StubParser:
    name = "stub"
    version = "0"

    def parse(self, pdf_path: Path) -> str:
        return f"# {pdf_path.stem}\n\nStub paragraph.\n"


class MarkerParser:
    """Marker's CLI in its own uv tool environment, so torch and its models
    never enter the project's environment. Needs llama.cpp's `llama-server`
    on the PATH."""

    name = "marker"
    version = MARKER_VERSION

    def __init__(
        self, command: Sequence[str] = MARKER_COMMAND, timeout_s: float = 3600
    ) -> None:
        self._command = list(command)
        self._timeout_s = timeout_s

    def parse(self, pdf_path: Path) -> str:
        with tempfile.TemporaryDirectory() as out:
            _run(
                [
                    *self._command,
                    str(pdf_path),
                    "--output_dir",
                    out,
                    "--output_format",
                    "markdown",
                    "--disable_image_extraction",
                ],
                self._timeout_s,
            )
            return _single_markdown(Path(out))


def build_parser(settings: Settings) -> Parser:
    match settings.parser:
        case "marker":
            return MarkerParser()
        case "paddle":
            raise NotImplementedError("PaddleOCR-VL adapter")


def _run(args: list[str], timeout_s: float) -> None:
    try:
        subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.CalledProcessError as exc:
        tail = "\n".join((exc.stderr or "").strip().splitlines()[-5:])
        raise ParserError(f"{args[0]} exited {exc.returncode}: {tail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ParserError(f"{args[0]} timed out after {timeout_s:.0f}s") from exc
    except FileNotFoundError as exc:
        raise ParserError(f"{args[0]} not found") from exc


def _single_markdown(out: Path) -> str:
    found = sorted(out.rglob("*.md"))
    if len(found) != 1:
        raise ParserError(f"expected one Markdown file, found {len(found)}")
    return found[0].read_text()
