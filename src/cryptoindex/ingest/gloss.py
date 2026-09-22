"""Segmentation and glossing, apart from the database: which paragraphs go to
the model together, what it is asked, how its reply is checked against what it
was given, and the mechanical quality flags (docs/phases/03-segment-gloss.md)."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, get_args

from pydantic import BaseModel

from cryptoindex.core.llm import LLMRequest, Message
from cryptoindex.core.prompts import Prompt

GLOSS_PROMPT = "gloss-v4"  # D26

# A chunk's paragraph text is at most CHUNK_CHARS (about 8k tokens), which
# fits a local model's context with room for the reply. Consecutive chunks of
# a long section share up to OVERLAP_CHARS: a unit starting in the overlap is
# left to the next chunk (Chunk.keep_before), which sees it whole if it fits.
CHUNK_CHARS = 24_000
OVERLAP_CHARS = 4_000

GLOSS_MAX_CHARS = 600
QUESTIONS_MIN, QUESTIONS_MAX = 3, 5
RESTATEMENT_RATIO = 0.8


AnchorKind = Literal["theorem", "definition", "algorithm", "game", "example", "other"]
# A paragraph of the bibliography is stored and searchable, but is no part of
# any argument, so it is not sent for glossing at all (D25).
UNGLOSSED: frozenset[str] = frozenset({"reference"})
# These are sent, because the argument around them may use what they say, but
# a unit need not cover them: in a specification most footnotes only point at
# a source file (D26).
OPTIONAL: frozenset[str] = frozenset({"footnote", "caption"})


@dataclass(frozen=True, slots=True)
class SourceParagraph:
    position: int
    section_path: tuple[str, ...]
    text: str
    content_hash: str
    block_kind: str | None


@dataclass(frozen=True, slots=True)
class Chunk:
    """Consecutive paragraphs of one section, sent to the model in one call.
    Units starting at or after `keep_before` are left to the next chunk,
    which starts there."""

    section_path: tuple[str, ...]
    paragraphs: tuple[SourceParagraph, ...]
    keep_before: int | None

    @property
    def first_pos(self) -> int:
        return self.paragraphs[0].position

    @property
    def last_pos(self) -> int:
        return self.paragraphs[-1].position


def chunks_of(paragraphs: Sequence[SourceParagraph]) -> list[Chunk]:
    """Split the paragraphs to gloss (every kind but UNGLOSSED, in position
    order) into sections, consecutive paragraphs with the same section path,
    and each section into chunks of at most CHUNK_CHARS (a single longer
    paragraph is a chunk by itself)."""
    out: list[Chunk] = []
    for section in _sections(paragraphs):
        out += _split(section[0].section_path, section)
    return out


def _sections(paragraphs: Sequence[SourceParagraph]) -> list[list[SourceParagraph]]:
    """Runs of paragraphs with the same section path and consecutive
    positions, broken where an UNGLOSSED paragraph sits between them, so that
    no unit spans one."""
    sections: list[list[SourceParagraph]] = []
    previous: SourceParagraph | None = None
    for p in paragraphs:
        if p.block_kind in UNGLOSSED:
            previous = None
            continue
        if (
            previous is None
            or previous.section_path != p.section_path
            or previous.position + 1 != p.position
        ):
            sections.append([])
        sections[-1].append(p)
        previous = p
    return sections


def _split(path: tuple[str, ...], section: list[SourceParagraph]) -> list[Chunk]:
    out: list[Chunk] = []
    start = 0
    while True:
        end, size = start, 0
        while end < len(section) and (
            end == start or size + len(section[end].text) <= CHUNK_CHARS
        ):
            size += len(section[end].text)
            end += 1
        if end == len(section):
            out.append(Chunk(path, tuple(section[start:end]), None))
            return out
        next_start, overlap = end, 0
        while (
            next_start - 1 > start
            and overlap + len(section[next_start - 1].text) <= OVERLAP_CHARS
        ):
            next_start -= 1
            overlap += len(section[next_start].text)
        out.append(Chunk(path, tuple(section[start:end]), section[next_start].position))
        start = next_start


def _user_message(chunk: Chunk, title: str) -> str:
    return json.dumps(
        {
            "title": title,
            "section": list(chunk.section_path),
            "paragraphs": [
                {"pos": p.position, "text": p.text}
                | ({"kind": p.block_kind} if p.block_kind else {})
                for p in chunk.paragraphs
            ],
        },
        ensure_ascii=False,
    )


def input_hash(chunk: Chunk, title: str, prompt_version: str, model: str) -> str:
    """The chunk's input hash (Invariants 2, 10): title, section path, each
    paragraph's position, content hash and kind, prompt version, and model.
    REPLY_SCHEMA is not in it; a schema change needs a new prompt version."""
    key = {
        "title": title,
        "section": list(chunk.section_path),
        "paragraphs": [
            [p.position, p.content_hash, p.block_kind] for p in chunk.paragraphs
        ],
        "prompt": prompt_version,
        "model": model,
    }
    canonical = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def gloss_request(chunk: Chunk, title: str, prompt: Prompt) -> LLMRequest:
    return LLMRequest(
        system=prompt.text,
        messages=[Message(role="user", content=_user_message(chunk, title))],
        json_schema=REPLY_SCHEMA,
        cache_prefix=True,
    )


_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
_NULLABLE_KIND = {
    "anyOf": [{"type": "string", "enum": list(get_args(AnchorKind))}, {"type": "null"}]
}
_STRINGS = {"type": "array", "items": {"type": "string"}}

REPLY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_pos": {"type": "integer"},
                    "last_pos": {"type": "integer"},
                    "anchor_label": _NULLABLE_STRING,
                    "anchor_pos": _NULLABLE_INT,
                    "anchor_kind": _NULLABLE_KIND,
                    "anchor_term": _NULLABLE_STRING,
                    "gloss": {"type": "string"},
                    "key_terms": _STRINGS,
                    "questions": _STRINGS,
                },
                "required": [
                    "first_pos",
                    "last_pos",
                    "anchor_label",
                    "anchor_pos",
                    "anchor_kind",
                    "anchor_term",
                    "gloss",
                    "key_terms",
                    "questions",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["units"],
    "additionalProperties": False,
}


class UnitReply(BaseModel, frozen=True):
    first_pos: int
    last_pos: int
    anchor_label: str | None
    anchor_pos: int | None
    anchor_kind: AnchorKind | None
    anchor_term: str | None  # the document's own word, e.g. "lemma"
    gloss: str
    key_terms: tuple[str, ...]
    questions: tuple[str, ...]

    @property
    def key(self) -> tuple[int, int, str | None]:
        """A unit's identity within its revision (DATA_MODEL, Invariant 5)."""
        return (self.first_pos, self.last_pos, self.anchor_label)


