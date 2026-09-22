import dataclasses
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from cryptoindex.core import backends
from cryptoindex.core.backends import (
    AnthropicLLM,
    ClaudeCodeLLM,
    OpenAIFormatLLM,
    build_llm,
)
from cryptoindex.core.config import ConfigError, Settings, load_settings
from cryptoindex.core.llm import (
    FakeLLM,
    LLMError,
    LLMRequest,
    Message,
    RecordingLLM,
)

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}
ASK = [Message(role="user", content="What is 2+2?")]

# A stand-in for `claude`: records how it was called, then prints the next
# queued result (the last one repeats).
FAKE_CLAUDE = """\
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_CLAUDE_STATE"])
calls = json.loads(state.read_text()) if state.exists() else []
calls.append({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "cwd_entries": os.listdir("."),
    "api_key_seen": "ANTHROPIC_API_KEY" in os.environ,
})
state.write_text(json.dumps(calls))
results = json.loads(os.environ["FAKE_CLAUDE_RESULTS"])
print(json.dumps(results[min(len(calls), len(results)) - 1]))
"""


def result(**fields: object) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"answer": "4"}',
        "structured_output": {"answer": "4"},
        "api_error_status": None,
        "modelUsage": {"claude-opus-5": {"outputTokens": 3}},
    } | fields


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    state = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    return script, state


async def test_claude_code_runs_isolated_and_reads_structured_output(
    fake_claude: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, state = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_RESULTS", json.dumps([result()]))
    llm = ClaudeCodeLLM("claude-opus-5", str(script))
    got = await llm.complete("Be brief.", ASK, json_schema=SCHEMA)

    assert got.parsed == {"answer": "4"} and got.model == "claude-opus-5"
    (call,) = json.loads(state.read_text())
    assert call["stdin"] == "What is 2+2?"
    assert call["cwd_entries"] == []
    assert call["api_key_seen"] is False  # D22: bill the subscription, not the API
    assert call["argv"] == [
        "-p",
        "--output-format=json",
        "--model=claude-opus-5",
        "--system-prompt=Be brief.",
        "--tools=",
        "--setting-sources=",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        f"--json-schema={json.dumps(SCHEMA)}",
    ]


async def test_claude_code_waits_out_a_usage_limit(
    fake_claude: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, state = fake_claude
    limited = result(
        subtype="error_during_execution",
        is_error=True,
        result="usage limit reached",
        structured_output=None,
        api_error_status=429,
    )
    monkeypatch.setenv("FAKE_CLAUDE_RESULTS", json.dumps([limited, limited, result()]))
    monkeypatch.setattr(backends, "USAGE_LIMIT_WAIT_S", 0.0)
    got = await ClaudeCodeLLM("claude-opus-5", str(script)).complete(
        "s", ASK, json_schema=SCHEMA
    )
    assert got.parsed == {"answer": "4"}
    assert len(json.loads(state.read_text())) == 3


async def test_claude_code_reports_other_errors(
    fake_claude: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, _ = fake_claude
    failed = result(
        subtype="error_during_execution",
        is_error=True,
        result="Not logged in",
        structured_output=None,
    )
    monkeypatch.setenv("FAKE_CLAUDE_RESULTS", json.dumps([failed]))
    with pytest.raises(LLMError, match="Not logged in"):
        await ClaudeCodeLLM("m", str(script)).complete("s", ASK, json_schema=SCHEMA)


async def test_claude_code_takes_one_user_message_and_no_tools() -> None:
    llm = ClaudeCodeLLM("m", "/nonexistent/claude")
    two = [*ASK, Message(role="assistant", content="4")]
    with pytest.raises(LLMError, match="one user message"):
        await llm.complete("s", two)


Handler = Callable[[httpx2.Request], httpx2.Response]


def openai_llm(handler: Handler) -> OpenAIFormatLLM:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = openai.AsyncOpenAI(
        base_url="http://local/v1", api_key="x", http_client=http
    )
    return OpenAIFormatLLM(client, "qwen")


def chat_reply(content: str) -> dict[str, object]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "qwen-served",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


async def test_openai_format_asks_for_the_schema() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=chat_reply('{"answer": "4"}'))

    got = await openai_llm(handler).complete("Be brief.", ASK, json_schema=SCHEMA)
    assert got.parsed == {"answer": "4"} and got.model == "qwen-served"
    assert sent[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "reply", "schema": SCHEMA},
    }
    assert sent[0]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "What is 2+2?"},
    ]


async def test_openai_format_falls_back_to_json_mode() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["response_format"]["type"] == "json_schema":
            return httpx2.Response(400, json={"error": {"message": "unsupported"}})
        return httpx2.Response(200, json=chat_reply('{"answer": "4"}'))

    llm = openai_llm(handler)
    assert (await llm.complete("s", ASK, json_schema=SCHEMA)).parsed == {"answer": "4"}
    assert (await llm.complete("s", ASK, json_schema=SCHEMA)).parsed == {"answer": "4"}
    # The second request skips the schema format the server rejected.
    assert [b["response_format"]["type"] for b in sent] == [
        "json_schema",
        "json_object",
        "json_object",
    ]
    assert json.dumps(SCHEMA) in sent[1]["messages"][0]["content"]


