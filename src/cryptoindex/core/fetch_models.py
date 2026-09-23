"""`make fetch-models`: download, once, the pinned weights of the embedding
models the settings name (EMBED_REVISIONS), into the Hugging Face cache the
embedder reads with `local_files_only`. A download that stops resumes on the
next run."""

from huggingface_hub import snapshot_download

from cryptoindex.core import config
from cryptoindex.core.config import ConfigError
from cryptoindex.core.embed import EMBED_REVISIONS


def main() -> None:
    settings = config.settings
    models = [settings.embed_model]
    if settings.embed_model_alt:
        models.append(settings.embed_model_alt)
    for model in models:
        revision = EMBED_REVISIONS.get(model)
        if revision is None:
            raise ConfigError(f"embedding model {model!r} has no pinned revision")
        path = snapshot_download(model, revision=revision)
        print(f"{model} @ {revision[:12]}: {path}")


if __name__ == "__main__":
    main()
