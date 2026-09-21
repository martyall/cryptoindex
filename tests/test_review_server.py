import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from cryptoindex.evaluation.review_server import (
    Proposal,
    ReviewItem,
    create_app,
    read_judgements,
)

ITEMS = [
    ReviewItem.model_validate({"excerpt": "ex", "page": p, "parsers": {}, "image": "x"})
    for p in (0, 1)
]
# Claude agrees with the blind scores below except on ex/1/paddle.
PROPOSALS = {
    "ex/0/marker": Proposal(score=2, note="fine"),
    "ex/0/paddle": Proposal(score=1, note="ok"),
    "ex/1/marker": Proposal(score=0, note="scrambled"),
    "ex/1/paddle": Proposal(score=2, note="box intact"),
}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ITEMS, tmp_path / "blind.json", tmp_path / "rec.json", PROPOSALS)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def score(client: httpx.AsyncClient, page: int, parser: str, value: int) -> int:
    response = await client.put(
        "/api/score",
        json={"excerpt": "ex", "page": page, "parser": parser, "score": value},
    )
    return response.status_code


async def test_the_page_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_blind_mode_never_sends_proposals(client: httpx.AsyncClient) -> None:
    state = (await client.get("/api/state")).json()
    assert state["mode"] == "blind" and len(state["items"]) == 2
    assert "proposals" not in json.dumps(state) and "box intact" not in json.dumps(
        state
    )


async def test_each_score_is_saved_to_disk_immediately(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert await score(client, 0, "marker", 2) == 200
    saved = read_judgements(tmp_path / "blind.json")
    assert saved["ex/0/marker"].score == 2 and saved["ex/0/marker"].at


async def test_invalid_scores_are_rejected(client: httpx.AsyncClient) -> None:
    assert await score(client, 0, "marker", 3) == 422
    assert await score(client, 7, "marker", 1) == 404
    assert await score(client, 0, "gpt", 1) == 422


async def test_reconcile_follows_a_complete_blind_pass(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    for page, parser, value in [
        (0, "marker", 2),
        (0, "paddle", 1),
        (1, "marker", 0),
        (1, "paddle", 1),
    ]:
        assert await score(client, page, parser, value) == 200

    state = (await client.get("/api/state")).json()
    assert state["mode"] == "reconcile"
    assert [i["page"] for i in state["items"]] == [1]
    assert list(state["proposals"]) == ["ex/1/paddle"]

    assert await score(client, 1, "paddle", 2) == 200
    assert read_judgements(tmp_path / "rec.json")["ex/1/paddle"].score == 2
    assert read_judgements(tmp_path / "blind.json")["ex/1/paddle"].score == 1
