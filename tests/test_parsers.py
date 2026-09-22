import sys
from pathlib import Path

import pytest

from cryptoindex.ingest.parsers import (
    PADDLE_MODEL,
    MarkerParser,
    PaddleVLParser,
    ParserError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "parsers"

# Stands in for tools/*_parse.py: `script PDF OUT_JSON [...]` copies a
# recorded result to OUT_JSON, or fails the way a crashing parser would.
FAKE_TOOL = """
import shutil, sys, pathlib
pdf, out, extra = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
if extra != {expected_extra!r}:
    print(f"unexpected arguments {{extra}}", file=sys.stderr)
    sys.exit(4)
if pdf.stem == "crash":
    print("Traceback: boom", file=sys.stderr)
    sys.exit(3)
if pdf.stem != "silent":
    shutil.copy({fixture!r}, out)
"""


def tool(tmp_path: Path, parser: str, expected_extra: list[str]) -> list[str]:
    script = tmp_path / f"fake_{parser}.py"
    fixture = FIXTURES / f"erickson-p121-123.{parser}.json"
    script.write_text(
        FAKE_TOOL.format(fixture=str(fixture), expected_extra=expected_extra)
    )
    return [sys.executable, str(script)]


def test_marker_adapter_runs_and_reads(tmp_path: Path) -> None:
    parser = MarkerParser(command=tool(tmp_path, "marker", []), timeout_s=30)
    doc = parser.read(parser.run(tmp_path / "book.pdf"))
    assert doc.page_count == 3


def test_paddle_adapter_passes_server_and_model(tmp_path: Path) -> None:
    expected = ["http://127.0.0.1:1/", PADDLE_MODEL]
    parser = PaddleVLParser(
        "http://127.0.0.1:1/", command=tool(tmp_path, "paddle", expected)
    )
    doc = parser.read(parser.run(tmp_path / "book.pdf"))
    assert doc.page_count == 3


def test_failure_carries_stderr(tmp_path: Path) -> None:
    parser = MarkerParser(command=tool(tmp_path, "marker", []), timeout_s=30)
    with pytest.raises(ParserError, match="exited 3: Traceback: boom"):
        parser.run(tmp_path / "crash.pdf")


def test_clean_exit_without_result_is_an_error(tmp_path: Path) -> None:
    parser = MarkerParser(command=tool(tmp_path, "marker", []), timeout_s=30)
    with pytest.raises(ParserError, match="wrote no result"):
        parser.run(tmp_path / "silent.pdf")


def test_missing_executable_is_an_error(tmp_path: Path) -> None:
    parser = MarkerParser(command=["/nonexistent/uvx"], timeout_s=5)
    with pytest.raises(ParserError, match="not found"):
        parser.run(tmp_path / "x.pdf")
