"""Logging helpers shared by CLI, API and workers."""

from __future__ import annotations

import logging

from oiltech_digest import config


class _ServiceFilter(logging.Filter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self.service
        return True


def setup_logging(service: str, *, verbose: bool = False, force: bool = False) -> None:
    level_name = "DEBUG" if verbose else config.LOG_LEVEL
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s level=%(levelname)s service=%(service)s logger=%(name)s %(message)s"

    logging.basicConfig(level=level, format=fmt, force=force)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(level)
        handler.addFilter(_ServiceFilter(service))
        if force:
            handler.setFormatter(logging.Formatter(fmt))
