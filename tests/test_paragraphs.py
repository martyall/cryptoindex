import pytest

from cryptoindex.ingest.paragraphs import content_hash, latex_norm, split_paragraphs

DOC = r"""
# 2 Groups

A *group* is a set with an operation.
It continues on the next line.

![](_page_3_Picture_1.jpeg)

**Definition 2.1.** A group $(G, \cdot)$ is abelian if $ab = ba$.

$$
a \cdot b
  = b \cdot a
$$

## 2.1 Subgroups

**Theorem 2.4** (Lagrange). If $H \le G$ then $|H|$ divides $|G|$.

*Proof.* Cosets partition $G$.

```
for x in G:

    visit(x)
```

| a | b |
|---|---|
| 1 | 2 |

\begin{align}
x &= y \\

y &= z
\end{align}

# 3 Rings

Definitions of rings follow.
"""


def test_paragraph_boundaries_and_sections() -> None:
    paras = split_paragraphs(DOC)
    sub = "2 Groups > 2.1 Subgroups"
    assert [(p.section_path, p.text.splitlines()[0][:20]) for p in paras] == [
        ("2 Groups", "A *group* is a set w"),
        ("2 Groups", "**Definition 2.1.** "),
        ("2 Groups", "$$"),
        (sub, "**Theorem 2.4** (Lag"),
        (sub, "*Proof.* Cosets part"),
        (sub, "```"),
        (sub, "| a | b |"),
        (sub, r"\begin{align}"),
        ("3 Rings", "Definitions of rings"),
    ]
    assert [p.position for p in paras] == list(range(len(paras)))
    assert "visit(x)" in paras[5].text  # blank line inside the fence did not split it
    assert r"y &= z" in paras[7].text  # nor inside the environment


def test_formal_blocks_and_equations() -> None:
    kinds = [(p.block_kind, p.block_label) for p in split_paragraphs(DOC)]
    assert kinds == [
        (None, None),
        ("definition", "Definition 2.1"),
        ("equation", None),
        ("theorem", "Theorem 2.4"),
        ("proof", "Proof"),
        (None, None),
        (None, None),
        ("equation", None),
        (None, None),  # "Definitions of…" is prose, not a definition
    ]


@pytest.mark.parametrize(
    "text, kind, label",
    [
        ("Lemma 3. Every x.", "lemma", "Lemma 3"),
        ("_Corollary 1.2.3_ Hence y.", "corollary", "Corollary 1.2.3"),
        ("ALGORITHM 1 Merge sort", "algorithm", "Algorithm 1"),
        ("Remark. Note z.", "remark", "Remark"),
        ("Game G₀ is the real game.", "game", "Game"),
        ("Theorems are nice.", None, None),
        (r"\begin{algorithm}", None, None),
    ],
)
def test_formal_block_variants(text: str, kind: str | None, label: str | None) -> None:
    [p] = split_paragraphs(text)
    assert (p.block_kind, p.block_label) == (kind, label)


def test_same_markdown_same_paragraphs() -> None:
    assert split_paragraphs(DOC) == split_paragraphs(DOC)


def test_hash_ignores_reflow_only() -> None:
    assert content_hash("a  b\nc") == content_hash("a b c")
    assert content_hash("a b c") != content_hash("a b d")


def test_latex_norm() -> None:
    assert latex_norm(r"If $\left( a \, + b \right)$ and $$x\quad y$$.") == "(a+b) xy"
    assert latex_norm(r"\begin{align} a &= b \end{align}") == "a&=b"
    assert latex_norm("no math here") is None


def test_empty_and_image_only_input() -> None:
    assert split_paragraphs("") == []
    assert split_paragraphs("![](x.png)\n\n   \n") == []
