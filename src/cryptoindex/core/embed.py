import hashlib
import threading
from typing import TYPE_CHECKING, Protocol

import numpy as np
import numpy.typing as npt
from psycopg import AsyncConnection

from cryptoindex.core.config import ConfigError, Settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

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


# Weights are downloaded once, at these revisions, into the Hugging Face cache
# (`hf download <model> --revision <rev>`); nothing is fetched at run time.
EMBED_REVISIONS = {  # D3
    "Qwen/Qwen3-Embedding-0.6B": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
    "Qwen/Qwen3-Embedding-8B": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
}
BATCH_SIZE = 16


class Qwen3Embedder:
    """Qwen3-Embedding through sentence-transformers (D3), cut to `dims`
    dimensions: the models are trained so that a prefix of the vector is
    itself an embedding, and the index's columns are fixed at CI_EMBED_DIMS.
    The model is loaded on first use, so starting the process stays fast;
    loading and encoding block, so call through `asyncio.to_thread`. Safe to
    share between threads."""

    def __init__(self, model: str, revision: str, dims: int, device: str) -> None:
        self.model = model
        self.dims = dims
        self._revision = revision
        self._device = device
        self._loaded: SentenceTransformer | None = None
        self._lock = threading.Lock()
        # One model serves the embed stage and searches, from different
        # threads, and torch on MPS is not safe to drive from two at once.
        # Taken per batch, so a search waits for one batch of a long
        # ingestion, not for all of it.
        self._encoding = threading.Lock()

    def _model(self) -> "SentenceTransformer":
        with self._lock:
            if self._loaded is None:
                import torch
                from sentence_transformers import SentenceTransformer

                self._loaded = SentenceTransformer(
                    self.model,
                    revision=self._revision,
                    device=self._device,
                    local_files_only=True,
                    truncate_dim=self.dims,
                    # 8B weights at full precision would take 32 GB of this
                    # machine's 48; half precision keeps them at 16.
                    model_kwargs={"torch_dtype": torch.float16},
                )
            return self._loaded

    def embed_documents(self, texts: list[str]) -> Vectors:
        return self._encode(texts, prompt=None)

    def embed_queries(self, texts: list[str], instruction: str) -> Vectors:
        # Qwen3-Embedding's query format; documents take no prompt.
        return self._encode(texts, prompt=f"Instruct: {instruction}\nQuery:")

    def _encode(self, texts: list[str], prompt: str | None) -> Vectors:
        if not texts:
            return np.empty((0, self.dims), dtype=np.float32)
        model = self._model()
        batches = []
        for start in range(0, len(texts), BATCH_SIZE):
            with self._encoding:
                batches.append(
                    model.encode(
                        texts[start : start + BATCH_SIZE],
                        prompt=prompt,
                        batch_size=BATCH_SIZE,
                        convert_to_numpy=True,
                    )
                )
        vectors = np.concatenate(batches).astype(np.float32)
        if vectors.shape[1] != self.dims:
            raise ConfigError(
                f"{self.model} gives {vectors.shape[1]} dimensions,"
                f" CI_EMBED_DIMS is {self.dims}"
            )
        # Normalized after the cut: a prefix of a unit vector is not one.
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def build_embedder(settings: Settings) -> Embedder:
    """Raises ConfigError for a model without a pinned revision."""
    revision = EMBED_REVISIONS.get(settings.embed_model)
    if revision is None:
        raise ConfigError(
            f"CI_EMBED_MODEL={settings.embed_model!r} has no pinned revision;"
            f" known: {sorted(EMBED_REVISIONS)}"
        )
    return Qwen3Embedder(
        settings.embed_model, revision, settings.embed_dims, settings.embed_device
    )


class EmbedModelMismatchError(RuntimeError):
    """The index was built with a different embedding model (Invariant 6)."""


async def check_embed_model(conn: AsyncConnection, model: str) -> None:
    """Raise EmbedModelMismatchError if `docs.meta` records an embedding model
    other than `model`. An index with no vectors yet records none."""
    cur = await conn.execute("SELECT value FROM docs.meta WHERE key = 'embed_model'")
    row = await cur.fetchone()
    if row is not None and row[0] != model:
        raise EmbedModelMismatchError(
            f"the index was embedded with {row[0]}, but CI_EMBED_MODEL is {model};"
            " re-embedding is a separate job (Invariant 6)"
        )
