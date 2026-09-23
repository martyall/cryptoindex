"""docs/RUNNING.md stays true: its `make` commands name real targets, and its
local URLs are routes the app serves."""

import dataclasses
import shlex
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from gloss_fakes import CoveringLLM
from markdown_it import MarkdownIt

from cryptoindex.api import create_app
from cryptoindex.core.config import Settings
from cryptoindex.core.db import Pool
from cryptoindex.core.embed import FakeEmbedder
from cryptoindex.ingest.parsers import StubParser
from cryptoindex.ingest.runner import Runner
from cryptoindex.ingest.stages import StageContext
from cryptoindex.query.answer import AgentEvent, Tools

ROOT = Path(__file__).parents[1]
RUNNING = ROOT / "docs" / "RUNNING.md"
SERVICE_HOST = "127.0.0.1"

TOKENS = MarkdownIt().parse(RUNNING.read_text())


def _commands() -> list[list[str]]:
    """Every simple command in the page's `sh` blocks: each line as the shell
    reads it, split by shlex into words and cut at its operators (`|`, `|&`,
    `&&`, `;`), which shlex returns as tokens of their own."""
    blocks = [t.content for t in TOKENS if t.type == "fence" and t.info == "sh"]
    commands: list[list[str]] = []
    for line in (line for b in blocks for line in b.splitlines()):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        command: list[str] = []
        for token in lexer:
            if set(token) <= set(lexer.punctuation_chars):
                commands.append(command)
                command = []
            else:
                command.append(token)
        commands.append(command)
    return [c for c in commands if c]


def _make_targets() -> list[str]:
    # make reads an argument containing `=` as a variable assignment.
    return [
        arg
        for argv in _commands()
        if argv[0] == "make"
        for arg in argv[1:]
        if "=" not in arg
    ]


def _local_paths() -> list[str]:
    links = [
        child.attrGet("href")
        for t in TOKENS
        if t.type == "inline"
        for child in t.children or []
        if child.type == "link_open"
    ]
    words = [word for argv in _commands() for word in argv]
    urls = [urlsplit(u) for u in [*links, *words] if isinstance(u, str)]
    return [u.path for u in urls if u.hostname == SERVICE_HOST]


@pytest.mark.parametrize("target", _make_targets())
def test_each_make_command_names_a_target(target: str) -> None:
    run = subprocess.run(["make", "-n", target], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, run.stderr.decode()


def test_the_page_has_commands_and_urls_to_check() -> None:
    assert _make_targets() and _local_paths()


class NoAnswers:
    """The Ask page is mounted only with a handler; these tests never ask."""

    async def answer(self, question: str, tools: Tools) -> AsyncIterator[AgentEvent]:
        yield AgentEvent("error", {"error": "not used"})


@pytest.fixture
async def client(
    settings: Settings, pool: Pool, query_pool: Pool, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    ctx = StageContext(
        pool=pool,
        settings=dataclasses.replace(settings, data_dir=tmp_path),
        parser=StubParser(),
        llm=CoveringLLM(),
        embedder=FakeEmbedder(),
    )
    app = create_app(
        Runner(ctx), pool, ctx.settings, query_pool, ctx.embedder, handler=NoAnswers()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.mark.parametrize("path", _local_paths())
async def test_each_local_url_is_a_route(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code != 404
