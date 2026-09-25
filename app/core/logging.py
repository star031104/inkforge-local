"""Application logging with bounded local files and basic secret redaction."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[-_ ]?key|token|password)\s*[:=]\s*)[^\s,;]+"),
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in _SECRET_PATTERNS:
            message = pattern.sub(r"\1***", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(log_path: Path, *, verbose: bool = False) -> None:
    """Configure one predictable logging pipeline for every application entrypoint."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    redaction = SecretRedactionFilter()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, stream_handler):
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
