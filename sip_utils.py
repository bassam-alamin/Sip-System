import logging
import sys
from pathlib import Path

from sip_message import SIPMessage

LOGS_DIR = Path(__file__).parent / "logs"


def setup_logs(component: str) -> None:
    LOGS_DIR.mkdir(exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / f"{component}.log", encoding="utf-8"),
    ]

    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


logger = logging.getLogger(__name__)


def show_message(msg: SIPMessage) -> None:
    kind = "request" if msg.is_request else "response"
    display_headers: dict[str, str] = {}
    for name, value in msg.headers.items():
        if isinstance(value, list):
            display_headers[name] = ", ".join(value)
        else:
            display_headers[name] = value

    logger.info(
        "Received %s:\n%s\nHeaders: %s",
        kind,
        msg.start_line,
        display_headers,
    )
