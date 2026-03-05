"""
Async WebSocket-to-TCP proxy for freeciv-server.

Pure asyncio WebSocket-to-TCP bridge. Runs as a module inside the
FastAPI process (server.py).

Protocol:
  Browser ←→ WebSocket (JSON text frames)
  Proxy   ←→ TCP (2-byte big-endian length + UTF-8 JSON + NUL terminator)
"""

import asyncio
import json
import logging
import re
import struct
import time
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("xbworld-proxy")

CONNECTION_LIMIT = 1000
_connections: dict[str, "CivBridge"] = {}


def validate_username(name: str) -> bool:
    if not name or len(name) <= 2 or len(name) >= 32:
        return False
    return name.lower() != "pbem" and re.fullmatch(r"[a-z][a-z0-9]*", name.lower()) is not None


class CivBridge:
    """Async bridge between a single WebSocket client and a freeciv-server TCP connection."""

    def __init__(self, ws: WebSocket, username: str, server_port: int, key: str):
        self.ws = ws
        self.username = username
        self.server_port = server_port
        self.key = key
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._stopped = False
        self._send_buffer: list[str] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._tcp_pkt_count = 0
        self._ws_send_count = 0
        self._start_time = time.monotonic()

    async def connect_to_server(self, login_packet: str) -> bool:
        logger.info("[proxy:%s] Connecting to civserver at 127.0.0.1:%d", self.username, self.server_port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.server_port),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError) as e:
            logger.error("[proxy:%s] Failed to connect to civserver port %d: %s",
                         self.username, self.server_port, e)
            await self._send_error(f"Proxy unable to connect to civserver on port {self.server_port}: {e}")
            return False

        logger.info("[proxy:%s] TCP connected to civserver, forwarding login packet", self.username)
        await self._send_to_server(login_packet)
        self._flush_task = asyncio.create_task(self._server_reader_loop())
        return True

    async def send_from_client(self, message: str):
        await self._send_to_server(message)

    async def close(self):
        elapsed = time.monotonic() - self._start_time
        logger.info("[proxy:%s] Closing bridge (server_port=%d, tcp_pkts=%d, ws_sends=%d, uptime=%.1fs)",
                     self.username, self.server_port, self._tcp_pkt_count, self._ws_send_count, elapsed)
        self._stopped = True
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        _connections.pop(self.key, None)
        logger.info("[proxy:%s] Bridge closed, active connections=%d", self.username, len(_connections))

    async def _send_to_server(self, message: str):
        if self._writer is None or self._stopped:
            return
        try:
            encoded = message.encode("utf-8")
            header = struct.pack(">H", len(encoded) + 3)
            self._writer.write(header + encoded + b"\0")
            await self._writer.drain()
        except Exception as e:
            logger.warning("[proxy:%s] Failed to send to civserver: %s", self.username, e)

    async def _server_reader_loop(self):
        """Read packets from freeciv-server TCP and forward to WebSocket client."""
        exit_reason = "unknown"
        try:
            while not self._stopped and self._reader:
                header_data = await self._read_exact(2)
                if header_data is None:
                    exit_reason = "TCP read returned None (connection closed or timeout)"
                    break

                (packet_size,) = struct.unpack(">H", header_data)
                body_size = packet_size - 2
                if body_size <= 0 or body_size > 32767:
                    logger.error("[proxy:%s] Invalid packet size %d from server", self.username, body_size)
                    continue

                body = await self._read_exact(body_size)
                if body is None:
                    exit_reason = "TCP body read returned None"
                    break

                if body and body[-1] == 0:
                    body = body[:-1]

                self._tcp_pkt_count += 1

                try:
                    text = body.decode("utf-8", errors="ignore")
                    self._send_buffer.append(text)
                except UnicodeDecodeError:
                    logger.error("[proxy:%s] UTF-8 decode error", self.username)
                    continue

                flush_ok = await self._flush_to_client()
                if not flush_ok:
                    exit_reason = "flush_to_client failed (WebSocket send error)"
                    break

            if self._stopped:
                exit_reason = "stopped flag set"

        except asyncio.CancelledError:
            exit_reason = "cancelled"
        except Exception as e:
            exit_reason = f"exception: {type(e).__name__}: {e}"
            logger.warning("[proxy:%s] Server reader error: %s", self.username, e)
        finally:
            logger.info("[proxy:%s] _server_reader_loop exited: reason='%s' tcp_pkts=%d ws_sends=%d",
                         self.username, exit_reason, self._tcp_pkt_count, self._ws_send_count)
            if not self._stopped:
                logger.info("[proxy:%s] TCP connection from civserver closed (server initiated)", self.username)
                await self.close()

    async def _read_exact(self, n: int) -> Optional[bytes]:
        try:
            data = await asyncio.wait_for(self._reader.readexactly(n), timeout=300)
            return data
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
            logger.info("[proxy:%s] _read_exact(%d) failed: %s", self.username, n, type(e).__name__)
            return None

    async def _flush_to_client(self) -> bool:
        """Flush send buffer to WebSocket client. Returns False if send failed."""
        if not self._send_buffer or self._stopped:
            return True
        packet = "[" + ",".join(self._send_buffer) + "]"
        self._send_buffer.clear()
        try:
            await self.ws.send_text(packet)
            self._ws_send_count += 1
            if self._ws_send_count % 500 == 0:
                elapsed = time.monotonic() - self._start_time
                logger.info("[proxy:%s] WS send stats: %d sends, %d tcp_pkts, %.1f sends/s, uptime=%.1fs",
                             self.username, self._ws_send_count, self._tcp_pkt_count,
                             self._ws_send_count / elapsed if elapsed > 0 else 0, elapsed)
            return True
        except Exception as e:
            logger.error("[proxy:%s] _flush_to_client FAILED: %s: %s (after %d sends, %d tcp_pkts)",
                          self.username, type(e).__name__, e, self._ws_send_count, self._tcp_pkt_count)
            self._stopped = True
            return False

    async def _send_error(self, message: str):
        error_json = json.dumps({
            "pid": 25, "event": 100, "message": message
        })
        try:
            await self.ws.send_text(f"[{error_json}]")
        except Exception:
            pass


