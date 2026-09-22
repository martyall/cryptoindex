"""Hybrid search (ARCHITECTURE data flow 6): five channels over the current,
ready revisions, fused with Reciprocal Rank Fusion at the level of argument
units. Read-only, on the ci_query role (Invariant 7)."""

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection

from cryptoindex.core.db import Pool
from cryptoindex.core.embed import Embedder
from cryptoindex.core.latex import normalize

QUERY_INSTRUCTION = (
    "Given a question about mathematics, computer science or cryptography,"
    " retrieve the passage of a document that answers it"
)
CHANNEL_K = 50  # candidates each channel contributes before fusion
RRF_K = 60  # the usual Reciprocal Rank Fusion constant


class Channel(StrEnum):
    PARAGRAPH = "paragraph"  # paragraph vectors
    GLOSS = "gloss"  # gloss vectors
    QUESTION = "question"  # question vectors
    FULL_TEXT = "full_text"  # `simple` full-text search (D15)
    LATEX = "latex"  # trigram similarity on normalized LaTeX


ALL_CHANNELS = frozenset(Channel)
# The channels that search what the LLM wrote at ingestion time.
GENERATED = frozenset({Channel.GLOSS, Channel.QUESTION})


@dataclass(frozen=True, slots=True)
class Paragraph:
    position: int
    page: int
    text: str
    block_kind: str | None
    block_label: str | None


@dataclass(frozen=True, slots=True)
class Hit:
    """One argument unit. `channels` maps each channel that found the unit, or
    one of its paragraphs, to its best rank there (1 = first);
    `matched_positions` are the paragraphs that earned those ranks in the
    paragraph-level channels."""

    unit_id: int
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
) -> list[Hit]:
    """The `k` best units for `q` over current, ready revisions, optionally
    only those of `paper_ids`. A unit's score is the sum over channels of
    1 / (RRF_K + rank), where a paragraph-level channel ranks a unit by its
    best paragraph. Read-only; the query is embedded in a thread."""
    wanted = frozenset(channels)
    vector = None
    if wanted & {Channel.PARAGRAPH, Channel.GLOSS, Channel.QUESTION}:
        vectors = await asyncio.to_thread(
            embedder.embed_queries, [q], QUERY_INSTRUCTION
        )
        vector = vectors[0]
    papers = list(paper_ids) if paper_ids is not None else None
    tex = normalize(q) if Channel.LATEX in wanted else None

    async with pool.connection() as conn:
        paragraph_ranks: dict[Channel, list[int]] = {}
        unit_ranks: dict[Channel, list[int]] = {}
        if Channel.PARAGRAPH in wanted and vector is not None:
            paragraph_ranks[Channel.PARAGRAPH] = await _ids(
                conn,
                "SELECT p.id FROM docs.paragraphs p"
                + _CURRENT_P
                + " AND p.emb IS NOT NULL"
                " ORDER BY p.emb <=> %(v)s::halfvec LIMIT %(n)s",
                {"v": vector, "papers": papers, "n": CHANNEL_K},
            )
        if Channel.FULL_TEXT in wanted:
            paragraph_ranks[Channel.FULL_TEXT] = await _ids(
                conn,
                "SELECT p.id FROM docs.paragraphs p"
                + _CURRENT_P
                + " AND p.tsv @@ websearch_to_tsquery('simple', %(q)s)"
                " ORDER BY ts_rank_cd(p.tsv, websearch_to_tsquery('simple', %(q)s))"
                " DESC, p.id LIMIT %(n)s",
                {"q": q, "papers": papers, "n": CHANNEL_K},
            )
        if Channel.LATEX in wanted and tex:
            paragraph_ranks[Channel.LATEX] = await _ids(
                conn,
                "SELECT p.id FROM docs.paragraphs p"
                + _CURRENT_P
                + " AND p.latex_norm IS NOT NULL AND %(t)s <%% p.latex_norm"
                " ORDER BY word_similarity(%(t)s, p.latex_norm) DESC, p.id"
                " LIMIT %(n)s",
                {"t": tex, "papers": papers, "n": CHANNEL_K},
            )
        if Channel.GLOSS in wanted and vector is not None:
            unit_ranks[Channel.GLOSS] = await _ids(
                conn,
                "SELECT u.id FROM docs.units u"
                + _CURRENT_U
                + " AND u.emb_gloss IS NOT NULL"
                " ORDER BY u.emb_gloss <=> %(v)s::halfvec LIMIT %(n)s",
                {"v": vector, "papers": papers, "n": CHANNEL_K},
            )
        if Channel.QUESTION in wanted and vector is not None:
            unit_ranks[Channel.QUESTION] = await _ids(
                conn,
                "SELECT u.id FROM docs.unit_questions q"
                " JOIN docs.units u ON u.id = q.unit_id"
                + _CURRENT_U
                + " AND q.emb IS NOT NULL"
                " GROUP BY u.id ORDER BY min(q.emb <=> %(v)s::halfvec) LIMIT %(n)s",
                {"v": vector, "papers": papers, "n": CHANNEL_K},
            )

        found = {pid for ids in paragraph_ranks.values() for pid in ids}
        units_of = await _enclosing_units(conn, found)
        scores: dict[int, float] = {}
        best: dict[int, dict[Channel, int]] = {}
        # Per unit and paragraph-level channel, the paragraph that earned the
        # unit its rank there. Vector channels return their nearest
        # paragraphs however far, so "a channel returned it" says little.
        best_at: dict[int, dict[Channel, int]] = {}
        for channel, ids in paragraph_ranks.items():
            for rank, pid in enumerate(ids, start=1):
                for unit_id, position in units_of.get(pid, []):
                    ranks = best.setdefault(unit_id, {})
                    if channel not in ranks:  # ids are in rank order
                        ranks[channel] = rank
                        best_at.setdefault(unit_id, {})[channel] = position
        for channel, ids in unit_ranks.items():
            for rank, unit_id in enumerate(ids, start=1):
                best.setdefault(unit_id, {})[channel] = rank
        for unit_id, ranks in best.items():
            scores[unit_id] = sum(1.0 / (RRF_K + r) for r in ranks.values())
        top = sorted(scores, key=lambda u: (-scores[u], u))[:k]
        matched = {u: set(at.values()) for u, at in best_at.items()}
        return await _hits(conn, top, scores, best, matched)


