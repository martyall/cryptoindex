"""The manual QA search page (D24), `make search`: a query box over the real
index, using the same query functions as the API and agent will. Each hit
shows the unit's passage with its math rendered, which channels found it at
what rank, and the paragraphs they matched. Binds to 127.0.0.1 only."""

import asyncio
import html
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel

from cryptoindex.core import config
from cryptoindex.core.db import Pool, open_pool
from cryptoindex.core.embed import Embedder, build_embedder, check_embed_model
from cryptoindex.core.latex import formulas
from cryptoindex.evaluation.parse_eval import KATEX_TOOL
from cryptoindex.query.search import ALL_CHANNELS, GENERATED, Hit, search

PAGE = Path(__file__).with_name("search_page.html")
PORT = 8011

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
    return HitView(
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


def create_app(pool: Pool, embedder: Embedder, katex_dir: Path) -> FastAPI:
    app = FastAPI(title="search QA")

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(PAGE)

    @app.get("/api/search")
    async def run(q: str, generated: bool = True, k: int = 10) -> list[HitView]:
        channels = ALL_CHANNELS if generated else ALL_CHANNELS - GENERATED
        hits = await search(pool, embedder, q, k=k, channels=channels)
        return [hit_view(h) for h in hits]

    app.mount("/katex", StaticFiles(directory=katex_dir), name="katex")
    return app


async def serve(settings: config.Settings) -> None:
    """Refuses to start if the index was embedded with another model."""
    pool = await open_pool(settings.query_dsn, 4)
    try:
        async with pool.connection() as conn:
            await check_embed_model(conn, settings.embed_model)  # Invariant 6
        app = create_app(
            pool,
            build_embedder(settings),
            KATEX_TOOL / "node_modules" / "katex" / "dist",
        )
        print(f"search page: http://127.0.0.1:{PORT}/")
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
        )
        await server.serve()
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(serve(config.settings))


if __name__ == "__main__":
    main()
