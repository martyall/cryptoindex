"""The human's parser scores (Phase 2): the reconcile pass and the final report.

Scoring is blind first (review page without Claude's proposals), then only
disagreements are reconciled:

    make parse-reconcile SCORES=<exported parse-scores.csv>
    make parse-report RECONCILED=<exported parse-scores-reconciled.csv>
"""

import csv
import json
import shutil
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from statistics import mean
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from cryptoindex.evaluation.parse_eval import ITEMS, LOCAL, SAMPLE, write_review_page

ParserName = Literal["marker", "paddle"]
Key = str  # "<excerpt>/<page>/<parser>", as the review page keys scores

BLIND = SAMPLE / "parse-scores.csv"
RECONCILED = SAMPLE / "parse-scores-reconciled.csv"
PROPOSALS = LOCAL / "proposals"
REPORT = Path("eval/parse-report.md")


class ScoreRow(BaseModel):
    excerpt: str
    page: int
    pdf_page: int
    parser: ParserName
    score: int = Field(ge=0, le=2)
    note: str = ""

    @property
    def key(self) -> Key:
        return f"{self.excerpt}/{self.page}/{self.parser}"


class Proposal(BaseModel):
    score: int = Field(ge=0, le=2)
    note: str = ""


_PROPOSAL_FILE = TypeAdapter(dict[Key, Proposal])


def read_scores(path: Path) -> dict[Key, ScoreRow]:
    """Rows of an exported scores CSV, validated, keyed by excerpt/page/parser."""
    with path.open(newline="") as f:
        rows = [ScoreRow.model_validate(row) for row in csv.DictReader(f)]
    return {row.key: row for row in rows}


def read_proposals(directory: Path) -> dict[Key, Proposal]:
    """Claude's proposals, one JSON file per excerpt."""
    merged: dict[Key, Proposal] = {}
    for path in sorted(directory.glob("*.json")):
        merged |= _PROPOSAL_FILE.validate_json(path.read_bytes())
    return merged


def disagreements(
    blind: dict[Key, ScoreRow], proposals: dict[Key, Proposal]
) -> set[Key]:
    return {
        k
        for k, row in blind.items()
        if k in proposals and proposals[k].score != row.score
    }


def final_scores(
    blind: dict[Key, ScoreRow], reconciled: dict[Key, ScoreRow]
) -> dict[Key, ScoreRow]:
    """The blind scores, with reconciled rows taking precedence."""
    return blind | reconciled


def reconcile(scores_csv: Path) -> None:
    shutil.copyfile(scores_csv, BLIND)
    blind = read_scores(BLIND)
    proposals = read_proposals(PROPOSALS)
    missing = sorted(set(proposals) - set(blind))
    if missing:
        sys.exit(f"{len(missing)} scores missing from {scores_csv}, e.g. {missing[0]}")
    differ = disagreements(blind, proposals)
    items = json.loads(ITEMS.read_text())
    subset = [
        item
        for item in items
        if any(
            f"{item['excerpt']}/{item['page']}/{p}" in differ
            for p in ("marker", "paddle")
        )
    ]
    write_review_page(
        {
            "mode": "reconcile",
            "items": subset,
            "proposals": {k: p.model_dump() for k, p in proposals.items()},
            "first_pass": {
                k: {"score": r.score, "note": r.note} for k, r in blind.items()
            },
        }
    )
    print(
        f"{len(differ)} of {len(blind)} scores differ from Claude's, on {len(subset)} "
        f"pages. Reload {(LOCAL / 'review.html').resolve()}"
    )


def report(reconciled_csv: Path | None) -> None:
    blind = read_scores(BLIND)
    reconciled: dict[Key, ScoreRow] = {}
    if reconciled_csv is not None:
        shutil.copyfile(reconciled_csv, RECONCILED)
        reconciled = read_scores(RECONCILED)
    proposals = read_proposals(PROPOSALS)
    final = final_scores(blind, reconciled)
    formulas = json.loads((LOCAL / "formulas.json").read_text())
    REPORT.write_text(render_report(final, blind, proposals, formulas))
    print(f"wrote {REPORT}")


def render_report(
    final: dict[Key, ScoreRow],
    blind: dict[Key, ScoreRow],
    proposals: dict[Key, Proposal],
    formulas: dict[str, dict[str, dict[str, int]]],
) -> str:
    by_excerpt: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for row in final.values():
        by_excerpt[row.excerpt][row.parser].append(row.score)
    parsers: tuple[ParserName, ...] = ("marker", "paddle")
    lines = [
        "# Parser evaluation report (Phase 2)",
        "",
        "Scores are the human's: blind first, then reconciled where they differed "
        "from Claude's proposal. 0 scrambled, 1 partially intact, 2 faithful, per "
        "page (EVALUATION.md §1). Formulas: math each parser recognized as math, "
        "and how many KaTeX could not render (undelimited equations count as "
        "failures).",
        "",
        "| Excerpt | Marker score | PaddleOCR-VL score | Marker formulas (failed) "
        "| PaddleOCR-VL formulas (failed) |",
        "|---|---|---|---|---|",
    ]
    for excerpt, scores in by_excerpt.items():
        f = formulas.get(excerpt, {})
        lines.append(
            f"| {excerpt} | "
            + " | ".join(f"{mean(scores[p]):.2f}" for p in parsers)
            + " | "
            + " | ".join(
                f"{f[p]['formulas']} ({f[p]['failed']})" if p in f else "—"
                for p in parsers
            )
            + " |"
        )
    overall = {
        p: _mean(r.score for r in final.values() if r.parser == p) for p in parsers
    }
    totals = {
        p: (
            sum(f[p]["formulas"] for f in formulas.values()),
            sum(f[p]["failed"] for f in formulas.values()),
        )
        for p in parsers
    }
    lines.append(
        "| **all** | "
        + " | ".join(f"**{overall[p]:.2f}**" for p in parsers)
        + " | "
        + " | ".join(
            f"**{totals[p][0]} ({totals[p][1]}, {totals[p][1] / totals[p][0]:.1%})**"
            for p in parsers
        )
        + " |"
    )
    differ = disagreements(blind, proposals)
    changed = sum(1 for k in differ if final[k].score != blind[k].score)
    lines += [
        "",
        f"Claude's proposals differed from the blind score on {len(differ)} of "
        f"{len(blind)} page scores; the human changed {changed} of those on "
        "reconciling.",
        "",
    ]
    return "\n".join(lines)


def _mean(values: Iterable[int]) -> float:
    items = list(values)
    return mean(items) if items else float("nan")


def main() -> None:
    command, *rest = sys.argv[1:]
    if command == "reconcile":
        reconcile(Path(rest[0]).expanduser())
    elif command == "report":
        report(Path(rest[0]).expanduser() if rest and rest[0] else None)
    else:
        sys.exit("usage: parse_scores reconcile SCORES.csv | report [RECONCILED.csv]")


if __name__ == "__main__":
    main()
