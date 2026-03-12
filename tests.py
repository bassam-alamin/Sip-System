import asyncio
import contextlib
import unittest
from unittest.mock import patch
from sip_message import RESPONSE_COPY_HEADERS, SIPMessage

class TestBuildResponse(unittest.TestCase):
    def _invite(self) -> SIPMessage:
        raw_message = (
            "INVITE sip:bob@biloxi.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.atlanta.com;branch=z9hG4bK776asdhds\r\n"
            "From: Alice <sip:alice@atlanta.com>;tag=1928301774\r\n"
            "To: Bob <sip:bob@biloxi.com>\r\n"
            "Call-ID: a84b4c76e66710@pc33.atlanta.com\r\n"
            "CSeq: 314159 INVITE\r\n"
            "Contact: <sip:alice@pc33.atlanta.com>\r\n"
            "Expires: 3600\r\n"
            "Max-Forwards: 70\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )

        msg = SIPMessage.read_raw(raw_message)
        return msg

    def test_copies_all_required_headers_into_response(self) -> None:
        invite = self._invite()
        resp = invite.make_reply("100 Trying")
        for header in RESPONSE_COPY_HEADERS:
            self.assertIn(header, resp.headers, f"{header!r} missing from response")
        self.assertEqual(resp.headers["Content-Length"], "0")
        self.assertNotIn("Max-Forwards", resp.headers)


class TestDecrementMaxForwards(unittest.TestCase):
    def test_decrements_by_one(self) -> None:

        headers: dict[str, str | list[str]] = {"Max-Forwards": "70"}

        msg = SIPMessage("INVITE sip:bob SIP/2.0", headers)

        msg.tick_down_hops()

        result = msg.headers["Max-Forwards"]

        self.assertEqual(result, "69")

    def test_validate_request_returns_483_when_max_forwards_is_zero(self) -> None:
        raw = (
            "INVITE sip:bob@biloxi.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.atlanta.com;branch=z9hG4bKtest\r\n"
            "From: Alice <sip:alice@atlanta.com>;tag=1\r\n"
            "To: Bob <sip:bob@biloxi.com>\r\n"
            "Call-ID: test-mf-zero@atlanta.com\r\n"
            "CSeq: 1 INVITE\r\n"
            "Max-Forwards: 0\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        msg = SIPMessage.read_raw(raw)
        self.assertEqual(msg.check_if_ok(), "483 Too Many Hops")

    def test_validate_request_passes_when_max_forwards_is_one(self) -> None:
        raw = (
            "INVITE sip:bob@biloxi.com SIP/2.0\r\n"
            "Via: SIP/2.0/UDP pc33.atlanta.com;branch=z9hG4bKtest\r\n"
            "From: Alice <sip:alice@atlanta.com>;tag=1\r\n"
            "To: Bob <sip:bob@biloxi.com>\r\n"
            "Call-ID: test-mf-one@atlanta.com\r\n"
            "CSeq: 1 INVITE\r\n"
            "Max-Forwards: 1\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        msg = SIPMessage.read_raw(raw)
        self.assertIsNone(msg.check_if_ok())


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    _PROXY_PORT = 15060
    _SERVER_PORT = 15070

    _RAW_REQUEST = (
        b"INVITE sip:bob@biloxi.com SIP/2.0\r\n"
        b"Via: SIP/2.0/UDP client.atlanta.com;branch=z9hG4bKe2e\r\n"
        b"From: Alice <sip:alice@atlanta.com>;tag=e2e\r\n"
        b"To: Bob <sip:bob@biloxi.com>\r\n"
        b"Call-ID: e2e-test-call-id\r\n"
        b"CSeq: 1 INVITE\r\n"
        b"Max-Forwards: 70\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )

    async def asyncSetUp(self) -> None:

        import proxy as proxy_mod

        self._orig_server_port = proxy_mod.FINAL_SERVER_PORT

        proxy_mod.FINAL_SERVER_PORT = self._SERVER_PORT

        from proxy import boot_proxy
        from server import boot_server

        self._server_task = asyncio.create_task(boot_server("127.0.0.1", self._SERVER_PORT))

        self._proxy_task = asyncio.create_task(boot_proxy("127.0.0.1", self._PROXY_PORT))

        await asyncio.sleep(0.1)

        tasks = [self._server_task, self._proxy_task]

        for task in tasks:
            if task.done():
                raise RuntimeError(f"Component failed to start: {task.exception()}")

    async def asyncTearDown(self) -> None:
        import proxy as proxy_mod
        proxy_mod.FINAL_SERVER_PORT = self._orig_server_port
        self._proxy_task.cancel()
        self._server_task.cancel()

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._proxy_task

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._server_task

        await asyncio.sleep(0.1)

    async def _send_udp(self) -> bytes:

        received = []

        class _Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                received.append(data)

        loop = asyncio.get_running_loop()

        transport, _ = await loop.create_datagram_endpoint(
            _Collector, remote_addr=("127.0.0.1", self._PROXY_PORT)
        )

        try:
            transport.sendto(self._RAW_REQUEST)

            # wait a bit for responses
            await asyncio.sleep(2.0)

        finally:
            transport.close()

        result = b"".join(received)

        return result

    async def _send_tcp(self) -> bytes:

        reader, writer = await asyncio.open_connection("127.0.0.1", self._PROXY_PORT)

        received = b""

        try:
            writer.write(self._RAW_REQUEST)
            await writer.drain()

            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=3.0)

                if not chunk:
                    break

                received = received + chunk

        except TimeoutError:
            pass

        finally:
            writer.close()

            with contextlib.suppress(Exception):
                await writer.wait_closed()

        return received

    async def test_udp_client_receives_100_trying_and_200_ok(self) -> None:

        data = await self._send_udp()

        self.assertIn(b"100 Trying", data)
        self.assertIn(b"200 OK", data)

    async def test_tcp_client_receives_100_trying_and_200_ok(self) -> None:

        data = await self._send_tcp()

        self.assertIn(b"100 Trying", data)
        self.assertIn(b"200 OK", data)

    async def test_proxy_sends_exactly_one_100_trying_to_tcp_client(self) -> None:

        data = await self._send_tcp()

        count = data.count(b"100 Trying")

        self.assertEqual(count, 1, "Client should receive exactly one 100 Trying")

        self.assertIn(b"200 OK", data)

    async def test_proxy_returns_400_for_malformed_request(self) -> None:

        # request missing Call-ID header
        malformed = (
            b"INVITE sip:ahmed@cybernaptics.com SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP bassam-pc.cybernaptics.com;branch=z9hG4bKmalform1\r\n"
            b"From: Bassam <sip:bassam@cybernaptics.com>;tag=112233\r\n"
            b"To: Ahmed <sip:ahmed@cybernaptics.com>\r\n"
            b"CSeq: 1 INVITE\r\n"
            b"Contact: <sip:bassam@192.168.1.10>\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )

        received = []

        class _Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                received.append(data)

        loop = asyncio.get_running_loop()

        transport, _ = await loop.create_datagram_endpoint(
            _Collector, remote_addr=("127.0.0.1", self._PROXY_PORT)
        )

        try:
            transport.sendto(malformed)

            await asyncio.sleep(0.5)

        finally:
            transport.close()

        data = b"".join(received)

        self.assertIn(b"400 Bad Request", data)
        self.assertNotIn(b"100 Trying", data)

    async def test_proxy_returns_483_when_max_forwards_is_zero(self) -> None:
        request = (
            b"INVITE sip:bob@biloxi.com SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP client.atlanta.com;branch=z9hG4bKmf0\r\n"
            b"From: Alice <sip:alice@atlanta.com>;tag=mf0\r\n"
            b"To: Bob <sip:bob@biloxi.com>\r\n"
            b"Call-ID: test-mf-zero-e2e\r\n"
            b"CSeq: 1 INVITE\r\n"
            b"Max-Forwards: 0\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )

        received = []

        class _Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                received.append(data)

        loop = asyncio.get_running_loop()

        transport, _ = await loop.create_datagram_endpoint(
            _Collector, remote_addr=("127.0.0.1", self._PROXY_PORT)
        )

        try:
            transport.sendto(request)
            await asyncio.sleep(0.5)
        finally:
            transport.close()

        data = b"".join(received)

        self.assertIn(b"483 Too Many Hops", data)
        self.assertNotIn(b"100 Trying", data)

    async def test_proxy_returns_483_when_last_hop_depletes_max_forwards(self) -> None:
        request = (
            b"INVITE sip:bob@biloxi.com SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP client.atlanta.com;branch=z9hG4bKmf1\r\n"
            b"From: Alice <sip:alice@atlanta.com>;tag=mf1\r\n"
            b"To: Bob <sip:bob@biloxi.com>\r\n"
            b"Call-ID: test-mf-one-e2e\r\n"
            b"CSeq: 1 INVITE\r\n"
            b"Max-Forwards: 1\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )

        received = []

        class _Collector(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                received.append(data)

        loop = asyncio.get_running_loop()

        transport, _ = await loop.create_datagram_endpoint(
            _Collector, remote_addr=("127.0.0.1", self._PROXY_PORT)
        )

        try:
            transport.sendto(request)
            await asyncio.sleep(2.0)
        finally:
            transport.close()

        data = b"".join(received)

        self.assertIn(b"100 Trying", data)
        self.assertIn(b"483 Too Many Hops", data)
        self.assertNotIn(b"200 OK", data)

    async def test_proxy_decrements_max_forwards_before_forwarding(self) -> None:

        intercepted = []
        from server import FinalServerUDP
        original = FinalServerUDP.datagram_received
        def capturing(self_inner: FinalServerUDP, data: bytes, addr: tuple[str, int]) -> None:

            intercepted.append(data)

            original(self_inner, data, addr)

        with patch.object(FinalServerUDP, "datagram_received", capturing):
            loop = asyncio.get_running_loop()

            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, remote_addr=("127.0.0.1", self._PROXY_PORT)
            )
            try:
                transport.sendto(self._RAW_REQUEST)
                await asyncio.sleep(0.5)
            finally:
                transport.close()

        self.assertTrue(intercepted, "Final server received no message")
        decoded = intercepted[0].decode("utf-8")
        msg = SIPMessage.read_raw(decoded)
        value = msg.grab_header("Max-Forwards")
        self.assertEqual(value, "69")


if __name__ == "__main__":
    unittest.main(verbosity=2)
