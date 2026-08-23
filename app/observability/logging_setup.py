"""Structured JSON logging with correlation IDs (P1-5).

- Every log line is a single JSON object: ts, level, logger, message, plus any
  fields passed via `extra`.
- A per-request correlation ID (from `X-Request-ID` or generated) is attached
  via contextvar and included in every line within that request, and echoed in
  the `X-Request-ID` response header.
"""
import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str | int = "INFO"):
    if isinstance(level, int):
        resolved = level
    else:
        resolved = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    # Uvicorn's own handlers would emit plain-text lines; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
