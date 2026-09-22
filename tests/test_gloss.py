import pytest
from pydantic import ValidationError

from cryptoindex.ingest import gloss
from cryptoindex.ingest.gloss import (
    Chunk,
    InvalidReplyError,
    SourceParagraph,
    UnitReply,
    chunks_of,
    input_hash,
    merge,
    quality_flags,
    validate_reply,
)


def para(
    pos: int, text: str = "x", path: tuple[str, ...] = ("S",), kind: str | None = None
) -> SourceParagraph:
    return SourceParagraph(pos, path, text, f"h{pos}:{text}", kind)


def unit(
    first: int,
    last: int,
    anchor: tuple[str, int] | None = None,
    gloss_text: str = "It shows a thing.",
    terms: tuple[str, ...] = (),
    questions: tuple[str, ...] = ("What?", "Why is that so?", "How is it done?"),
) -> dict[str, object]:
    return {
        "first_pos": first,
        "last_pos": last,
        "anchor_label": anchor[0] if anchor else None,
        "anchor_pos": anchor[1] if anchor else None,
        "anchor_kind": "theorem" if anchor else None,
        "anchor_term": "theorem" if anchor else None,
        "gloss": gloss_text,
        "key_terms": list(terms),
        "questions": list(questions),
    }


def reply(*units: dict[str, object]) -> dict[str, object]:
    return {"units": list(units)}


def test_sections_are_runs_of_equal_section_paths() -> None:
    ps = [para(0, path=()), para(1, path=("A",)), para(2, path=("A",)), para(3)]
    got = chunks_of(ps)
    assert [(c.section_path, c.first_pos, c.last_pos) for c in got] == [
        ((), 0, 0),
        (("A",), 1, 2),
        (("S",), 3, 3),
    ]
    assert all(c.keep_before is None for c in got)


def test_long_section_splits_with_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gloss, "CHUNK_CHARS", 10)
    monkeypatch.setattr(gloss, "OVERLAP_CHARS", 4)
    ps = [para(i, "abc") for i in range(7)]  # 3 chars each: 3 per chunk
    got = chunks_of(ps)
    spans = [(c.first_pos, c.last_pos, c.keep_before) for c in got]
    # Each chunk after the first starts one paragraph (3 <= 4 chars) back.
    assert spans == [(0, 2, 2), (2, 4, 4), (4, 6, None)]


def test_a_paragraph_longer_than_a_chunk_is_its_own_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gloss, "CHUNK_CHARS", 10)
    got = chunks_of([para(0, "a" * 50), para(1, "b" * 50)])
    assert [(c.first_pos, c.last_pos, c.keep_before) for c in got] == [
        (0, 0, 1),
        (1, 1, None),
    ]


CHUNK = Chunk(
    ("S",),
    (
        para(3, "Theorem 3.1. Every group has an identity."),
        para(4, "Proof. Take e."),
        para(5, "Next topic."),
    ),
    None,
)


def test_valid_reply_is_accepted() -> None:
    units = validate_reply(reply(unit(3, 4, ("Theorem 3.1", 3)), unit(4, 5)), CHUNK)
    assert [u.key for u in units] == [(3, 4, "Theorem 3.1"), (4, 5, None)]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (reply(), "no units"),
        (reply(unit(2, 5)), "outside"),
        (reply(unit(3, 6)), "outside"),
        (reply(unit(4, 5), unit(3, 3)), "out of order"),
        (reply(unit(3, 5), unit(3, 5)), "twice"),
        (reply(unit(3, 4)), r"paragraphs \[5\] are in no unit"),
        (reply(unit(3, 5, ("Theorem 3.1", 5))), "is not in paragraph 5"),
        (reply(unit(3, 4, ("Theorem 3.2", 3)), unit(5, 5)), "is not in paragraph 3"),
        (reply(unit(4, 5, ("Theorem 3.1", 3)), unit(3, 3)), "not in span"),
    ],
)
def test_reply_that_does_not_fit_the_chunk_is_rejected(
    bad: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidReplyError, match=message):
        validate_reply(bad, CHUNK)


