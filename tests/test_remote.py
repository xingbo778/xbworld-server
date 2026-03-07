"""
Remote tests for xbworld-server deployed on Railway.

Tests the production deployment end-to-end: REST API, game lifecycle,
WebSocket proxy handshake, multi-client connections, error handling,
CORS headers, and SSE event stream.

Usage:
    pytest tests/test_remote.py -v --timeout=120

    # Override the remote URL:
    REMOTE_URL=https://my-server.example.com pytest tests/test_remote.py -v
"""

import asyncio
import json
import os
import time

import aiohttp
import pytest
import websockets

REMOTE_URL = os.getenv("REMOTE_URL", "https://xbworld-server-production.up.railway.app")
WS_URL = REMOTE_URL.replace("https://", "wss://").replace("http://", "ws://")


async def _remote_is_reachable() -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/meta/status", timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status == 200
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_remote():
    if not asyncio.get_event_loop().run_until_complete(_remote_is_reachable()):
        pytest.skip(f"Remote server not reachable at {REMOTE_URL}")


# ---------------------------------------------------------------------------
# Helper: create a game and return port
# ---------------------------------------------------------------------------
async def _create_game() -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{REMOTE_URL}/civclientlauncher?action=new") as r:
            data = await r.json()
            assert data["result"] == "success"
            return data["port"]


