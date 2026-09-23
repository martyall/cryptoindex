import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal, TypeGuard, TypeVar, get_args

LLMBackend = Literal["anthropic", "openai_format", "claude_code", "fake"]  # D9, D22
ParserName = Literal["marker", "paddle"]
SearchVectors = Literal["primary", "alt"]

_Num = TypeVar("_Num", int, float)


def _is_backend(value: str) -> TypeGuard[LLMBackend]:
    return value in get_args(LLMBackend)


def _is_parser(value: str) -> TypeGuard[ParserName]:
    return value in get_args(ParserName)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    admin_dsn: str
    ingest_dsn: str
    query_dsn: str
    data_dir: Path
    embed_model: str
    embed_model_alt: str | None  # D27: a second model, for comparison
    search_vectors: SearchVectors  # which set searches use, for the whole run
    embed_dims: int
    embed_device: str
    llm_backend: LLMBackend
    llm_base_url: str
    llm_model: str
    anthropic_api_key: str | None = field(repr=False)
    claude_bin: str
    llm_record_dir: Path | None
    gloss_batch: bool
    parser: ParserName
    paddle_vlm_url: str
    eprint_base: str
    fetch_delay_s: float
    user_agent: str
    queue_capacity: int
    concurrency_parse: int
    concurrency_segment: int
    concurrency_embed: int
    max_attempts: int
    stale_lock_s: float
    max_upload_mb: int
    api_host: str
    api_port: int
    log_level: str


def load_settings(env: Mapping[str, str]) -> Settings:
    """Raises ConfigError naming every missing or malformed variable at once."""
    errors: list[str] = []

    def req(name: str) -> str:
        value = env.get(name, "")
        if not value:
            errors.append(f"{name} is required")
        return value

    def opt(name: str, default: str) -> str:
        return env.get(name) or default

    def num(name: str, default: str, kind: Callable[[str], _Num], min_: _Num) -> _Num:
        raw = opt(name, default)
        try:
            value = kind(raw)
        except ValueError:
            errors.append(f"{name}={raw!r} is not a number")
            return min_
        if value < min_:
            errors.append(f"{name}={raw!r} must be >= {min_}")
        return value

    def flag(name: str, default: str) -> bool:
        raw = opt(name, default)
        if raw not in ("true", "false"):
            errors.append(f"{name}={raw!r} must be true or false")
        return raw == "true"

    llm_backend: LLMBackend = "anthropic"
    raw_backend = opt("CI_LLM_BACKEND", llm_backend)
    if _is_backend(raw_backend):
        llm_backend = raw_backend
    else:
        errors.append(f"CI_LLM_BACKEND={raw_backend!r} not in {get_args(LLMBackend)}")

    search_vectors: SearchVectors = "primary"
    raw_vectors = opt("CI_SEARCH_VECTORS", search_vectors)
    if raw_vectors == "alt" and not env.get("CI_EMBED_MODEL_ALT"):
        errors.append("CI_SEARCH_VECTORS=alt needs CI_EMBED_MODEL_ALT")
    if raw_vectors in get_args(SearchVectors):
        search_vectors = "alt" if raw_vectors == "alt" else "primary"
    else:
        errors.append(
            f"CI_SEARCH_VECTORS={raw_vectors!r} not in {get_args(SearchVectors)}"
        )

    parser: ParserName = "paddle"  # D21
    raw_parser = opt("CI_PARSER", parser)
    if _is_parser(raw_parser):
        parser = raw_parser
    else:
        errors.append(f"CI_PARSER={raw_parser!r} not in {get_args(ParserName)}")

    settings = Settings(
        admin_dsn=req("CI_ADMIN_DSN"),
        ingest_dsn=req("CI_INGEST_DSN"),
        query_dsn=req("CI_QUERY_DSN"),
        data_dir=Path(req("CI_DATA_DIR")),
        embed_model=opt("CI_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        embed_model_alt=env.get("CI_EMBED_MODEL_ALT") or None,
        search_vectors=search_vectors,
        embed_dims=num("CI_EMBED_DIMS", "1024", int, 1),
        embed_device=opt("CI_EMBED_DEVICE", "mps"),
        llm_backend=llm_backend,
        llm_base_url=opt("CI_LLM_BASE_URL", "http://localhost:8080/v1"),
        llm_model=opt("CI_LLM_MODEL", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        claude_bin=opt("CI_CLAUDE_BIN", "claude"),
        llm_record_dir=Path(env["CI_LLM_RECORD_DIR"])
        if env.get("CI_LLM_RECORD_DIR")
        else None,
        gloss_batch=flag("CI_GLOSS_BATCH", "true"),  # D5
        parser=parser,
        paddle_vlm_url=opt("CI_PADDLE_VLM_URL", "http://localhost:8111/"),
        eprint_base=opt("CI_EPRINT_BASE", "https://eprint.iacr.org"),
        fetch_delay_s=num("CI_FETCH_DELAY_S", "2.0", float, 0.0),
        user_agent=opt("CI_USER_AGENT", "cryptoindex/0.1"),
        queue_capacity=num("CI_QUEUE_CAPACITY", "64", int, 1),
        concurrency_parse=num("CI_CONCURRENCY_PARSE", "1", int, 1),
        concurrency_segment=num("CI_CONCURRENCY_SEGMENT", "4", int, 1),
        concurrency_embed=num("CI_CONCURRENCY_EMBED", "1", int, 1),
        max_attempts=num("CI_MAX_ATTEMPTS", "3", int, 1),
        stale_lock_s=num("CI_STALE_LOCK_S", "600", float, 0.0),
        max_upload_mb=num("CI_MAX_UPLOAD_MB", "100", int, 1),
        api_host=opt("CI_API_HOST", "127.0.0.1"),
        api_port=num("CI_API_PORT", "8000", int, 0),  # 0: any free port
        log_level=opt("CI_LOG_LEVEL", "INFO"),
    )
    if errors:
        raise ConfigError("invalid configuration: " + "; ".join(errors))
    return settings


@cache
def _process_settings() -> Settings:
    return load_settings(os.environ)


def __getattr__(name: str) -> Settings:
    # `config.settings` is resolved on first access, so importing this module
    # (e.g. for Settings or load_settings in tests) never requires a full env.
    if name == "settings":
        return _process_settings()
    raise AttributeError(name)
