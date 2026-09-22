from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from cryptoindex.evaluation.gloss_eval import GlossedUnit, paragraph_html, select
from cryptoindex.evaluation.gloss_review import (
    Judgement,
    create_app,
    item_key,
    read_judgements,
    render_report,
)
from cryptoindex.ingest.document import Block, Math
from cryptoindex.ingest.gloss import AnchorKind, UnitReply

BOX = (0.0, 0.0, 1.0, 1.0)


def glossed(
    excerpt: str, first: int, anchor: tuple[str, AnchorKind] | None = None
) -> GlossedUnit:
    unit = UnitReply(
        first_pos=first,
        last_pos=first + 1,
        anchor_label=anchor[0] if anchor else None,
        anchor_pos=first if anchor else None,
        anchor_kind=anchor[1] if anchor else None,
        gloss=f"Gloss {first}.",
        key_terms=(),
        questions=("A?", "B?", "C?"),
    )
    return GlossedUnit(
        excerpt=excerpt,
        stresses="s",
        model="m",
        unit=unit,
        flags=[],
        paragraphs=[],
        images=[],
    )


ITEMS = [glossed("ex", 0, ("Theorem 1", "theorem")), glossed("ex", 2)]


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ITEMS, tmp_path / "blind.json", tmp_path, tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def update(item: GlossedUnit, **fields: object) -> dict[str, object]:
    return {
        "excerpt": item.excerpt,
        "first_pos": item.unit.first_pos,
        "last_pos": item.unit.last_pos,
        "anchor_label": item.unit.anchor_label,
    } | fields


async def test_each_choice_is_saved_as_it_is_made(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert (await client.get("/api/state")).json()["scores"] == {}
    response = await client.put("/api/score", json=update(ITEMS[0], verdict="wrong"))
    assert response.status_code == 200
    response = await client.put(
        "/api/score",
        json=update(ITEMS[0], verdict="wrong", boundaries="sensible", note="n"),
    )
    assert response.status_code == 200
    saved = read_judgements(tmp_path / "blind.json")[item_key(ITEMS[0])]
    assert (saved.verdict, saved.boundaries, saved.note) == ("wrong", "sensible", "n")


async def test_unknown_units_and_verdicts_are_refused(
    client: httpx.AsyncClient,
) -> None:
    other = update(ITEMS[1]) | {"first_pos": 7, "verdict": "faithful"}
    assert (await client.put("/api/score", json=other)).status_code == 404
    bad = update(ITEMS[1], verdict="great")
    assert (await client.put("/api/score", json=bad)).status_code == 422


def judged(item: GlossedUnit, verdict: str) -> Judgement:
    return Judgement.model_validate(update(item, verdict=verdict, at="t"))


def test_report_applies_the_freeze_criterion() -> None:
    faithful = {item_key(i): judged(i, "faithful") for i in ITEMS}
    assert "criterion" in (report := render_report(ITEMS, faithful, [], ITEMS))
    assert ": met." in report and "Faithful: 2 (100%)" in report
    wrong_theorem = faithful | {item_key(ITEMS[0]): judged(ITEMS[0], "wrong")}
    report = render_report(ITEMS, wrong_theorem, [], ITEMS)
    assert "Wrong theorem or definition glosses: 1." in report
    assert ": not met." in report


def test_selection_is_stratified_and_reproducible() -> None:
    units = [glossed("a", 2 * i, (f"Theorem {i}", "theorem")) for i in range(5)]
    units += [glossed("b", 2 * i) for i in range(5)]
    chosen = select(units, per_stratum=2)
    assert [s.stratum for s in chosen] == ["theorem", "theorem", "plain", "plain"]
    assert chosen == select(list(reversed(units)), per_stratum=2)


def test_every_block_is_rendered_with_math() -> None:
    raw = Block("equation", 0, BOX, r"x^2 + y^2", malformed_math=True)
    assert paragraph_html(raw) == '<div class="math block">x^2 + y^2</div>'
    display = Block("equation", 0, BOX, "$$a$$", math=(Math("a<b", True),))
    assert paragraph_html(display) == '<div class="math block">a&lt;b</div>'
    code = paragraph_html(Block("algorithm", 0, BOX, "for $i$ in S:\nreturn <x>"))
    assert 'class="math inline"' in code and "<br" in code and "&lt;x&gt;" in code
