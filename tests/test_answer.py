import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from httpx_sse import aconnect_sse

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.embed import FakeEmbedder
from cryptoindex.evaluation.ask_page import mount_ask
from cryptoindex.ingest.paragraphs import content_hash
from cryptoindex.query.answer import (
    MAX_PARAGRAPHS,
    AgentEvent,
    Location,
    Tools,
    ask,
    locations,
)
from cryptoindex.query.claude_agent import _sdk_tools, unexpected_tools

INTRO = ("Intro",)
OPENING = ("3 Commitments", "3.1 Opening")
PARAGRAPHS = [  # section, page (0-based), text, kind
    (INTRO, 0, "Halo avoids a trusted setup by nested amortization.", None),
    (OPENING, 4, "Definition 7.5. An opening proof is a record of cross terms.", None),
    (OPENING, 5, "The verifier folds the challenges into one point.", None),
    ((), 9, "[1] Bootle et al. Efficient zero-knowledge arguments.", "reference"),
]


@pytest.fixture
def document(settings: Settings) -> tuple[UUID, list[int]]:
    """A current, ready document; paragraphs 1-2 form the unit anchored on
    Definition 7.5. Returns its ID and its paragraph IDs by position."""
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO docs.papers (name) VALUES ('Doc') RETURNING id"
        ).fetchone()
        assert row is not None
        paper = row[0]
        row = conn.execute(
            "INSERT INTO docs.revisions (paper_id, revision, source_kind, pdf_sha256,"
            " stage, is_current) VALUES (%s, 1, 'pdf', 'sha', 'ready', true)"
            " RETURNING id",
            (paper,),
        ).fetchone()
        assert row is not None
        revision = row[0]
        ids = []
        for pos, (section, page, text, kind) in enumerate(PARAGRAPHS):
            row = conn.execute(
                "INSERT INTO docs.paragraphs (revision_id, position, page, bbox,"
                " section_path, text, content_hash, block_kind)"
                " VALUES (%s, %s, %s, '{0,0,1,1}', %s, %s, %s, %s) RETURNING id",
                (revision, pos, page, list(section), text, content_hash(text), kind),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
        for first, last, anchor in ((0, 0, None), (1, 2, "Definition 7.5")):
            conn.execute(
                "INSERT INTO docs.units (revision_id, first_pos, last_pos,"
                " anchor_label, anchor_pos, anchor_kind, anchor_term, gloss,"
                " gloss_model, prompt_version, input_hash)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 'g', 'm', 'gloss-v4', 'h')",
                (
                    revision,
                    first,
                    last,
                    anchor,
                    first if anchor else None,
                    "definition" if anchor else None,
                    "definition" if anchor else None,
                ),
            )
    return paper, ids


@pytest.fixture
def tools(query_pool: Pool) -> Tools:
    return Tools(query_pool, FakeEmbedder())


async def test_a_location_is_read_from_the_stored_paragraph(
    query_pool: Pool, document: tuple[UUID, list[int]]
) -> None:
    _, ids = document
    async with query_pool.connection() as conn:
        where = await locations(conn, ids)
    # Paragraph 2 has no label of its own; the unit holding it is anchored.
    assert where[ids[2]] == Location("Doc", 6, OPENING, "Definition 7.5")
    assert where[ids[2]].render() == "Doc, p. 6, 3.1 Opening, Definition 7.5"
    assert where[ids[0]].render() == "Doc, p. 1, Intro"


async def test_tools_record_every_paragraph_they_return(
    tools: Tools, document: tuple[UUID, list[int]]
) -> None:
    paper, ids = document
    found = await tools.search("amortization")
    (hit,) = found["results"]
    assert [p["paragraph_id"] for p in hit["paragraphs"]] == [ids[0]]
    assert tools.retrieved == {ids[0]}

    unit = await tools.get_paragraphs(str(paper), 1, 2)
    assert [p["where"] for p in unit["paragraphs"]] == [
        "Doc, p. 5, 3.1 Opening, Definition 7.5",
        "Doc, p. 6, 3.1 Opening, Definition 7.5",
    ]
    assert tools.retrieved == {ids[0], ids[1], ids[2]}


async def test_get_paragraphs_returns_at_most_a_page_of_them(
    tools: Tools, document: tuple[UUID, list[int]]
) -> None:
    paper, _ = document
    got = await tools.get_paragraphs(str(paper), 0, 10_000)
    assert len(got["paragraphs"]) == len(PARAGRAPHS) <= MAX_PARAGRAPHS
    assert await tools.get_unit(999_999) == {"error": "no unit 999999 in the index"}


async def test_get_document_gives_an_outline(
    tools: Tools, document: tuple[UUID, list[int]]
) -> None:
    paper, _ = document
    got = await tools.get_document(str(paper))
    assert got["document"] == "Doc" and got["paragraphs"] == len(PARAGRAPHS)
    assert [(s["section"], s["page"]) for s in got["outline"]] == [
        (list(INTRO), 1),
        (list(OPENING), 5),
        ([], 10),
    ]


class Scripted:
    """An AnswerHandler with no model: it searches, then cites what it found
    and one paragraph it never retrieved."""

    def __init__(self, invented: int) -> None:
        self.invented = invented

    async def answer(self, question: str, tools: Tools) -> AsyncIterator[AgentEvent]:
        found = await tools.search(question)
        yield AgentEvent("tool_call", {"tool": "search", "input": {"q": question}})
        cited = found["results"][0]["paragraphs"][0]["paragraph_id"]
        yield AgentEvent(
            "answer",
            {
                "answer": "Halo uses nested amortization [1], as shown in [2].",
                "citations": [
                    {"marker": "[1]", "paragraph_id": cited},
                    {"marker": "[2]", "paragraph_id": self.invented},
                ],
            },
        )


async def test_a_citation_of_an_unreturned_paragraph_is_removed_not_the_answer(
    tools: Tools, document: tuple[UUID, list[int]]
) -> None:
    _, ids = document
    events = [e async for e in ask(Scripted(invented=ids[1]), tools, "amortization")]
    assert [e.kind for e in events] == ["tool_call", "answer", "final"]
    final = events[-1].data
    assert final["answer"] == "Halo uses nested amortization [1], as shown in [2]."
    citations = final["citations"]
    assert isinstance(citations, list)
    kept, removed = citations
    assert kept["rendered"] == "Doc, p. 1, Intro" and kept["removed"] is None
    assert removed["rendered"] is None and removed["location"] is None
    assert removed["removed"] == "not among the paragraphs the tools returned"


async def test_the_ask_page_streams_the_events_then_done(
    query_pool: Pool, document: tuple[UUID, list[int]], tmp_path: Path
) -> None:
    _, ids = document
    app = FastAPI()
    mount_ask(
        app, Scripted(invented=ids[1]), query_pool, FakeEmbedder(), katex_dir=tmp_path
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
        aconnect_sse(
            client, "GET", "/ask/api/ask", params={"q": "amortization"}
        ) as source,
    ):
        assert source.response.headers["content-type"].startswith("text/event-stream")
        events = [e async for e in source.aiter_sse()]
    assert [(e.event, e.json()["kind"]) for e in events[:-1]] == [
        ("message", "tool_call"),
        ("message", "answer"),
        ("message", "final"),
    ]
    assert events[-1].event == "done"


async def test_sdk_tools_return_json_and_report_what_was_new(
    tools: Tools, document: tuple[UUID, list[int]]
) -> None:
    paper, _ = document
    queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
    by_name = {t.name: t for t in _sdk_tools(tools, queue)}
    assert set(by_name) == {"search", "get_unit", "get_paragraphs", "get_document"}

    out = await by_name["get_paragraphs"].handler(
        {"document_id": str(paper), "first": 0, "last": 1}
    )
    (content,) = out["content"]
    assert len(json.loads(content["text"])["paragraphs"]) == 2
    assert queue.get_nowait().data == {
        "tool": "get_paragraphs",
        "new_paragraphs": 2,
        "error": None,
    }

    bad = await by_name["get_document"].handler({"document_id": "not-a-uuid"})
    assert "error" in json.loads(bad["content"][0]["text"])


def test_a_session_with_tools_beyond_ours_is_refused() -> None:
    ours = {"mcp__cryptoindex__search": "search"}
    assert (
        unexpected_tools(["StructuredOutput", "mcp__cryptoindex__search"], ours) == []
    )
    assert unexpected_tools(
        ["mcp__cryptoindex__search", "WebFetch", "mcp__claude_ai_Docs__read"], ours
    ) == ["WebFetch", "mcp__claude_ai_Docs__read"]
