from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str | Path, level: int = logging.INFO) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(Path(log_dir) / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
