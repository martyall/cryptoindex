"""The real LLM backends (D9, D22) and the factory that picks one from settings.
Every reply is read from the SDK's typed objects or from JSON validated with
pydantic."""

import asyncio
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence

import anthropic
import claude_agent_sdk
import openai
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaTextBlockParam,
    BetaToolParam,
)
from anthropic.types.beta.message_create_params import (
    MessageCreateParamsNonStreaming,
)
from anthropic.types.beta.messages.batch_create_params import Request
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultError,
    ResultMessage,
)
from claude_agent_sdk import Message as SdkMessage
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from openai.types.shared_params import (
    ResponseFormatJSONObject,
    ResponseFormatJSONSchema,
)

from cryptoindex.core.config import ConfigError, Settings
from cryptoindex.core.llm import (
    LLM,
    Completion,
    FakeLLM,
    LLMError,
    LLMRequest,
    Message,
    RecordingLLM,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger(__name__)

MAX_TOKENS = 32_000  # thinking plus reply; Opus 5 thinks by default
# Opus 5's safety classifiers can decline benign security text; the server
# then re-runs the request on the recommended fallback model. Not accepted by
# the Batches API.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
BATCH_POLL_S = 30.0


class AnthropicLLM:
    """Anthropic's native SDK (D9), with prompt caching, the server-side refusal
    fallback, and the Batches API for bulk glossing (D5)."""

    name = "anthropic"

    def __init__(self, client: anthropic.AsyncAnthropic, model: str) -> None:
        self._client = client
        self.model = model

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion:
        async with self._client.beta.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=_system_blocks(system, cache_prefix),
            messages=_anthropic_messages(messages),
            tools=[_anthropic_tool(t) for t in tools or []],
            output_config=anthropic.omit
            if json_schema is None
            else _output_config(json_schema),
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            message = await stream.get_final_message()
        return _anthropic_completion(message, json_schema is not None)

    async def batch(self, requests: list[LLMRequest]) -> list[Completion]:
        batch_requests: list[Request] = []
        for i, r in enumerate(requests):
            params: MessageCreateParamsNonStreaming = {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "system": _system_blocks(r.system, r.cache_prefix),
                "messages": _anthropic_messages(r.messages),
            }
            if r.json_schema is not None:
                params["output_config"] = _output_config(r.json_schema)
            batch_requests.append(Request(custom_id=str(i), params=params))
        created = await self._client.beta.messages.batches.create(
            requests=batch_requests
        )
        log.info(
            "llm_batch_created",
            extra={"batch_id": created.id, "requests": len(requests)},
        )
        try:
            while True:
                batch = await self._client.beta.messages.batches.retrieve(created.id)
                if batch.processing_status == "ended":
                    break
                await asyncio.sleep(BATCH_POLL_S)
        except asyncio.CancelledError:
            await asyncio.shield(self._client.beta.messages.batches.cancel(created.id))
            raise
        by_id: dict[str, Completion] = {}
        async for item in await self._client.beta.messages.batches.results(created.id):
            result = item.result
            if result.type != "succeeded":
                raise LLMError(
                    f"batch {created.id} request {item.custom_id}: {result.type}"
                )
            want_json = requests[int(item.custom_id)].json_schema is not None
            by_id[item.custom_id] = _anthropic_completion(result.message, want_json)
        return [by_id[str(i)] for i in range(len(requests))]


def _system_blocks(system: str, cache_prefix: bool) -> list[BetaTextBlockParam]:
    block: BetaTextBlockParam = {"type": "text", "text": system}
    if cache_prefix:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _output_config(json_schema: dict[str, object]) -> BetaOutputConfigParam:
    return {"format": {"type": "json_schema", "schema": json_schema}}


def _anthropic_tool(tool: ToolSpec) -> BetaToolParam:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _anthropic_messages(messages: Sequence[Message]) -> list[BetaMessageParam]:
    out: list[BetaMessageParam] = []
    for m in messages:
        if m.role == "tool":
            if m.tool_call_id is None:
                raise ValueError("a tool message needs tool_call_id")
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            content: list = [{"type": "text", "text": m.content}] if m.content else []
            content += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": "user", "content": m.content})
    return out


def _anthropic_completion(message: BetaMessage, want_json: bool) -> Completion:
    if message.stop_reason == "refusal":
        raise LLMError(f"{message.model} refused the request: {message.stop_details}")
    if message.stop_reason == "max_tokens":
        raise LLMError(f"{message.model} reply truncated at {MAX_TOKENS} tokens")
    text = "".join(b.text for b in message.content if b.type == "text")
    calls = tuple(
        ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
        for b in message.content
        if b.type == "tool_use"
    )
    return Completion(
        text=text,
        model=message.model,
        parsed=json.loads(text) if want_json else None,
        tool_calls=calls,
    )


class OpenAIFormatLLM:
    """A local server speaking the OpenAI chat format (D9): llama.cpp,
    mlx_lm.server, Ollama, LM Studio. JSON replies use a json_schema response
    format, or plain JSON mode with the schema in the system prompt where the
    server rejects json_schema."""

    name = "openai_format"

    def __init__(self, client: openai.AsyncOpenAI, model: str) -> None:
        self._client = client
        self.model = model
        self._schema_supported = True

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion:
        if json_schema is not None and self._schema_supported:
            try:
                return await self._create(
                    system,
                    messages,
                    tools,
                    json_schema,
                    ResponseFormatJSONSchema(
                        type="json_schema",
                        json_schema={"name": "reply", "schema": json_schema},
                    ),
                )
            # Any 400 is taken to mean json_schema is unsupported; the server
            # gives no structured reason, and the flag stays off for this
            # backend's lifetime.
            except openai.BadRequestError as e:
                log.warning(
                    "llm_json_schema_rejected",
                    extra={"model": self.model, "error": str(e)},
                )
                self._schema_supported = False
        if json_schema is not None:
            json_mode: ResponseFormatJSONObject = {"type": "json_object"}
            system = (
                f"{system}\n\nReply with one JSON object matching this JSON"
                f" Schema:\n{json.dumps(json_schema)}"
            )
            return await self._create(system, messages, tools, json_schema, json_mode)
        return await self._create(system, messages, tools, None, openai.omit)

    async def _create(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        json_schema: dict[str, object] | None,
        response_format: ResponseFormatJSONSchema
        | ResponseFormatJSONObject
        | openai.Omit,
    ) -> Completion:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=_openai_messages(system, messages),
            tools=[_openai_tool(t) for t in tools] if tools else openai.omit,
            response_format=response_format,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError(f"{response.model} reply truncated")
        text = choice.message.content or ""
        calls = tuple(
            ToolCall(
                id=c.id,
                name=c.function.name,
                arguments=_json_object(c.function.arguments),
            )
            for c in choice.message.tool_calls or []
            if c.type == "function"
        )
        return Completion(
            text=text,
            model=response.model,
            parsed=json.loads(text) if json_schema is not None else None,
            tool_calls=calls,
        )


def _json_object(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise LLMError(f"tool arguments are not a JSON object: {raw!r}")
    return value


def _openai_tool(tool: ToolSpec) -> ChatCompletionToolParam:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _openai_messages(
    system: str, messages: Sequence[Message]
) -> list[ChatCompletionMessageParam]:
    out: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "tool":
            if m.tool_call_id is None:
                raise ValueError("a tool message needs tool_call_id")
            out.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
            )
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": json.dumps(c.arguments),
                            },
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        elif m.role == "assistant":
            out.append({"role": "assistant", "content": m.content})
        else:
            out.append({"role": "user", "content": m.content})
    return out


