from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import LiteralString

import httpx
import psycopg
import pytest
from psycopg.rows import TupleRow

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool, open_pool
from cryptoindex.core.embed import (
    EmbedModelMismatchError,
    FakeEmbedder,
    Vectors,
    check_embed_model,
)
from cryptoindex.core.llm import FakeLLM
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.evaluation.search_page import create_app, paragraph_html
from cryptoindex.ingest.embedding import embed_stage
from cryptoindex.ingest.paragraphs import content_hash
from cryptoindex.ingest.parsers import StubParser
from cryptoindex.ingest.stages import StageContext
from cryptoindex.query.search import (
    ALL_CHANNELS,
    GENERATED,
    QUERY_INSTRUCTION,
    Channel,
    search,
)

# FakeEmbedder gives a query the same vector as a document whose text is the
# instruction, a newline and the query, so a vector channel can be made to
# match exactly.
ASKED = "how is a field extension built"
MATCHES_QUERY = f"{QUERY_INSTRUCTION}\n{ASKED}"

PARAGRAPHS = [
    ("Theorem 1. Every nonconstant polynomial has a root.", None, "Theorem 1"),
    (
        "[7] Bootle et al. Efficient zero-knowledge nonconstant arguments.",
        "reference",
        None,
    ),
    (r"$$\mathbb Z_{2} [x] / \langle p \rangle$$", "equation", None),
    ("A ring is a set with two operations.", None, None),
    (MATCHES_QUERY, None, None),
]
# A reference paragraph (position 1) is in no unit: it is not glossed (D25).
UNITS = [  # first, last, anchor kind and term, gloss, questions
    (0, 0, ("theorem", "theorem"), "Kronecker's theorem on roots.", ["Why roots?"]),
    (2, 3, None, "Defines rings.", [MATCHES_QUERY]),
    (4, 4, None, MATCHES_QUERY, ["Something else?"]),
]


def query(settings: Settings, sql: LiteralString) -> list[TupleRow]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(sql).fetchall()


@pytest.fixture
def work_id(settings: Settings, seed: Callable[[list[Stage]], list[int]]) -> int:
    """A revision at `embed`, claimed, with paragraphs, units and questions."""
    (work_id,) = seed([Stage.EMBED])
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE docs.revisions SET locked_at = now() WHERE id = %s", (work_id,)
        )
        for pos, (text, kind, label) in enumerate(PARAGRAPHS):
            conn.execute(
                "INSERT INTO docs.paragraphs (revision_id, position, page, bbox,"
                " text, content_hash, block_kind, block_label)"
                " VALUES (%s, %s, 0, '{0,0,1,1}', %s, %s, %s, %s)",
                (work_id, pos, text, content_hash(text), kind, label),
            )
        for first, last, anchor, gloss, questions in UNITS:
            row = conn.execute(
                "INSERT INTO docs.units (revision_id, first_pos, last_pos,"
                " anchor_label, anchor_pos, anchor_kind, anchor_term, gloss,"
                " gloss_model, prompt_version, input_hash)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'm', 'gloss-v3', 'h')"
                " RETURNING id",
                (
                    work_id,
                    first,
                    last,
                    *(("Theorem 1", first, *anchor) if anchor else (None,) * 4),
                    gloss,
                ),
            ).fetchone()
            assert row is not None
            for q in questions:
                conn.execute(
                    "INSERT INTO docs.unit_questions (unit_id, question)"
                    " VALUES (%s, %s)",
                    (row[0], q),
                )
    return work_id


def ctx(pool: Pool, settings: Settings, embedder: FakeEmbedder) -> StageContext:
    return StageContext(
        pool=pool,
        settings=settings,
        parser=StubParser(),
        llm=FakeLLM({}),
        embedder=embedder,
    )


@pytest.fixture
async def query_pool(settings: Settings) -> AsyncIterator[Pool]:
    p = await open_pool(settings.query_dsn, 2)
    try:
        yield p
    finally:
        await p.close()


