"""Logging setup for the edupagetasks application."""

from __future__ import annotations

import logging
import os

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: str, file: str | None) -> None:
    logger = logging.getLogger("edupagetasks")
    level_num = logging.getLevelName(str(level).strip().upper())
    if not isinstance(level_num, int):
        level_num = logging.INFO
    logger.setLevel(level_num)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if file is None:
        handler: logging.Handler = logging.StreamHandler()
    else:
        handler = logging.FileHandler(file, encoding="utf-8")
        os.chmod(file, 0o600)
    handler.setLevel(level_num)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
