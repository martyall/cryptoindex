import dataclasses
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.model import RevisionId, Stage
from cryptoindex.ingest.parse import parse_stage
from cryptoindex.ingest.parsers import ParserError
from cryptoindex.ingest.stages import StageContext

MD = """# Groups

A group is a set.

**Theorem 1.** Every subgroup of a cyclic group is cyclic.

*Proof.* Take the least positive exponent.
"""


class FakeParser:
    name = "fake"

    def __init__(self, markdown: str, version: str = "1") -> None:
        self.markdown = markdown
        self.version = version
        self.calls = 0

    def parse(self, pdf_path: Path) -> str:
        self.calls += 1
        if self.markdown == "boom":
            raise ParserError("parser crashed")
        return self.markdown


@pytest.fixture
def claimed(
    settings: Settings, seed: Callable[[list[Stage]], list[int]]
) -> Callable[[], RevisionId]:
    """Seed one revision at `parse` with a pdf_path, and return a function
    that (re)claims it at `parse`, as the runner would."""
    (work_id,) = seed([Stage.PARSE])

    def claim() -> RevisionId:
        with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE docs.revisions SET stage = 'parse', locked_at = now(),"
                " pdf_path = 'pdfs/x/doc.pdf' WHERE id = %s",
                (work_id,),
            )
        return RevisionId(work_id)

    return claim


def ctx(
    pool: Pool, settings: Settings, tmp_path: Path, parser: FakeParser
) -> StageContext:
    return StageContext(
        pool=pool,
        settings=dataclasses.replace(settings, data_dir=tmp_path),
        parser=parser,
    )


def paragraphs(settings: Settings) -> list[tuple]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(
            "SELECT id, position, content_hash, block_kind, block_label, section_path"
            " FROM docs.paragraphs ORDER BY position"
        ).fetchall()


def revision(settings: Settings) -> tuple:
    with psycopg.connect(settings.admin_dsn) as conn:
        row = conn.execute(
            "SELECT r.stage, r.locked_at, r.parser, r.parser_version, p.title"
            " FROM docs.revisions r JOIN docs.papers p ON p.id = r.paper_id"
        ).fetchone()
    assert row is not None
    return tuple(row)


async def test_parse_stores_paragraphs_and_advances(
    settings: Settings, pool: Pool, tmp_path: Path, claimed: Callable[[], RevisionId]
) -> None:
    parser = FakeParser(MD)
    await parse_stage(claimed(), ctx(pool, settings, tmp_path, parser))

    rows = paragraphs(settings)
    assert [(r[1], r[3], r[4], r[5]) for r in rows] == [
        (0, None, None, "Groups"),
        (1, "theorem", "Theorem 1", "Groups"),
        (2, "proof", "Proof", "Groups"),
    ]
    assert revision(settings) == ("segment", None, "fake", "1", "Groups")
    assert list((tmp_path / "parsed" / "fake-1").glob("*.md"))


async def test_reparse_unchanged_keeps_ids_without_calling_parser(
    settings: Settings, pool: Pool, tmp_path: Path, claimed: Callable[[], RevisionId]
) -> None:
    parser = FakeParser(MD)
    await parse_stage(claimed(), ctx(pool, settings, tmp_path, parser))
    before = paragraphs(settings)

    await parse_stage(claimed(), ctx(pool, settings, tmp_path, parser))

    assert paragraphs(settings) == before
    assert parser.calls == 1  # the second run used the cached Markdown


async def test_changed_output_keeps_ids_only_for_unchanged_paragraphs(
    settings: Settings, pool: Pool, tmp_path: Path, claimed: Callable[[], RevisionId]
) -> None:
    await parse_stage(claimed(), ctx(pool, settings, tmp_path, FakeParser(MD)))
    before = paragraphs(settings)

    edited = MD.replace("A group is a set.", "A group is a set with an operation.")
    shorter = edited.rsplit("*Proof.*", 1)[0]
    new_version = FakeParser(shorter, version="2")
    await parse_stage(claimed(), ctx(pool, settings, tmp_path, new_version))
    after = paragraphs(settings)

    assert len(after) == 2
    assert after[0][0] != before[0][0]  # position 0 changed: new ID
    assert after[1][0] == before[1][0]  # position 1 unchanged: same ID
    assert revision(settings)[3] == "2"


async def test_parser_failure_leaves_nothing_and_raises(
    settings: Settings, pool: Pool, tmp_path: Path, claimed: Callable[[], RevisionId]
) -> None:
    with pytest.raises(ParserError, match="crashed"):
        await parse_stage(claimed(), ctx(pool, settings, tmp_path, FakeParser("boom")))
    assert paragraphs(settings) == []
    assert revision(settings)[0] == "parse"
    assert not (tmp_path / "parsed").exists() or not any(
        (tmp_path / "parsed").rglob("*.md")
    )
