"""Answering a question (D28-D30): the tools an answer handler may
call, the handler interface, and the citation checker that runs after it.

A handler only chooses which tools to call and writes the answer. The tools
record every paragraph they return, and the checker keeps a citation only if
it names one of those paragraphs; a citation is then shown by its location,
read from the stored paragraph (D30). Read-only, on the ci_query role
(Invariant 7)."""

import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Sequence
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import BaseModel

from cryptoindex.core.db import Pool
from cryptoindex.core.embed import PRIMARY, Embedder, VectorSet
from cryptoindex.query.search import EXCLUDED_BY_DEFAULT, search

log = logging.getLogger(__name__)

SEARCH_K = 6
MAX_PARAGRAPHS = 20  # per get_paragraphs call, to keep one reply readable

EventKind = Literal["tool_call", "tool_result", "answer", "final", "error"]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: EventKind
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class Location:
    """Where a person finds a paragraph: all read from stored data (D30)."""

    document: str
    page: int  # 1-based index of the page in the PDF file, not its printed label
    section: tuple[str, ...]
    label: str | None  # its block label, or the anchor of a unit holding it

    def render(self) -> str:
        parts = [self.document, f"p. {self.page}"]
        if self.section:
            parts.append(self.section[-1])
        if self.label and self.label not in parts:
            parts.append(self.label)
        return ", ".join(parts)


class Citation(BaseModel):
    marker: str  # as it appears in the answer, e.g. "[1]"
    paragraph_id: int


class AnswerReply(BaseModel):
    """What a handler returns: prose citing its main points (D29)."""

    answer: str
    citations: list[Citation]


ANSWER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marker": {"type": "string"},
                    "paragraph_id": {"type": "integer"},
                },
                "required": ["marker", "paragraph_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}


@dataclass
class Tools:
    """The tools of one question's session. Every paragraph a tool returns is
    added to `retrieved`, which is what the checker accepts citations of."""

    pool: Pool
    embedder: Embedder
    vectors: VectorSet = PRIMARY
    retrieved: set[int] = field(default_factory=set)

    async def search(self, q: str, kinds: Collection[str] | None = None) -> dict:
        hits = await search(
            self.pool,
            self.embedder,
            q,
            k=SEARCH_K,
            kinds=kinds or None,
            exclude=EXCLUDED_BY_DEFAULT,
            vectors=self.vectors,
        )
        rows = [await self._rows(h.paper_id, h.first_pos, h.last_pos) for h in hits]
        where = await self._locations([r[0] for unit in rows for r in unit])
        return {
            "results": [
                {
                    "unit_id": h.unit_id,
                    "document_id": str(h.paper_id),
                    "document": h.name,
                    "paragraphs": [_paragraph(r, where) for r in unit],
                }
                for h, unit in zip(hits, rows, strict=True)
            ]
        }

    async def get_unit(self, unit_id: int) -> dict:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT r.paper_id, u.first_pos, u.last_pos FROM docs.units u"
                " JOIN docs.revisions r ON r.id = u.revision_id"
                " WHERE u.id = %s AND r.is_current AND r.stage = 'ready'",
                (unit_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return {"error": f"no unit {unit_id} in the index"}
        return await self.get_paragraphs(str(row[0]), row[1], row[2])

    async def get_paragraphs(self, document_id: str, first: int, last: int) -> dict:
        last = min(last, first + MAX_PARAGRAPHS - 1)
        rows = await self._rows(UUID(document_id), first, last)
        if not rows:
            return {"error": f"no paragraphs {first}-{last} in {document_id}"}
        where = await self._locations([r[0] for r in rows])
        return {"paragraphs": [_paragraph(r, where) for r in rows]}

    async def get_document(self, document_id: str) -> dict:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT pa.name, max(p.position) FROM docs.papers pa"
                " JOIN docs.revisions r ON r.paper_id = pa.id"
                "  AND r.is_current AND r.stage = 'ready'"
                " JOIN docs.paragraphs p ON p.revision_id = r.id"
                " WHERE pa.id = %s GROUP BY pa.id",
                (UUID(document_id),),
            )
            row = await cur.fetchone()
            if row is None:
                return {"error": f"no document {document_id} in the index"}
            name, last = row
            cur = await conn.execute(
                "SELECT p.section_path, min(p.position), min(p.page)"
                " FROM docs.paragraphs p"
                " JOIN docs.revisions r ON r.id = p.revision_id"
                "  AND r.is_current AND r.stage = 'ready'"
                " WHERE r.paper_id = %s GROUP BY p.section_path"
                " ORDER BY min(p.position)",
                (UUID(document_id),),
            )
            outline = [
                {"section": list(path), "first_paragraph": pos, "page": page + 1}
                for path, pos, page in await cur.fetchall()
            ]
        return {"document": name, "paragraphs": last + 1, "outline": outline}

    async def _rows(self, paper_id: UUID, first: int, last: int) -> list[tuple]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT p.id, p.position, p.page, p.block_kind, p.text"
                " FROM docs.paragraphs p"
                " JOIN docs.revisions r ON r.id = p.revision_id"
                " WHERE r.paper_id = %s AND r.is_current AND r.stage = 'ready'"
                "  AND p.position BETWEEN %s AND %s ORDER BY p.position",
                (paper_id, first, last),
            )
            rows = await cur.fetchall()
        self.retrieved.update(r[0] for r in rows)
        return rows

    async def _locations(self, paragraph_ids: Sequence[int]) -> dict[int, Location]:
        async with self.pool.connection() as conn:
            return await locations(conn, paragraph_ids)


