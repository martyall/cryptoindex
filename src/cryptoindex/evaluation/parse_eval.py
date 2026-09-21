"""Phase 2 parser evaluation (EVALUATION.md §1; D8, D19, D20). Local only: it
runs the real parsers, which CI never does. `make parse-eval`.

For each excerpt in eval/parse-sample/ids.toml it cuts the pages out of the
source, runs both parsers (caching their raw JSON), counts formulas KaTeX
cannot render, and renders page images for the review page
(`make parse-review`).
Everything it writes goes under eval/parse-sample/local/, which is not in git.
"""

import hashlib
import html
import json
import socket
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel

from cryptoindex.core import config
from cryptoindex.ingest.document import Block, Math, ParsedDocument
from cryptoindex.ingest.parsers import MarkerParser, PaddleVLParser, Parser

SAMPLE = Path("eval/parse-sample")
LOCAL = SAMPLE / "local"
KATEX_TOOL = Path("tools/katex-check")
ITEMS = LOCAL / "review-items.json"  # every page, served by review_server


class Source(BaseModel):
    name: str
    sha256: str
    pages: int
    license: str
    url: str


class Excerpt(BaseModel):
    name: str
    source: str
    first_page: int  # 1-based PDF page index, inclusive
    last_page: int
    stresses: str


class Sample(BaseModel):
    source: list[Source]
    excerpt: list[Excerpt]

    def source_of(self, excerpt: Excerpt) -> Source:
        return next(s for s in self.source if s.name == excerpt.source)


def load_sample(path: Path) -> Sample:
    """Raises ValueError if an excerpt names an unknown source or has an empty
    page range, and pydantic's ValidationError for a malformed file."""
    sample = Sample.model_validate(tomllib.loads(path.read_text()))
    names = {s.name for s in sample.source}
    for e in sample.excerpt:
        if e.source not in names:
            raise ValueError(f"excerpt {e.name} names unknown source {e.source}")
        if not 1 <= e.first_page <= e.last_page:
            raise ValueError(
                f"excerpt {e.name} has page range {e.first_page}-{e.last_page}"
            )
    return sample


def verify_source(pdf: Path, source: Source) -> None:
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if digest != source.sha256:
        raise ValueError(
            f"{pdf} has sha256 {digest}, expected {source.sha256}; "
            f"re-download from {source.url}"
        )


