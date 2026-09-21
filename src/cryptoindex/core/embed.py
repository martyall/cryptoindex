import hashlib
from typing import Protocol

import numpy as np
import numpy.typing as npt

Vectors = npt.NDArray[np.float32]


class Embedder(Protocol):
    """Blocking; call through `asyncio.to_thread`. Returns one L2-normalized row
    per input text. `instruction` is prefixed to queries only, hence two
    methods. Invariant 6: one model per index."""

    model: str
    dims: int

    def embed_documents(self, texts: list[str]) -> Vectors: ...

    def embed_queries(self, texts: list[str], instruction: str) -> Vectors: ...


class FakeEmbedder:
    """Deterministic vectors derived from a hash of the text, for tests. Equal
    texts give equal vectors; anything else is unrelated."""

    def __init__(self, dims: int = 1024, model: str = "fake-embedder") -> None:
        self.model = model
        self.dims = dims

    def embed_documents(self, texts: list[str]) -> Vectors:
        return self._embed(texts)

    def embed_queries(self, texts: list[str], instruction: str) -> Vectors:
        return self._embed([f"{instruction}\n{t}" for t in texts])

    def _embed(self, texts: list[str]) -> Vectors:
        out = np.empty((len(texts), self.dims), dtype=np.float32)
        for i, text in enumerate(texts):
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
            row = np.random.default_rng(seed).standard_normal(self.dims)
            out[i] = row / np.linalg.norm(row)
        return out
