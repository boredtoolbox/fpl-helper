"""Rotating file + console logging."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .config import STATE_ROOT

# Overridable so tests (and anyone packaging this elsewhere) don't write into
# the project's own logs/ directory.
LOG_DIR = Path(os.environ.get("FPL_LOG_DIR") or (STATE_ROOT / "logs"))
LOG_FILE = LOG_DIR / "fpl-helper.log"
FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"

_configured = False


def setup_logging(level: int = logging.INFO, *, console: bool = True) -> None:
    """Idempotent — safe to call from both the CLI and the Flask app factory."""
    global _configured
    if _configured:
        logging.getLogger().setLevel(level)
        return

    log_dir = Path(os.environ.get("FPL_LOG_DIR") or LOG_DIR)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(FORMAT)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "fpl-helper.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # read-only filesystem, bad path — keep the console
        logging.getLogger(__name__).warning("file logging disabled: %s", exc)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # These are chatty and we don't own them.
    for noisy in ("urllib3", "werkzeug", "apscheduler.executors", "trafilatura", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
