from pathlib import Path

import pytest
from pydantic import ValidationError

from cryptoindex.evaluation.parse_eval import (
    KATEX_TOOL,
    block_html,
    formula_report,
    load_sample,
)
from cryptoindex.ingest.document import Block, Math, read_paddle

ROOT = Path(__file__).resolve().parents[1]
BOX = (0.0, 0.0, 1.0, 1.0)
KATEX_INSTALLED = (ROOT / KATEX_TOOL / "node_modules").exists()


def test_committed_sample_is_consistent() -> None:
    sample = load_sample(ROOT / "eval/parse-sample/ids.toml")
    assert len(sample.source) == 9 and len(sample.excerpt) == 9
    for excerpt in sample.excerpt:
        assert excerpt.last_page <= sample.source_of(excerpt).pages


@pytest.mark.parametrize(
    "toml",
    [
        '[[source]]\nname = "a"\n',  # source fields missing, no excerpts
        'source = []\n[[excerpt]]\nname = "e"\nfirst_page = "one"\n',
    ],
)
def test_malformed_samples_are_rejected(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "ids.toml"
    path.write_text(toml)
    with pytest.raises(ValidationError):
        load_sample(path)


def test_dangling_excerpt_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ids.toml"
    path.write_text(
        "source = []\n"
        '[[excerpt]]\nname = "e"\nsource = "nowhere"\nfirst_page = 1\n'
        'last_page = 2\nstresses = "x"\n'
    )
    with pytest.raises(ValueError, match="unknown source"):
        load_sample(path)


def test_block_html_escapes_text_and_marks_math() -> None:
    text = Block("text", 0, BOX, "a < b and $x$", math=(Math("x", False),))
    assert (
        block_html(text) == '<p>a &lt; b and <span class="math inline">x</span></p>\n'
    )
    code = Block("algorithm", 0, BOX, "if a < b:\n  return")
    assert block_html(code) == "<pre>if a &lt; b:\n  return</pre>"
    eq = Block("equation", 0, BOX, "a<b", math=(Math("a<b", True),))
    assert block_html(eq) == '<div class="math block">a&lt;b</div>'


@pytest.mark.skipif(not KATEX_INSTALLED, reason="tools/katex-check not installed")
def test_formula_report_counts_render_and_delimiter_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    fixture = ROOT / "eval/fixtures/parsers/erickson-p121-123.paddle.json"
    report = formula_report(read_paddle(fixture.read_bytes()))
    undelimited = [f for f in report.failures if "not delimited" in f.error]
    assert [f.page for f in undelimited] == [0, 2]
    assert report.total > len(report.failures)
