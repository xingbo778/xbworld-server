"""
Integration tests for xbworld-server — requires the server running with freeciv.

Tests the full stack: REST API, game creation, WebSocket proxy handshake,
GameClient lifecycle, and SSE event stream.

Usage:
    # Start server first:  python server.py
    pytest tests/test_integration.py -v --timeout=60
"""

import asyncio
import json
import time

import aiohttp
import pytest
import websockets

BASE_URL = "http://localhost:8080"
WS_BASE = "ws://localhost:8080/civsocket"


async def _check_server():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/meta/status", timeout=aiohttp.ClientTimeout(total=2)) as r:
                return r.status == 200
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def require_server():
    if not asyncio.get_event_loop().run_until_complete(_check_server()):
        pytest.skip("Server not running on localhost:8080")


# ---------------------------------------------------------------------------
# 1. REST API
# ---------------------------------------------------------------------------
class TestRestAPI:
    async def test_meta_status(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/meta/status") as r:
                text = await r.text()
                assert r.status == 200
                assert text.startswith("ok;")
                parts = text.strip().split(";")
                assert len(parts) == 4

    async def test_servers(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/servers") as r:
                data = await r.json()
                assert r.status == 200
                assert "total" in data
                assert "ports" in data
                assert isinstance(data["ports"], list)

    async def test_validate_user_post(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE_URL}/validate_user") as r:
                assert (await r.text()).strip() == "user_does_not_exist"

    async def test_validate_user_get(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/validate_user") as r:
                assert (await r.text()).strip() == "user_does_not_exist"

    async def test_login_user(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE_URL}/login_user") as r:
                assert (await r.text()).strip() == "OK"

    async def test_root(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/") as r:
                assert r.status == 200

    async def test_observer(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/observer") as r:
                assert r.status in (200, 404)
                if r.status == 200:
                    text = await r.text()
                    assert "XBWorld" in text

    async def test_motd_js(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/motd.js") as r:
                assert r.status == 200
                assert "defined_motd" in await r.text()


# ---------------------------------------------------------------------------
# 2. Game launcher
# ---------------------------------------------------------------------------
class TestGameLauncher:
    async def test_create_game(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/servers") as r:
                before = (await r.json())["total"]

            async with s.post(f"{BASE_URL}/civclientlauncher?action=new") as r:
                data = await r.json()
                assert data["result"] == "success"
                assert data["port"] >= 6000
                assert r.headers.get("result") == "success"
                assert r.headers.get("port") == str(data["port"])

            async with s.get(f"{BASE_URL}/servers") as r:
                after = await r.json()
                assert after["total"] == before + 1
                assert data["port"] in after["ports"]

    async def test_connect_existing_port(self):
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{BASE_URL}/civclientlauncher?civserverport=9999") as r:
                data = await r.json()
                assert data["result"] == "success"
                assert data["port"] == 9999

    async def test_meta_status_after_create(self):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BASE_URL}/meta/status") as r:
                text = await r.text()
                parts = text.strip().split(";")
                total = int(parts[1])
                assert total >= 1


# ---------------------------------------------------------------------------
# 3. WebSocket proxy — raw protocol handshake
# ---------------------------------------------------------------------------
async def _create_game() -> int:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE_URL}/civclientlauncher?action=new") as r:
            data = await r.json()
            assert data["result"] == "success"
    await asyncio.sleep(2)
    return data["port"]


class TestWebSocketProxy:
    async def test_full_handshake(self):
        port = await _create_game()
        proxy_port = 1000 + port
        ws = await websockets.connect(
            f"{WS_BASE}/{proxy_port}",
            ping_interval=20, ping_timeout=60, max_size=None,
        )

        login = {
            "pid": 4,
            "username": "wstest",
            "capability": "+Freeciv.Web.Devel-3.3",
            "version_label": "-dev",
            "major_version": 3, "minor_version": 1, "patch_version": 90,
            "port": port,
            "password": "",
        }
        await ws.send(json.dumps(login))

        pids = set()
        join_ok = False
        player_id = -1

        async def drain(seconds):
            nonlocal join_ok, player_id
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    for pkt in json.loads(raw):
                        if not pkt:
                            continue
                        pid = pkt.get("pid", -1)
                        pids.add(pid)
                        if pid == 5:
                            join_ok = pkt.get("you_can_join", False)
                        if pid == 115 and pkt.get("established"):
                            player_id = pkt.get("player_num", -1)
                except asyncio.TimeoutError:
                    break

        await drain(8)
        assert join_ok, "Server should accept join"
        assert 16 in pids, "Should receive GAME_INFO"
        assert 51 in pids, "Should receive PLAYER_INFO"

        if player_id >= 0:
            await ws.send(json.dumps({"pid": 11, "is_ready": True, "player_no": player_id}))
            await drain(15)
            assert 17 in pids, "Should receive MAP_INFO after game start"
            assert 128 in pids, "Should receive BEGIN_TURN"
            assert 63 in pids, "Should receive UNIT_INFO"

        await ws.close()

    async def test_invalid_username_rejected(self):
        port = await _create_game()
        proxy_port = 1000 + port
        ws = await websockets.connect(f"{WS_BASE}/{proxy_port}", max_size=None)
        login = {"pid": 4, "username": "ab", "port": port, "password": ""}
        await ws.send(json.dumps(login))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        pkts = json.loads(raw)
        assert pkts[0].get("you_can_join") is False
        await ws.close()


# ---------------------------------------------------------------------------
# 4. GameClient integration
# ---------------------------------------------------------------------------
class TestGameClient:
    async def test_join_and_play(self):
        port = await _create_game()

        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from game_client import GameClient

        client = GameClient(username="integtest")
        await client.join_game(port)
        assert client.state.connected

        for _ in range(10):
            await asyncio.sleep(0.5)
            if client.state.my_conn_id >= 0:
                break
        assert client.state.my_conn_id >= 0

        await client.send_chat("/list")
        await asyncio.sleep(1)
        assert len(client.state.messages) > 0

        await client.player_ready()
        for _ in range(20):
            await asyncio.sleep(0.5)
            if client.state.phase == "playing":
                break
        assert client.state.phase == "playing"
        assert client.state.map_info.get("xsize", 0) > 0
        assert len(client.state.my_units()) > 0
        assert len(client.state.tiles) > 0
        assert client.state.rulesets_ready

        stats = client.get_ws_stats()
        assert stats["total_ws_msgs"] > 0
        assert stats["packets_processed"] > 0

        await client.close()
        assert not client.state.connected


# ---------------------------------------------------------------------------
# 5. SSE event stream
# ---------------------------------------------------------------------------
class TestSSE:
    async def test_sse_connects(self):
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(
                    f"{BASE_URL}/game/events",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    assert r.status == 200
                    assert "text/event-stream" in r.headers.get("content-type", "")
            except asyncio.TimeoutError:
                pass  # SSE is long-lived, timeout is expected
