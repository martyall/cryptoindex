import sys
from pathlib import Path

import pytest

from cryptoindex.ingest.parsers import MarkerParser, ParserError

# Stands in for `marker_single PDF --output_dir DIR ...`: writes DIR/<stem>/<stem>.md.
FAKE_MARKER = """
import sys, pathlib
args = sys.argv[1:]
pdf = pathlib.Path(args[0])
out = pathlib.Path(args[args.index("--output_dir") + 1]) / pdf.stem
if pdf.stem == "crash":
    print("Traceback: boom", file=sys.stderr)
    sys.exit(3)
out.mkdir(parents=True)
if pdf.stem != "empty":
    (out / f"{pdf.stem}.md").write_text("# " + pdf.stem + "\\n\\nBody.\\n")
"""


@pytest.fixture
def fake_marker(tmp_path: Path) -> MarkerParser:
    script = tmp_path / "fake_marker.py"
    script.write_text(FAKE_MARKER)
    return MarkerParser(command=[sys.executable, str(script)], timeout_s=30)


def test_marker_returns_its_markdown(fake_marker: MarkerParser, tmp_path: Path) -> None:
    assert fake_marker.parse(tmp_path / "groups.pdf") == "# groups\n\nBody.\n"


def test_marker_failure_carries_stderr(
    fake_marker: MarkerParser, tmp_path: Path
) -> None:
    with pytest.raises(ParserError, match="exited 3: Traceback: boom"):
        fake_marker.parse(tmp_path / "crash.pdf")


def test_marker_without_output_is_an_error(
    fake_marker: MarkerParser, tmp_path: Path
) -> None:
    with pytest.raises(ParserError, match="found 0"):
        fake_marker.parse(tmp_path / "empty.pdf")


def test_missing_executable_is_an_error(tmp_path: Path) -> None:
    parser = MarkerParser(command=["/nonexistent/marker"], timeout_s=5)
    with pytest.raises(ParserError, match="not found"):
        parser.parse(tmp_path / "x.pdf")
