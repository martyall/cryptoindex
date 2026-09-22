from cryptoindex.evaluation.parse_scores import final_scores, render_report
from cryptoindex.evaluation.review_server import Judgement, Proposal, key


def judged(page: int, parser: str, score: int) -> tuple[str, Judgement]:
    return key("ex", page, parser), Judgement.model_validate(
        {"excerpt": "ex", "page": page, "parser": parser, "score": score}
    )


def test_reconciled_scores_override_blind_ones() -> None:
    blind = dict([judged(0, "marker", 1), judged(0, "paddle", 2)])
    final = final_scores(blind, dict([judged(0, "marker", 2)]))
    assert final["ex/0/marker"].score == 2 and final["ex/0/paddle"].score == 2


def test_report_summarizes_scores_formulas_and_reconciling() -> None:
    blind = dict(
        [
            judged(0, "marker", 0),
            judged(1, "marker", 2),
            judged(0, "paddle", 1),
            judged(1, "paddle", 1),
        ]
    )
    reconciled = dict([judged(0, "marker", 1)])
    proposals = {"ex/0/marker": Proposal(score=1), "ex/1/paddle": Proposal(score=2)}
    formulas = {
        "ex": {
            "marker": {"formulas": 10, "failed": 1},
            "paddle": {"formulas": 40, "failed": 4},
        }
    }
    excerpts = {"ex": (3, "pseudocode"), "unseen": (5, "diagrams")}
    text = render_report(blind, reconciled, proposals, formulas, excerpts)
    assert "| ex | 2 of 3 | 1.50 | 1.00 | 10 (1) | 40 (4) |" in text
    assert "**Not scored**" in text and "unseen (diagrams)" in text
    assert "**10 (1, 10.0%)**" in text
    assert "differed from the blind score on 2 of 4 page scores" in text
    assert "the human changed 1 of them" in text