def _paragraph(row: tuple, where: dict[int, Location]) -> dict:
    pid, position, _, kind, text = row
    out: dict[str, object] = {
        "paragraph_id": pid,
        "position": position,
        "where": where[pid].render(),
        "text": text,
    }
    if kind:
        out["kind"] = kind
    return out


async def locations(
    conn: AsyncConnection, paragraph_ids: Sequence[int]
) -> dict[int, Location]:
    """The location of each paragraph: document, 1-based page, section path,
    and its block label or the anchor label of the narrowest unit holding it."""
    if not paragraph_ids:
        return {}
    cur = await conn.execute(
        "SELECT p.id, pa.name, p.page, p.section_path, coalesce(p.block_label,"
        "  (SELECT u.anchor_label FROM docs.units u"
        "   WHERE u.revision_id = p.revision_id AND u.anchor_label IS NOT NULL"
        "    AND p.position BETWEEN u.first_pos AND u.last_pos"
        "   ORDER BY u.last_pos - u.first_pos, u.id LIMIT 1))"
        " FROM docs.paragraphs p"
        " JOIN docs.revisions r ON r.id = p.revision_id"
        " JOIN docs.papers pa ON pa.id = r.paper_id"
        " WHERE p.id = ANY(%s)",
        (list(paragraph_ids),),
    )
    return {
        pid: Location(name, page + 1, tuple(path), label)
        for pid, name, page, path, label in await cur.fetchall()
    }


class AnswerHandler(Protocol):
    """Answers one question with `tools` (D28). Yields `tool_call` and
    `tool_result` events as it works, then one `answer` event whose data is
    an AnswerReply, or an `error` event. Never raises for a failed answer:
    the error is an event."""

    def answer(self, question: str, tools: Tools) -> AsyncIterator[AgentEvent]: ...


async def ask(
    handler: AnswerHandler, tools: Tools, question: str
) -> AsyncGenerator[AgentEvent]:
    """The handler's events, then, if it answered, a `final` event: the
    answer with each citation located, or marked removed if its paragraph was
    never returned by a tool in this session (D29, D30). The answer itself is
    never dropped. Logs `answer_done` with the question and what came of it,
    also when the handler fails or the caller stops early."""
    started = time.monotonic()
    reply: AnswerReply | None = None
    outcome: dict[str, object] | None = None
    calls = 0
    try:
        async for event in handler.answer(question, tools):
            yield event
            if event.kind == "tool_call":
                calls += 1
            elif event.kind == "answer":
                reply = AnswerReply.model_validate(event.data)
            elif event.kind == "error":
                outcome = {"outcome": "error", "error": event.data}
        if reply is None:
            outcome = outcome or {"outcome": "no_answer"}
            return
        final, removed = await _final(reply, tools)
        outcome = {
            "outcome": "answered",
            "citations": len(reply.citations),
            "removed": removed,
        }
        yield AgentEvent("final", final)
    finally:
        log.info(
            "answer_done",
            extra={
                "question": question,
                "seconds": round(time.monotonic() - started, 1),
                "tool_calls": calls,
                "paragraphs_read": len(tools.retrieved),
                **(outcome or {"outcome": "stopped"}),  # the caller went away
            },
        )


async def _final(reply: AnswerReply, tools: Tools) -> tuple[dict[str, object], int]:
    """The `final` event's data, and how many citations it removed."""
    kept = [
        c.paragraph_id for c in reply.citations if c.paragraph_id in tools.retrieved
    ]
    async with tools.pool.connection() as conn:
        where = await locations(conn, kept)
    removed = sum(1 for c in reply.citations if c.paragraph_id not in where)
    return {
        "answer": reply.answer,
        "citations": [
            {
                "marker": c.marker,
                "paragraph_id": c.paragraph_id,
                "location": asdict(where[c.paragraph_id])
                if c.paragraph_id in where
                else None,
                "rendered": where[c.paragraph_id].render()
                if c.paragraph_id in where
                else None,
                "removed": None
                if c.paragraph_id in where
                else "not among the paragraphs the tools returned",
            }
            for c in reply.citations
        ],
    }, removed
