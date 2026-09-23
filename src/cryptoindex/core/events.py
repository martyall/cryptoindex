from enum import StrEnum

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


class EventKind(StrEnum):
    """Values of `docs.events.kind` (docs/DATA_MODEL.md)."""

    REVISION_READY = "revision_ready"
    UNIT_CHANGED = "unit_changed"
    PAPER_REVISED = "paper_revised"
    EMBED_MODEL_CHANGED = "embed_model_changed"


async def write_event(
    conn: AsyncConnection, kind: EventKind, payload: dict[str, object]
) -> None:
    """Append to the outbox inside the caller's transaction, so an event exists
    exactly when the change it describes was committed."""
    await conn.execute(
        "INSERT INTO docs.events (kind, payload) VALUES (%s, %s)",
        (kind.value, Jsonb(payload)),
    )
