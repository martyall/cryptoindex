import hashlib
from dataclasses import dataclass

from cryptoindex.ingest.document import BBox, Block, BlockKind, ParsedDocument

# Headings become section paths, not paragraphs; furniture is page decoration.
_NOT_PARAGRAPHS: frozenset[BlockKind] = frozenset({"heading", "furniture"})
# Stored as block_kind; plain text is stored as NULL.
_UNMARKED: frozenset[BlockKind] = frozenset({"text"})


@dataclass(frozen=True, slots=True)
class Paragraph:
    position: int
    page: int
    bbox: BBox
    section_path: str
    text: str
    content_hash: str
    block_kind: str | None


def paragraphs_from(document: ParsedDocument) -> list[Paragraph]:
    """The document's paragraphs in reading order: every block except headings,
    page furniture, and empty blocks (figures without text). Deterministic, so
    the same parser output always yields the same positions and hashes
    (Invariant 5)."""
    kept = [b for b in document.blocks if _is_paragraph(b)]
    return [
        Paragraph(
            position=position,
            page=block.page,
            bbox=block.bbox,
            section_path=" > ".join(block.section_path),
            text=block.text,
            content_hash=content_hash(block.text),
            block_kind=None if block.kind in _UNMARKED else block.kind,
        )
        for position, block in enumerate(kept)
    ]


def content_hash(text: str) -> str:
    """Hash of the text with whitespace runs collapsed, so reflowed lines keep
    the same hash."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _is_paragraph(block: Block) -> bool:
    return block.kind not in _NOT_PARAGRAPHS and bool(block.text.strip())
