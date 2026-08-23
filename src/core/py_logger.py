#!/usr/bin/env python3
"""
py_logger.py — one consistent console log format for every stage/
checkpoint/control script, so run_pipeline.py's batch output reads as
one stream instead of each script's own ad-hoc print() formatting.

Singleton by logger name: calling get_logger("stage03_research") twice
in the same process returns the same configured logger rather than
attaching a second handler and doubling every line. This matters
specifically for run_pipeline.py, which imports and calls into
multiple stages within one process.
"""

from __future__ import annotations
import logging
import sys

_CONFIGURED: set[str] = set()

_FORMAT = "[%(name)s] %(message)s"


def get_logger(name: str, *, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if name not in _CONFIGURED:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
        _CONFIGURED.add(name)
    return logger