# Only the current revision of each document, once it is ready.
_CURRENT_P: LiteralString = (
    " JOIN docs.revisions r ON r.id = p.revision_id"
    " WHERE r.is_current AND r.stage = 'ready'"
    " AND (%(papers)s::uuid[] IS NULL OR r.paper_id = ANY(%(papers)s::uuid[]))"
)
_CURRENT_U: LiteralString = (
    " JOIN docs.revisions r ON r.id = u.revision_id"
    " WHERE r.is_current AND r.stage = 'ready'"
    " AND (%(papers)s::uuid[] IS NULL OR r.paper_id = ANY(%(papers)s::uuid[]))"
)


async def _ids(conn: AsyncConnection, sql: LiteralString, params: dict) -> list[int]:
    cur = await conn.execute(sql, params)
    return [row[0] for row in await cur.fetchall()]


async def _enclosing_units(
    conn: AsyncConnection, paragraph_ids: set[int]
) -> dict[int, list[tuple[int, int]]]:
    """For each paragraph, the units containing it and its position."""
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
    unit_ids: list[int],
    scores: dict[int, float],
    best: dict[int, dict[Channel, int]],
    matched: dict[int, set[int]],
) -> list[Hit]:
    if not unit_ids:
        return []
    cur = await conn.execute(
        "SELECT u.id, r.paper_id, pa.name, r.revision, u.first_pos, u.last_pos,"
        "  u.anchor_label, u.gloss,"
        "  array_agg(array[p.position, p.page]::int[] ORDER BY p.position),"
        "  array_agg(p.text ORDER BY p.position),"
        "  array_agg(p.block_kind ORDER BY p.position),"
        "  array_agg(p.block_label ORDER BY p.position)"
        " FROM docs.units u"
        " JOIN docs.revisions r ON r.id = u.revision_id"
        " JOIN docs.papers pa ON pa.id = r.paper_id"
        " JOIN docs.paragraphs p ON p.revision_id = u.revision_id"
        "  AND p.position BETWEEN u.first_pos AND u.last_pos"
        " WHERE u.id = ANY(%s) GROUP BY u.id, r.id, pa.id",
        (unit_ids,),
    )
    rows = {row[0]: row for row in await cur.fetchall()}
    hits = []
    for unit_id in unit_ids:
        (
            _,
            paper_id,
            name,
            revision,
            first,
            last,
            anchor,
            gloss,
            where,
            texts,
            kinds,
            labels,
        ) = rows[unit_id]
        hits.append(
            Hit(
                unit_id=unit_id,
                paper_id=paper_id,
                name=name,
                revision=revision,
                first_pos=first,
                last_pos=last,
                anchor_label=anchor,
                gloss=gloss,
                score=scores[unit_id],
                channels=best[unit_id],
                matched_positions=tuple(sorted(matched.get(unit_id, ()))),
                paragraphs=tuple(
                    Paragraph(pos, page, text, kind, label)
                    for (pos, page), text, kind, label in zip(
                        where, texts, kinds, labels, strict=True
                    )
                ),
            )
        )
    return hits