# Environment variables that make Claude Code bill the API instead of the
# logged-in subscription (D22). The SDK layers `env` over the inherited
# environment, so they are blanked rather than removed.
_API_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
USAGE_LIMIT_WAIT_S = 300.0

Query = Callable[..., AsyncIterator[SdkMessage]]


class ClaudeCodeLLM:
    """Dev mode (D22, D31): Claude Code through the Claude Agent SDK and its
    bundled CLI, isolated as D22 describes. Single-turn JSON requests only. A
    usage-limit refusal (HTTP 429) is retried every USAGE_LIMIT_WAIT_S without
    limit, so it does not count as a failed attempt. Raises LLMError for any
    other reported failure, for an SDK error, or for a request with tools or
    more than one message.
    """

    name = "claude_code"

    def __init__(self, model: str, query: Query = claude_agent_sdk.query) -> None:
        self.model = model
        self._query = query

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion:
        if tools or len(messages) != 1 or messages[0].role != "user":
            raise LLMError("the claude_code backend takes one user message, no tools")
        while True:
            result = await self._run(system, messages[0].content, json_schema)
            if result.api_error_status != 429:
                break
            log.warning(
                "llm_usage_limit",
                extra={"backend": "claude_code", "wait_s": USAGE_LIMIT_WAIT_S},
            )
            await asyncio.sleep(USAGE_LIMIT_WAIT_S)
        if result.is_error:
            raise LLMError(
                f"Claude Code failed ({result.subtype},"
                f" status {result.api_error_status}): {result.result}"
            )
        if json_schema is not None and result.structured_output is None:
            raise LLMError("Claude Code returned no structured output")
        served = list(result.model_usage or {})
        return Completion(
            text=result.result or "",
            model=served[0] if len(served) == 1 else self.model,
            parsed=result.structured_output if json_schema is not None else None,
        )

    async def _run(
        self, system: str, prompt: str, json_schema: dict[str, object] | None
    ) -> ResultMessage:
        result: ResultMessage | None = None
        with tempfile.TemporaryDirectory(prefix="ci-claude-") as cwd:
            options = ClaudeAgentOptions(
                system_prompt=system,
                model=self.model,
                tools=[],
                setting_sources=[],
                strict_mcp_config=True,
                cwd=cwd,
                env=dict.fromkeys(_API_CREDENTIALS, ""),
                extra_args={
                    "disable-slash-commands": None,
                    "no-session-persistence": None,
                },
                output_format=(
                    {"type": "json_schema", "schema": json_schema}
                    if json_schema is not None
                    else None
                ),
            )
            try:
                async for message in self._query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        result = message
            except ResultError:
                # The CLI exits non-zero after an error result, on purpose;
                # the result already says what went wrong.
                if result is None:
                    raise
            except ClaudeSDKError as e:
                raise LLMError(f"Claude Code failed: {e!r}") from e
        if result is None:
            raise LLMError("Claude Code ended without a result")
        return result


def build_llm(settings: Settings) -> LLM:
    """The backend CI_LLM_BACKEND names, wrapped to record every completion
    when CI_LLM_RECORD_DIR is set. Raises ConfigError if the backend's
    settings are missing."""
    llm: LLM
    if settings.llm_backend == "anthropic":
        if settings.anthropic_api_key is None:
            raise ConfigError(
                "CI_LLM_BACKEND=anthropic needs ANTHROPIC_API_KEY"
                " (or use claude_code, D22)"
            )
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        llm = AnthropicLLM(client, settings.llm_model)
    elif settings.llm_backend == "openai_format":
        # Local servers ignore the key, but the SDK requires one. A local model
        # can take minutes on a full chunk, well past the SDK's default.
        client = openai.AsyncOpenAI(
            base_url=settings.llm_base_url, api_key="local", timeout=900.0
        )
        llm = OpenAIFormatLLM(client, settings.llm_model)
    elif settings.llm_backend == "claude_code":
        llm = ClaudeCodeLLM(settings.llm_model)
    else:
        llm = FakeLLM.from_dir(settings.data_dir / "llm-recordings")
    if settings.llm_record_dir is not None:
        llm = RecordingLLM(llm, settings.llm_record_dir)
    return llm
