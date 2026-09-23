"""Rerankers (D32): a cross-encoder reads a query and a passage together and
scores how well the passage answers it. Search uses one, when configured, to
reorder its fused candidates."""

import threading
from typing import TYPE_CHECKING, Protocol

from cryptoindex.core.config import ConfigError, Settings

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder


class Reranker(Protocol):
    """Blocking; call through `asyncio.to_thread`. One score per passage,
    higher for a passage that better answers `query`. Scores compare only
    within one call."""

    model: str

    def score(
        self, query: str, passages: list[str], instruction: str
    ) -> list[float]: ...


class FakeReranker:
    """For tests: scores passages in the reverse of the order given, so a
    reranked search returns its fused candidates backwards."""

    model = "fake-reranker"

    def score(self, query: str, passages: list[str], instruction: str) -> list[float]:
        return [float(i) for i in range(len(passages))]


# Pinned revisions, fetched by `make fetch-models` and loaded with
# local_files_only, as for the embedders.
RERANK_REVISIONS = {  # D32
    "Qwen/Qwen3-Reranker-0.6B": "e61197ed45024b0ed8a2d74b80b4d909f1255473",
    "Qwen/Qwen3-Reranker-4B": "22e683669bc0f0bd69640a1354a6d0aebcfeede5",
    "BAAI/bge-reranker-v2-m3": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
}
MAX_TOKENS = 1024  # per query and passage; the end of a longer passage is cut
BATCH_SIZE = 8


class CrossEncoderReranker:
    """A cross-encoder through sentence-transformers, loaded at float16 on
    first use. Blocking; safe to share between threads."""

    def __init__(self, model: str, revision: str, device: str) -> None:
        self.model = model
        self._revision = revision
        self._device = device
        self._loaded: CrossEncoder | None = None
        self._lock = threading.Lock()
        # torch on MPS is not safe to drive from two threads at once.
        self._scoring = threading.Lock()

    def _model(self) -> "CrossEncoder":
        with self._lock:
            if self._loaded is None:
                import torch
                from sentence_transformers import CrossEncoder
                from transformers.utils import logging as transformers_logging

                transformers_logging.disable_progress_bar()
                self._loaded = CrossEncoder(
                    self.model,
                    revision=self._revision,
                    device=self._device,
                    local_files_only=True,
                    max_length=MAX_TOKENS,
                    model_kwargs={"torch_dtype": torch.float16},
                )
            return self._loaded

    def score(self, query: str, passages: list[str], instruction: str) -> list[float]:
        if not passages:
            return []
        model = self._model()
        # A model trained with instructions (Qwen3-Reranker) declares prompts
        # and places one in its own template; given to one without, the
        # instruction would only be glued onto the query.
        prompt = instruction if model.prompts else None
        with self._scoring:
            scores = model.predict(
                [(query, p) for p in passages],
                prompt=prompt,
                batch_size=BATCH_SIZE,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return [float(s) for s in scores]


def build_reranker(settings: Settings) -> Reranker | None:
    """The reranker CI_RERANK_MODEL names, or None when it is empty. Raises
    ConfigError for a model without a pinned revision."""
    if settings.rerank_model is None:
        return None
    revision = RERANK_REVISIONS.get(settings.rerank_model)
    if revision is None:
        raise ConfigError(
            f"reranker {settings.rerank_model!r} has no pinned revision;"
            f" known: {sorted(RERANK_REVISIONS)}"
        )
    return CrossEncoderReranker(settings.rerank_model, revision, settings.embed_device)
