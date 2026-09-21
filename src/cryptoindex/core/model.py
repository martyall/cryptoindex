from enum import StrEnum
from typing import NewType

RevisionId = NewType("RevisionId", int)


class Stage(StrEnum):
    """Values of `docs.revisions.stage`."""

    PARSE = "parse"
    SEGMENT = "segment"
    EMBED = "embed"
    READY = "ready"
    FAILED = "failed"
