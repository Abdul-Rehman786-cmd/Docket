"""Application-only logging; never enable verbose HTTP/WebSocket wire logging."""

import logging

from app.core.config import Settings


def configure_logging(settings: Settings) -> None:
    # Feature tracing is opt-out and always INFO; it never enables transport debug logs.
    logging.getLogger('team_memory.features').setLevel(logging.INFO)
    logger = logging.getLogger("team_memory")
    logger.setLevel(settings.log_level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
