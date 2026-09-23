"""Logging for every entry point: the standard library's `logging`, one JSON
object per line (python-json-logger). A call site names the event as the
message and passes its fields as `extra`, never formatted into the text, so
each field is a JSON value; pipeline events include `work_id`."""

import logging

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.json import JsonFormatter


def configure(level: str | int = "INFO") -> None:
    """Replace any handlers on the root logger with one writing JSON lines to
    stderr: `time`, `level`, `logger`, `event`, then the event's fields."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            "{asctime}{levelname}{name}{message}",
            style="{",
            # uvicorn adds its message again with terminal colour codes.
            reserved_attrs=[*RESERVED_ATTRS, "color_message"],
            rename_fields={
                "asctime": "time",
                "levelname": "level",
                "name": "logger",
                "message": "event",
            },
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
