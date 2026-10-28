import asyncio
import struct

import httpx

from proxyhub.checker.pipeline import Pipeline, Result
from proxyhub.checker.udp import check_udp
from proxyhub.config import Settings


async def test_real_udp_associate_local_fixture():
    received = []
    class Relay(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, packet, addr):
            received.append(packet)
            # A synthetically successful DNS response with the exact query ID.
            answer = bytearray(packet)
            answer[12] |= 0x80
            self.transport.sendto(bytes(answer), addr)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Relay, local_addr=('127.0.0.1', 0))
    relay_port = transport.get_extra_info('sockname')[1]
    async def control(reader, writer):
        try:
            assert await reader.readexactly(3) == b'\x05\x01\x00'
            writer.write(b'\x05\x00')
            await writer.drain()
            assert (await reader.readexactly(10))[:3] == b'\x05\x03\x00'
            writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01'+struct.pack('!H', relay_port))
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(control, '127.0.0.1', 0)
    try:
        assert await check_udp(server.sockets[0].getsockname()[1], 2)
        assert received[0][:10] == b'\x00\x00\x00\x01\x01\x01\x01\x01\x00\x35'
    finally:
        server.close()
        await server.wait_closed()
        transport.close()


async def test_speed_fallback_and_bounded_read():
    consumed = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(100):
                consumed.append(i)
                yield b'x' * 16384
    def handler(request):
        if request.url.host == 'bad.example':
            return httpx.Response(503)
        return httpx.Response(200, stream=Stream())
    pipeline = Pipeline(Settings(measure_speed=True, speed_max_mb=.05, min_speed=0, speed_urls=['https://bad.example', 'https://good.example']), None)
    pipeline.client = lambda port=None: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await pipeline.speed(1, Result(ok=True))
    assert result.ok and result.speed_mbps > 0
    assert len(consumed) == 4  # At most one 16 KiB chunk beyond the byte cap on the wire.


async def test_require_udp_failure(monkeypatch):
    async def unavailable(port, timeout):
        return False
    monkeypatch.setattr('proxyhub.checker.pipeline.check_udp', unavailable)
    pipeline = Pipeline(Settings(measure_geo=False, measure_ping=False, dedupe_by_exit_ip=False, check_udp=True, require_udp=True), None)
    pipeline.client = lambda port=None: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    result = await pipeline.cheap(1)
    assert not result.ok and result.reason == 'udp_fail' and result.udp is False
