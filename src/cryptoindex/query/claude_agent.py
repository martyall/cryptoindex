"""The first answer handler (D28): the Claude Agent SDK, which runs the tool
loop in a Claude Code subprocess. It uses the Claude Code login, so the
human's subscription (dev mode, D22), unless ANTHROPIC_API_KEY is set, in
which case the API."""

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    SdkMcpTool,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from cryptoindex.core.prompts import Prompt
from cryptoindex.query.answer import ANSWER_SCHEMA, AgentEvent, Tools

SERVER = "cryptoindex"

_STRING = {"type": "string"}
_INT = {"type": "integer"}


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


class ClaudeAgentHandler:
    """An AnswerHandler on the Claude Agent SDK: our tools as in-process
    functions, Claude Code's own tools, settings and session files off, the
    answer as structured output in ANSWER_SCHEMA."""

    def __init__(self, model: str, prompt: Prompt, max_turns: int) -> None:
        self.model = model
        self._prompt = prompt
        self._max_turns = max_turns

    async def answer(self, question: str, tools: Tools) -> AsyncIterator[AgentEvent]:
        try:
            async for event in self._answer(question, tools):
                yield event
        except ClaudeSDKError as e:
            yield AgentEvent("error", {"error": repr(e)})

    async def _answer(self, question: str, tools: Tools) -> AsyncIterator[AgentEvent]:
        results: asyncio.Queue[AgentEvent] = asyncio.Queue()
        sdk_tools = _sdk_tools(tools, results)
        # The SDK names our tools mcp__<server>__<tool>; the events use ours.
        ours = {f"mcp__{SERVER}__{t.name}": t.name for t in sdk_tools}
        with tempfile.TemporaryDirectory(prefix="ci-agent-") as cwd:
            options = ClaudeAgentOptions(
                system_prompt=self._prompt.text,
                model=self.model,
                mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=sdk_tools)},
                tools=[],
                allowed_tools=list(ours),
                setting_sources=[],
                max_turns=self._max_turns,
                cwd=cwd,
                output_format={"type": "json_schema", "schema": ANSWER_SCHEMA},
            )
            async with ClaudeSDKClient(options=options) as client:
                await client.query(question)
                async for message in client.receive_response():
                    while not results.empty():
                        yield results.get_nowait()
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ToolUseBlock) and block.name in ours:
                                yield AgentEvent(
                                    "tool_call",
                                    {"tool": ours[block.name], "input": block.input},
                                )
                    elif isinstance(message, ResultMessage):
                        yield _result_event(message)
                while not results.empty():
                    yield results.get_nowait()


def _result_event(message: ResultMessage) -> AgentEvent:
    if message.is_error or message.structured_output is None:
        return AgentEvent(
            "error",
            {
                "subtype": message.subtype,
                "result": message.result,
            },
        )
    return AgentEvent("answer", dict(message.structured_output))


def _sdk_tools(tools: Tools, results: asyncio.Queue[AgentEvent]) -> list[SdkMcpTool]:
    """The tools as the SDK takes them. Each returns its result as JSON text,
    and queues a `tool_result` event saying how many paragraphs it returned
    that the session had not seen."""

    def wrap(
        name: str, run: Callable[[dict], Awaitable[dict]]
    ) -> Callable[[dict], Awaitable[dict]]:
        async def call(args: dict) -> dict:
            seen = len(tools.retrieved)
            out: dict[str, object]
            try:
                out = await run(args)
            except ValueError as e:  # a malformed ID from the model
                out = {"error": str(e)}
            await results.put(
                AgentEvent(
                    "tool_result",
                    {
                        "tool": name,
                        "new_paragraphs": len(tools.retrieved) - seen,
                        "error": out.get("error"),
                    },
                )
            )
            return {"content": [{"type": "text", "text": json.dumps(out)}]}

        return call

    return [
        tool(
            "search",
            "Hybrid search over the indexed documents. Returns up to six passages,"
            " each with its paragraphs: paragraph_id, where it is, and its text."
            " `kinds` keeps only blocks of those kinds, e.g. definition, theorem.",
            _schema(
                {"q": _STRING, "kinds": {"type": "array", "items": _STRING}}, ["q"]
            ),
        )(wrap("search", lambda a: tools.search(a["q"], a.get("kinds")))),
        tool(
            "get_unit",
            "The full paragraphs of one passage, by the unit_id search returned.",
            _schema({"unit_id": _INT}, ["unit_id"]),
        )(wrap("get_unit", lambda a: tools.get_unit(a["unit_id"]))),
        tool(
            "get_paragraphs",
            "Paragraphs first..last (positions, at most 20) of a document, to"
            " read around a hit.",
            _schema(
                {"document_id": _STRING, "first": _INT, "last": _INT},
                ["document_id", "first", "last"],
            ),
        )(
            wrap(
                "get_paragraphs",
                lambda a: tools.get_paragraphs(a["document_id"], a["first"], a["last"]),
            )
        ),
        tool(
            "get_document",
            "A document's name and outline: its sections, each with its first"
            " paragraph position and page.",
            _schema({"document_id": _STRING}, ["document_id"]),
        )(wrap("get_document", lambda a: tools.get_document(a["document_id"]))),
    ]
