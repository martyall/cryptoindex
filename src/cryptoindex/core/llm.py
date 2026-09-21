import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Protocol

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
    parsed: object | None = None  # set when a json_schema was requested
    tool_calls: tuple[ToolCall, ...] = field(default=())


class LLM(Protocol):
    """An LLM backend (Invariant 9). `cache_prefix` affects cost only and is
    ignored where unsupported. `Completion.parsed` is set exactly when
    `json_schema` is given. Raises on transport or parse failure; FakeLLM raises
    UnrecordedRequestError."""

    name: str

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion: ...


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

    def __init__(self, recordings: Mapping[str, Completion]) -> None:
        self._recordings = dict(recordings)

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
        try:
            return self._recordings[key]
        except KeyError:
            raise UnrecordedRequestError(
                f"no recorded completion for request hash {key}"
            ) from None


def _completion_from_json(doc: dict[str, object]) -> Completion:
    text = doc.get("text", "")
    raw_calls = doc.get("tool_calls", [])
    if not isinstance(text, str) or not isinstance(raw_calls, list):
        raise ValueError(f"malformed recorded completion: {doc!r}")
    calls = tuple(
        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
        for c in raw_calls
    )
    return Completion(text=text, parsed=doc.get("parsed"), tool_calls=calls)
