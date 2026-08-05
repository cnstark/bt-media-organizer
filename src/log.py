"""日志初始化。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LogConf


def setup_logger(conf: LogConf) -> logging.Logger:
    logger = logging.getLogger("lite-organizer")
    logger.setLevel(getattr(logging, conf.level.upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if conf.file:
        try:
            fh = RotatingFileHandler(
                conf.file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as e:
            logger.warning(f"日志文件不可写,仅输出到 stdout: {e}")

    return logger
