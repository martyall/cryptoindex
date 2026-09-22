import json

from cryptoindex.core.llm import Completion, Message, ToolSpec


class CoveringLLM:
    """Answers every gloss request with one unanchored unit spanning the
    whole chunk, read from the request's JSON; for pipeline tests where the
    glosses themselves do not matter."""

    name = "covering"
    model = "covering-1"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, object] | None = None,
        cache_prefix: bool = False,
    ) -> Completion:
        self.calls += 1
        positions = [p["pos"] for p in json.loads(messages[-1].content)["paragraphs"]]
        unit = {
            "first_pos": positions[0],
            "last_pos": positions[-1],
            "anchor_label": None,
            "anchor_pos": None,
            "anchor_kind": None,
            "gloss": "A passage.",
            "key_terms": [],
            "questions": ["What is it?", "Why?", "How?"],
        }
        return Completion(text="", model=self.model, parsed={"units": [unit]})
