"""结构化日志。JSON 输出，必带 thread_id / device_id / node（CLAUDE.md §14）。"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO", pretty: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    renderer = (
        structlog.dev.ConsoleRenderer() if pretty else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_request(thread_id: str, device_id: str) -> None:
    """把请求级字段绑到 contextvars，后续所有日志自动带上。"""
    structlog.contextvars.bind_contextvars(thread_id=thread_id, device_id=device_id)


def clear_request() -> None:
    structlog.contextvars.clear_contextvars()
