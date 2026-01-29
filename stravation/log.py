from __future__ import annotations
import logging, sys

def setup_logging(level: int = logging.INFO) -> None:
    fmt = "[%(levelname)s] %(name)s | %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=level, handlers=[handler])