async def handle_civsocket(ws: WebSocket, proxy_port: int):
    """FastAPI WebSocket endpoint handler for /civsocket/{port}.

    The first message from the client is the login packet containing
    username and server port. Subsequent messages are forwarded to the
    freeciv-server.
    """
    logger.info("[proxy] New WebSocket connection on proxy_port=%d, active=%d", proxy_port, len(_connections))
    await ws.accept()

    if len(_connections) >= CONNECTION_LIMIT:
        logger.error("[proxy] Connection limit reached (%d), rejecting", CONNECTION_LIMIT)
        await ws.close(code=1013, reason="Connection limit reached")
        return

    conn_id = str(uuid.uuid4())
    bridge: Optional[CivBridge] = None

    try:
        while True:
            message = await ws.receive_text()

            if bridge is None:
                try:
                    login = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("[proxy] Invalid login JSON from client")
                    await ws.send_text('[{"pid":5,"message":"Invalid login packet","you_can_join":false,"conn_id":-1}]')
                    continue

                username = login.get("username", "")
                if not validate_username(username):
                    logger.warning("[proxy] Invalid username: '%s'", username)
                    await ws.send_text('[{"pid":5,"message":"Invalid username","you_can_join":false,"conn_id":-1}]')
                    continue

                server_port = int(login.get("port", 0))
                if server_port < 5000:
                    logger.warning("[proxy] Invalid server port: %d from user '%s'", server_port, username)
                    await ws.send_text('[{"pid":5,"message":"Invalid server port","you_can_join":false,"conn_id":-1}]')
                    continue

                logger.info("[proxy] Login: user='%s' server_port=%d conn_id=%s", username, server_port, conn_id[:8])
                key = f"{username}{server_port}{conn_id}"
                bridge = CivBridge(ws, username, server_port, key)
                _connections[key] = bridge

                ok = await bridge.connect_to_server(message)
                if not ok:
                    logger.error("[proxy] Bridge connect failed for user='%s' port=%d", username, server_port)
                    _connections.pop(key, None)
                    bridge = None
                continue

            await bridge.send_from_client(message)

    except WebSocketDisconnect as e:
        logger.info("[proxy] WebSocket disconnected for conn_id=%s: code=%s reason=%s",
                     conn_id[:8], getattr(e, 'code', '?'), getattr(e, 'reason', '?'))
    except Exception as e:
        logger.warning("[proxy] WebSocket error for conn_id=%s: %s: %s", conn_id[:8], type(e).__name__, e)
    finally:
        if bridge:
            await bridge.close()
