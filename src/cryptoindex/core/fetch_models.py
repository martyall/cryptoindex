"""`make fetch-models`: download, once, the pinned weights of the embedding
models and the reranker the settings name (EMBED_REVISIONS,
RERANK_REVISIONS), into the Hugging Face cache they are read from with
`local_files_only`. A download that stops resumes on the next run."""

from huggingface_hub import snapshot_download

from cryptoindex.core import config
from cryptoindex.core.config import ConfigError
from cryptoindex.core.embed import EMBED_REVISIONS
from cryptoindex.core.rerank import RERANK_REVISIONS


def main() -> None:
    settings = config.settings
    models = [(settings.embed_model, EMBED_REVISIONS)]
    if settings.embed_model_alt:
        models.append((settings.embed_model_alt, EMBED_REVISIONS))
    if settings.rerank_model:
        models.append((settings.rerank_model, RERANK_REVISIONS))
    for model, pinned in models:
        revision = pinned.get(model)
        if revision is None:
            raise ConfigError(f"model {model!r} has no pinned revision")
        path = snapshot_download(model, revision=revision)
        print(f"{model} @ {revision[:12]}: {path}")


if __name__ == "__main__":
    main()
