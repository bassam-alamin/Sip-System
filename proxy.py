import asyncio
import logging
from collections.abc import Callable

from sip_message import SIPMessage, SIPParseError
from sip_utils import setup_logs, show_message

logger = logging.getLogger(__name__)

FINAL_SERVER_HOST = "127.0.0.1"
FINAL_SERVER_PORT = 5070

# The final server sends 200 OK after about 1 second,
# so waiting 3 seconds gives enough time.
UDP_FORWARD_WINDOW_S: float = 3.0


def _is_trying_response(msg: SIPMessage) -> bool:
    return bool(msg.is_response and msg.start_line.startswith("SIP/2.0 1"))


class _ForwardingProtocol(asyncio.DatagramProtocol):
    def __init__(
            self,
            client_addr: tuple[str, int],
            relay_cb: Callable[[bytes, tuple[str, int]], None],
            error_cb: Callable[[], None] | None = None,
    ) -> None:
        self._client_addr = client_addr
        self._relay_cb = relay_cb
        self._error_cb = error_cb
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            text = data.decode("utf-8")
            msg = SIPMessage.read_raw(text)
        except (SIPParseError, UnicodeDecodeError) as exc:
            logger.warning("Proxy: malformed response from final server %s: %s", addr, exc)
            return

        show_message(msg)

        if _is_trying_response(msg):
            logger.debug("Proxy: already sent our own provisional response %s", msg.start_line)
            return

        logger.debug("Proxy UDP: relaying %s to client %s", msg.start_line, self._client_addr)

        self._relay_cb(data, self._client_addr)

    def error_received(self, exc: Exception) -> None:
        logger.error("Proxy UDP: final server unreachable: %s", exc)
        if self._error_cb:
            self._error_cb()

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            logger.error("ForwardingProtocol connection lost: %s", exc)


