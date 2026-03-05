#!/usr/bin/env python3
"""Standalone WebSocket-to-TCP proxy for freeciv-server.

Uses aiohttp instead of websockets library to avoid strict HTTP upgrade
validation issues with Nginx reverse proxy.

Usage: python standalone_proxy.py <listen_port>
       python standalone_proxy.py 7001          # proxies browser WS to civserver

The actual civserver port is read from the login packet sent by the browser.
"""

import asyncio
import json
import random
import struct
import sys
import logging

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [proxy] %(levelname)s: %(message)s")
logger = logging.getLogger("proxy")

AGENT_NAMES = {"alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"}
_observer_counter = 0


async def handle_ws(request):
    global _observer_counter

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("WS connection from %s path=%s", request.remote, request.path)

    first_msg = await ws.receive_str()
    try:
        login = json.loads(first_msg)
    except (json.JSONDecodeError, TypeError):
        await ws.close(code=1008, message=b"Invalid login")
        return ws

    server_port = int(login.get("port", 0))
    username = login.get("username", "?")

    if server_port < 5000:
        await ws.send_str(json.dumps([{"pid": 5, "message": "Bad port", "you_can_join": False, "conn_id": -1}]))
        await ws.close()
        return ws

    if username.lower() in AGENT_NAMES or len(username) < 3:
        _observer_counter += 1
        username = f"obs{_observer_counter}_{random.randint(10, 99)}"
        login["username"] = username
        first_msg = json.dumps(login)
        logger.info("Renamed to '%s' to avoid agent name conflict", username)

    logger.info("Bridging %s -> civserver 127.0.0.1:%d", username, server_port)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", server_port), timeout=5.0
        )
    except Exception as e:
        logger.error("TCP connect to %d failed: %s", server_port, e)
        await ws.send_str(json.dumps([{"pid": 25, "message": f"Proxy connect failed: {e}"}]))
        await ws.close()
        return ws

    encoded = first_msg.encode("utf-8")
    writer.write(struct.pack(">H", len(encoded) + 3) + encoded + b"\0")
    await writer.drain()

    async def tcp_to_ws():
        try:
            while not ws.closed:
                header = await reader.readexactly(2)
                (size,) = struct.unpack(">H", header)
                body = await reader.readexactly(size - 2)
                if body and body[-1] == 0:
                    body = body[:-1]
                text = body.decode("utf-8", errors="ignore")
                await ws.send_str(f"[{text}]")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:
            if not ws.closed:
                logger.warning("tcp_to_ws error: %s", e)

    async def ws_to_tcp():
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    data = msg.data.encode("utf-8")
                    writer.write(struct.pack(">H", len(data) + 3) + data + b"\0")
                    await writer.drain()
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        except Exception as e:
            logger.warning("ws_to_tcp error: %s", e)

    tasks = [asyncio.create_task(tcp_to_ws()), asyncio.create_task(ws_to_tcp())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    writer.close()
    if not ws.closed:
        await ws.close()
    logger.info("Bridge closed for %s", username)
    return ws


async def main(listen_port):
    app = web.Application()
    app.router.add_get("/{path:.*}", handle_ws)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", listen_port)
    await site.start()
    logger.info("WebSocket proxy listening on port %d", listen_port)
    await asyncio.Future()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7001
    asyncio.run(main(port))
