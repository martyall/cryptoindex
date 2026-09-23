"""Hybrid search (ARCHITECTURE data flow 6): five channels over the current,
ready revisions, fused with Reciprocal Rank Fusion per argument unit, or per
paragraph that belongs to none. Read-only, on the ci_query role
(Invariant 7)."""

import asyncio
import dataclasses
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, LiteralString
from uuid import UUID

from psycopg import AsyncConnection, sql

from cryptoindex.core.db import Pool
from cryptoindex.core.embed import PRIMARY, Embedder, VectorSet
from cryptoindex.core.latex import normalize

QUERY_INSTRUCTION = (
    "Given a question about mathematics, computer science or cryptography,"
    " retrieve the passage of a document that answers it"
)
CHANNEL_K = 50  # candidates each channel contributes before fusion
RRF_K = 60  # the usual Reciprocal Rank Fusion constant


class Channel(StrEnum):
    PARAGRAPH = "paragraph"
    GLOSS = "gloss"
    QUESTION = "question"
    FULL_TEXT = "full_text"  # the `simple` configuration (D15)
    LATEX = "latex"  # trigram similarity on normalized LaTeX


ALL_CHANNELS = frozenset(Channel)
# The channels that search what the LLM wrote at ingestion time.
GENERATED = frozenset({Channel.GLOSS, Channel.QUESTION})
# Kinds are an open vocabulary (D25): a paragraph's structural kind from the
# parser (equation, algorithm, table, reference, …) and a unit's anchor kind
# and the document's own word for it (theorem, lemma, protocol, …).
# References are indexed and searchable, but not what anyone means by
# default, so they are dropped unless the caller passes `exclude=()`.
EXCLUDED_BY_DEFAULT = frozenset({"reference"})


@dataclass(frozen=True, slots=True)
class Paragraph:
    position: int
    page: int
    text: str
    block_kind: str | None
    block_label: str | None


@dataclass(frozen=True, slots=True)
class Hit:
    """One argument unit, or a single paragraph that belongs to none (a
    footnote, caption or reference). `channels` maps each channel that found
    it to its best rank there (1 = first); `matched_positions` are the
    paragraphs that earned those ranks in the paragraph-level channels."""

    unit_id: int | None
    anchor_kind: str | None
    anchor_term: str | None
    paper_id: UUID
    name: str
    revision: int
    first_pos: int
    last_pos: int
    anchor_label: str | None
    gloss: str
    score: float
    channels: dict[Channel, int]
    matched_positions: tuple[int, ...]
    paragraphs: tuple[Paragraph, ...] = field(default=())


