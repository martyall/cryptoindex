import json
import logging

import pytest

from cryptoindex.core.logs import configure


def test_each_line_is_one_json_object_with_the_fields_as_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure("INFO")
    try:
        logging.getLogger("cryptoindex.test").warning(
            "stage_failed",
            extra={"work_id": 7, "gave_up": False, "error": 'it said "no", twice'},
        )
        line = capsys.readouterr().err.strip()
    finally:
        logging.basicConfig(handlers=[], force=True)
    record = json.loads(line)
    assert {k: record[k] for k in ("level", "logger", "event")} == {
        "level": "WARNING",
        "logger": "cryptoindex.test",
        "event": "stage_failed",
    }
    assert (record["work_id"], record["gave_up"]) == (7, False)
    assert record["error"] == 'it said "no", twice'
    assert "time" in record
