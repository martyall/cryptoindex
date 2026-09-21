import csv
from pathlib import Path

import pytest
from pydantic import ValidationError

from cryptoindex.evaluation.parse_scores import (
    Proposal,
    ScoreRow,
    disagreements,
    final_scores,
    read_proposals,
    read_scores,
    render_report,
)

HEADER = ["excerpt", "page", "pdf_page", "parser", "score", "note"]


def write_csv(path: Path, rows: list[list[object]]) -> Path:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def row(excerpt: str, page: int, parser: str, score: int, note: str = "") -> ScoreRow:
    return ScoreRow.model_validate(
        {
            "excerpt": excerpt,
            "page": page,
            "pdf_page": page + 10,
            "parser": parser,
            "score": score,
            "note": note,
        }
    )


def test_exported_csv_round_trips_with_quoted_notes(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "s.csv",
        [
            ["ex", 0, 10, "marker", 1, 'box "A", line 3 lost\nand more'],
            ["ex", 0, 10, "paddle", 2, ""],
        ],
    )
    scores = read_scores(path)
    assert scores["ex/0/marker"].note == 'box "A", line 3 lost\nand more'
    assert scores["ex/0/paddle"].score == 2


@pytest.mark.parametrize(
    "bad", [["ex", 0, 10, "marker", 3, ""], ["ex", 0, 10, "gpt", 1, ""]]
)
def test_out_of_range_score_or_unknown_parser_is_rejected(
    tmp_path: Path, bad: list[object]
) -> None:
    with pytest.raises(ValidationError):
        read_scores(write_csv(tmp_path / "s.csv", [bad]))


def test_proposals_merge_across_files(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"a/0/marker": {"score": 1, "note": "x"}}')
    (tmp_path / "b.json").write_text('{"b/0/paddle": {"score": 2}}')
    assert read_proposals(tmp_path) == {
        "a/0/marker": Proposal(score=1, note="x"),
        "b/0/paddle": Proposal(score=2),
    }


def test_reconciled_scores_override_blind_ones() -> None:
    blind = {r.key: r for r in (row("ex", 0, "marker", 1), row("ex", 0, "paddle", 2))}
    proposals = {"ex/0/marker": Proposal(score=2), "ex/0/paddle": Proposal(score=2)}
    assert disagreements(blind, proposals) == {"ex/0/marker"}
    final = final_scores(blind, {"ex/0/marker": row("ex", 0, "marker", 2)})
    assert final["ex/0/marker"].score == 2 and final["ex/0/paddle"].score == 2


def test_report_summarizes_scores_formulas_and_reconciling() -> None:
    blind = {
        r.key: r
        for r in (
            row("ex", 0, "marker", 0),
            row("ex", 1, "marker", 2),
            row("ex", 0, "paddle", 1),
            row("ex", 1, "paddle", 1),
        )
    }
    proposals = {"ex/0/marker": Proposal(score=1), "ex/1/paddle": Proposal(score=2)}
    final = final_scores(blind, {"ex/0/marker": row("ex", 0, "marker", 1)})
    formulas = {
        "ex": {
            "marker": {"formulas": 10, "failed": 1},
            "paddle": {"formulas": 40, "failed": 4},
        }
    }
    text = render_report(final, blind, proposals, formulas)
    assert "| ex | 1.50 | 1.00 | 10 (1) | 40 (4) |" in text
    assert "**10 (1, 10.0%)**" in text
    assert (
        "differed from the blind score on 2 of 4 page scores; the human changed 1"
        in text
    )
