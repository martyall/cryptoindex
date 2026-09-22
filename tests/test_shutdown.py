import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from cryptoindex.core.config import Settings
from cryptoindex.core.model import Stage

CHILD = Path(__file__).with_name("serve_child.py")


def locked(settings: Settings) -> int:
    with psycopg.connect(settings.admin_dsn) as conn:
        row = conn.execute("SELECT count(locked_at) FROM docs.revisions").fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_releases_claims_and_exits_cleanly(
    settings: Settings,
    test_env: dict[str, str],
    seed: Callable[[list[Stage]], list[int]],
    sig: signal.Signals,
) -> None:
    seed([Stage.PARSE])
    child = subprocess.Popen(
        [sys.executable, str(CHILD)],
        # The parse stage is replaced by `hang`; marker only avoids starting
        # PaddleOCR-VL's (Mac-only) model server, which serve() does for paddle.
        env=test_env | {"CI_API_PORT": "0", "CI_PARSER": "marker"},
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while locked(settings) == 0:
        assert child.poll() is None, child.stderr.read() if child.stderr else ""
        assert time.monotonic() < deadline, "stage never claimed the revision"
        time.sleep(0.05)

    child.send_signal(sig)
    _, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, stderr
    assert "Traceback" not in stderr
    assert locked(settings) == 0
