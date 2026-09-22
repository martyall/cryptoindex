"""A parser-independent document model, read from each parser's own structured
output. Nothing here parses text by hand:

- each parser's JSON is validated against a pydantic schema of its output;
- Marker's block HTML goes through an HTML parser;
- PaddleOCR-VL's block Markdown goes through markdown-it with its math plugin;
- every parser block type maps to a block kind through an explicit table, and an
  unmapped type is an error.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel

BlockKind = Literal[
    "heading",
    "text",
    "list_item",
    "equation",
    "algorithm",
    "code",
    "table",
    "caption",
    "footnote",
    "figure",
    "furniture",  # running headers, footers, page numbers: never paragraphs
]
BBox = tuple[float, float, float, float]  # x0, y0, x1, y1 in the parser's page units


class UnknownBlockError(ValueError):
    """A parser emitted a block type with no entry in its mapping table."""


@dataclass(frozen=True, slots=True)
class Math:
    tex: str
    display: bool


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    page: int  # 0-based
    bbox: BBox
    # PaddleOCR-VL: its Markdown. Marker: plain text with $-delimited math.
    # Equations: LaTeX (raw text if undelimited). Tables: the parser's HTML.
    text: str
    math: tuple[Math, ...] = ()
    heading_level: int | None = None  # None if the parser reports no levels
    section_path: tuple[str, ...] = ()
    malformed_math: bool = False  # an equation block the parser did not delimit


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    page_count: int
    blocks: tuple[Block, ...]

    @property
    def title(self) -> str | None:
        """The first heading, if any."""
        return next((b.text for b in self.blocks if b.kind == "heading"), None)


# --- Marker (marker-pdf 2.0, --output_format json) ----------------------------


class MarkerBlockJSON(BaseModel):
    """The fields of marker.renderers.json.JSONBlockOutput this reader uses."""

    id: str
    block_type: str
    html: str
    bbox: BBox
    children: list["MarkerBlockJSON"] | None = None
    section_hierarchy: dict[int, str] | None = None


class MarkerDocumentJSON(BaseModel):
    """The field of marker.renderers.json.JSONOutput this reader uses: the
    children are pages."""

    children: list[MarkerBlockJSON]


MARKER_KINDS: Mapping[str, BlockKind] = {
    "SectionHeader": "heading",
    "Text": "text",
    "TextInlineMath": "text",
    "Handwriting": "text",
    "ComplexRegion": "text",
    "Form": "text",
    "TableOfContents": "text",
    "Bibliography": "text",
    "Reference": "text",
    "ListItem": "list_item",
    "Equation": "equation",
    "Code": "code",
    "Table": "table",
    "Caption": "caption",
    "Footnote": "footnote",
    "Figure": "figure",
    "Picture": "figure",
    "Diagram": "figure",
    "ChemicalBlock": "figure",
    "PageHeader": "furniture",
    "PageFooter": "furniture",
}
# Containers whose children are the real blocks.
MARKER_GROUPS = frozenset({"FigureGroup", "TableGroup", "ListGroup", "PictureGroup"})


def read_marker(raw: str | bytes) -> ParsedDocument:
    """Read Marker's JSON document (Document > Page > blocks). Pages are
    numbered by their position in the document. Raises pydantic's
    ValidationError if the JSON does not match the schema, and
    UnknownBlockError for a block type missing from MARKER_KINDS."""
    document = MarkerDocumentJSON.model_validate_json(raw)
    located = [
        (page_index, block)
        for page_index, page in enumerate(document.children)
        for block in _marker_blocks(page.children or [])
    ]
    headings = {
        block.id: _HtmlContent.of(block.html).markdown
        for _, block in located
        if block.block_type == "SectionHeader"
    }
    return ParsedDocument(
        page_count=len(document.children),
        blocks=tuple(_marker_block(page, block, headings) for page, block in located),
    )


def _marker_blocks(blocks: list[MarkerBlockJSON]) -> Iterator[MarkerBlockJSON]:
    for block in blocks:
        if block.block_type in MARKER_GROUPS:
            yield from _marker_blocks(block.children or [])
        else:
            yield block


def _marker_block(page: int, block: MarkerBlockJSON, headings: dict[str, str]) -> Block:
    if block.block_type not in MARKER_KINDS:
        raise UnknownBlockError(f"Marker block type {block.block_type!r}")
    kind = MARKER_KINDS[block.block_type]
    hierarchy = sorted((block.section_hierarchy or {}).items())
    section_path = tuple(
        headings[ident]
        for _, ident in hierarchy
        if ident in headings and ident != block.id  # a heading is not inside itself
    )
    if kind == "table":
        return Block(kind, page, block.bbox, block.html, section_path=section_path)
    content = _HtmlContent.of(block.html)
    return Block(
        kind=kind,
        page=page,
        bbox=block.bbox,
        # An equation Marker left undelimited keeps its text, like PaddleOCR-VL's.
        text=content.equations_tex
        if kind == "equation" and content.math
        else content.markdown,
        math=content.math,
        heading_level=content.heading_level if kind == "heading" else None,
        section_path=section_path,
        malformed_math=kind == "equation" and not content.math,
    )


class _HtmlContent(HTMLParser):
    """Walks one block's HTML. Text is kept as plain text, with only `$`
    escaped so math delimiters stay unambiguous; each <math> element becomes a
    `$`-delimited formula, and <h1>…<h6> gives the heading level."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._math: list[Math] = []
        self._display: bool | None = None  # set while inside <math>
        self._tex: list[str] = []
        self.heading_level: int | None = None

    @classmethod
    def of(cls, html: str) -> "_HtmlContent":
        parser = cls()
        parser.feed(html)
        parser.close()
        return parser

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "math":
            self._display = dict(attrs).get("display") == "block"
            self._tex = []
        elif tag in _HEADING_LEVELS:
            self.heading_level = _HEADING_LEVELS[tag]
        elif tag in _LINE_BREAKING and self._parts:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "math" and self._display is not None:
            math = Math("".join(self._tex).strip(), self._display)
            self._math.append(math)
            self._parts.append(f"$${math.tex}$$" if math.display else f"${math.tex}$")
            self._display = None

    def handle_data(self, data: str) -> None:
        if self._display is not None:
            self._tex.append(data)
        else:
            self._parts.append(data.replace("$", r"\$"))

    @property
    def markdown(self) -> str:
        return "".join(self._parts).strip()

    @property
    def math(self) -> tuple[Math, ...]:
        return tuple(self._math)

    @property
    def equations_tex(self) -> str:
        return "\n\n".join(m.tex for m in self._math)


