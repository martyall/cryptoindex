"""The parser evaluation report (Phase 2), from the human's saved judgements:
the blind scores, overridden by the reconciled ones. `make parse-report`."""

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from statistics import mean

from cryptoindex.evaluation.parse_eval import LOCAL, SAMPLE, load_sample
from cryptoindex.evaluation.review_server import (
    BLIND,
    PARSERS,
    PROPOSALS,
    RECONCILED,
    Judgement,
    Key,
    Proposal,
    disagreements,
    read_judgements,
    read_proposals,
)

REPORT = Path("eval/parse-report.md")


def final_scores(
    blind: dict[Key, Judgement], reconciled: dict[Key, Judgement]
) -> dict[Key, Judgement]:
    """The blind scores, with reconciled ones taking precedence."""
    return blind | reconciled


def render_report(
    blind: dict[Key, Judgement],
    reconciled: dict[Key, Judgement],
    proposals: dict[Key, Proposal],
    formulas: dict[str, dict[str, dict[str, int]]],
    excerpts: dict[str, tuple[int, str]],
) -> str:
    """`excerpts` maps each excerpt to (pages, what it stresses), in sample
    order, so unscored pages and excerpts are reported rather than hidden."""
    final = final_scores(blind, reconciled)
    by_excerpt: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for judgement in final.values():
        by_excerpt[judgement.excerpt][judgement.parser].append(judgement.score)
    lines = [
        "# Parser evaluation report (Phase 2)",
        "",
        "Scores are the human's, per page, given blind (without seeing Claude's "
        "independent proposals) and optionally reconciled afterwards where the two "
        "differed. 0 scrambled, 1 partially "
        "intact, 2 faithful (EVALUATION.md §1). Formulas: math each parser "
        "recognized as math, and how many KaTeX could not render (undelimited "
        "equations count as failures). A parser that emits math as plain text has "
        "fewer formulas to fail, so read the two columns together.",
        "",
        "| Excerpt | Pages scored | Marker score | PaddleOCR-VL score "
        "| Marker formulas (failed) | PaddleOCR-VL formulas (failed) |",
        "|---|---|---|---|---|---|",
    ]
    for excerpt, scores in by_excerpt.items():
        counts = formulas.get(excerpt, {})
        scored = min(len(scores[p]) for p in PARSERS)
        lines.append(
            f"| {excerpt} | {scored} of {excerpts[excerpt][0]} | "
            + " | ".join(f"{_mean(scores[p]):.2f}" for p in PARSERS)
            + " | "
            + " | ".join(
                f"{counts[p]['formulas']} ({counts[p]['failed']})"
                if p in counts
                else "—"
                for p in PARSERS
            )
            + " |"
        )
    totals = {
        p: (
            sum(f[p]["formulas"] for f in formulas.values() if p in f),
            sum(f[p]["failed"] for f in formulas.values() if p in f),
        )
        for p in PARSERS
    }
    scored_pages = sum(min(len(s[p]) for p in PARSERS) for s in by_excerpt.values())
    lines.append(
        f"| **all** | {scored_pages} of {sum(n for n, _ in excerpts.values())} | "
        + " | ".join(
            f"**{_mean(s for e in by_excerpt.values() for s in e[p]):.2f}**"
            for p in PARSERS
        )
        + " | "
        + " | ".join(
            f"**{found} ({failed}, {failed / found:.1%})**" if found else "**0**"
            for found, failed in totals.values()
        )
        + " |"
    )
    unscored = [(e, why) for e, (_, why) in excerpts.items() if e not in by_excerpt]
    if unscored:
        lines += [
            "",
            "**Not scored** (the blind pass was closed early, so these were not "
            "evaluated): " + "; ".join(f"{e} ({why})" for e, why in unscored) + ".",
        ]
    differ = disagreements(blind, proposals)
    changed = sum(1 for k in differ if final[k].score != blind[k].score)
    outcome = (
        f"on reconciling, the human changed {changed} of them."
        if reconciled
        else "reconciliation was skipped, so the blind scores are final."
    )
    lines += [
        "",
        f"Claude's proposals differed from the blind score on {len(differ)} of "
        f"{len(blind)} page scores; {outcome}",
        "",
    ]
    return "\n".join(lines)


def _mean(values: Iterable[int]) -> float:
    items = list(values)
    return mean(items) if items else float("nan")


def main() -> None:
    blind = read_judgements(BLIND)
    if not blind:
        raise SystemExit(f"no blind scores in {BLIND}; run make parse-review first")
    sample = load_sample(SAMPLE / "ids.toml")
    report = render_report(
        blind,
        read_judgements(RECONCILED),
        read_proposals(PROPOSALS),
        json.loads((LOCAL / "formulas.json").read_text()),
        {e.name: (e.last_page - e.first_page + 1, e.stresses) for e in sample.excerpt},
    )
    REPORT.write_text(report)
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
