"""Local SOCKS5 bridge: no-auth in, authenticated SOCKS5 out.

Why this exists: Asocks shared ports are SOCKS5 with username/password auth, and
Chromium has no support for SOCKS5 authentication — not via `--proxy-server` and
not via CDP `Fetch.continueWithAuth` (which only answers HTTP 407 challenges). So
we run a tiny SOCKS5 server on 127.0.0.1 that accepts Chrome's unauthenticated
connections and tunnels each one upstream to the Asocks proxy, performing the
RFC-1929 user/pass handshake on Chrome's behalf. Chrome then just uses
`socks5://127.0.0.1:<port>`.

One bridge is kept alive per (upstream host, port, user) and reused across runs,
so it pairs naturally with the proxy caching in asocks.py. Pure asyncio, no deps.

Address types (ATYP) are forwarded verbatim, so a domain sent by Chrome stays a
domain upstream (remote DNS) — the proxy resolves it from the exit IP's vantage.
"""
from __future__ import annotations

import asyncio
import contextlib
import struct
from typing import Optional
from urllib.parse import urlparse

from .events import bus
from .models import Proxy

_BUF = 64 * 1024


def _split_server(server: str) -> tuple[str, int]:
    """'socks5://host:port' | 'host:port' -> (host, port)."""
    s = server if "://" in server else f"socks5://{server}"
    u = urlparse(s)
    if not u.hostname or not u.port:
        raise ValueError(f"bad proxy server: {server!r}")
    return u.hostname, u.port


async def _read_addr(reader: asyncio.StreamReader, atyp: int) -> bytes:
    """Read a SOCKS5 address of the given type, returning it in the exact wire
    form (domain keeps its 1-byte length prefix) so it can be re-sent as-is."""
    if atyp == 0x01:  # IPv4
        return await reader.readexactly(4)
    if atyp == 0x04:  # IPv6
        return await reader.readexactly(16)
    if atyp == 0x03:  # domain
        length = (await reader.readexactly(1))[0]
        return bytes([length]) + await reader.readexactly(length)
    raise ValueError(f"unsupported ATYP {atyp}")


async def _connect_upstream(
    host: str,
    port: int,
    user: Optional[str],
    password: Optional[str],
    atyp: int,
    addr: bytes,
    dport: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open an upstream SOCKS5 tunnel to (addr:dport), authenticating if asked."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        # Greeting: offer username/password (0x02) and no-auth (0x00).
        writer.write(b"\x05\x02\x00\x02")
        await writer.drain()
        ver, method = await reader.readexactly(2)
        if ver != 0x05:
            raise ConnectionError("upstream is not SOCKS5")
        if method == 0x02:
            u = (user or "").encode()
            p = (password or "").encode()
            writer.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            await writer.drain()
            _, status = await reader.readexactly(2)
            if status != 0x00:
                raise ConnectionError("upstream proxy auth rejected")
        elif method != 0x00:
            raise ConnectionError(f"upstream chose unsupported method {method:#x}")

        # CONNECT, forwarding the client's address type/bytes untouched.
        writer.write(
            b"\x05\x01\x00" + bytes([atyp]) + addr + struct.pack("!H", dport)
        )
        await writer.drain()
        ver, rep, _, batyp = await reader.readexactly(4)
        if rep != 0x00:
            raise ConnectionError(f"upstream CONNECT failed (REP {rep})")
        # Drain BND.ADDR/BND.PORT, which we don't use.
        await _read_addr(reader, batyp)
        await reader.readexactly(2)
        return reader, writer
    except BaseException:
        with contextlib.suppress(Exception):
            writer.close()
        raise


async def _pipe(
    a_r: asyncio.StreamReader,
    a_w: asyncio.StreamWriter,
    b_r: asyncio.StreamReader,
    b_w: asyncio.StreamWriter,
) -> None:
    async def copy(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await r.read(_BUF)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                w.close()

    await asyncio.gather(copy(a_r, b_w), copy(b_r, a_w))


class _Bridge:
    def __init__(
        self, host: str, port: int, user: Optional[str], password: Optional[str]
    ) -> None:
        self._u_host = host
        self._u_port = port
        self._u_user = user
        self._u_pass = password
        self._server: Optional[asyncio.AbstractServer] = None
        self.port = 0

    @property
    def serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, rep: int) -> None:
        # Reply with a dummy BND.ADDR/PORT (0.0.0.0:0).
        writer.write(b"\x05" + bytes([rep]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # Client greeting; we always answer "no auth required".
            ver, nmethods = await reader.readexactly(2)
            await reader.readexactly(nmethods)
            if ver != 0x05:
                return
            writer.write(b"\x05\x00")
            await writer.drain()

            # Request: only CONNECT is supported.
            ver, cmd, _rsv, atyp = await reader.readexactly(4)
            try:
                addr = await _read_addr(reader, atyp)
            except ValueError:
                await self._reply(writer, 0x08)  # address type not supported
                return
            dport = struct.unpack("!H", await reader.readexactly(2))[0]
            if cmd != 0x01:
                await self._reply(writer, 0x07)  # command not supported
                return

            try:
                u_r, u_w = await _connect_upstream(
                    self._u_host, self._u_port, self._u_user, self._u_pass,
                    atyp, addr, dport,
                )
            except Exception:
                await self._reply(writer, 0x01)  # general failure
                return

            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success
            await writer.drain()
            await _pipe(reader, writer, u_r, u_w)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


class BridgeManager:
    """Starts/reuses one local bridge per upstream and hands out a local,
    auth-free proxy that Chrome can consume directly."""

    def __init__(self) -> None:
        self._bridges: dict[str, _Bridge] = {}
        self._lock = asyncio.Lock()

    async def local_proxy_for(self, upstream: Proxy) -> Proxy:
        host, port = _split_server(upstream.server)
        key = f"{host}:{port}:{upstream.username or ''}"
        async with self._lock:
            bridge = self._bridges.get(key)
            if bridge is None or not bridge.serving:
                bridge = _Bridge(host, port, upstream.username, upstream.password)
                await bridge.start()
                self._bridges[key] = bridge
                bus.emit(
                    "info", "system", "asocks", "socks bridge up",
                    f"127.0.0.1:{bridge.port} -> {host}:{port}",
                )
            return Proxy(server=f"socks5://127.0.0.1:{bridge.port}")

    async def shutdown(self) -> None:
        async with self._lock:
            for bridge in self._bridges.values():
                await bridge.stop()
            self._bridges.clear()


manager = BridgeManager()
