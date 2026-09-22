import dataclasses
from collections.abc import Callable, Sequence
from typing import LiteralString

import psycopg
import pytest
from psycopg.rows import TupleRow

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.llm import (
    LLM,
    Completion,
    FakeLLM,
    LLMRequest,
    UnrecordedRequestError,
    request_hash,
    request_payload,
)
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.core.prompts import load_prompt
from cryptoindex.ingest import gloss
from cryptoindex.ingest.gloss import (
    GLOSS_PROMPT,
    InvalidReplyError,
    SourceParagraph,
    chunks_of,
    gloss_request,
)
from cryptoindex.ingest.paragraphs import content_hash
from cryptoindex.ingest.parsers import StubParser
from cryptoindex.ingest.segment import segment_stage
from cryptoindex.ingest.stages import StageContext

TITLE = "Paper 0"  # the seeded paper's name; it has no parsed title
PARAGRAPHS = [
    (("Groups",), "Definition 1. A group is a set with an operation.", None),
    (("Groups",), "ab = ba", "equation"),
    (("Groups",), "Theorem 2. Every subgroup of an abelian group is normal.", None),
    (("Groups",), "Proof. Conjugation is trivial.", None),
    (("Rings",), "A ring has two operations.", None),
]


def source() -> list[SourceParagraph]:
    return [
        SourceParagraph(pos, path, text, content_hash(text), kind)
        for pos, (path, text, kind) in enumerate(PARAGRAPHS)
    ]


def unit(
    first: int, last: int, anchor: tuple[str, int] | None = None, gloss_text: str = ""
) -> dict[str, object]:
    return {
        "first_pos": first,
        "last_pos": last,
        "anchor_label": anchor[0] if anchor else None,
        "anchor_pos": anchor[1] if anchor else None,
        "anchor_kind": "definition" if anchor else None,
        "gloss": gloss_text or f"Paragraphs {first} to {last}.",
        "key_terms": [],
        "questions": [f"Q{first}a?", f"Q{first}b?", f"Q{first}c?"],
    }


GROUPS = [unit(0, 1, ("Definition 1", 0)), unit(2, 3, ("Theorem 2", 2))]
RINGS = [unit(4, 4)]


def recorded(*replies: Sequence[dict[str, object]], model: str = "m") -> FakeLLM:
    """A FakeLLM answering the stage's requests for each section, in order."""
    prompt = load_prompt(GLOSS_PROMPT)
    recordings = {}
    for chunk, units in zip(chunks_of(source()), replies, strict=True):
        r = gloss_request(chunk, TITLE, prompt)
        key = request_hash(request_payload(r.system, r.messages, None, r.json_schema))
        recordings[key] = Completion("", model, {"units": list(units)})
    return FakeLLM(recordings, model=model)


@pytest.fixture
def work_id(settings: Settings, seed: Callable[[list[Stage]], list[int]]) -> int:
    (work_id,) = seed([Stage.SEGMENT])
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        for p in source():
            conn.execute(
                "INSERT INTO docs.paragraphs (revision_id, position, page, bbox,"
                " section_path, text, content_hash, block_kind)"
                " VALUES (%s, %s, 0, '{0,0,1,1}', %s, %s, %s, %s)",
                (
                    work_id,
                    p.position,
                    list(p.section_path),
                    p.text,
                    p.content_hash,
                    p.block_kind,
                ),
            )
    return work_id


def claim(settings: Settings, work_id: int) -> RevisionId:
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE docs.revisions SET stage = 'segment', locked_at = now()"
            " WHERE id = %s",
            (work_id,),
        )
    return RevisionId(work_id)


def ctx(pool: Pool, settings: Settings, llm: LLM, batch: bool = True) -> StageContext:
    return StageContext(
        pool=pool,
        settings=dataclasses.replace(settings, gloss_batch=batch),
        parser=StubParser(),
        llm=llm,
    )


def query(settings: Settings, sql: LiteralString) -> list[TupleRow]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(sql).fetchall()


def units(settings: Settings) -> list[TupleRow]:
    return query(
        settings,
        "SELECT id, first_pos, last_pos, anchor_label, anchor_pos, gloss,"
        " gloss_model, prompt_version FROM docs.units ORDER BY first_pos",
    )


