from typing import LiteralString
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import TupleRow

from cryptoindex.core.config import Settings
from cryptoindex.core.model import Stage
from cryptoindex.ingest.requeue import ClaimedError, requeue


@pytest.fixture
def indexed(settings: Settings) -> list[int]:
    """One ready revision of each of two documents, with a vector everywhere."""
    ids = []
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        for i in range(2):
            paper = conn.execute(
                "INSERT INTO docs.papers (name) VALUES (%s) RETURNING id",
                (f"Paper {i}",),
            ).fetchone()
            assert paper is not None
            revision = conn.execute(
                "INSERT INTO docs.revisions (paper_id, revision, source_kind,"
                " pdf_sha256, stage, is_current)"
                " VALUES (%s, 1, 'pdf', %s, 'ready', true) RETURNING id",
                (paper[0], f"sha-{i}"),
            ).fetchone()
            assert revision is not None
            work_id = revision[0]
            ids.append(work_id)
            conn.execute(
                "INSERT INTO docs.paragraphs (revision_id, position, page, bbox,"
                " text, content_hash, emb, emb_alt)"
                " VALUES (%s, 0, 0, '{0,0,1,1}', 'text', %s,"
                " array_fill(0.5, array[1024])::halfvec,"
                " array_fill(0.5, array[1024])::halfvec)",
                (work_id, f"h{i}"),
            )
            unit = conn.execute(
                "INSERT INTO docs.units (revision_id, first_pos, last_pos, gloss,"
                " gloss_model, prompt_version, input_hash, emb_gloss)"
                " VALUES (%s, 0, 0, 'g', 'm', 'gloss-v4', 'h',"
                " array_fill(0.5, array[1024])::halfvec) RETURNING id",
                (work_id,),
            ).fetchone()
            assert unit is not None
            conn.execute(
                "INSERT INTO docs.unit_questions (unit_id, question, emb)"
                " VALUES (%s, 'q?', array_fill(0.5, array[1024])::halfvec)",
                (unit[0],),
            )
        conn.execute(
            "INSERT INTO docs.meta (key, value) VALUES"
            " ('embed_model', 'fake-embedder'), ('embed_model_alt', 'alt-embedder')"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        )
    return ids


def rows(settings: Settings, sql: LiteralString) -> list[TupleRow]:
    with psycopg.connect(settings.admin_dsn) as conn:
        return conn.execute(sql).fetchall()


def test_requeue_sends_every_document_back_and_clears_its_errors(
    settings: Settings, indexed: list[int]
) -> None:
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE docs.revisions SET attempts = 3, last_error = 'boom',"
            " failed_stage = 'segment'"
        )
        got = requeue(conn, Stage.SEGMENT)
    assert sorted(got) == sorted(indexed)
    assert rows(
        settings,
        "SELECT DISTINCT stage, attempts, last_error, failed_stage FROM docs.revisions",
    ) == [("segment", 0, None, None)]
    # Glosses and their vectors are untouched: the segment stage rewrites them.
    assert rows(settings, "SELECT count(emb_gloss) FROM docs.units") == [(2,)]


def test_requeue_to_embed_clears_the_vectors_and_the_recorded_model(
    settings: Settings, indexed: list[int]
) -> None:
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        requeue(conn, Stage.EMBED)
    assert rows(
        settings,
        "SELECT (SELECT count(emb) + count(emb_alt) FROM docs.paragraphs),"
        " (SELECT count(emb_gloss) FROM docs.units),"
        " (SELECT count(emb) FROM docs.unit_questions),"
        " (SELECT count(*) FROM docs.meta WHERE key LIKE 'embed_model%')",
    ) == [(0, 0, 0, 0)]


def test_requeue_of_one_document_leaves_the_others_and_the_model_alone(
    settings: Settings, indexed: list[int]
) -> None:
    with psycopg.connect(settings.admin_dsn) as conn:
        paper = conn.execute(
            "SELECT paper_id FROM docs.revisions WHERE id = %s", (indexed[0],)
        ).fetchone()
        assert paper is not None
        got = requeue(conn, Stage.EMBED, [UUID(str(paper[0]))])
    assert got == [indexed[0]]
    assert rows(settings, "SELECT stage FROM docs.revisions ORDER BY id") == [
        ("embed",),
        ("ready",),
    ]
    assert rows(settings, "SELECT count(emb) FROM docs.paragraphs") == [(1,)]
    assert rows(settings, "SELECT value FROM docs.meta WHERE key = 'embed_model'") == [
        ("fake-embedder",)
    ]


def test_requeue_refuses_while_the_pipeline_holds_a_claim(
    settings: Settings, indexed: list[int]
) -> None:
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE docs.revisions SET locked_at = now() WHERE id = %s", (indexed[0],)
        )
        with pytest.raises(ClaimedError, match="1 revision"):
            requeue(conn, Stage.SEGMENT)
    assert rows(settings, "SELECT DISTINCT stage FROM docs.revisions") == [("ready",)]
