from pathlib import Path

from cryptoindex.ingest.document import Block, ParsedDocument, read_marker
from cryptoindex.ingest.paragraphs import content_hash, paragraphs_from

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "eval/fixtures/parsers/erickson-p121-123.marker.json"
)
BOX = (0.0, 0.0, 1.0, 1.0)


def test_headings_furniture_and_empty_blocks_are_not_paragraphs() -> None:
    doc = ParsedDocument(
        page_count=2,
        blocks=(
            Block("heading", 0, BOX, "Groups", heading_level=1),
            Block("furniture", 0, BOX, "103"),
            Block("text", 0, BOX, "A group is a set.", section_path=("Groups",)),
            Block("figure", 1, BOX, "  "),
            Block(
                "equation",
                1,
                (1, 2, 3, 4),
                "ab = ba",
                section_path=("Groups", "Abelian"),
            ),
        ),
    )
    paras = paragraphs_from(doc)
    assert [
        (p.position, p.page, p.section_path, p.block_kind, p.text) for p in paras
    ] == [
        (0, 0, "Groups", None, "A group is a set."),
        (1, 1, "Groups > Abelian", "equation", "ab = ba"),
    ]
    assert paras[1].bbox == (1, 2, 3, 4)


def test_real_output_is_deterministic() -> None:
    raw = FIXTURE.read_bytes()
    first, second = paragraphs_from(read_marker(raw)), paragraphs_from(read_marker(raw))
    assert first == second
    assert len({p.content_hash for p in first}) == len(first)


def test_hash_ignores_reflow_only() -> None:
    assert content_hash("a  b\nc") == content_hash("a b c")
    assert content_hash("a b c") != content_hash("a b d")