async def test_units_questions_and_labels_commit_with_the_transition(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    llm = recorded(GROUPS, RINGS)
    await segment_stage(claim(settings, work_id), ctx(pool, settings, llm))

    assert [u[1:] for u in units(settings)] == [
        (0, 1, "Definition 1", 0, "Paragraphs 0 to 1.", "m", GLOSS_PROMPT),
        (2, 3, "Theorem 2", 2, "Paragraphs 2 to 3.", "m", GLOSS_PROMPT),
        (4, 4, None, None, "Paragraphs 4 to 4.", "m", GLOSS_PROMPT),
    ]
    assert query(settings, "SELECT count(*) FROM docs.unit_questions") == [(9,)]
    assert query(
        settings,
        "SELECT position, block_label FROM docs.paragraphs"
        " WHERE block_label IS NOT NULL ORDER BY position",
    ) == [(0, "Definition 1"), (2, "Theorem 2")]
    assert query(settings, "SELECT stage, locked_at FROM docs.revisions") == [
        ("embed", None)
    ]


async def test_unchanged_input_makes_no_llm_calls(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    await segment_stage(
        claim(settings, work_id), ctx(pool, settings, recorded(GROUPS, RINGS))
    )
    before = units(settings)

    silent = FakeLLM({}, model="m")
    await segment_stage(claim(settings, work_id), ctx(pool, settings, silent))
    assert silent.calls == 0
    assert units(settings) == before  # same IDs too (Invariant 5)
    assert query(settings, "SELECT count(*) FROM docs.events") == [(0,)]


async def test_resegmentation_keeps_matched_units_and_reports_removed_ones(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    await segment_stage(
        claim(settings, work_id), ctx(pool, settings, recorded(GROUPS, RINGS))
    )
    old = {u[1:4]: u[0] for u in units(settings)}

    regrouped = [unit(0, 1, ("Definition 1", 0), "Better."), unit(2, 2), unit(3, 3)]
    # A different model changes every input hash, so each section is re-asked.
    llm = recorded(regrouped, RINGS, model="m2")
    await segment_stage(claim(settings, work_id), ctx(pool, settings, llm))

    now = {u[1:4]: u for u in units(settings)}
    assert now[(0, 1, "Definition 1")][0] == old[(0, 1, "Definition 1")]
    assert now[(0, 1, "Definition 1")][5] == "Better."
    assert now[(4, 4, None)][0] == old[(4, 4, None)]
    assert (2, 3, "Theorem 2") not in now
    events = query(settings, "SELECT kind, payload FROM docs.events")
    assert events == [
        (
            "unit_changed",
            {
                "unit_id": old[(2, 3, "Theorem 2")],
                "revision_id": work_id,
                "first_pos": 2,
                "last_pos": 3,
                "anchor_label": "Theorem 2",
                "change": "deleted",
            },
        )
    ]
    assert query(
        settings,
        "SELECT position FROM docs.paragraphs WHERE block_label IS NOT NULL",
    ) == [(0,)]
    assert query(settings, "SELECT DISTINCT gloss_model FROM docs.segment_chunks") == [
        ("m2",)
    ]


async def test_invalid_reply_stores_no_units_but_keeps_valid_replies(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    bad_rings = [unit(4, 4, ("Lemma 9", 4))]  # not in the paragraph's text
    llm = recorded(GROUPS, bad_rings)
    with pytest.raises(InvalidReplyError, match="Lemma 9"):
        await segment_stage(claim(settings, work_id), ctx(pool, settings, llm))
    assert units(settings) == []
    assert query(settings, "SELECT stage FROM docs.revisions") == [("segment",)]
    assert query(settings, "SELECT count(*) FROM docs.segment_chunks") == [(1,)]

    # The retry asks only for the section that failed.
    retry = recorded(GROUPS, RINGS)
    await segment_stage(claim(settings, work_id), ctx(pool, settings, retry))
    assert retry.calls == 1
    assert len(units(settings)) == 3


async def test_backend_failure_leaves_the_revision_unchanged(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    with pytest.raises(UnrecordedRequestError):
        await segment_stage(
            claim(settings, work_id), ctx(pool, settings, FakeLLM({}, model="m"))
        )
    assert units(settings) == []
    assert query(settings, "SELECT stage FROM docs.revisions") == [("segment",)]


class BatchingFake:
    """A BatchLLM whose batch replays another fake's recordings."""

    name = "batching"

    def __init__(self, inner: FakeLLM) -> None:
        self.inner = inner
        self.model = inner.model
        self.batches: list[int] = []

    async def complete(self, *args: object, **kwargs: object) -> Completion:
        raise AssertionError("expected a batch")

    async def batch(self, requests: list[LLMRequest]) -> list[Completion]:
        self.batches.append(len(requests))
        return [
            await self.inner.complete(r.system, r.messages, json_schema=r.json_schema)
            for r in requests
        ]


async def test_missing_chunks_go_as_one_batch(
    settings: Settings, pool: Pool, work_id: int
) -> None:
    llm = BatchingFake(recorded(GROUPS, RINGS))
    await segment_stage(claim(settings, work_id), ctx(pool, settings, llm))
    assert llm.batches == [2]
    assert len(units(settings)) == 3


async def test_long_sections_are_split_and_merged(
    settings: Settings, pool: Pool, work_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gloss, "CHUNK_CHARS", 60)
    monkeypatch.setattr(gloss, "OVERLAP_CHARS", 40)
    spans = [(c.first_pos, c.last_pos, c.keep_before) for c in chunks_of(source())]
    assert spans == [(0, 1, 1), (1, 1, 2), (2, 2, 3), (3, 3, None), (4, 4, None)]
    llm = recorded(
        [unit(0, 1, ("Definition 1", 0))],
        [unit(1, 1)],
        [unit(2, 2, ("Theorem 2", 2))],
        [unit(3, 3)],
        RINGS,
    )
    await segment_stage(claim(settings, work_id), ctx(pool, settings, llm, batch=False))
    assert [u[1:4] for u in units(settings)] == [
        (0, 1, "Definition 1"),
        (1, 1, None),
        (2, 2, "Theorem 2"),
        (3, 3, None),
        (4, 4, None),
    ]