@pytest.mark.parametrize(
    "missing", ["anchor_label", "anchor_pos", "anchor_kind", "anchor_term"]
)
def test_part_of_an_anchor_is_rejected(missing: str) -> None:
    partial = unit(3, 5, ("Theorem 3.1", 3)) | {missing: None}
    with pytest.raises(InvalidReplyError, match="part of an anchor"):
        validate_reply(reply(partial), CHUNK)


def test_an_anchor_term_must_be_part_of_the_label() -> None:
    wrong = unit(3, 5, ("Theorem 3.1", 3)) | {"anchor_term": "lemma"}
    with pytest.raises(InvalidReplyError, match="is not in"):
        validate_reply(reply(wrong), CHUNK)


def test_an_anchor_term_is_compared_without_case() -> None:
    capitalized = unit(3, 5, ("Theorem 3.1", 3)) | {"anchor_term": "Theorem"}
    assert validate_reply(reply(capitalized), CHUNK)


def test_references_are_not_glossed() -> None:
    ps = [para(0), para(1, kind="reference"), para(2, kind="reference"), para(3)]
    assert [(c.first_pos, c.last_pos) for c in chunks_of(ps)] == [(0, 0), (3, 3)]


def test_reply_not_matching_the_schema_is_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_reply({"units": [{"first_pos": 3}]}, CHUNK)


def test_a_footnote_need_not_be_in_any_unit() -> None:
    chunk = Chunk(
        ("S",),
        (
            para(0, "Body."),
            para(1, "1. See main.rs", kind="footnote"),
            para(2, "More."),
        ),
        None,
    )
    assert validate_reply(reply(unit(0, 0), unit(2, 2)), chunk)
    with pytest.raises(InvalidReplyError, match=r"paragraphs \[2\]"):
        validate_reply(reply(unit(0, 0)), chunk)


def test_merge_keeps_each_chunks_units_before_the_next_chunk() -> None:
    first = Chunk(("S",), tuple(para(i) for i in range(0, 4)), keep_before=2)
    second = Chunk(("S",), tuple(para(i) for i in range(2, 6)), keep_before=None)
    a = [UnitReply.model_validate(u) for u in (unit(0, 1), unit(1, 3), unit(2, 3))]
    b = [UnitReply.model_validate(u) for u in (unit(2, 3), unit(4, 5))]
    got = merge([(first, a), (second, b)])
    # (2, 3) from the first chunk starts in the second chunk's territory; the
    # second chunk's own (2, 3) is kept instead, once.
    assert [u.key for u in got] == [
        (0, 1, None),
        (1, 3, None),
        (2, 3, None),
        (4, 5, None),
    ]


TEXT = {0: "A one-way function is easy to compute.", 1: "It is hard to invert."}


def flags(**kw: object) -> list[str]:
    fields = unit(0, 1) | kw
    return quality_flags(UnitReply.model_validate(fields), TEXT)


def test_a_good_unit_has_no_flags() -> None:
    assert flags(key_terms=["One-Way Function"]) == []


def test_quality_flags() -> None:
    assert flags(gloss="  ") == ["gloss_empty"]
    assert flags(gloss="x" * 601) == ["gloss_long"]
    assert flags(key_terms=["one-way function", "trapdoor"]) == ["term_absent"]
    assert flags(questions=["What?"]) == ["question_count"]
    assert flags(
        gloss="A one-way function is easy to compute and hard to invert.",
        questions=[
            "A one-way function is easy to compute and hard to invert?",
            "Why?",
            "How?",
        ],
    ) == ["question_restates_gloss"]


def test_input_hash_follows_content_prompt_and_model() -> None:
    base = input_hash(CHUNK, "T", "gloss-v1", "m")
    assert base == input_hash(CHUNK, "T", "gloss-v1", "m")
    changed = Chunk(
        CHUNK.section_path, (para(3, "changed"),) + CHUNK.paragraphs[1:], None
    )
    assert input_hash(changed, "T", "gloss-v1", "m") != base
    assert input_hash(CHUNK, "T", "gloss-v2", "m") != base
    assert input_hash(CHUNK, "T", "gloss-v1", "other") != base
    assert input_hash(CHUNK, "Other title", "gloss-v1", "m") != base
