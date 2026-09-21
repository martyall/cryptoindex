from pathlib import Path
from typing import Protocol


class Parser(Protocol):
    """PDF to Markdown with LaTeX math. Blocking, and runs the actual parser
    out-of-process (D7) so a native crash cannot take down the pipeline.
    Raises when the parser fails or crashes; the stage records a failed
    attempt."""

    name: str
    version: str

    def parse(self, pdf_path: Path) -> str: ...


class StubParser:
    name = "stub"
    version = "0"

    def parse(self, pdf_path: Path) -> str:
        return f"# {pdf_path.stem}\n\nStub paragraph.\n"
