import json
import logging
import os
import sys
import time
import uuid


class JsonFormatter(logging.Formatter):
    """One-line JSON per record -- easy to grep/ship to a log aggregator."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level=None):
    """
    Structured logging for the whole app. Defaults to plain text (readable in
    a terminal); set LOG_FORMAT=json for JSON lines (suited to log aggregators
    in a real deployment). Level comes from LOG_LEVEL, default INFO.
    """
    logger = logging.getLogger('finflow')
    level = level or getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)

        if os.environ.get("LOG_FORMAT", "").lower() == "json":
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))

        logger.addHandler(handler)
        logger.propagate = False

    return logger


logger = setup_logging()


def new_request_id():
    return uuid.uuid4().hex[:12]


class Timer:
    """`with Timer() as t: ...` then `t.elapsed_ms`."""

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = round((time.monotonic() - self._start) * 1000, 1)
