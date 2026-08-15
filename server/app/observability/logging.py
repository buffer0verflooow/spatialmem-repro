"""基于标准 logging 的最小实现，输出到 stdout。"""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", pretty: bool = False) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s"
        if pretty
        else "%(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        level=level.upper(),
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
