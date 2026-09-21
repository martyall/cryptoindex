import dataclasses
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.document import Block, ParsedDocument
from cryptoindex.ingest.parse import parse_stage
from cryptoindex.ingest.parsers import ParserError
from cryptoindex.ingest.stages import StageContext

BOX = (10.0, 20.0, 30.0, 40.0)
BLOCKS = (
    Block("heading", 0, BOX, "Groups", heading_level=1),
    Block("text", 0, BOX, "A group is a set.", section_path=("Groups",)),
    Block("equation", 0, BOX, "ab = ba", section_path=("Groups",)),
    Block("text", 1, BOX, "Every subgroup is normal here.", section_path=("Groups",)),
)


class FakeParser:
    """Its "raw output" is just the index of the block list to return, so the
    stage's caching of raw output is observable."""

    name = "fake"

    def __init__(self, *versions: tuple[Block, ...], version: str = "1") -> None:
        self.outputs = versions
        self.version = version
        self.runs = 0

    def run(self, pdf_path: Path) -> bytes:
        self.runs += 1
        if not self.outputs:
            raise ParserError("parser crashed")
        return str(self.runs - 1).encode()

    def read(self, raw: bytes) -> ParsedDocument:
        blocks = self.outputs[min(int(raw), len(self.outputs) - 1)]
        return ParsedDocument(page_count=2, blocks=blocks)


@pytest.fixture
def claim(
    settings: Settings, seed: Callable[[list[Stage]], list[int]]
) -> Callable[[], RevisionId]:
    """(Re)claims one seeded revision at `parse`, as the runner would."""
    (work_id,) = seed([Stage.PARSE])

    def again() -> RevisionId:
        with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE docs.revisions SET stage = 'parse', locked_at = now(),"
                " pdf_path = 'pdfs/x/doc.pdf' WHERE id = %s",
                (work_id,),
            )
        return RevisionId(work_id)

    return again


def ctx(
    pool: Pool, settings: Settings, tmp_path: Path, parser: FakeParser
) -> StageContext:
    return StageContext(
        pool=pool,
        settings=dataclasses.replace(settings, data_dir=tmp_path),
        parser=parser,
    )


def paragraphs(settings: Settings) -> list[TupleRow]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(
            "SELECT id, position, page, bbox, section_path, block_kind, text"
            " FROM docs.paragraphs ORDER BY position"
        ).fetchall()


def revision(settings: Settings) -> TupleRow:
    with psycopg.connect(settings.admin_dsn) as conn:
        row = conn.execute(
            "SELECT r.stage, r.locked_at, r.parser, r.parser_version, p.title"
            " FROM docs.revisions r JOIN docs.papers p ON p.id = r.paper_id"
        ).fetchone()
    assert row is not None
    return row


async def test_parse_stores_paragraphs_and_advances(
    settings: Settings, pool: Pool, tmp_path: Path, claim: Callable[[], RevisionId]
) -> None:
    await parse_stage(claim(), ctx(pool, settings, tmp_path, FakeParser(BLOCKS)))

    assert [row[1:] for row in paragraphs(settings)] == [
        (0, 0, [10, 20, 30, 40], "Groups", None, "A group is a set."),
        (1, 0, [10, 20, 30, 40], "Groups", "equation", "ab = ba"),
        (2, 1, [10, 20, 30, 40], "Groups", None, "Every subgroup is normal here."),
    ]
    assert tuple(revision(settings)) == ("segment", None, "fake", "1", "Groups")
    assert list((tmp_path / "parsed" / "fake-1").glob("*.json"))


async def test_reparse_keeps_ids_without_rerunning_parser(
    settings: Settings, pool: Pool, tmp_path: Path, claim: Callable[[], RevisionId]
) -> None:
    parser = FakeParser(BLOCKS)
    await parse_stage(claim(), ctx(pool, settings, tmp_path, parser))
    before = paragraphs(settings)

    await parse_stage(claim(), ctx(pool, settings, tmp_path, parser))

    assert paragraphs(settings) == before
    assert parser.runs == 1  # the second parse read the cached output


async def test_new_parser_version_keeps_ids_only_for_unchanged_paragraphs(
    settings: Settings, pool: Pool, tmp_path: Path, claim: Callable[[], RevisionId]
) -> None:
    await parse_stage(claim(), ctx(pool, settings, tmp_path, FakeParser(BLOCKS)))
    before = paragraphs(settings)

    changed = (
        BLOCKS[0],
        dataclasses.replace(BLOCKS[1], text="A group is a set with an operation."),
        BLOCKS[2],
    )
    await parse_stage(
        claim(), ctx(pool, settings, tmp_path, FakeParser(changed, version="2"))
    )
    after = paragraphs(settings)

    assert len(after) == 2
    assert after[0][0] != before[0][0]  # position 0 changed: new ID
    assert after[1][0] == before[1][0]  # position 1 unchanged: same ID
    assert revision(settings)[3] == "2"


async def test_parser_failure_stores_nothing(
    settings: Settings, pool: Pool, tmp_path: Path, claim: Callable[[], RevisionId]
) -> None:
    with pytest.raises(ParserError, match="crashed"):
        await parse_stage(claim(), ctx(pool, settings, tmp_path, FakeParser()))
    assert paragraphs(settings) == []
    assert revision(settings)[0] == "parse"
    assert not list(tmp_path.rglob("*.json"))
