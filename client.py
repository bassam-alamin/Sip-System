import asyncio
import logging

from sip_message import SIPMessage, SIPParseError
from sip_utils import show_message

logger = logging.getLogger(__name__)

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 5060
UDP_RECEIVE_WINDOW_S: float = 3.0


def _handle_data(data: bytes, label: str) -> SIPMessage | None:
    try:
        msg = SIPMessage.read_raw(data.decode("utf-8"))
    except (SIPParseError, UnicodeDecodeError) as exc:
        logger.warning("Client %s: malformed message: %s", label, exc)
        return None
    show_message(msg)
    return msg


class _ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _handle_data(data, label=f"UDP from {addr}")

    def error_received(self, exc: Exception) -> None:
        logger.error("Client UDP: proxy unreachable at %s:%d — %s", PROXY_HOST, PROXY_PORT, exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            logger.error("Client UDP connection lost: %s", exc)


async def fire_udp(raw_message: str) -> None:
    loop = asyncio.get_running_loop()

    transport, _ = await loop.create_datagram_endpoint(
        _ClientProtocol,
        remote_addr=(PROXY_HOST, PROXY_PORT),
    )

    try:
        transport.sendto(raw_message.encode("utf-8"))
        logger.debug("Client UDP: request sent to %s:%d", PROXY_HOST, PROXY_PORT)
        await asyncio.sleep(UDP_RECEIVE_WINDOW_S)
    finally:
        transport.close()


async def fire_tcp(raw_message: str) -> None:
    try:
        reader, writer = await asyncio.open_connection(PROXY_HOST, PROXY_PORT)
    except OSError as exc:
        logger.error("Client TCP: proxy unreachable at %s:%d — %s", PROXY_HOST, PROXY_PORT, exc)
        return

    try:
        writer.write(raw_message.encode("utf-8"))
        await writer.drain()
        logger.debug("Client TCP: request sent to %s:%d", PROXY_HOST, PROXY_PORT)

        while True:
            data = await reader.read(65536)
            if not data:
                break
            _handle_data(data, label="TCP")

    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.warning("Client TCP: connection error: %s", exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass
