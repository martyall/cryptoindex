import pytest

from cryptoindex.core.latex import formulas, latex_norm, normalize


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (r"\mathbb Z_{2} [x]", r"\mathbb{Z}_2[x]"),
        (r"x^{2} + 1", "x^2+1"),
        (r"\left( a \right) \, + b", "(a)+b"),
        (r"\frac12", r"\frac{1}{2}"),
        (r"\displaystyle\sum_{j=i}^{k} f_j", r"\sum_{j=i}^k f_j"),
    ],
)
def test_layout_does_not_change_the_normal_form(a: str, b: str) -> None:
    assert normalize(a) == normalize(b)


def test_different_formulas_stay_different() -> None:
    assert normalize(r"\alpha x") != normalize(r"\alphax")
    assert normalize(r"\mathbb{Z}_2") != normalize(r"\mathbb{Z}_3")


def test_unparseable_math_is_left_out() -> None:
    assert normalize(r"\frac{1}{") is None


def test_formulas_come_from_the_markdown_math() -> None:
    text = r"Since $x^2 + 1$ is irreducible over $$\mathbb{R}$$, see Table 1."
    assert formulas(text, None) == ["x^2 + 1", r"\mathbb{R}"]
    assert formulas("No math here.", None) == []


def test_an_undelimited_equation_block_is_all_formula() -> None:
    raw = r"\alpha^{2}+1=0"
    assert formulas(raw, "equation") == [raw]
    assert formulas(raw, None) == []
    assert latex_norm(raw, "equation") == normalize(raw)
    assert latex_norm("Prose.", None) is None