async def search(
    pool: Pool,
    embedder: Embedder,
    q: str,
    k: int = 10,
    channels: Collection[Channel] = ALL_CHANNELS,
    paper_ids: Sequence[UUID] | None = None,
    kinds: Collection[str] | None = None,
    exclude: Collection[str] = EXCLUDED_BY_DEFAULT,
    vectors: VectorSet = PRIMARY,
) -> list[Hit]:
    """The `k` best units, or paragraphs belonging to none, for `q` over
    current, ready revisions, optionally only those of `paper_ids`. A score
    is the sum over channels of 1 / (RRF_K + rank), where a paragraph-level
    channel ranks a unit by its best paragraph. Read-only; the query is
    embedded in a thread.

    `kinds`, if given, keeps only paragraphs and units of those kinds, and
    `exclude` drops them; both take a paragraph's structural kind, a unit's
    anchor kind, or a unit's anchor term (D25). `exclude` wins, so a kind in
    both is dropped. A paragraph kept by kind still expands to its enclosing
    unit, whatever that unit's kind.

    The vector channels read `vectors`, whose model `embedder` must be
    (D27); the caller checks that against `docs.meta` once, at startup."""
    wanted = frozenset(channels)
    vector = None
    if wanted & {Channel.PARAGRAPH, Channel.GLOSS, Channel.QUESTION}:
        embedded = await asyncio.to_thread(
            embedder.embed_queries, [q], QUERY_INSTRUCTION
        )
        vector = embedded[0]
    papers = list(paper_ids) if paper_ids is not None else None
    limits: dict[str, object] = {
        "papers": papers,
        "kinds": sorted(kinds) if kinds is not None else None,
        "exclude": sorted(exclude),
    }
    tex = normalize(q) if Channel.LATEX in wanted else None

    async with pool.connection() as conn:
        # pgvector 0.8: without this, an HNSW scan filtered by kind or
        # document can return fewer rows than asked for.
        await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        paragraph_ranks: dict[Channel, list[int]] = {}
        unit_ranks: dict[Channel, list[int]] = {}
        if Channel.PARAGRAPH in wanted and vector is not None:
            paragraph_ranks[Channel.PARAGRAPH] = await _ids(
                conn,
                sql.SQL(
                    "SELECT p.id FROM docs.paragraphs p"
                    + _CURRENT_P
                    + " AND p.{col} IS NOT NULL"
                    " ORDER BY p.{col} <=> %(v)s::halfvec LIMIT %(n)s"
                ).format(col=sql.Identifier(vectors.paragraph)),
                {"v": vector, "n": CHANNEL_K, **limits},
            )
        if Channel.FULL_TEXT in wanted:
            paragraph_ranks[Channel.FULL_TEXT] = await _ids(
                conn,
                "SELECT p.id FROM docs.paragraphs p"
                + _CURRENT_P
                + " AND p.tsv @@ websearch_to_tsquery('simple', %(q)s)"
                " ORDER BY ts_rank_cd(p.tsv, websearch_to_tsquery('simple', %(q)s))"
                " DESC, p.id LIMIT %(n)s",
                {"q": q, "n": CHANNEL_K, **limits},
            )
        if Channel.LATEX in wanted and tex:
            paragraph_ranks[Channel.LATEX] = await _ids(
                conn,
                "SELECT p.id FROM docs.paragraphs p"
                + _CURRENT_P
                + " AND p.latex_norm IS NOT NULL AND %(t)s <%% p.latex_norm"
                " ORDER BY word_similarity(%(t)s, p.latex_norm) DESC, p.id"
                " LIMIT %(n)s",
                {"t": tex, "n": CHANNEL_K, **limits},
            )
        if Channel.GLOSS in wanted and vector is not None:
            unit_ranks[Channel.GLOSS] = await _ids(
                conn,
                sql.SQL(
                    "SELECT u.id FROM docs.units u"
                    + _CURRENT_U
                    + " AND u.{col} IS NOT NULL"
                    " ORDER BY u.{col} <=> %(v)s::halfvec LIMIT %(n)s"
                ).format(col=sql.Identifier(vectors.gloss)),
                {"v": vector, "n": CHANNEL_K, **limits},
            )
        if Channel.QUESTION in wanted and vector is not None:
            unit_ranks[Channel.QUESTION] = await _ids(
                conn,
                sql.SQL(
                    "SELECT u.id FROM docs.unit_questions q"
                    " JOIN docs.units u ON u.id = q.unit_id"
                    + _CURRENT_U
                    + " AND q.{col} IS NOT NULL"
                    " GROUP BY u.id"
                    " ORDER BY min(q.{col} <=> %(v)s::halfvec) LIMIT %(n)s"
                ).format(col=sql.Identifier(vectors.question)),
                {"v": vector, "n": CHANNEL_K, **limits},
            )

        found = {pid for ids in paragraph_ranks.values() for pid in ids}
        units_of = await _enclosing_units(conn, found)
        best: dict[Item, dict[Channel, int]] = {}
        # Per item and paragraph-level channel, the paragraph that earned the
        # rank. Vector channels return their nearest paragraphs however far,
        # so "a channel returned it" says little.
        best_at: dict[Item, dict[Channel, int]] = {}
        for channel, ids in paragraph_ranks.items():
            for rank, pid in enumerate(ids, start=1):
                # A paragraph in no unit stands alone; its matched position
                # comes from its own row, not from this bookkeeping.
                enclosing = units_of.get(pid) or [(None, None)]
                for unit_id, position in enclosing:
                    item: Item = ("unit", unit_id) if unit_id else ("paragraph", pid)
                    ranks = best.setdefault(item, {})
                    if channel not in ranks:  # ids are in rank order
                        ranks[channel] = rank
                        if position is not None:
                            best_at.setdefault(item, {})[channel] = position
        for channel, ids in unit_ranks.items():
            for rank, unit_id in enumerate(ids, start=1):
                best.setdefault(("unit", unit_id), {})[channel] = rank
        scores = {
            item: sum(1.0 / (RRF_K + r) for r in ranks.values())
            for item, ranks in best.items()
        }
        top = sorted(scores, key=lambda i: (-scores[i], i))[:k]
        matched = {i: set(at.values()) for i, at in best_at.items()}
        return await _hits(conn, top, scores, best, matched)


_CURRENT_P: LiteralString = (
    " JOIN docs.revisions r ON r.id = p.revision_id"
    " WHERE r.is_current AND r.stage = 'ready'"
    " AND (%(papers)s::uuid[] IS NULL OR r.paper_id = ANY(%(papers)s::uuid[]))"
    " AND (%(kinds)s::text[] IS NULL OR p.block_kind = ANY(%(kinds)s::text[]))"
    " AND NOT coalesce(p.block_kind = ANY(%(exclude)s::text[]), false)"
)
_CURRENT_U: LiteralString = (
    " JOIN docs.revisions r ON r.id = u.revision_id"
    " WHERE r.is_current AND r.stage = 'ready'"
    " AND (%(papers)s::uuid[] IS NULL OR r.paper_id = ANY(%(papers)s::uuid[]))"
    " AND (%(kinds)s::text[] IS NULL"
    "      OR u.anchor_kind = ANY(%(kinds)s::text[])"
    "      OR u.anchor_term = ANY(%(kinds)s::text[]))"
    " AND NOT coalesce(u.anchor_kind = ANY(%(exclude)s::text[]), false)"
    " AND NOT coalesce(u.anchor_term = ANY(%(exclude)s::text[]), false)"
)


