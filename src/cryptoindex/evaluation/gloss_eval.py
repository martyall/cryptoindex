"""Phase 3 gloss spot-check (EVALUATION.md §2; D19). Local only: it calls the
configured real LLM backend, which CI never does. `make gloss-eval`.

It glosses the paragraphs PaddleOCR-VL produced for the Phase 2 excerpts,
exactly as the segment stage would (same chunks, prompt, and validation),
caching each validated reply by input hash so a re-run makes no new calls.
Then it picks a sample stratified by anchor kind for the review page
(`make gloss-review`). Each prompt version gets its own sample, listed in
eval/gloss-sample/spot-check-<prompt>.json (committed); everything derived
from the sources goes under eval/gloss-sample/local/, which is not in git.
"""

import asyncio
import hashlib
import html
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel, TypeAdapter, ValidationError

from cryptoindex.core import config
from cryptoindex.core.backends import build_llm
from cryptoindex.core.llm import LLM
from cryptoindex.core.prompts import Prompt, load_prompt
from cryptoindex.evaluation import parse_eval
from cryptoindex.ingest.document import Block
from cryptoindex.ingest.gloss import (
    GLOSS_PROMPT,
    Chunk,
    InvalidReplyError,
    SourceParagraph,
    UnitReply,
    chunks_of,
    gloss_request,
    input_hash,
    merge,
    quality_flags,
    validate_reply,
)
from cryptoindex.ingest.paragraphs import content_hash, paragraph_blocks
from cryptoindex.ingest.parsers import PaddleVLParser

log = logging.getLogger(__name__)

SAMPLE = Path("eval/gloss-sample")
LOCAL = SAMPLE / "local"
SPOT_CHECK = SAMPLE / f"spot-check-{GLOSS_PROMPT}.json"
UNITS = LOCAL / f"units-{GLOSS_PROMPT}.json"  # every glossed unit
FAILURES = LOCAL / f"failures-{GLOSS_PROMPT}.json"
ITEMS = LOCAL / f"review-items-{GLOSS_PROMPT}.json"  # the sampled units
# PaddleOCR-VL's raw output for the excerpts, written by `make parse-eval`
# (D21: the parser the pipeline uses).
PARSED = parse_eval.LOCAL / "output" / "paddle-3.7.0+PaddleOCR-VL-1.6"

CONCURRENCY = 3
ATTEMPTS = 2  # per chunk; a reply that fails validation is asked again
# Units per stratum in the spot-check; strata with fewer contribute all.
PER_STRATUM = 14
STRATA = ("theorem", "definition", "algorithm", "game", "example", "other", "plain")


@dataclass(frozen=True, slots=True)
class ExcerptText:
    name: str
    stresses: str
    title: str
    blocks: tuple[Block, ...]  # blocks[i] is paragraph i
    paragraphs: tuple[SourceParagraph, ...]
    images: tuple[str, ...]  # page images, relative to parse_eval.LOCAL


class Paragraph(BaseModel):
    pos: int
    page: int
    kind: str
    html: str


class GlossedUnit(BaseModel):
    """One unit as the review page shows it."""

    excerpt: str
    stresses: str
    model: str
    unit: UnitReply
    flags: list[str]
    paragraphs: list[Paragraph]
    images: list[str]  # the unit's page images


class Selected(BaseModel):
    excerpt: str
    first_pos: int
    last_pos: int
    anchor_label: str | None
    stratum: str


class SpotCheck(BaseModel):
    prompt: str
    model: str
    units: list[Selected]


class CachedReply(BaseModel):
    model: str
    response: object


class Failure(BaseModel):
    excerpt: str
    first_pos: int
    last_pos: int
    error: str


_ITEMS = TypeAdapter(list[GlossedUnit])

# Every block the reviewer sees is rendered: prose as Markdown with math, all
# equations (including those the parser left undelimited, which are raw
# LaTeX) and pseudocode lines as math where they contain it. Raw HTML in
# parser text is escaped; tables are the parser's own HTML.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)
_LINES = MarkdownIt("commonmark", {"html": False, "breaks": True}).use(
    dollarmath_plugin, allow_space=True, allow_digits=True, double_inline=True
)


