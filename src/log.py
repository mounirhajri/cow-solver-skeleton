import logging
import sys
from typing import IO, TextIO, cast

import structlog


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    """Configure structlog to emit JSON to stdout (or supplied stream)."""

    out_stream: TextIO = cast(TextIO, stream) if stream is not None else sys.stdout

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=out_stream),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
