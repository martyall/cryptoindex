"""Normalized LaTeX for trigram search (`paragraphs.latex_norm`). Formulas are
found with markdown-it's math plugin and read with pylatexenc's LaTeX
parser; nothing here scans text by hand. Used by ingestion (to fill the
column) and by queries (to normalize what the user typed the same way)."""

import logging
from collections.abc import Iterable, Sequence

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexNode,
    LatexSpecialsNode,
    LatexWalker,
    LatexWalkerError,
)

log = logging.getLogger(__name__)

# Spacing, sizing and style-of-display commands: they change how a formula
# looks, never what it says, and authors use them inconsistently.
_LAYOUT = frozenset(
    {",", ";", ":", "!", " ", "quad", "qquad", "left", "right", "middle"}
    | {"big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"}
    | {"displaystyle", "textstyle", "scriptstyle", "limits", "nolimits"}
)
_LAYOUT_SPECIALS = frozenset({"~"})

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)
_MATH_TOKENS = frozenset({"math_inline", "math_inline_double", "math_block"})


def formulas(text: str, block_kind: str | None) -> list[str]:
    """The LaTeX formulas in a paragraph's stored text: its `$`-delimited math,
    or, for an equation block with none (one the parser did not delimit, D21),
    the whole text."""
    found = [t.content for t in _inline_tokens(_MARKDOWN.parse(text))]
    if not found and block_kind == "equation":
        return [text]
    return found


def _inline_tokens(tokens: Sequence) -> Iterable:
    for t in tokens:
        if t.type in _MATH_TOKENS:
            yield t
        if t.children:
            yield from _inline_tokens(t.children)


def normalize(tex: str) -> str | None:
    """A canonical form of one formula: layout commands and comments dropped,
    whitespace removed, groups canonically braced (single characters bare), so
    `\\mathbb Z_{2}` and `\\mathbb{Z}_2` compare equal. None if the parser
    rejects it or nothing survives; the formula is then left out of the index,
    never guessed at."""
    try:
        nodes, _, _ = LatexWalker(tex, tolerant_parsing=False).get_latex_nodes()
    except LatexWalkerError as e:
        log.debug("latex_unparsed", extra={"error": str(e)})
        return None
    return _emit(nodes).strip() or None


def latex_norm(text: str, block_kind: str | None) -> str | None:
    """The value of `paragraphs.latex_norm`: the paragraph's normalized
    formulas, space-separated, or None if it has none that parse."""
    normalized = [n for f in formulas(text, block_kind) if (n := normalize(f))]
    return " ".join(normalized) or None


def _emit(nodes: Iterable[LatexNode | None]) -> str:
    out: list[str] = []
    for node in nodes:
        if node is None or isinstance(node, LatexCommentNode):
            continue
        if isinstance(node, LatexCharsNode):
            out.append("".join(ch for ch in node.chars if not ch.isspace()))
        elif isinstance(node, LatexGroupNode):
            out.append(_group(node.nodelist))
        elif isinstance(node, LatexMacroNode):
            if node.macroname in _LAYOUT:
                continue
            # The space keeps `\alpha x` from reading as `\alphax`.
            out.append(f"\\{node.macroname} ")
            for arg in node.nodeargd.argnlist if node.nodeargd else []:
                out.append(_argument(arg))
        elif isinstance(node, LatexSpecialsNode):
            if node.specials_chars not in _LAYOUT_SPECIALS:
                out.append(node.specials_chars)
        elif isinstance(node, LatexEnvironmentNode):
            out.append(
                f"\\begin{{{node.environmentname}}}{_emit(node.nodelist)}"
                f"\\end{{{node.environmentname}}}"
            )
        elif isinstance(node, LatexMathNode):
            out.append(_emit(node.nodelist))
    return "".join(out)


def _group(nodelist: Iterable[LatexNode | None]) -> str:
    inner = _emit(nodelist).strip()
    return inner if len(inner) == 1 else f"{{{inner}}}"


def _argument(arg: LatexNode | None) -> str:
    if arg is None:
        return ""
    inner = _emit(arg.nodelist) if isinstance(arg, LatexGroupNode) else _emit([arg])
    return f"{{{inner.strip()}}}"
