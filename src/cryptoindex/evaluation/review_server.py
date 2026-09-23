"""The parser review page as a small local server (`make parse-review`).

It shows one page at a time and saves every judgement to disk as it is made.
The mode follows from what is saved:
- blind, until every page has both parsers scored or the pass is closed
  early (`make parse-close-blind` writes blind-closed.json): Claude's
  proposals are never sent to the browser, so they cannot anchor the human's
  scores;
- reconcile, afterwards: only the pages where a blind score and Claude's
  proposal differ, with both shown; the human's decisions go to their own file.
Binds to 127.0.0.1 only.
"""

import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cryptoindex.evaluation.parse_eval import ITEMS, KATEX_TOOL, LOCAL, SAMPLE

PARSERS = ("marker", "paddle")
SCORES = SAMPLE / "scores"
BLIND = SCORES / "blind.json"
RECONCILED = SCORES / "reconciled.json"
# Written when the human ends the blind pass early (`make parse-close-blind`).
CLOSED = SCORES / "blind-closed.json"
PROPOSALS = LOCAL / "proposals"
PAGE = Path(__file__).with_name("review.html")

Mode = Literal["blind", "reconcile"]
ParserName = Literal["marker", "paddle"]
# A lookup key built from (excerpt, page, parser) by `key()`; never parsed
# back: every record carries those fields itself.
Key = str


def key(excerpt: str, page: int, parser: str) -> Key:
    return f"{excerpt}/{page}/{parser}"


class Judgement(BaseModel):
    excerpt: str
    page: int  # 0-based within the excerpt
    parser: ParserName
    score: int = Field(ge=0, le=2)
    note: str = ""
    at: str = ""  # when it was saved, ISO 8601


class Proposal(BaseModel):
    score: int = Field(ge=0, le=2)
    note: str = ""


class BlindClosed(BaseModel):
    """The human ended the blind pass before scoring every page."""

    closed_at: str
    scored: int  # judgements saved when it closed
    total: int  # judgements a complete pass would have


class ReviewItem(BaseModel):
    """One excerpt page as parse_eval writes it; the rest of its fields (image,
    parser blocks) pass through to the page unchanged."""

    model_config = ConfigDict(extra="allow")
    excerpt: str
    page: int


class ScoreUpdate(BaseModel):
    excerpt: str
    page: int
    parser: ParserName
    score: int = Field(ge=0, le=2)
    note: str = ""


_JUDGEMENTS = TypeAdapter(dict[Key, Judgement])
_PROPOSALS = TypeAdapter(dict[Key, Proposal])
_ITEMS = TypeAdapter(list[ReviewItem])


def read_judgements(path: Path) -> dict[Key, Judgement]:
    return _JUDGEMENTS.validate_json(path.read_bytes()) if path.exists() else {}


def write_judgements(path: Path, judgements: dict[Key, Judgement]) -> None:
    """Atomic: a crash mid-write leaves the previous file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(_JUDGEMENTS.dump_json(judgements, indent=2))
    Path(tmp).replace(path)


def read_proposals(directory: Path) -> dict[Key, Proposal]:
    merged: dict[Key, Proposal] = {}
    for path in sorted(directory.glob("*.json")):
        merged |= _PROPOSALS.validate_json(path.read_bytes())
    return merged


def read_items(path: Path) -> list[ReviewItem]:
    return _ITEMS.validate_json(path.read_bytes())


def item_keys(item: ReviewItem) -> list[Key]:
    return [key(item.excerpt, item.page, p) for p in PARSERS]


def disagreements(
    blind: dict[Key, Judgement], proposals: dict[Key, Proposal]
) -> set[Key]:
    return {
        k for k, j in blind.items() if k in proposals and proposals[k].score != j.score
    }


def create_app(
    items: list[ReviewItem],
    blind_path: Path,
    reconciled_path: Path,
    proposals: dict[Key, Proposal],
    closed_path: Path,
    pages_dir: Path,
    katex_dir: Path,
) -> FastAPI:
    app = FastAPI(title="parser review")
    all_keys = {k for item in items for k in item_keys(item)}

    def mode() -> Mode:
        complete = all_keys <= set(read_judgements(blind_path))
        return "reconcile" if complete or closed_path.exists() else "blind"

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(PAGE)

    @app.get("/api/state")
    async def state() -> dict[str, object]:
        blind = read_judgements(blind_path)
        if mode() == "blind":
            return {"mode": "blind", "items": items, "scores": blind}
        differ = disagreements(blind, proposals)
        return {
            "mode": "reconcile",
            "items": [i for i in items if differ & set(item_keys(i))],
            "scores": read_judgements(reconciled_path),
            "blind": blind,
            "proposals": {k: proposals[k] for k in differ},
        }

    @app.put("/api/score")
    async def save(update: ScoreUpdate) -> Judgement:
        k = key(update.excerpt, update.page, update.parser)
        if k not in all_keys:
            raise HTTPException(404, f"no page {update.page} in {update.excerpt}")
        path = blind_path if mode() == "blind" else reconciled_path
        judgements = read_judgements(path)
        judgements[k] = Judgement(
            **update.model_dump(), at=datetime.now(UTC).isoformat()
        )
        write_judgements(path, judgements)
        return judgements[k]

    app.mount("/pages", StaticFiles(directory=pages_dir), name="pages")
    app.mount("/katex", StaticFiles(directory=katex_dir), name="katex")
    return app


def close_blind(
    items: list[ReviewItem], blind_path: Path, closed_path: Path
) -> BlindClosed:
    """End the blind pass where it stands; reconciling then covers only the
    pages that were scored."""
    total = sum(len(item_keys(i)) for i in items)
    closed = BlindClosed(
        closed_at=datetime.now(UTC).isoformat(),
        scored=len(read_judgements(blind_path)),
        total=total,
    )
    closed_path.parent.mkdir(parents=True, exist_ok=True)
    closed_path.write_text(closed.model_dump_json(indent=2))
    return closed


def main() -> None:
    items = read_items(ITEMS)
    if sys.argv[1:] == ["close-blind"]:
        closed = close_blind(items, BLIND, CLOSED)
        print(f"blind pass closed at {closed.scored} of {closed.total} judgements")
        return
    app = create_app(
        items,
        BLIND,
        RECONCILED,
        read_proposals(PROPOSALS),
        CLOSED,
        pages_dir=LOCAL / "pages",
        katex_dir=KATEX_TOOL / "node_modules" / "katex" / "dist",
    )
    print("review page: http://127.0.0.1:8009/")
    uvicorn.run(app, host="127.0.0.1", port=8009, log_level="warning")


if __name__ == "__main__":
    main()
