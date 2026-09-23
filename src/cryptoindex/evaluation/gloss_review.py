"""The gloss spot-check review page as a small local server (`make
gloss-review`), and its report (`make gloss-report`).

One unit at a time, every judgement saved to disk as it is made. The pass is
blind: the page shows the unit's paragraphs and what the model wrote, never
Claude's opinion of it. Binds to 127.0.0.1 only.
"""

import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter

from cryptoindex.evaluation import parse_eval
from cryptoindex.evaluation.gloss_eval import (
    FAILURES,
    ITEMS,
    SAMPLE,
    STRATA,
    UNITS,
    Failure,
    GlossedUnit,
    stratum,
)
from cryptoindex.ingest.gloss import GLOSS_PROMPT

BLIND = SAMPLE / "scores" / GLOSS_PROMPT / "blind.json"
REPORT = Path(f"eval/gloss-report-{GLOSS_PROMPT}.md")
PAGE = Path(__file__).with_name("gloss_review.html")

Verdict = Literal["faithful", "overstated", "wrong", "unclear"]
# not_sensible appears only in gloss-v1 scores; the page offers too_narrow
# and too_broad instead.
Boundaries = Literal["sensible", "too_narrow", "too_broad", "not_sensible"]
# Whether the parsed text the unit was glossed from is readable, so parser
# noise is not mistaken for a bad gloss (D23).
Source = Literal["readable", "garbled"]
# A lookup key built by `key()`; never parsed back: every record carries its
# fields itself.
Key = str
# D23: a prompt version is frozen when at least this share of glosses on
# readable source text is faithful and no theorem or definition gloss is wrong.
FREEZE_FAITHFUL = 0.9


def key(excerpt: str, first_pos: int, last_pos: int, anchor: str | None) -> Key:
    return f"{excerpt}/{first_pos}-{last_pos}/{anchor or ''}"


def item_key(item: GlossedUnit) -> Key:
    return key(item.excerpt, *item.unit.key)


class ScoreUpdate(BaseModel):
    excerpt: str
    first_pos: int
    last_pos: int
    anchor_label: str | None
    verdict: Verdict | None = None
    boundaries: Boundaries | None = None
    source: Source | None = None
    note: str = ""


class Judgement(ScoreUpdate):
    at: str  # when it was last saved, ISO 8601


_JUDGEMENTS = TypeAdapter(dict[Key, Judgement])
_ITEMS = TypeAdapter(list[GlossedUnit])
_FAILURES = TypeAdapter(list[Failure])


def read_judgements(path: Path) -> dict[Key, Judgement]:
    return _JUDGEMENTS.validate_json(path.read_bytes()) if path.exists() else {}