def paragraph_html(block: Block) -> str:
    if block.kind == "table":
        return block.text
    if block.kind == "equation" and block.malformed_math:
        return f'<div class="math block">{html.escape(block.text)}</div>'
    if block.kind == "equation":
        return "".join(
            f'<div class="math block">{html.escape(m.tex)}</div>' for m in block.math
        )
    if block.kind in ("algorithm", "code"):
        return f'<div class="lines">{_LINES.render(block.text)}</div>'
    return _MARKDOWN.render(block.text)


def load_excerpts() -> list[ExcerptText]:
    parser = PaddleVLParser(config.settings.paddle_vlm_url)
    sample = parse_eval.load_sample(parse_eval.SAMPLE / "ids.toml")
    out = []
    for excerpt in sample.excerpt:
        document = parser.read((PARSED / f"{excerpt.name}.json").read_bytes())
        blocks = paragraph_blocks(document)
        paragraphs = tuple(
            SourceParagraph(
                pos,
                b.section_path,
                b.text,
                content_hash(b.text),
                None if b.kind == "text" else b.kind,
            )
            for pos, b in enumerate(blocks)
        )
        images = parse_eval.render_pages(
            parse_eval.LOCAL / "excerpts" / f"{excerpt.name}.pdf",
            parse_eval.LOCAL / "pages" / excerpt.name,
        )
        out.append(
            ExcerptText(
                excerpt.name,
                excerpt.stresses,
                # An excerpt's first heading ("Exercises") is no title.
                sample.source_of(excerpt).name,
                tuple(blocks),
                paragraphs,
                tuple(str(i.relative_to(parse_eval.LOCAL)) for i in images),
            )
        )
    return out


async def gloss_all(
    llm: LLM, prompt: Prompt, excerpts: Sequence[ExcerptText], cache: Path
) -> tuple[dict[str, list[tuple[Chunk, str, Sequence[UnitReply]]]], list[Failure]]:
    """Every chunk of every excerpt, CONCURRENCY calls at a time; replies are
    validated as the segment stage validates them and cached by input hash."""
    cache.mkdir(parents=True, exist_ok=True)
    limit = asyncio.Semaphore(CONCURRENCY)
    replies: dict[str, list[tuple[Chunk, str, Sequence[UnitReply]]]] = {
        e.name: [] for e in excerpts
    }
    failures: list[Failure] = []

    async def one(excerpt: ExcerptText, chunk: Chunk) -> None:
        path = (
            cache
            / f"{input_hash(chunk, excerpt.title, prompt.version, llm.model)}.json"
        )
        if path.exists():
            cached = CachedReply.model_validate_json(path.read_bytes())
            replies[excerpt.name].append(
                (chunk, cached.model, validate_reply(cached.response, chunk))
            )
            return
        request = gloss_request(chunk, excerpt.title, prompt)
        error = ""
        for attempt in range(ATTEMPTS):
            async with limit:
                log.info(
                    "gloss_call excerpt=%s span=%d-%d attempt=%d",
                    excerpt.name,
                    chunk.first_pos,
                    chunk.last_pos,
                    attempt + 1,
                )
                completion = await llm.complete(
                    request.system,
                    request.messages,
                    json_schema=request.json_schema,
                    cache_prefix=request.cache_prefix,
                )
            try:
                units = validate_reply(completion.parsed, chunk)
            except (ValidationError, InvalidReplyError) as e:
                error = str(e)
                log.warning("gloss_rejected excerpt=%s error=%s", excerpt.name, e)
                continue
            path.write_text(
                CachedReply(
                    model=completion.model,
                    response={"units": [u.model_dump(mode="json") for u in units]},
                ).model_dump_json()
            )
            replies[excerpt.name].append((chunk, completion.model, units))
            return
        failures.append(
            Failure(
                excerpt=excerpt.name,
                first_pos=chunk.first_pos,
                last_pos=chunk.last_pos,
                error=error,
            )
        )

    async with asyncio.TaskGroup() as tg:
        for excerpt in excerpts:
            for chunk in chunks_of(excerpt.paragraphs):
                tg.create_task(one(excerpt, chunk))
    return replies, failures


