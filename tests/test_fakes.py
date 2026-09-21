import json
from pathlib import Path

import numpy as np
import pytest

from cryptoindex.core.embed import FakeEmbedder
from cryptoindex.core.llm import (
    Completion,
    FakeLLM,
    Message,
    UnrecordedRequestError,
    request_hash,
    request_payload,
)

SYSTEM = "You gloss cryptography."
MESSAGES = [Message(role="user", content="What is IND-CCA2?")]


def record(directory: Path, name: str, messages: list[Message], text: str) -> None:
    doc = {
        "request": request_payload(SYSTEM, messages, None, None),
        "completion": {"text": text, "parsed": {"ok": True}, "tool_calls": []},
    }
    (directory / f"{name}.json").write_text(json.dumps(doc))


async def test_fake_llm_replays_by_request_hash(tmp_path: Path) -> None:
    record(tmp_path, "a", MESSAGES, "Chosen-ciphertext security.")
    llm = FakeLLM.from_dir(tmp_path)
    got = await llm.complete(SYSTEM, MESSAGES)
    assert got == Completion(text="Chosen-ciphertext security.", parsed={"ok": True})


async def test_fake_llm_ignores_cache_prefix(tmp_path: Path) -> None:
    record(tmp_path, "a", MESSAGES, "same")
    llm = FakeLLM.from_dir(tmp_path)
    assert (await llm.complete(SYSTEM, MESSAGES, cache_prefix=True)).text == "same"


async def test_fake_llm_raises_on_unrecorded_request(tmp_path: Path) -> None:
    record(tmp_path, "a", MESSAGES, "recorded")
    llm = FakeLLM.from_dir(tmp_path)
    other = [Message(role="user", content="What is IND-CPA?")]
    expected = request_hash(request_payload(SYSTEM, other, None, None))
    with pytest.raises(UnrecordedRequestError, match=expected):
        await llm.complete(SYSTEM, other)


def test_fake_embedder_is_deterministic_and_normalized() -> None:
    emb = FakeEmbedder(dims=64)
    a = emb.embed_documents(["x", "y", "x"])
    assert a.shape == (3, 64) and a.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(a, axis=1), 1.0, rtol=1e-6)
    assert np.array_equal(a[0], a[2]) and not np.array_equal(a[0], a[1])
    assert not np.array_equal(emb.embed_queries(["x"], "Find proofs")[0], a[0])
