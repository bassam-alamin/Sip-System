import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sip_utils import setup_logs

DEFAULT_REQUEST_FILE = "sip-requests/bye.txt"


def main() -> None:
    setup_logs("client")

    parser = argparse.ArgumentParser(
        description="Mini SIP client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--protocol", choices=["udp", "tcp"], default="udp")
    parser.add_argument("--file", default=DEFAULT_REQUEST_FILE)
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.is_file():
        logging.error("Request file not found: %s", file_path)
        sys.exit(1)

    raw_message = file_path.read_text(encoding="utf-8")

    if args.protocol == "udp":
        from client import fire_udp

        asyncio.run(fire_udp(raw_message))
    else:
        from client import fire_tcp

        asyncio.run(fire_tcp(raw_message))


if __name__ == "__main__":
    main()