def anthropic_llm(handler: Handler) -> AnthropicLLM:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = anthropic.AsyncAnthropic(api_key="x", http_client=http, max_retries=0)
    return AnthropicLLM(client, "claude-opus-5")


def message(text: str, stop_reason: str = "end_turn") -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def sse(text: str, stop_reason: str = "end_turn") -> str:
    """The event stream the Messages API sends for message(text, stop_reason)."""
    start = message(text) | {"content": [], "stop_reason": None}
    events = [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


async def test_anthropic_streams_with_schema_cache_and_fallback() -> None:
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(
            200,
            text=sse('{"answer": "4"}'),
            headers={"content-type": "text/event-stream"},
        )

    llm = anthropic_llm(handler)
    got = await llm.complete("Be brief.", ASK, json_schema=SCHEMA, cache_prefix=True)
    assert got.parsed == {"answer": "4"} and got.model == "claude-opus-5"
    body = json.loads(sent[0].content)
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA}
    }
    assert body["system"] == [
        {"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["fallbacks"] == "default"
    assert backends.FALLBACK_BETA in sent[0].headers["anthropic-beta"]


async def test_anthropic_refusal_is_an_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            text=sse("", stop_reason="refusal"),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(LLMError, match="refused"):
        await anthropic_llm(handler).complete("s", ASK)


async def test_anthropic_batch_returns_completions_in_request_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backends, "BATCH_POLL_S", 0.0)
    batch = {
        "id": "b1",
        "type": "message_batch",
        "processing_status": "in_progress",
        "request_counts": {
            "processing": 2,
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        "created_at": "2026-09-21T00:00:00Z",
        "expires_at": "2026-09-22T00:00:00Z",
        "ended_at": None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": None,
    }
    polls: list[int] = []
    created: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/batches"):
            created.append(json.loads(request.content))
            return httpx2.Response(200, json=batch)
        if path.endswith("/results"):
            lines = [
                {
                    "custom_id": str(i),
                    "result": {
                        "type": "succeeded",
                        "message": message(f'{{"n": {i}}}'),
                    },
                }
                for i in (1, 0)  # results arrive in any order
            ]
            return httpx2.Response(200, text="\n".join(json.dumps(x) for x in lines))
        polls.append(1)
        ended = len(polls) > 1
        return httpx2.Response(
            200,
            json=batch
            | {
                "processing_status": "ended" if ended else "in_progress",
                "results_url": "https://api.anthropic.com/v1/messages/batches/b1/results",
            },
        )

    requests = [
        LLMRequest("s", [Message("user", "a")], json_schema=SCHEMA),
        LLMRequest("s", [Message("user", "b")], json_schema=SCHEMA),
    ]
    got = await anthropic_llm(handler).batch(requests)
    assert [c.parsed for c in got] == [{"n": 0}, {"n": 1}]
    assert len(polls) >= 2  # in progress, then ended
    params = created[0]["requests"][0]["params"]
    assert "fallbacks" not in params  # the Batches API rejects it
    assert params["output_config"]["format"]["schema"] == SCHEMA


def settings_with(tmp_path: Path, **env: str) -> Settings:
    return load_settings(
        {
            "CI_ADMIN_DSN": "postgresql://a@h/d",
            "CI_INGEST_DSN": "postgresql://i@h/d",
            "CI_QUERY_DSN": "postgresql://q@h/d",
            "CI_DATA_DIR": str(tmp_path),
            "CI_LLM_MODEL": "claude-opus-5",
        }
        | env
    )


def test_build_llm_picks_the_configured_backend(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_llm(settings_with(tmp_path, CI_LLM_BACKEND="anthropic"))
    keyed = settings_with(tmp_path, CI_LLM_BACKEND="anthropic", ANTHROPIC_API_KEY="k")
    assert isinstance(build_llm(keyed), AnthropicLLM)
    local = settings_with(tmp_path, CI_LLM_BACKEND="openai_format")
    assert isinstance(build_llm(local), OpenAIFormatLLM)
    dev = settings_with(tmp_path, CI_LLM_BACKEND="claude_code")
    assert isinstance(build_llm(dev), ClaudeCodeLLM)
    recorded = dataclasses.replace(dev, llm_record_dir=tmp_path / "rec")
    assert isinstance(build_llm(recorded), RecordingLLM)


async def test_recordings_replay_offline(
    fake_claude: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script, _ = fake_claude
    monkeypatch.setenv("FAKE_CLAUDE_RESULTS", json.dumps([result()]))
    recording = RecordingLLM(
        ClaudeCodeLLM("claude-opus-5", str(script)), tmp_path / "r"
    )
    live = await recording.complete("s", ASK, json_schema=SCHEMA)
    replayed = await FakeLLM.from_dir(tmp_path / "r").complete(
        "s", ASK, json_schema=SCHEMA
    )
    assert replayed == live