class ProxyUDP(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport
        logger.info("Proxy UDP listening on 0.0.0.0:5060")

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:

        assert self.transport is not None

        try:
            text = data.decode("utf-8")
            msg = SIPMessage.read_raw(text)

        except (SIPParseError, UnicodeDecodeError) as exc:
            logger.warning("Proxy: dropping malformed UDP datagram from %s: %s", addr, exc)
            return

        show_message(msg)

        logger.info("Proxy: received request from client %s via UDP", addr)

        error_status = msg.check_if_ok()

        if error_status:
            logger.warning("Proxy UDP: invalid request from %s — sending %s", addr, error_status)

            response = msg.make_reply(error_status)

            self.transport.sendto(response.dump_bytes(), addr)

            return

        trying = msg.make_reply("100 Trying")

        self.transport.sendto(trying.dump_bytes(), addr)

        logger.debug("Proxy: sent 100 Trying (UDP) to client %s", addr)

        msg.tick_down_hops()

        loop = asyncio.get_event_loop()

        loop.create_task(
            self._send_to_server_udp(msg, addr),
            name=f"proxy-udp-fwd-{addr}",
        )

    async def _send_to_server_udp(self, msg: SIPMessage, client_addr: tuple[str, int]) -> None:

        assert self.transport is not None

        loop = asyncio.get_running_loop()

        proxy_transport = self.transport

        def _send_503() -> None:
            response = msg.make_reply("503 Service Unavailable")
            proxy_transport.sendto(response.dump_bytes(), client_addr)
            logger.warning("Proxy UDP: sent 503 to %s — final server unreachable", client_addr)

        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ForwardingProtocol(
                    client_addr,
                    relay_cb=lambda data, addr: proxy_transport.sendto(data, addr),
                    error_cb=_send_503,
                ),
                remote_addr=(FINAL_SERVER_HOST, FINAL_SERVER_PORT),
            )
        except OSError as exc:
            logger.error("Proxy UDP: cannot reach final server: %s", exc)
            _send_503()
            return

        try:
            transport.sendto(msg.dump_bytes())
            logger.debug("Proxy UDP: forwarded request to final server")
            await asyncio.sleep(UDP_FORWARD_WINDOW_S)
        finally:
            transport.close()

    def error_received(self, exc: Exception) -> None:
        logger.error("ProxyUDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            logger.error("ProxyUDP connection lost: %s", exc)


async def handle_tcp_conn(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:

    peer = client_writer.get_extra_info("peername")
    logger.debug("Proxy TCP: new connection from %s", peer)
    try:
        data = await client_reader.read(65536)
        if not data:
            logger.debug("Proxy TCP: empty read from %s, closing", peer)
            return

        try:
            text = data.decode("utf-8")
            msg = SIPMessage.read_raw(text)
        except (SIPParseError, UnicodeDecodeError) as exc:
            logger.warning("Proxy TCP: dropping malformed message from %s: %s", peer, exc)
            return

        show_message(msg)

        logger.info("Proxy: received request from client %s via TCP", peer)

        error_status = msg.check_if_ok()

        if error_status:
            logger.warning("Proxy TCP: invalid request from %s — sending %s", peer, error_status)

            response = msg.make_reply(error_status)

            client_writer.write(response.dump_bytes())

            await client_writer.drain()

            return
        trying = msg.make_reply("100 Trying")
        client_writer.write(trying.dump_bytes())
        await client_writer.drain()
        msg.tick_down_hops()

        await _send_to_server_tcp(msg, client_writer)

    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.warning("Proxy TCP: connection error with client %s: %s", peer, exc)

    finally:
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass


async def _send_to_server_tcp(
    msg: SIPMessage,
    client_writer: asyncio.StreamWriter,
) -> None:

    try:
        server_reader, server_writer = await asyncio.open_connection(
            FINAL_SERVER_HOST,
            FINAL_SERVER_PORT,
        )
    except (ConnectionRefusedError, OSError) as exc:
        logger.error("Proxy TCP: cannot connect to final server: %s", exc)
        response = msg.make_reply("503 Service Unavailable")
        client_writer.write(response.dump_bytes())
        await client_writer.drain()
        return
    try:
        server_writer.write(msg.dump_bytes())
        await server_writer.drain()
        logger.debug("Proxy TCP: forwarded request to final server")
        buffer = b""
        while True:
            chunk = await server_reader.read(65536)
            if not chunk:
                break
            buffer = buffer + chunk
            while b"\r\n\r\n" in buffer:
                raw_msg, buffer = buffer.split(b"\r\n\r\n", 1)
                raw_msg_str = raw_msg.decode("utf-8") + "\r\n\r\n"
                try:
                    resp = SIPMessage.read_raw(raw_msg_str)

                except (SIPParseError, UnicodeDecodeError) as exc:
                    logger.warning("Proxy TCP: malformed response from final server: %s", exc)
                    continue
                show_message(resp)
                if _is_trying_response(resp):
                    continue
                client_writer.write(raw_msg_str.encode("utf-8"))
                await client_writer.drain()
    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.warning("Proxy TCP: connection to final server dropped: %s", exc)
        response = msg.make_reply("503 Service Unavailable")
        try:
            client_writer.write(response.dump_bytes())
            await client_writer.drain()
        except Exception:
            pass

    finally:
        try:
            server_writer.close()
            await server_writer.wait_closed()
        except Exception:
            pass


async def boot_proxy(host: str = "0.0.0.0", port: int = 5060) -> None:

    loop = asyncio.get_running_loop()

    udp_transport, _ = await loop.create_datagram_endpoint(
        ProxyUDP,
        local_addr=(host, port),
    )

    tcp_server = await asyncio.start_server(
        handle_tcp_conn,
        host,
        port,
    )

    logger.info("Proxy TCP listening on %s:%s", host, port)

    try:
        async with tcp_server:
            await tcp_server.serve_forever()

    finally:
        udp_transport.close()


if __name__ == "__main__":
    setup_logs("proxy")
    asyncio.run(boot_proxy())
