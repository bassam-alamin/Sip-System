import asyncio
import logging

from sip_message import SIPMessage, SIPParseError
from sip_utils import setup_logs, show_message

logger = logging.getLogger(__name__)

RESPONSE_DELAY_S: float = 1.0


class FinalServerUDP(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport
        logger.info("Final Server UDP listening on 0.0.0.0:5070")

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        assert self.transport is not None

        try:
            text = data.decode("utf-8")
            msg = SIPMessage.read_raw(text)
        except (SIPParseError, UnicodeDecodeError) as exc:
            logger.warning("Dropping malformed UDP datagram from %s: %s", addr, exc)
            return

        show_message(msg)
        logger.info("Final Server: received request from %s via UDP", addr)

        error_status = msg.check_if_ok()

        if error_status:
            logger.warning(
                "Final Server UDP: invalid request from %s — sending %s", addr, error_status
            )
            response = msg.make_reply(error_status)
            self.transport.sendto(response.dump_bytes(), addr)
            return

        trying = msg.make_reply("100 Trying")
        self.transport.sendto(trying.dump_bytes(), addr)

        logger.debug("Sent 100 Trying (UDP) to %s", addr)

        loop = asyncio.get_event_loop()
        loop.create_task(
            self._do_200_ok(msg, addr),
            name=f"final-udp-ok-{addr}",
        )

    async def _do_200_ok(self, msg: SIPMessage, addr: tuple[str, int]) -> None:
        assert self.transport is not None

        await asyncio.sleep(RESPONSE_DELAY_S)

        ok_response = msg.make_reply("200 OK")
        data = ok_response.dump_bytes()

        self.transport.sendto(data, addr)

        logger.debug("Sent 200 OK (UDP) to %s", addr)

    def error_received(self, exc: Exception) -> None:
        logger.error("FinalServerUDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            logger.error("FinalServerUDP connection lost: %s", exc)


async def handle_tcp_conn(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:

    peer = writer.get_extra_info("peername")
    logger.debug("Final Server TCP: new connection from %s", peer)

    try:
        data = await reader.read(65536)

        if not data:
            logger.debug("Final Server TCP: empty read from %s, closing", peer)
            return

        try:
            text = data.decode("utf-8")
            msg = SIPMessage.read_raw(text)
        except (SIPParseError, UnicodeDecodeError) as exc:
            logger.warning("Dropping malformed TCP message from %s: %s", peer, exc)
            return

        show_message(msg)
        logger.info("Final Server: received request from %s via TCP", peer)

        error_status = msg.check_if_ok()

        if error_status:
            logger.warning(
                "Final Server TCP: invalid request from "
                "%s — sending %s", peer, error_status
            )
            response = msg.make_reply(error_status)

            writer.write(response.dump_bytes())
            await writer.drain()
            return

        trying = msg.make_reply("100 Trying")

        writer.write(trying.dump_bytes())
        await writer.drain()

        logger.debug("Sent 100 Trying (TCP) to %s", peer)

        await asyncio.sleep(RESPONSE_DELAY_S)

        ok_response = msg.make_reply("200 OK")

        writer.write(ok_response.dump_bytes())
        await writer.drain()

        logger.debug("Sent 200 OK (TCP) to %s", peer)

    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.warning("Final Server TCP: connection error with %s: %s", peer, exc)

    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def boot_server(host: str = "0.0.0.0", port: int = 5070) -> None:

    loop = asyncio.get_running_loop()

    udp_transport, _ = await loop.create_datagram_endpoint(
        FinalServerUDP,
        local_addr=(host, port),
    )

    tcp_server = await asyncio.start_server(
        handle_tcp_conn,
        host,
        port,
    )

    logger.info("Final Server TCP listening on %s:%s", host, port)

    try:
        async with tcp_server:
            await tcp_server.serve_forever()
    finally:
        udp_transport.close()


if __name__ == "__main__":
    setup_logs("server")
    asyncio.run(boot_server())
