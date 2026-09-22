from collections.abc import AsyncIterator, Callable
from typing import LiteralString

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
    (r"$$\mathbb Z_{2} [x] / \langle p \rangle$$", "equation", None),
    ("A ring is a set with two operations.", None, None),
    (MATCHES_QUERY, None, None),
]
UNITS = [  # first, last, gloss, questions
    (0, 1, "Kronecker's theorem on roots.", ["Why do roots exist?"]),
    (2, 2, "Defines rings.", [MATCHES_QUERY]),
    (3, 3, MATCHES_QUERY, ["Something else?"]),
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
        for first, last, gloss, questions in UNITS:
            row = conn.execute(
                "INSERT INTO docs.units (revision_id, first_pos, last_pos, gloss,"
                " gloss_model, prompt_version, input_hash)"
                " VALUES (%s, %s, %s, %s, 'm', 'gloss-v2', 'h') RETURNING id",
                (work_id, first, last, gloss),
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
    ) == [(0, 1)]
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
    assert await first(r"\mathbb{Z}_2[x]", Channel.LATEX) == (0, 1)
    assert await first(ASKED, Channel.PARAGRAPH) == (3, 1)
    assert await first(ASKED, Channel.GLOSS) == (3, 1)
    assert await first(ASKED, Channel.QUESTION) == (2, 1)


async def test_fusion_ranks_units_found_by_more_channels_higher(
    settings: Settings, pool: Pool, query_pool: Pool, work_id: int
) -> None:
    await indexed(settings, pool, work_id)
    hits = await search(query_pool, FakeEmbedder(), ASKED)
    assert hits[0].first_pos == 3
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
    assert (hit.first_pos, hit.last_pos, hit.matched_positions) == (0, 1, (0,))
    assert [p.block_label for p in hit.paragraphs] == ["Theorem 1", None]


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