async def test_embed_stage_fills_vectors_and_latex_and_makes_ready(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    await embed_stage(RevisionId(work_id), ctx(pool, settings, FakeEmbedder()))
    assert query(
        settings,
        "SELECT count(*) FILTER (WHERE emb IS NULL),"
        " count(*) FILTER (WHERE latex_norm IS NOT NULL) FROM docs.paragraphs",
    ) == [(0, 1)]  # the reference is embedded too, and has no math
    assert query(
        settings, "SELECT count(*) FROM docs.units WHERE emb_gloss IS NULL"
    ) == [(0,)]
    assert query(
        settings, "SELECT count(*) FROM docs.unit_questions WHERE emb IS NULL"
    ) == [(0,)]
    assert query(settings, "SELECT stage, is_current FROM docs.revisions") == [
        ("ready", True)
    ]
    assert query(settings, "SELECT value FROM docs.meta WHERE key = 'embed_model'") == [
        ("fake-embedder",)
    ]


async def test_embed_stage_embeds_only_what_has_no_vector(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    await embed_stage(RevisionId(work_id), ctx(pool, settings, FakeEmbedder()))
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE docs.units SET emb_gloss = NULL WHERE first_pos = 2;"
            " UPDATE docs.revisions SET stage = 'embed', locked_at = now()"
        )
    counted = CountingEmbedder()
    await embed_stage(RevisionId(work_id), ctx(pool, settings, counted))
    assert counted.texts == ["Defines rings."]


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed_documents(self, texts: list[str]) -> Vectors:
        self.texts += texts
        return super().embed_documents(texts)


async def test_a_different_embedding_model_is_refused(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    await embed_stage(RevisionId(work_id), ctx(pool, settings, FakeEmbedder()))
    async with pool.connection() as conn:
        with pytest.raises(EmbedModelMismatchError, match="fake-embedder"):
            await check_embed_model(conn, "Qwen/Qwen3-Embedding-0.6B")
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute("UPDATE docs.revisions SET stage = 'embed', locked_at = now()")
    other = FakeEmbedder(model="other-embedder")
    with pytest.raises(EmbedModelMismatchError):
        await embed_stage(RevisionId(work_id), ctx(pool, settings, other))


async def indexed(settings: Settings, pool: Pool, work_id: int) -> None:
    await embed_stage(RevisionId(work_id), ctx(pool, settings, FakeEmbedder()))


async def test_each_channel_finds_its_match(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    embedder = FakeEmbedder()

    async def first(q: str, channel: Channel) -> tuple[int, int]:
        (hit, *_) = await search(query_pool, embedder, q, channels={channel})
        return hit.first_pos, hit.channels[channel]

    assert await first("nonconstant polynomial", Channel.FULL_TEXT) == (0, 1)
    assert await first(r"\mathbb{Z}_2[x]", Channel.LATEX) == (2, 1)
    assert await first(ASKED, Channel.PARAGRAPH) == (4, 1)
    assert await first(ASKED, Channel.GLOSS) == (4, 1)
    assert await first(ASKED, Channel.QUESTION) == (2, 1)


async def test_fusion_ranks_units_found_by_more_channels_higher(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    hits = await search(query_pool, FakeEmbedder(), ASKED)
    assert hits[0].first_pos == 4
    assert {Channel.PARAGRAPH, Channel.GLOSS} <= set(hits[0].channels)
    assert hits[0].paragraphs[0].text == MATCHES_QUERY
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    without = await search(
        query_pool, FakeEmbedder(), ASKED, channels=ALL_CHANNELS - GENERATED
    )
    assert all(not (set(h.channels) & GENERATED) for h in without)


async def test_a_paragraph_match_expands_to_its_unit(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    (hit,) = await search(
        query_pool, FakeEmbedder(), "nonconstant", channels={Channel.FULL_TEXT}
    )
    assert (hit.first_pos, hit.last_pos, hit.matched_positions) == (0, 0, (0,))
    assert [p.block_label for p in hit.paragraphs] == ["Theorem 1"]


async def test_only_current_ready_revisions_are_searched(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute("UPDATE docs.revisions SET is_current = false")
    assert await search(query_pool, FakeEmbedder(), "nonconstant") == []


async def test_search_cannot_write(query_pool: Pool) -> None:
    async with query_pool.connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM docs.units")


async def test_search_page_returns_rendered_hits(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int, tmp_path: Path
) -> None:
    await indexed(settings, pool, work_id)
    app = create_app(query_pool, FakeEmbedder(), tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/api/search", params={"q": "nonconstant"})
        without = await client.get(
            "/api/search", params={"q": ASKED, "generated": "false"}
        )
        refs = await client.get(
            "/api/search", params={"q": "Bootle", "references": "true"}
        )
        kinds = await client.get(
            "/api/search", params={"q": "nonconstant", "kinds": "theorem"}
        )
    (hit, *_) = response.json()
    assert hit["anchor_label"] == "Theorem 1" and hit["first_pos"] == 0
    assert hit["paragraphs"][0]["matched"]  # the full-text match
    equation = next(h for h in response.json() if h["first_pos"] == 2)
    assert 'class="math block"' in equation["paragraphs"][0]["html"]
    assert all(not {"gloss", "question"} & set(h["channels"]) for h in without.json())


def test_an_undelimited_equation_is_shown_as_math() -> None:
    raw = r"\alpha^{2}+1=0 <b>"
    assert paragraph_html(raw, "equation") == (
        r'<div class="math block">\alpha^{2}+1=0 &lt;b&gt;</div>'
    )
    assert "<b>" not in paragraph_html("<b>bold</b> $x$", None)


async def test_references_are_left_out_unless_asked_for(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    embedder = FakeEmbedder()
    text_only = {Channel.FULL_TEXT}
    assert await search(query_pool, embedder, "Bootle", channels=text_only) == []
    (hit,) = await search(
        query_pool, embedder, "Bootle", channels=text_only, exclude=()
    )
    # A reference is in no unit, so it is a hit on its own.
    assert (hit.unit_id, hit.first_pos, hit.matched_positions) == (None, 1, (1,))
    assert hit.paragraphs[0].block_kind == "reference"


async def test_kinds_filter_paragraphs_and_units(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    embedder = FakeEmbedder()
    (hit,) = await search(query_pool, embedder, "nonconstant", kinds={"theorem"})
    assert (hit.anchor_kind, hit.anchor_term) == ("theorem", "theorem")
    assert await search(query_pool, embedder, "nonconstant", kinds={"lemma"}) == []
    (equation,) = await search(
        query_pool, embedder, r"\mathbb{Z}_2[x]", kinds={"equation"}
    )
    assert equation.first_pos == 2  # the unit holding the matching equation
