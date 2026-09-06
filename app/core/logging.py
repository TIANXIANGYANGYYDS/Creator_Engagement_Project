from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.core.config import PROJECT_ROOT, Settings, get_settings


def configure_logging(settings: Settings | None = None) -> None:
    active_settings = settings or get_settings()
    level = getattr(logging, active_settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log_path = PROJECT_ROOT / ".local" / "logs" / "api.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not any(
        isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(log_path)
        for handler in app_logger.handlers
    ):
        handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
