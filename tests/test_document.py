import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from cryptoindex.ingest.document import (
    Math,
    UnknownBlockError,
    markdown_math,
    read_marker,
    read_paddle,
)

FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "parsers"
SECTION = "3.2 Aside: Even Faster Fibonacci Numbers"


def fixture(parser: str) -> bytes:
    return (FIXTURES / f"erickson-p121-123.{parser}.json").read_bytes()


def marker_doc(*blocks: dict) -> bytes:
    return json.dumps(
        {
            "children": [
                {
                    "id": "/page/0/Page/0",
                    "block_type": "Page",
                    "html": "",
                    "bbox": [0, 0, 1, 1],
                    "children": list(blocks),
                }
            ]
        }
    ).encode()


def marker_block(
    block_type: str, html: str, ident: str = "/page/0/Text/1", **extra
) -> dict:
    return {
        "id": ident,
        "block_type": block_type,
        "html": html,
        "bbox": [0, 0, 1, 1],
        **extra,
    }


def paddle_doc(*blocks: tuple[str, str], bbox: list[float] | None = None) -> bytes:
    return json.dumps(
        {
            "pages": [
                {
                    "page_index": 0,
                    "parsing_res_list": [
                        {
                            "block_label": label,
                            "block_content": content,
                            "block_bbox": [0, 0, 1, 1] if bbox is None else bbox,
                        }
                        for label, content in blocks
                    ],
                }
            ]
        }
    ).encode()


def test_marker_fixture() -> None:
    doc = read_marker(fixture("marker"))
    assert doc.page_count == 3
    assert [b.page for b in doc.blocks] == sorted(b.page for b in doc.blocks)
    kinds = Counter(b.kind for b in doc.blocks)
    # 3.2, "Whoa! Not so fast!", 3.3, 3.4; three equations on p121, Splittable on p123
    assert kinds["equation"] == 4 and kinds["heading"] == 4 and kinds["furniture"] == 6
    heading = next(b for b in doc.blocks if b.kind == "heading")
    assert heading.heading_level == 2 and heading.section_path == ()
    assert SECTION in heading.text
    after = doc.blocks[doc.blocks.index(heading) + 1]
    assert len(after.section_path) == 1 and SECTION in after.section_path[0]
    two = next(b for b in doc.blocks if b.kind == "equation" and len(b.math) == 2)
    assert two.math[0] == Math("F_{2n-1} = F_{n-1}^2 + F_n^2", display=True)


def test_paddle_fixture() -> None:
    doc = read_paddle(fixture("paddle"))
    assert doc.page_count == 3
    algorithm = next(b for b in doc.blocks if b.kind == "algorithm")
    assert algorithm.text.splitlines()[:2] == ["ITERFIBO2(n):", "prev ← 1"]
    # Both lost their opening delimiter and end in a stray \].
    malformed = [b for b in doc.blocks if b.malformed_math]
    assert [(b.page, b.text[:12]) for b in malformed] == [
        (0, "F_{2n}=F_{n}"),
        (2, "Splittable(i"),
    ]
    inline = next(b for b in doc.blocks if b.kind == "text" and b.math)
    assert inline.math[0] == Math("F_{-1} = 1", display=False)
    assert all(b.heading_level is None for b in doc.blocks if b.kind != "heading")


def test_marker_html_becomes_markdown_with_math() -> None:
    html = (
        "<p>Costs $5 &amp; <math>x^2</math>, then<br/>"
        '<math display="block">y</math></p>'
    )
    [block] = read_marker(marker_doc(marker_block("Text", html))).blocks
    assert block.text == "Costs \\$5 & $x^2$, then\n$$y$$"
    assert block.math == (Math("x^2", False), Math("y", True))


def test_marker_groups_are_flattened_and_sections_nest() -> None:
    doc = read_marker(
        marker_doc(
            marker_block(
                "SectionHeader", "<h1>Intro</h1>", "/h1", section_hierarchy={1: "/h1"}
            ),
            marker_block(
                "SectionHeader",
                "<h2>Sub</h2>",
                "/h2",
                section_hierarchy={1: "/h1", 2: "/h2"},
            ),
            marker_block(
                "ListGroup",
                "",
                "/g",
                children=[
                    marker_block(
                        "ListItem",
                        "<li>one</li>",
                        "/i1",
                        section_hierarchy={1: "/h1", 2: "/h2"},
                    ),
                ],
            ),
        )
    )
    assert [(b.kind, b.heading_level, b.section_path) for b in doc.blocks] == [
        ("heading", 1, ()),
        ("heading", 2, ("Intro",)),
        ("list_item", None, ("Intro", "Sub")),
    ]


def test_marker_undelimited_equation_is_malformed() -> None:
    [block] = read_marker(marker_doc(marker_block("Equation", "<p>x = y</p>"))).blocks
    assert block.kind == "equation" and block.malformed_math and block.math == ()


def test_paddle_sections_are_title_then_section() -> None:
    doc = read_paddle(
        paddle_doc(
            ("doc_title", "Book"),
            ("paragraph_title", "One"),
            ("text", "a"),
            ("paragraph_title", "Two"),
            ("text", "b"),
        )
    )
    assert [(b.kind, b.section_path) for b in doc.blocks] == [
        ("heading", ()),
        ("heading", ("Book",)),
        ("text", ("Book", "One")),
        ("heading", ("Book",)),
        ("text", ("Book", "Two")),
    ]


@pytest.mark.parametrize(
    "reader, raw",
    [
        (read_marker, marker_doc(marker_block("Hologram", "<p/>"))),
        (read_paddle, paddle_doc(("hologram", "x"))),
    ],
)
def test_unmapped_block_types_are_errors(reader, raw: bytes) -> None:
    with pytest.raises(UnknownBlockError, match="ologram"):
        reader(raw)


@pytest.mark.parametrize(
    "reader, raw",
    [
        (read_marker, b'{"children": [{"block_type": "Page"}]}'),
        (read_paddle, b'{"pages": [{"page_index": 0}]}'),
        (read_paddle, paddle_doc(("text", "x"), bbox=[0, 0])),
    ],
)
def test_output_not_matching_the_schema_is_rejected(reader, raw: bytes) -> None:
    with pytest.raises(ValidationError):
        reader(raw)


def test_markdown_math_uses_a_real_parser() -> None:
    assert markdown_math(r"a \$5 fee, $x$ and $$y$$ and `$code$`") == (
        Math("x", False),
        Math("y", True),
    )