def cut_excerpt(source_pdf: Path, excerpt: Excerpt, dest: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(source_pdf)
    writer = PdfWriter()
    for index in range(excerpt.first_page - 1, excerpt.last_page):
        writer.add_page(reader.pages[index])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        writer.write(f)


def render_pages(pdf: Path, dest_dir: Path, scale: float = 1.4) -> list[Path]:
    """One PNG per page, p000.png, p001.png, … (0-based, like Block.page)."""
    import pypdfium2

    dest_dir.mkdir(parents=True, exist_ok=True)
    doc = pypdfium2.PdfDocument(pdf)
    out = []
    for i in range(len(doc)):
        path = dest_dir / f"p{i:03d}.png"
        if not path.exists():
            doc[i].render(scale=scale).to_pil().save(path)
        out.append(path)
    return out


@dataclass(frozen=True, slots=True)
class FormulaFailure:
    page: int
    tex: str
    error: str


@dataclass(frozen=True, slots=True)
class FormulaReport:
    total: int  # formulas found plus undelimited equation blocks
    failures: tuple[FormulaFailure, ...]


def formula_report(document: ParsedDocument, katex: Path = KATEX_TOOL) -> FormulaReport:
    """Every formula the reader found is rendered with KaTeX; an equation block
    the parser did not delimit counts as a failure without rendering."""
    found = [(b.page, m) for b in document.blocks for m in b.math]
    undelimited = [
        FormulaFailure(b.page, b.text, "equation not delimited as math")
        for b in document.blocks
        if b.malformed_math
    ]
    errors = katex_errors([m for _, m in found], katex)
    rendered = [
        FormulaFailure(page, m.tex, error)
        for (page, m), error in zip(found, errors, strict=True)
        if error is not None
    ]
    return FormulaReport(len(found) + len(undelimited), tuple(undelimited + rendered))


def katex_errors(
    formulas: Sequence[Math], katex: Path = KATEX_TOOL
) -> list[str | None]:
    """KaTeX's error for each formula, or None where it renders. Needs Node and
    `npm ci` in tools/katex-check."""
    if not formulas:
        return []
    lines = "".join(
        json.dumps({"tex": m.tex, "display": m.display}) + "\n" for m in formulas
    )
    done = subprocess.run(
        ["node", str(katex / "check.mjs")],
        input=lines,
        capture_output=True,
        text=True,
        check=True,
    )
    results = [json.loads(line) for line in done.stdout.splitlines()]
    if len(results) != len(formulas):
        raise RuntimeError("katex-check returned the wrong number of results")
    return [None if r["ok"] else r["error"] for r in results]


# Raw HTML in parser text is escaped, not passed through.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)


def block_html(block: Block) -> str:
    """How the review page shows a block. Math is emitted as the elements the
    dollarmath plugin produces (class "math inline" / "math block"), which
    KaTeX renders in the browser. Tables are the parser's own HTML."""
    if block.kind == "table":
        return block.text
    if block.kind == "equation" and not block.malformed_math:
        return "".join(
            f'<div class="math block">{html.escape(m.tex)}</div>' for m in block.math
        )
    if block.kind in ("algorithm", "code") or block.malformed_math:
        return f"<pre>{html.escape(block.text)}</pre>"
    return _MARKDOWN.render(block.text)


def _parsers(settings: config.Settings) -> list[Parser]:
    url = urlsplit(settings.paddle_vlm_url)
    try:
        socket.create_connection((url.hostname, url.port or 80), timeout=2).close()
    except OSError:
        sys.exit(
            f"no MLX-VLM server at {settings.paddle_vlm_url}; start it with "
            "`make paddle-server` in another terminal"
        )
    return [MarkerParser(), PaddleVLParser(settings.paddle_vlm_url)]


def main() -> None:
    settings = config.settings
    sample = load_sample(SAMPLE / "ids.toml")
    parsers = _parsers(settings)
    review: list[dict[str, object]] = []
    summary: dict[str, dict[str, dict[str, int]]] = {}
    for excerpt in sample.excerpt:
        source = sample.source_of(excerpt)
        source_pdf = LOCAL / "sources" / f"{source.name}.pdf"
        verify_source(source_pdf, source)
        pdf = LOCAL / "excerpts" / f"{excerpt.name}.pdf"
        if not pdf.exists():
            cut_excerpt(source_pdf, excerpt, pdf)
        images = render_pages(pdf, LOCAL / "pages" / excerpt.name)
        documents: dict[str, ParsedDocument] = {}
        for parser in parsers:
            raw_path = (
                LOCAL
                / "output"
                / f"{parser.name}-{parser.version}"
                / (f"{excerpt.name}.json")
            )
            if not raw_path.exists():
                print(f"parsing {excerpt.name} with {parser.name}", flush=True)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(parser.run(pdf))
            documents[parser.name] = parser.read(raw_path.read_bytes())
            report = formula_report(documents[parser.name])
            summary.setdefault(excerpt.name, {})[parser.name] = {
                "formulas": report.total,
                "failed": len(report.failures),
            }
        for page, image in enumerate(images):
            review.append(
                {
                    "excerpt": excerpt.name,
                    "stresses": excerpt.stresses,
                    "page": page,
                    "source_page": excerpt.first_page + page,
                    "image": str(image.relative_to(LOCAL)),
                    "parsers": {
                        name: [
                            {"kind": b.kind, "html": block_html(b)}
                            for b in doc.blocks
                            if b.page == page and b.kind != "furniture"
                        ]
                        for name, doc in documents.items()
                    },
                }
            )
    (LOCAL / "formulas.json").write_text(json.dumps(summary, indent=2))
    ITEMS.write_text(json.dumps(review))
    print(json.dumps(summary, indent=2))
    print("review: make parse-review")


if __name__ == "__main__":
    main()
