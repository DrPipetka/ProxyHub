import asyncio
import ipaddress
import secrets
import socket
import struct


async def check_udp(port, timeout):
    """RFC 1928 UDP ASSOCIATE followed by a real DNS query through the relay."""
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            if await reader.readexactly(2) != b"\x05\x00":
                return False
            writer.write(b"\x05\x03\x00\x01" + b"\x00" * 6)
            await writer.drain()
            header = await reader.readexactly(4)
            if header[:2] != b"\x05\x00":
                return False
            if header[3] == 1:
                relay = str(ipaddress.ip_address(await reader.readexactly(4)))
            elif header[3] == 4:
                relay = str(ipaddress.ip_address(await reader.readexactly(16)))
            elif header[3] == 3:
                relay = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                return False
            relay_port = struct.unpack("!H", await reader.readexactly(2))[0]
            if relay in ("0.0.0.0", "::"):
                relay = "127.0.0.1"
            query_id = secrets.token_bytes(2)
            dns = query_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"\x07example\x03com\x00\x00\x01\x00\x01"
            packet = b"\x00\x00\x00\x01\x01\x01\x01\x01\x00\x35" + dns
            family = socket.AF_INET6 if ":" in relay else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                loop = asyncio.get_running_loop()
                await loop.sock_connect(sock, (relay, relay_port))
                await loop.sock_sendall(sock, packet)
                response = await loop.sock_recv(sock, 4096)
            if len(response) < 10 or response[:3] != b"\x00\x00\x00":
                return False
            kind = response[3]
            offset = 10 if kind == 1 else 22 if kind == 4 else 7 + response[4] if kind == 3 else len(response)
            answer = response[offset:]
            return len(answer) >= 12 and answer[:2] == query_id and bool(answer[2] & 0x80) and answer[3] & 15 == 0
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        return False
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