class SegmentReply(BaseModel):
    units: tuple[UnitReply, ...]


class InvalidReplyError(ValueError):
    """The model's reply does not fit the chunk it was given."""


def validate_reply(parsed: object, chunk: Chunk) -> tuple[UnitReply, ...]:
    """The reply's units, if it matches REPLY_SCHEMA and fits the chunk:
    spans inside the chunk and in order, every paragraph but an OPTIONAL one
    covered, no unit twice, each anchor label found verbatim in its anchor
    paragraph (it may be cited, Invariant 3) and its term inside that label,
    both casefolded ("Fig" of "Fig. 1"). Raises pydantic's ValidationError or
    InvalidReplyError; a reply is accepted whole or not at all."""
    units = SegmentReply.model_validate(parsed).units
    text = {p.position: p.text for p in chunk.paragraphs}
    required = {p.position for p in chunk.paragraphs if p.block_kind not in OPTIONAL}
    if not units:
        raise InvalidReplyError("no units")
    covered: set[int] = set()
    seen: set[tuple[int, int, str | None]] = set()
    previous_first = chunk.first_pos
    for u in units:
        where = f"unit {u.first_pos}-{u.last_pos}"
        if not chunk.first_pos <= u.first_pos <= u.last_pos <= chunk.last_pos:
            raise InvalidReplyError(
                f"{where} is outside {chunk.first_pos}-{chunk.last_pos}"
            )
        if u.first_pos < previous_first:
            raise InvalidReplyError(f"{where} is out of order")
        if u.key in seen:
            raise InvalidReplyError(f"{where} appears twice")
        parts = {
            u.anchor_label is None,
            u.anchor_pos is None,
            u.anchor_kind is None,
            u.anchor_term is None,
        }
        if len(parts) > 1:
            raise InvalidReplyError(f"{where} has part of an anchor")
        if u.anchor_label is not None and u.anchor_pos is not None:
            if not u.first_pos <= u.anchor_pos <= u.last_pos:
                raise InvalidReplyError(f"{where}: anchor {u.anchor_pos} not in span")
            if not u.anchor_label.strip() or u.anchor_label not in text[u.anchor_pos]:
                raise InvalidReplyError(
                    f"{where}: {u.anchor_label!r} is not in paragraph {u.anchor_pos}"
                )
            if (
                u.anchor_term is None
                or u.anchor_term.casefold() not in u.anchor_label.casefold()
            ):
                raise InvalidReplyError(
                    f"{where}: {u.anchor_term!r} is not in {u.anchor_label!r}"
                )
        previous_first = u.first_pos
        seen.add(u.key)
        covered.update(range(u.first_pos, u.last_pos + 1))
    missing = sorted(required - covered)
    if missing:
        raise InvalidReplyError(f"paragraphs {missing} are in no unit")
    return units


def merge(replies: Sequence[tuple[Chunk, Sequence[UnitReply]]]) -> list[UnitReply]:
    """Units of all chunks, each chunk contributing those starting before its
    `keep_before`, so a unit two chunks both produced comes from the later
    one. Coverage
    carries over: a paragraph before `keep_before` is covered by a unit that
    starts before it, and one after by the next chunk, all of whose units
    start at or after it."""
    out: dict[tuple[int, int, str | None], UnitReply] = {}
    for chunk, units in replies:
        for u in units:
            if chunk.keep_before is None or u.first_pos < chunk.keep_before:
                out.setdefault(u.key, u)
    return list(out.values())


def quality_flags(unit: UnitReply, text: Mapping[int, str]) -> list[str]:
    """Mechanical checks that only flag a unit for review; nothing is rewritten.
    `text` maps positions to paragraph text."""
    flags: list[str] = []
    gloss = unit.gloss.strip()
    if not gloss:
        flags.append("gloss_empty")
    if len(gloss) > GLOSS_MAX_CHARS:
        flags.append("gloss_long")
    body = " ".join(
        text[pos] for pos in range(unit.first_pos, unit.last_pos + 1)
    ).casefold()
    if any(term.casefold() not in body for term in unit.key_terms):
        flags.append("term_absent")
    if not QUESTIONS_MIN <= len(unit.questions) <= QUESTIONS_MAX:
        flags.append("question_count")
    if gloss and any(
        SequenceMatcher(None, q.casefold(), gloss.casefold()).ratio()
        >= RESTATEMENT_RATIO
        for q in unit.questions
    ):
        flags.append("question_restates_gloss")
    return flags
