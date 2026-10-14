import logging
import logging.handlers

from .constants import CONFIG_DIR

LOG_DIR = CONFIG_DIR / "logs"
LOG_FILE = LOG_DIR / "myriaxon.log"

logger = logging.getLogger("myriaxon")


def configure_logging() -> None:
    """Best-effort: if the log directory can't be created/written, Myriaxon still runs --
    logging is a diagnostic aid, never a hard requirement."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    except OSError:
        pass
