import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

FORMAL_KINDS = (
    "theorem",
    "lemma",
    "proposition",
    "corollary",
    "definition",
    "proof",
    "example",
    "exercise",
    "remark",
    "algorithm",
    "game",
)
_FORMAL = re.compile(
    r"^[\s*_]*(" + "|".join(FORMAL_KINDS) + r")\b[\s*_]*([0-9][0-9A-Za-z.\-]*)?",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_IMAGE_ONLY = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_BEGIN_ENV = re.compile(r"^\s*\\begin\{(\w+\*?)\}")
_MATH_ENVS = {"equation", "align", "gather", "multline", "eqnarray", "displaymath"}
_MATH_SPAN = re.compile(
    r"\$\$(.+?)\$\$|\$(.+?)\$|\\\[(.+?)\\\]|\\\((.+?)\\\)"
    r"|\\begin\{(\w+\*?)\}(.+?)\\end\{\5\}",
    re.DOTALL,
)
_LATEX_NOISE = re.compile(r"\\(left|right|displaystyle|[,;:! ]|q?quad)(?![a-zA-Z])|\s+")


@dataclass(frozen=True, slots=True)
class Paragraph:
    position: int
    section_path: str
    text: str
    content_hash: str
    block_kind: str | None
    block_label: str | None
    latex_norm: str | None


def split_paragraphs(markdown: str) -> list[Paragraph]:
    """Split parser Markdown into paragraphs in document order.

    Deterministic: the same Markdown always yields the same positions and
    hashes, which is what keeps paragraph IDs stable across re-parses
    (Invariant 5). Blank lines separate paragraphs. Fenced code, `$$` display
    math, `\\begin…\\end` environments and tables are paragraphs of their own,
    even across blank lines. Headings are not paragraphs: they set
    `section_path`. Image-only lines are dropped.
    """
    blocks: list[tuple[str, str]] = []  # (section_path, text)
    sections: list[tuple[int, str]] = []
    current: list[str] = []

    def flush() -> None:
        text = "\n".join(current).strip()
        if text:
            blocks.append((" > ".join(title for _, title in sections), text))
        current.clear()

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if heading := _HEADING.match(line):
            flush()
            level = len(heading.group(1))
            while sections and sections[-1][0] >= level:
                sections.pop()
            sections.append((level, heading.group(2).strip("*_ ")))
        elif _FENCE.match(line):
            flush()
            end = _find(lines, i + 1, lambda s: _FENCE.match(s) is not None)
            current.extend(lines[i : end + 1])
            flush()
            i = end
        elif line.strip().startswith("$$") and not _closes_display(line):
            flush()
            end = _find(lines, i + 1, lambda s: s.rstrip().endswith("$$"))
            current.extend(lines[i : end + 1])
            flush()
            i = end
        elif line.strip().startswith("$$"):
            flush()
            current.append(line)
            flush()
        elif env := _BEGIN_ENV.match(line):
            flush()
            closing = f"\\end{{{env.group(1)}}}"
            end = (
                i
                if closing in line
                else _find(lines, i + 1, lambda s, c=closing: c in s)
            )
            current.extend(lines[i : end + 1])
            flush()
            i = end
        elif line.lstrip().startswith("|"):
            flush()
            end = i
            while end + 1 < len(lines) and lines[end + 1].lstrip().startswith("|"):
                end += 1
            current.extend(lines[i : end + 1])
            flush()
            i = end
        elif not line.strip():
            flush()
        elif not _IMAGE_ONLY.match(line):
            current.append(line)
        i += 1
    flush()

    return [_paragraph(pos, path, text) for pos, (path, text) in enumerate(blocks)]


def content_hash(text: str) -> str:
    """Hash of the text with whitespace runs collapsed, so reflowed lines keep
    the same hash."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def latex_norm(text: str) -> str | None:
    """The paragraph's math with spacing commands and whitespace removed, for
    trigram matching; None if it has no math."""
    spans: list[str] = []
    for m in _MATH_SPAN.finditer(text):
        body = m.group(6) if m.group(5) else next(g for g in m.groups()[:4] if g)
        spans.append(_LATEX_NOISE.sub("", body))
    joined = " ".join(s for s in spans if s)
    return joined or None


def _paragraph(position: int, section_path: str, text: str) -> Paragraph:
    kind = label = None
    if formal := _FORMAL.match(text):
        kind = formal.group(1).lower()
        number = formal.group(2)
        label = kind.capitalize() + (f" {number.rstrip('.')}" if number else "")
    elif text.lstrip().startswith(("$$", "\\[")) or (
        (env := _BEGIN_ENV.match(text)) and env.group(1).rstrip("*") in _MATH_ENVS
    ):
        kind = "equation"
    return Paragraph(
        position=position,
        section_path=section_path,
        text=text,
        content_hash=content_hash(text),
        block_kind=kind,
        block_label=label,
        latex_norm=latex_norm(text),
    )


def _closes_display(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) > 2 and stripped.endswith("$$")


def _find(lines: list[str], start: int, pred: Callable[[str], bool]) -> int:
    """Index of the first line from `start` matching `pred`, or the last line."""
    for j in range(start, len(lines)):
        if pred(lines[j]):
            return j
    return len(lines) - 1
