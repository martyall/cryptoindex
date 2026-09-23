from pathlib import Path

import pytest

from cryptoindex.core.reset import unsafe_reason

REPO = Path("/work/cryptoindex")


@pytest.mark.parametrize(
    "data_dir, reason",
    [
        (Path("/"), "filesystem root"),
        (Path.home(), "home directory"),
        (REPO, "contains the repository"),
        (Path("/work"), "contains the repository"),
        (REPO / "data", None),
        (Path("/Volumes/archive/cryptoindex-data"), None),
    ],
)
def test_unsafe_targets_are_refused(data_dir: Path, reason: str | None) -> None:
    result = unsafe_reason(data_dir, REPO)
    assert result is None if reason is None else reason in (result or "")