# ---------------------------------------------------------------------------
# 1. REST API — Health & Status
# ---------------------------------------------------------------------------
class TestHealthAndStatus:
    async def test_meta_status_format(self):
        """Meta status returns ok;total;single;multi format."""
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/meta/status") as r:
                assert r.status == 200
                text = await r.text()
                parts = text.strip().split(";")
                assert len(parts) == 4
                assert parts[0] == "ok"
                for p in parts[1:]:
                    int(p)  # all should be integers

    async def test_servers_json_schema(self):
        """Servers endpoint returns correct JSON schema."""
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/servers") as r:
                assert r.status == 200
                data = await r.json()
                assert isinstance(data["total"], int)
                assert isinstance(data["single"], int)
                assert isinstance(data["multi"], int)
                assert isinstance(data["ports"], list)
                assert data["total"] == len(data["ports"])

    async def test_root_returns_html(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/") as r:
                assert r.status == 200
                assert "text/html" in r.headers.get("content-type", "")

    async def test_observer_returns_html(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/observer") as r:
                assert r.status == 200
                text = await r.text()
                assert "XBWorld" in text
                assert "EventSource" in text  # SSE client code


# ---------------------------------------------------------------------------
# 2. Legacy client compatibility
# ---------------------------------------------------------------------------
class TestLegacyCompat:
    async def test_validate_user_post(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/validate_user") as r:
                assert (await r.text()).strip() == "user_does_not_exist"

    async def test_validate_user_get(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/validate_user") as r:
                assert (await r.text()).strip() == "user_does_not_exist"

    async def test_login_user_post(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/login_user") as r:
                assert (await r.text()).strip() == "OK"

    async def test_login_user_get(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/login_user") as r:
                assert (await r.text()).strip() == "OK"

    async def test_motd_js(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/motd.js") as r:
                assert r.status == 200
                text = await r.text()
                assert "defined_motd" in text
                assert "application/javascript" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# 3. CORS headers
# ---------------------------------------------------------------------------
class TestCORS:
    async def test_cors_headers_on_get(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/servers", headers={"Origin": "https://example.com"}) as r:
                assert r.status == 200
                assert r.headers.get("access-control-allow-origin") == "*"

    async def test_cors_preflight(self):
        async with aiohttp.ClientSession() as s:
            async with s.options(
                f"{REMOTE_URL}/civclientlauncher",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                },
            ) as r:
                assert r.status == 200
                # With allow_credentials=True, CORS echoes the origin instead of "*"
                origin = r.headers.get("access-control-allow-origin", "")
                assert origin in ("*", "https://example.com")

    async def test_launcher_exposes_custom_headers(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/civclientlauncher?civserverport=9999") as r:
                expose = r.headers.get("access-control-expose-headers", "")
                assert "port" in expose
                assert "result" in expose


# ---------------------------------------------------------------------------
# 4. Game launcher
# ---------------------------------------------------------------------------
class TestGameLauncher:
    async def test_create_singleplayer_game(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/civclientlauncher?action=new") as r:
                data = await r.json()
                assert data["result"] == "success"
                assert data["port"] >= 6000
                assert r.headers.get("result") == "success"
                assert r.headers.get("port") == str(data["port"])

    async def test_create_multiplayer_game(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/civclientlauncher?action=multi") as r:
                data = await r.json()
                assert data["result"] == "success"
                assert data["port"] >= 6000

    async def test_connect_existing_port(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/civclientlauncher?civserverport=7777") as r:
                data = await r.json()
                assert data["result"] == "success"
                assert data["port"] == 7777

    async def test_server_count_increases(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/servers") as r:
                before = (await r.json())["total"]

            async with s.post(f"{REMOTE_URL}/civclientlauncher?action=new") as r:
                data = await r.json()
                assert data["result"] == "success"

            async with s.get(f"{REMOTE_URL}/servers") as r:
                after = await r.json()
                assert after["total"] >= before + 1
                assert data["port"] in after["ports"]


# ---------------------------------------------------------------------------
# 5. WebSocket proxy — protocol handshake
# ---------------------------------------------------------------------------
class TestWebSocketProxy:
    async def test_login_and_pregame_packets(self):
        """Full login handshake: connect, send login, receive pregame packets."""
        port = await _create_game()
        await asyncio.sleep(1)
        proxy_port = 1000 + port

        ws = await websockets.connect(
            f"{WS_URL}/civsocket/{proxy_port}",
            ping_interval=20, ping_timeout=60, max_size=None,
        )
        login = {
            "pid": 4, "username": "remtest",
            "capability": "+Freeciv.Web.Devel-3.3", "version_label": "-dev",
            "major_version": 3, "minor_version": 1, "patch_version": 90,
            "port": port, "password": "",
        }
        await ws.send(json.dumps(login))

        pids = set()
        join_ok = False
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                for pkt in json.loads(raw):
                    if pkt:
                        pids.add(pkt.get("pid", -1))
                        if pkt.get("pid") == 5:
                            join_ok = pkt.get("you_can_join", False)
            except asyncio.TimeoutError:
                break

        await ws.close()
        assert join_ok, "Server should accept login"
        assert 5 in pids, "Should get JOIN_REPLY"
        assert 16 in pids, "Should get GAME_INFO"
        assert 51 in pids, "Should get PLAYER_INFO"
        assert 115 in pids, "Should get CONN_INFO"

    async def test_ready_and_game_start(self):
        """Send player_ready and verify game starts with map/unit data."""
        port = await _create_game()
        await asyncio.sleep(1)
        proxy_port = 1000 + port

        ws = await websockets.connect(
            f"{WS_URL}/civsocket/{proxy_port}",
            ping_interval=20, ping_timeout=60, max_size=None,
        )
        login = {
            "pid": 4, "username": "readytest",
            "capability": "+Freeciv.Web.Devel-3.3", "version_label": "-dev",
            "major_version": 3, "minor_version": 1, "patch_version": 90,
            "port": port, "password": "",
        }
        await ws.send(json.dumps(login))

        pids = set()
        player_id = -1

        async def drain(seconds):
            nonlocal player_id
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    for pkt in json.loads(raw):
                        if not pkt:
                            continue
                        pids.add(pkt.get("pid", -1))
                        if pkt.get("pid") == 115 and pkt.get("established"):
                            player_id = pkt.get("player_num", -1)
                except asyncio.TimeoutError:
                    break

        await drain(8)
        assert player_id >= 0, "Should get player assignment"

        # Send ready
        await ws.send(json.dumps({"pid": 11, "is_ready": True, "player_no": player_id}))
        await drain(15)

        await ws.close()
        assert 17 in pids, "Should get MAP_INFO after game start"
        assert 15 in pids, "Should get TILE_INFO"
        assert 63 in pids, "Should get UNIT_INFO"
        assert 128 in pids, "Should get BEGIN_TURN"

    async def test_invalid_username_rejected(self):
        """Proxy rejects usernames shorter than 3 characters."""
        port = await _create_game()
        await asyncio.sleep(1)
        proxy_port = 1000 + port
        ws = await websockets.connect(f"{WS_URL}/civsocket/{proxy_port}", max_size=None)
        await ws.send(json.dumps({"pid": 4, "username": "ab", "port": port, "password": ""}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        pkts = json.loads(raw)
        assert pkts[0].get("you_can_join") is False
        await ws.close()

    async def test_invalid_json_handled(self):
        """Proxy handles invalid JSON gracefully."""
        port = await _create_game()
        await asyncio.sleep(1)
        proxy_port = 1000 + port
        ws = await websockets.connect(f"{WS_URL}/civsocket/{proxy_port}", max_size=None)
        await ws.send("not valid json{{{")
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        pkts = json.loads(raw)
        assert pkts[0].get("you_can_join") is False
        await ws.close()

    async def test_bad_port_rejected(self):
        """Proxy rejects server ports below 5000."""
        port = await _create_game()
        await asyncio.sleep(1)
        proxy_port = 1000 + port
        ws = await websockets.connect(f"{WS_URL}/civsocket/{proxy_port}", max_size=None)
        await ws.send(json.dumps({"pid": 4, "username": "badport", "port": 100, "password": ""}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        pkts = json.loads(raw)
        assert pkts[0].get("you_can_join") is False
        await ws.close()


# ---------------------------------------------------------------------------
# 6. Multi-client connection
# ---------------------------------------------------------------------------
class TestMultiClient:
    async def test_two_clients_same_game(self):
        """Two clients can connect to the same game server."""
        port = await _create_game()
        await asyncio.sleep(1)
        proxy_port = 1000 + port

        async def connect_and_login(username):
            ws = await websockets.connect(
                f"{WS_URL}/civsocket/{proxy_port}",
                ping_interval=20, ping_timeout=60, max_size=None,
            )
            login = {
                "pid": 4, "username": username,
                "capability": "+Freeciv.Web.Devel-3.3", "version_label": "-dev",
                "major_version": 3, "minor_version": 1, "patch_version": 90,
                "port": port, "password": "",
            }
            await ws.send(json.dumps(login))

            join_ok = False
            end = time.monotonic() + 8
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    for pkt in json.loads(raw):
                        if pkt and pkt.get("pid") == 5:
                            join_ok = pkt.get("you_can_join", False)
                except asyncio.TimeoutError:
                    break
            return ws, join_ok

        ws1, ok1 = await connect_and_login("clientone")
        ws2, ok2 = await connect_and_login("clienttwo")

        assert ok1, "First client should join successfully"
        assert ok2, "Second client should join successfully"

        await ws1.close()
        await ws2.close()


# ---------------------------------------------------------------------------
# 7. SSE event stream
# ---------------------------------------------------------------------------
class TestSSE:
    async def test_sse_connects_and_returns_stream(self):
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(
                    f"{REMOTE_URL}/game/events",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    assert r.status == 200
                    ct = r.headers.get("content-type", "")
                    assert "text/event-stream" in ct
                    assert r.headers.get("cache-control") == "no-cache"
            except asyncio.TimeoutError:
                pass  # Expected for SSE


# ---------------------------------------------------------------------------
# 8. Error handling & edge cases
# ---------------------------------------------------------------------------
class TestErrorHandling:
    async def test_404_on_unknown_route(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{REMOTE_URL}/nonexistent/path") as r:
                assert r.status in (404, 405)

    async def test_launcher_default_action_is_singleplayer(self):
        """No action param defaults to singleplayer."""
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{REMOTE_URL}/civclientlauncher") as r:
                data = await r.json()
                assert data["result"] == "success"
                assert data["port"] >= 6000