def write_judgements(path: Path, judgements: dict[Key, Judgement]) -> None:
    """Atomic: a crash mid-write leaves the previous file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(_JUDGEMENTS.dump_json(judgements, indent=2))
        f.flush()
        os.fsync(f.fileno())
    Path(tmp).replace(path)


def create_app(
    items: list[GlossedUnit], blind_path: Path, pages_dir: Path, katex_dir: Path
) -> FastAPI:
    app = FastAPI(title="gloss review")
    keys = {item_key(i) for i in items}

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(PAGE)

    @app.get("/api/state")
    async def state() -> dict[str, object]:
        return {"items": items, "scores": read_judgements(blind_path)}

    @app.put("/api/score")
    async def save(update: ScoreUpdate) -> Judgement:
        k = key(update.excerpt, update.first_pos, update.last_pos, update.anchor_label)
        if k not in keys:
            raise HTTPException(404, f"no unit {k}")
        judgements = read_judgements(blind_path)
        judgements[k] = Judgement(
            **update.model_dump(), at=datetime.now(UTC).isoformat()
        )
        write_judgements(blind_path, judgements)
        return judgements[k]

    app.mount("/pages", StaticFiles(directory=pages_dir), name="pages")
    app.mount("/katex", StaticFiles(directory=katex_dir), name="katex")
    return app


def render_report(
    items: list[GlossedUnit],
    judgements: dict[Key, Judgement],
    failures: list[Failure],
    all_units: list[GlossedUnit],
    prompt: str = GLOSS_PROMPT,
) -> str:
    judged = [
        (i, judgements[item_key(i)])
        for i in items
        if item_key(i) in judgements and judgements[item_key(i)].verdict
    ]
    verdicts = Counter(j.verdict for _, j in judged)
    faithful = verdicts["faithful"] / len(judged) if judged else 0.0
    readable = [j for _, j in judged if j.source == "readable"]
    readable_faithful = sum(1 for j in readable if j.verdict == "faithful")
    wrong_formal = [
        (i, j)
        for i, j in judged
        if j.verdict == "wrong" and stratum(i.unit) in ("theorem", "definition")
    ]
    bounded = Counter(j.boundaries for _, j in judged if j.boundaries)
    frozen = (
        readable
        and readable_faithful / len(readable) >= FREEZE_FAITHFUL
        and not wrong_formal
    )
    lines = [
        f"# Gloss spot-check report: {prompt}",
        "",
        "Generated by `make gloss-report` from"
        f" eval/gloss-sample/scores/{prompt}/blind.json.",
        "",
        f"- Units glossed: {len(all_units)} from"
        f" {len({u.excerpt for u in all_units})} excerpts;"
        f" {len(failures)} chunks failed validation after retries.",
        f"- Reviewed: {len(judged)} of {len(items)} sampled units.",
        f"- Faithful: {verdicts['faithful']} ({faithful:.0%});"
        f" overstated {verdicts['overstated']}, wrong {verdicts['wrong']},"
        f" unclear {verdicts['unclear']}.",
        f"- On readable source text: {readable_faithful} of {len(readable)}"
        f" faithful; {sum(1 for _, j in judged if j.source == 'garbled')}"
        " units had garbled source text.",
        f"- Wrong theorem or definition glosses: {len(wrong_formal)}.",
        f"- Boundaries sensible: {bounded['sensible']} of {bounded.total()}"
        f" (too narrow {bounded['too_narrow']}, too broad {bounded['too_broad']},"
        f" not sensible {bounded['not_sensible']}).",
        f"- Freeze criterion (D23: at least {FREEZE_FAITHFUL:.0%} faithful on"
        " readable source text, no wrong theorem or definition gloss):"
        f" {'met' if frozen else 'not met'}.",
        "",
        "## By stratum",
        "",
        "| stratum | reviewed | faithful | overstated | wrong | unclear |",
        "|---|---|---|---|---|---|",
    ]
    for s in STRATA:
        c = Counter(j.verdict for i, j in judged if stratum(i.unit) == s)
        if c:
            lines.append(
                f"| {s} | {c.total()} | {c['faithful']} | {c['overstated']}"
                f" | {c['wrong']} | {c['unclear']} |"
            )
    flagged = Counter(f for u in all_units for f in u.flags)
    lines += ["", "## Mechanical flags (all glossed units)", ""]
    lines += [f"- {f}: {n}" for f, n in sorted(flagged.items())] or ["- none"]
    notes = [(i, j) for i, j in judged if j.verdict != "faithful" or j.note]
    lines += ["", "## Reviewer notes and non-faithful units", ""]
    for i, j in notes:
        label = f" ({i.unit.anchor_label})" if i.unit.anchor_label else ""
        lines.append(
            f"- **{i.excerpt} {i.unit.first_pos}-{i.unit.last_pos}{label}**:"
            f" {j.verdict}, boundaries {j.boundaries or 'unjudged'},"
            f" source {j.source or 'unjudged'}."
            f" Gloss: {i.unit.gloss}" + (f" Note: {j.note}" if j.note else "")
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    items = _ITEMS.validate_json(ITEMS.read_bytes())
    if sys.argv[1:] == ["report"]:
        failures = _FAILURES.validate_json(FAILURES.read_bytes())
        all_units = _ITEMS.validate_json(UNITS.read_bytes())
        REPORT.write_text(
            render_report(items, read_judgements(BLIND), failures, all_units)
        )
        print(f"wrote {REPORT}")
        return
    app = create_app(
        items,
        BLIND,
        pages_dir=parse_eval.LOCAL / "pages",
        katex_dir=parse_eval.KATEX_TOOL / "node_modules" / "katex" / "dist",
    )
    print("review page: http://127.0.0.1:8010/")
    uvicorn.run(app, host="127.0.0.1", port=8010, log_level="warning")


if __name__ == "__main__":
    main()
