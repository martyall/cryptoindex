"""The manual QA search page (D24), served by `make run` at /search/: a query
box over the real index, using the same query functions as the API and agent
will. Each hit shows the unit's passage with its math rendered, which
channels found it at what rank, and the paragraphs they matched."""

import html
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel

from cryptoindex.core.db import Pool
from cryptoindex.core.embed import Embedder
from cryptoindex.core.latex import formulas
from cryptoindex.evaluation.parse_eval import KATEX_TOOL
from cryptoindex.query.search import (
    ALL_CHANNELS,
    EXCLUDED_BY_DEFAULT,
    GENERATED,
    Hit,
    search,
)

PAGE = Path(__file__).with_name("search_page.html")

# Parser text is untrusted: raw HTML in it is escaped, and only tables pass
# through as the parser's own HTML.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)
_LINES = MarkdownIt("commonmark", {"html": False, "breaks": True}).use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)


def paragraph_html(text: str, block_kind: str | None) -> str:
    """How the page shows a stored paragraph: math elements for KaTeX, and an
    equation the parser left undelimited (D21) as the display math it is."""
    if block_kind == "table":
        return text
    if block_kind in ("algorithm", "code"):
        return f'<div class="lines">{_LINES.render(text)}</div>'
    # None, not the kind: asks whether the text carries its own delimiters,
    # where passing "equation" would answer with the whole text (D21).
    if block_kind == "equation" and not formulas(text, None):
        return f'<div class="math block">{html.escape(text)}</div>'
    return _MARKDOWN.render(text)


class ParagraphView(BaseModel):
    position: int
    page: int
    kind: str | None
    label: str | None
    matched: bool
    html: str


class HitView(BaseModel):
    kinds: list[str]  # this hit's kinds, to filter by (D25)
    name: str
    revision: int
    first_pos: int
    last_pos: int
    anchor_label: str | None
    gloss: str
    score: float
    channels: dict[str, int]
    paragraphs: list[ParagraphView]


def hit_view(hit: Hit) -> HitView:
    matched = set(hit.matched_positions)
    kinds = {hit.anchor_kind, hit.anchor_term} | {p.block_kind for p in hit.paragraphs}
    return HitView(
        kinds=sorted(k for k in kinds if k),
        name=hit.name,
        revision=hit.revision,
        first_pos=hit.first_pos,
        last_pos=hit.last_pos,
        anchor_label=hit.anchor_label,
        gloss=hit.gloss,
        score=hit.score,
        channels={str(c): r for c, r in hit.channels.items()},
        paragraphs=[
            ParagraphView(
                position=p.position,
                page=p.page,
                kind=p.block_kind,
                label=p.block_label,
                matched=p.position in matched,
                html=paragraph_html(p.text, p.block_kind),
            )
            for p in hit.paragraphs
        ],
    )


KATEX_DIR = KATEX_TOOL / "node_modules" / "katex" / "dist"


def mount_search(
    app: FastAPI, pool: Pool, embedder: Embedder, katex_dir: Path = KATEX_DIR
) -> None:
    """Serve the page at /search/ in `app`, the pipeline's own process, so
    searches embed queries with the model the pipeline already holds: one
    copy per process, not one per server. `pool` is the ci_query role
    (Invariant 7). Math renders only once `make parse-eval` has installed
    KaTeX in tools/katex-check."""
    router = APIRouter(prefix="/search")

    @router.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(PAGE)

    @router.get("/api/search")
    async def run(
        q: str,
        generated: bool = True,
        k: int = 10,
        kinds: Annotated[list[str] | None, Query()] = None,
        references: bool = False,
    ) -> list[HitView]:
        """`kinds` keeps only those kinds; references are left out unless
        `references` is set (D25)."""
        hits = await search(
            pool,
            embedder,
            q,
            k=k,
            channels=ALL_CHANNELS if generated else ALL_CHANNELS - GENERATED,
            kinds=kinds or None,
            exclude=() if references else EXCLUDED_BY_DEFAULT,
        )
        return [hit_view(h) for h in hits]

    app.include_router(router)
    app.mount(
        "/search/katex", StaticFiles(directory=katex_dir, check_dir=False), "katex"
    )