Item = tuple[Literal["unit", "paragraph"], int]


async def _ids(
    conn: AsyncConnection, query: LiteralString | sql.Composed, params: dict
) -> list[int]:
    cur = await conn.execute(query, params)
    return [row[0] for row in await cur.fetchall()]


async def _enclosing_units(
    conn: AsyncConnection, paragraph_ids: set[int]
) -> dict[int, list[tuple[int, int]]]:
    """Units may overlap, so a paragraph can be in several; one in no unit is
    absent."""
    if not paragraph_ids:
        return {}
    cur = await conn.execute(
        "SELECT p.id, u.id, p.position FROM docs.paragraphs p"
        " JOIN docs.units u ON u.revision_id = p.revision_id"
        "  AND p.position BETWEEN u.first_pos AND u.last_pos"
        " WHERE p.id = ANY(%s)",
        (list(paragraph_ids),),
    )
    out: dict[int, list[tuple[int, int]]] = {}
    for pid, unit_id, position in await cur.fetchall():
        out.setdefault(pid, []).append((unit_id, position))
    return out


async def _hits(
    conn: AsyncConnection,
    items: list[Item],
    scores: dict[Item, float],
    best: dict[Item, dict[Channel, int]],
    matched: dict[Item, set[int]],
) -> list[Hit]:
    rows = await _unit_rows(conn, [i for kind, i in items if kind == "unit"])
    rows |= await _paragraph_rows(conn, [i for kind, i in items if kind == "paragraph"])
    return [
        dataclasses.replace(
            rows[item],
            score=scores[item],
            channels=best[item],
            matched_positions=tuple(sorted(matched.get(item, ())))
            or rows[item].matched_positions,
        )
        for item in items
    ]


_UNITS = (
    "SELECT u.id, r.paper_id, pa.name, r.revision, u.first_pos, u.last_pos,"
    "  u.anchor_label, u.anchor_kind, u.anchor_term, u.gloss,"
    "  array_agg(array[p.position, p.page]::int[] ORDER BY p.position),"
    "  array_agg(p.text ORDER BY p.position),"
    "  array_agg(p.block_kind ORDER BY p.position),"
    "  array_agg(p.block_label ORDER BY p.position)"
    " FROM docs.units u"
    " JOIN docs.revisions r ON r.id = u.revision_id"
    " JOIN docs.papers pa ON pa.id = r.paper_id"
    " JOIN docs.paragraphs p ON p.revision_id = u.revision_id"
    "  AND p.position BETWEEN u.first_pos AND u.last_pos"
    " WHERE u.id = ANY(%s) GROUP BY u.id, r.id, pa.id"
)


async def _unit_rows(conn: AsyncConnection, unit_ids: list[int]) -> dict[Item, Hit]:
    if not unit_ids:
        return {}
    cur = await conn.execute(_UNITS, (unit_ids,))
    out: dict[Item, Hit] = {}
    for row in await cur.fetchall():
        (
            unit_id,
            paper_id,
            name,
            revision,
            first,
            last,
            anchor,
            anchor_kind,
            anchor_term,
            gloss,
            where,
            texts,
            block_kinds,
            labels,
        ) = row
        out[("unit", unit_id)] = Hit(
            unit_id=unit_id,
            anchor_kind=anchor_kind,
            anchor_term=anchor_term,
            paper_id=paper_id,
            name=name,
            revision=revision,
            first_pos=first,
            last_pos=last,
            anchor_label=anchor,
            gloss=gloss,
            score=0.0,
            channels={},
            matched_positions=(),
            paragraphs=tuple(
                Paragraph(pos, page, text, kind, label)
                for (pos, page), text, kind, label in zip(
                    where, texts, block_kinds, labels, strict=True
                )
            ),
        )
    return out


async def _paragraph_rows(
    conn: AsyncConnection, paragraph_ids: list[int]
) -> dict[Item, Hit]:
    """Paragraphs that belong to no unit, each its own hit."""
    if not paragraph_ids:
        return {}
    cur = await conn.execute(
        "SELECT p.id, r.paper_id, pa.name, r.revision, p.position, p.page,"
        "  p.text, p.block_kind, p.block_label"
        " FROM docs.paragraphs p"
        " JOIN docs.revisions r ON r.id = p.revision_id"
        " JOIN docs.papers pa ON pa.id = r.paper_id"
        " WHERE p.id = ANY(%s)",
        (paragraph_ids,),
    )
    return {
        ("paragraph", pid): Hit(
            unit_id=None,
            anchor_kind=None,
            anchor_term=None,
            paper_id=paper_id,
            name=name,
            revision=revision,
            first_pos=position,
            last_pos=position,
            anchor_label=label,
            gloss="",
            score=0.0,
            channels={},
            matched_positions=(position,),
            paragraphs=(Paragraph(position, page, text, kind, label),),
        )
        for pid, paper_id, name, revision, position, page, text, kind, label in (
            await cur.fetchall()
        )
    }