def glossed_units(
    excerpt: ExcerptText, replies: Sequence[tuple[Chunk, str, Sequence[UnitReply]]]
) -> list[GlossedUnit]:
    ordered = sorted(replies, key=lambda r: r[0].first_pos)
    model_of = {u.key: model for _, model, units in ordered for u in units}
    text = {p.position: p.text for p in excerpt.paragraphs}
    out = []
    for u in merge([(chunk, units) for chunk, _, units in ordered]):
        span = range(u.first_pos, u.last_pos + 1)
        pages = sorted({excerpt.blocks[pos].page for pos in span})
        out.append(
            GlossedUnit(
                excerpt=excerpt.name,
                stresses=excerpt.stresses,
                model=model_of[u.key],
                unit=u,
                flags=quality_flags(u, text),
                paragraphs=[
                    Paragraph(
                        pos=pos,
                        page=excerpt.blocks[pos].page,
                        kind=excerpt.blocks[pos].kind,
                        html=paragraph_html(excerpt.blocks[pos]),
                    )
                    for pos in span
                ],
                images=[excerpt.images[p] for p in pages],
            )
        )
    return out


def stratum(unit: UnitReply) -> str:
    return unit.anchor_kind or "plain"


def select(
    units: Sequence[GlossedUnit], per_stratum: int = PER_STRATUM
) -> list[Selected]:
    """Up to `per_stratum` units of each stratum, in an order fixed by a hash
    of each unit's identity, so the choice is reproducible and not biased
    towards the first excerpts."""

    def order(g: GlossedUnit) -> str:
        identity = json.dumps([g.excerpt, *g.unit.key])
        return hashlib.sha256(identity.encode()).hexdigest()

    chosen = []
    for s in STRATA:
        in_stratum = sorted((g for g in units if stratum(g.unit) == s), key=order)
        chosen += [
            Selected(
                excerpt=g.excerpt,
                first_pos=g.unit.first_pos,
                last_pos=g.unit.last_pos,
                anchor_label=g.unit.anchor_label,
                stratum=s,
            )
            for g in in_stratum[:per_stratum]
        ]
    return chosen


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = config.settings
    llm = build_llm(settings)
    prompt = load_prompt(GLOSS_PROMPT)
    excerpts = load_excerpts()
    replies, failures = asyncio.run(gloss_all(llm, prompt, excerpts, LOCAL / "replies"))
    units = [g for e in excerpts for g in glossed_units(e, replies[e.name])]
    LOCAL.mkdir(parents=True, exist_ok=True)
    FAILURES.write_text(json.dumps([f.model_dump() for f in failures], indent=2))
    (LOCAL / "units.json").write_bytes(_ITEMS.dump_json(units))
    if failures:
        print(
            f"{len(failures)} chunks failed validation; see {LOCAL / 'failures.json'}"
        )
    if SPOT_CHECK.exists():
        spot = SpotCheck.model_validate_json(SPOT_CHECK.read_bytes())
    else:
        spot = SpotCheck(prompt=prompt.version, model=llm.model, units=select(units))
        SPOT_CHECK.write_text(spot.model_dump_json(indent=2))
    wanted = {(s.excerpt, s.first_pos, s.last_pos, s.anchor_label) for s in spot.units}
    items = [g for g in units if (g.excerpt, *g.unit.key) in wanted]
    ITEMS.write_bytes(_ITEMS.dump_json(items))
    counts = {s: sum(1 for g in units if stratum(g.unit) == s) for s in STRATA}
    print(f"{len(units)} units; by stratum {counts}")
    print(f"spot-check: {len(items)} of {len(spot.units)} chosen units present")
    print("review: make gloss-review")


if __name__ == "__main__":
    main()
