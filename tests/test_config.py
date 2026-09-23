from pathlib import Path

import pytest

from cryptoindex.core.config import ConfigError, load_settings

MINIMAL = {
    "CI_ADMIN_DSN": "postgresql://a@h/db",
    "CI_INGEST_DSN": "postgresql://i@h/db",
    "CI_QUERY_DSN": "postgresql://q@h/db",
    "CI_DATA_DIR": "./data",
}


def test_defaults_fill_everything_optional() -> None:
    s = load_settings(MINIMAL)
    assert s.data_dir == Path("data")
    assert (s.llm_backend, s.parser, s.embed_dims) == ("anthropic", "paddle", 1024)
    assert s.anthropic_api_key is None


def test_api_key_is_kept_out_of_repr() -> None:
    s = load_settings(MINIMAL | {"ANTHROPIC_API_KEY": "sk-secret"})
    assert s.anthropic_api_key == "sk-secret"
    assert "sk-secret" not in repr(s)


def test_all_problems_are_reported_together() -> None:
    env = {
        "CI_LLM_BACKEND": "gpt",
        "CI_EMBED_DIMS": "many",
        "CI_MAX_ATTEMPTS": "0",
    }
    with pytest.raises(ConfigError) as info:
        load_settings(env)
    message = str(info.value)
    for fragment in [
        "CI_ADMIN_DSN is required",
        "CI_DATA_DIR is required",
        "CI_LLM_BACKEND='gpt'",
        "CI_EMBED_DIMS='many' is not a number",
        "CI_MAX_ATTEMPTS='0' must be >= 1",
    ]:
        assert fragment in message


def test_searching_the_alternate_vectors_needs_an_alternate_model() -> None:
    assert load_settings(MINIMAL).search_vectors == "primary"
    with pytest.raises(ConfigError, match="CI_SEARCH_VECTORS=alt needs"):
        load_settings(MINIMAL | {"CI_SEARCH_VECTORS": "alt"})
    with pytest.raises(ConfigError, match="CI_SEARCH_VECTORS='both'"):
        load_settings(MINIMAL | {"CI_SEARCH_VECTORS": "both"})
    s = load_settings(MINIMAL | {"CI_SEARCH_VECTORS": "alt", "CI_EMBED_MODEL_ALT": "m"})
    assert (s.search_vectors, s.embed_model_alt) == ("alt", "m")
