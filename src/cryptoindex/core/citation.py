from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Citation:
    """D12. Per-source field meanings are in docs/INTERFACES.md (Citation)."""

    source_type: str
    source_id: str
    version: str
    locator: str
    quote: str


class CitationSource(Protocol):
    """Resolves citations of one `source_type` to stored original text, never
    to glosses (Invariant 3). `fetch` raises LookupError when the source,
    version or locator does not resolve."""

    source_type: str

    def fetch(self, source_id: str, version: str, locator: str) -> str: ...

    def render(self, c: Citation) -> str: ...
