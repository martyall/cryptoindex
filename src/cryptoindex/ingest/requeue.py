"""`make requeue`: send documents back to an earlier stage, so the pipeline
redoes it. A prompt version, a parser version or an embedding model changes
what a stage produces, and nothing else moves a revision that is already
`ready`.

What a stage would rather not repeat is cached, so only the changed work is
done again: the stored PDF, the parser's raw output per parser version, each
chunk's validated reply per input hash, and vectors already present. Sending
a revision back to `embed` therefore clears its vectors; nothing else is
deleted, and a stage rewrites its own output.

The runner reads its queues at startup, so this is a stop-requeue-start job:
run it while the pipeline is stopped, then `make run`.
"""

import argparse
import sys
from collections.abc import Sequence
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import TupleRow

from cryptoindex.core import config
from cryptoindex.core.model import Stage

STAGES = (Stage.PARSE, Stage.SEGMENT, Stage.EMBED)


class ClaimedError(RuntimeError):
    """Some of the revisions are claimed by a running pipeline."""


def requeue(
    conn: Connection[TupleRow], stage: Stage, paper_ids: Sequence[UUID] | None = None
) -> list[int]:
    """Send every revision (or those of `paper_ids`) back to `stage` and
    return their IDs, in one transaction. Vectors are cleared when the stage
    is `embed`, since the embed stage only fills in missing ones, and with
    them the index's embedding model once no vectors are left (Invariant 6).
    Raises ClaimedError if a revision is claimed: stop the pipeline first."""
    papers = list(paper_ids) if paper_ids is not None else None
    with conn.transaction():
        claimed = conn.execute(
            "SELECT count(*) FROM docs.revisions"
            " WHERE locked_at IS NOT NULL"
            " AND (%(papers)s::uuid[] IS NULL OR paper_id = ANY(%(papers)s::uuid[]))",
            {"papers": papers},
        ).fetchone()
        if claimed is not None and claimed[0]:
            raise ClaimedError(
                f"{claimed[0]} revision(s) are claimed by a running pipeline"
            )
        rows = conn.execute(
            "UPDATE docs.revisions SET stage = %(stage)s, attempts = 0,"
            "  last_error = NULL, failed_stage = NULL, updated_at = now()"
            " WHERE (%(papers)s::uuid[] IS NULL"
            "        OR paper_id = ANY(%(papers)s::uuid[])) RETURNING id",
            {"stage": stage.value, "papers": papers},
        ).fetchall()
        ids = [row[0] for row in rows]
        if stage is Stage.EMBED:
            _clear_vectors(conn, ids)
        return ids


def _clear_vectors(conn: Connection[TupleRow], revision_ids: Sequence[int]) -> None:
    ids = list(revision_ids)
    conn.execute(
        "UPDATE docs.paragraphs SET emb = NULL WHERE revision_id = ANY(%s)", (ids,)
    )
    conn.execute(
        "UPDATE docs.units SET emb_gloss = NULL WHERE revision_id = ANY(%s)", (ids,)
    )
    conn.execute(
        "UPDATE docs.unit_questions q SET emb = NULL FROM docs.units u"
        " WHERE u.id = q.unit_id AND u.revision_id = ANY(%s)",
        (ids,),
    )
    left = conn.execute("SELECT count(emb) FROM docs.paragraphs").fetchone()
    if left is not None and left[0] == 0:
        # The next embedding records the model again, which may be another one.
        conn.execute("DELETE FROM docs.meta WHERE key = 'embed_model'")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=[s.value for s in STAGES])
    parser.add_argument(
        "papers",
        nargs="*",
        type=UUID,
        help="document IDs; every document when none are given",
    )
    args = parser.parse_args()
    with psycopg.connect(config.settings.admin_dsn) as conn:
        try:
            ids = requeue(conn, Stage(args.stage), args.papers or None)
        except ClaimedError as e:
            sys.exit(f"{e}; stop `make run` first")
    print(f"{len(ids)} revision(s) sent back to {args.stage}; run `make run`")


if __name__ == "__main__":
    main()
