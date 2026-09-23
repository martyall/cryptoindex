import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. Assistant turns may carry tool calls; `tool` turns answer one
    call by `tool_call_id`. Backends translate this to their wire format."""

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str  # the model that produced it, which a fallback can change
    parsed: object | None = None  # set when a json_schema was requested
    tool_calls: tuple[ToolCall, ...] = field(default=())


class LLMError(RuntimeError):
    """The backend returned no usable completion (a refusal, truncation, a
    failed request, malformed output), or was given a request it cannot
    serve."""


class LLM(Protocol):
    """An LLM backend (Invariant 9, D22). `model` is the model requested.
    `cache_prefix` affects cost only and is ignored where unsupported.
    `Completion.parsed` is set exactly when `json_schema` is given. Raises
    LLMError, ValueError (including json.JSONDecodeError and pydantic's
    ValidationError) for an unparseable reply, or the SDK's own errors on
    transport failure; FakeLLM raises UnrecordedRequestError."""

    name: str
    model: str

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion: ...


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    messages: list[Message]
    json_schema: dict[str, object] | None = None
    cache_prefix: bool = False


@runtime_checkable
class BatchLLM(LLM, Protocol):
    """A backend that can also run many requests as one batch, at lower cost
    and higher latency (D5)."""

    async def batch(self, requests: list[LLMRequest]) -> list[Completion]:
        """Completions in request order, once the whole batch has ended. Waits
        (asynchronously) for as long as the provider takes. Raises LLMError if
        any request failed. Cancelling the call cancels the provider's batch;
        its results are lost."""
        ...


def request_payload(
    system: str,
    messages: list[Message],
    tools: list[ToolSpec] | None,
    json_schema: dict[str, object] | None,
) -> dict[str, object]:
    """The part of a request that determines its response. `cache_prefix` is
    excluded: it changes cost, not output."""
    return {
        "system": system,
        "messages": [asdict(m) for m in messages],
        "tools": [asdict(t) for t in tools or []],
        "json_schema": json_schema,
    }


def request_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class UnrecordedRequestError(LookupError):
    pass


class FakeLLM:
    """Replays recorded completions keyed by request hash.

    A recording is a JSON file `{"request": <request_payload>, "completion":
    {"text", "parsed", "tool_calls"}}`. The key is recomputed from the stored
    request, so a recording can never be filed under the wrong hash.
    """

    name = "fake"

    def __init__(
        self, recordings: Mapping[str, Completion], model: str = "fake"
    ) -> None:
        self._recordings = dict(recordings)
        self.model = model
        self.calls = 0

    @classmethod
    def from_dir(cls, directory: Path) -> "FakeLLM":
        recordings: dict[str, Completion] = {}
        for path in sorted(directory.glob("*.json")):
            doc = json.loads(path.read_text())
            recordings[request_hash(doc["request"])] = _completion_from_json(
                doc["completion"]
            )
        return cls(recordings)

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion:
        key = request_hash(request_payload(system, messages, tools, json_schema))
        self.calls += 1
        try:
            return self._recordings[key]
        except KeyError:
            raise UnrecordedRequestError(
                f"no recorded completion for request hash {key}"
            ) from None


class RecordingLLM:
    """Passes requests to another backend and writes each completion as a
    FakeLLM recording, named by its request hash, so a real run can be
    replayed offline. Not a BatchLLM: recording disables batching."""

    def __init__(self, inner: LLM, directory: Path) -> None:
        self._inner = inner
        self._directory = directory
        self.name = inner.name
        self.model = inner.model

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion:
        completion = await self._inner.complete(
            system, messages, tools, json_schema, cache_prefix
        )
        payload = request_payload(system, messages, tools, json_schema)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{request_hash(payload)}.json"
        path.write_text(
            json.dumps({"request": payload, "completion": asdict(completion)}, indent=1)
        )
        return completion


class _ToolCallJSON(BaseModel):
    id: str
    name: str
    arguments: dict[str, object]


class _CompletionJSON(BaseModel):
    text: str = ""
    model: str = "fake"
    parsed: object | None = None
    tool_calls: list[_ToolCallJSON] = []


def _completion_from_json(doc: object) -> Completion:
    c = _CompletionJSON.model_validate(doc)
    return Completion(
        text=c.text,
        model=c.model,
        parsed=c.parsed,
        tool_calls=tuple(ToolCall(t.id, t.name, t.arguments) for t in c.tool_calls),
    )