_HEADING_LEVELS = {f"h{n}": n for n in range(1, 7)}
_LINE_BREAKING = frozenset({"br", "p", "li", "tr", "div"})


# --- PaddleOCR-VL (paddleocr 3.7, tools/paddle_vl_parse.py) -------------------


class PaddleBlockJSON(BaseModel):
    block_label: str
    block_content: str
    block_bbox: BBox


class PaddlePageJSON(BaseModel):
    page_index: int
    parsing_res_list: list[PaddleBlockJSON]


class PaddleDocumentJSON(BaseModel):
    pages: list[PaddlePageJSON]


PADDLE_KINDS: Mapping[str, BlockKind] = {
    "doc_title": "heading",
    "paragraph_title": "heading",
    "text": "text",
    "abstract": "text",
    "content": "text",
    "vertical_text": "text",
    "aside_text": "text",
    "reference": "text",
    "reference_content": "text",
    "display_formula": "equation",
    "inline_formula": "equation",
    "algorithm": "algorithm",
    "table": "table",
    "figure_title": "caption",
    "vision_footnote": "caption",
    "footnote": "footnote",
    "image": "figure",
    "chart": "figure",
    "seal": "figure",
    "formula_number": "furniture",
    "header": "furniture",
    "header_image": "furniture",
    "footer": "furniture",
    "footer_image": "furniture",
    "number": "furniture",
}
# PaddleOCR-VL reports no heading levels, only document title vs. section title,
# so its section paths have at most two parts.
_PADDLE_HEADING_LEVEL = {"doc_title": 1, "paragraph_title": 2}

_MARKDOWN = MarkdownIt("commonmark").use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)


def read_paddle(raw: str | bytes) -> ParsedDocument:
    """Read the {"pages": [...]} JSON written by tools/paddle_vl_parse.py, in
    the pipeline's list order (its reading order). Raises pydantic's
    ValidationError if the JSON does not match the schema, and
    UnknownBlockError for a label missing from PADDLE_KINDS."""
    document = PaddleDocumentJSON.model_validate_json(raw)
    title: str | None = None
    section: str | None = None
    blocks = []
    for page in document.pages:
        for block in page.parsing_res_list:
            if block.block_label not in PADDLE_KINDS:
                raise UnknownBlockError(f"PaddleOCR-VL label {block.block_label!r}")
            kind = PADDLE_KINDS[block.block_label]
            content = block.block_content.strip()
            math = markdown_math(content)
            if kind == "equation":  # a display block, however it was delimited
                math = tuple(Math(m.tex, display=True) for m in math)
            if block.block_label == "doc_title":
                title, section = content, None
                heading_path: tuple[str, ...] = ()
            elif block.block_label == "paragraph_title":
                section = content
                heading_path = (title,) if title else ()
            blocks.append(
                Block(
                    kind=kind,
                    page=page.page_index,
                    bbox=block.block_bbox,
                    text="\n\n".join(m.tex for m in math)
                    if kind == "equation" and math
                    else content,
                    math=math,
                    heading_level=_PADDLE_HEADING_LEVEL.get(block.block_label),
                    section_path=heading_path
                    if kind == "heading"
                    else tuple(p for p in (title, section) if p),
                    malformed_math=kind == "equation" and not math,
                )
            )
    return ParsedDocument(page_count=len(document.pages), blocks=tuple(blocks))


def markdown_math(markdown: str) -> tuple[Math, ...]:
    """Every formula in a Markdown string, found by markdown-it's math plugin."""
    return tuple(_math_tokens(_MARKDOWN.parse(markdown)))


# The dollarmath plugin's token types, and whether each is display math.
_MATH_TOKENS = {
    "math_inline": False,
    "math_inline_double": True,
    "math_block": True,
    "math_block_label": True,
}


def _math_tokens(tokens: Sequence[Token]) -> Iterator[Math]:
    for token in tokens:
        if token.type in _MATH_TOKENS:
            yield Math(token.content.strip(), display=_MATH_TOKENS[token.type])
        if token.children:
            yield from _math_tokens(token.children)
