#!/usr/bin/env python3
"""
API integration test for XBWorld game server.

Tests:
1. REST API endpoints (meta/status, servers, launcher)
2. WebSocket proxy connection + freeciv protocol handshake
3. GameClient full lifecycle (connect, join, receive game state, close)

Usage:
    python test_api.py          # server must be running on localhost:8080
"""

import asyncio
import json
import sys
import time

import aiohttp
import websockets

BASE_URL = "http://localhost:8080"
WS_BASE = "ws://localhost:8080/civsocket"

passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = ""):
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1. REST API Tests
# ---------------------------------------------------------------------------
async def test_rest_apis(session: aiohttp.ClientSession):
    print("\n=== 1. REST API Tests ===")

    # GET /meta/status
    async with session.get(f"{BASE_URL}/meta/status") as resp:
        text = await resp.text()
        report("/meta/status", resp.status == 200 and text.startswith("ok;"), text.strip())

    # GET /servers
    async with session.get(f"{BASE_URL}/servers") as resp:
        data = await resp.json()
        report("/servers", resp.status == 200 and "total" in data, json.dumps(data))

    # POST /validate_user
    async with session.post(f"{BASE_URL}/validate_user") as resp:
        text = await resp.text()
        report("/validate_user", text.strip() == "user_does_not_exist", text.strip())

    # POST /login_user
    async with session.post(f"{BASE_URL}/login_user") as resp:
        text = await resp.text()
        report("/login_user", text.strip() == "OK", text.strip())

    # GET / (root)
    async with session.get(f"{BASE_URL}/") as resp:
        report("/ (root)", resp.status == 200)

    # GET /observer
    async with session.get(f"{BASE_URL}/observer") as resp:
        report("/observer", resp.status in (200, 404))


# ---------------------------------------------------------------------------
# 2. Game Launcher Test
# ---------------------------------------------------------------------------
async def test_launcher(session: aiohttp.ClientSession) -> int:
    print("\n=== 2. Game Launcher Test ===")

    # Create a singleplayer game
    async with session.post(f"{BASE_URL}/civclientlauncher?action=new") as resp:
        data = await resp.json()
        port = data.get("port", 0)
        result = data.get("result", "")
        report("POST /civclientlauncher (create game)", result == "success" and port > 0,
               f"port={port}, result={result}")

    # Verify server count increased
    async with session.get(f"{BASE_URL}/servers") as resp:
        data = await resp.json()
        report("/servers (after create)", data.get("total", 0) >= 1,
               f"total={data.get('total')}, ports={data.get('ports')}")

    # Verify meta/status
    async with session.get(f"{BASE_URL}/meta/status") as resp:
        text = await resp.text()
        parts = text.strip().split(";")
        total = int(parts[1]) if len(parts) > 1 else 0
        report("/meta/status (after create)", total >= 1, text.strip())

    return port


# ---------------------------------------------------------------------------
# 3. WebSocket Proxy Test (raw protocol)
# ---------------------------------------------------------------------------
async def test_websocket_raw(port: int):
    print("\n=== 3. WebSocket Proxy Test (raw protocol) ===")

    proxy_port = 1000 + port
    ws_url = f"{WS_BASE}/{proxy_port}"

    try:
        ws = await websockets.connect(ws_url, ping_interval=20, ping_timeout=60, max_size=None)
        report("WebSocket connect", True, ws_url)
    except Exception as e:
        report("WebSocket connect", False, str(e))
        return

    # Send login packet
    login = {
        "pid": 4,  # PACKET_SERVER_JOIN_REQ
        "username": "testuser",
        "capability": "+Freeciv.Web.Devel-3.3",
        "version_label": "-dev",
        "major_version": 3,
        "minor_version": 1,
        "patch_version": 90,
        "port": port,
        "password": "",
    }
    await ws.send(json.dumps(login))
    report("Send login packet", True)

    # Helper to drain packets from WebSocket for a duration
    packets_received = 0
    join_reply_ok = False
    received_pids = set()
    my_player_id = -1
    map_info_data = {}

    async def drain_packets(duration: float):
        nonlocal packets_received, join_reply_ok, my_player_id, map_info_data
        start = time.monotonic()
        while time.monotonic() - start < duration:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                data = json.loads(raw)
                pkts = data if isinstance(data, list) else [data]
                for pkt in pkts:
                    if not pkt:
                        continue
                    pid = pkt.get("pid", -1)
                    received_pids.add(pid)
                    packets_received += 1
                    if pid == 5:  # PID_SERVER_JOIN_REPLY
                        join_reply_ok = pkt.get("you_can_join", False)
                    elif pid == 115:  # PID_CONN_INFO
                        if pkt.get("used", False) and pkt.get("established", False):
                            my_player_id = pkt.get("player_num", -1)
                    elif pid == 17:  # PID_MAP_INFO
                        map_info_data = pkt
            except asyncio.TimeoutError:
                break

    # Phase 1: Receive pregame packets (login reply, player info, rulesets)
    try:
        await drain_packets(8)
    except Exception as e:
        report("Receive pregame packets", False, str(e))

    report("Receive pregame packets", packets_received > 0,
           f"{packets_received} packets, pids={sorted(received_pids)[:20]}")
    report("Server JOIN reply (you_can_join)", join_reply_ok)

    has_game_info = 16 in received_pids
    has_player_info = 51 in received_pids
    has_conn_info = 115 in received_pids
    report("Received GAME_INFO (pid=16)", has_game_info)
    report("Received PLAYER_INFO (pid=51)", has_player_info)
    report("Received CONN_INFO (pid=115)", has_conn_info)

    # Phase 2: Send player_ready to start the game, then receive MAP_INFO
    if join_reply_ok and my_player_id >= 0:
        ready_pkt = {
            "pid": 11,  # PACKET_PLAYER_READY
            "is_ready": True,
            "player_no": my_player_id,
        }
        await ws.send(json.dumps(ready_pkt))
        report("Send player_ready", True, f"player_no={my_player_id}")

        # Drain packets until we get MAP_INFO or timeout
        try:
            await drain_packets(15)
        except Exception as e:
            report("Receive game-start packets", False, str(e))

        has_map_info = 17 in received_pids
        report("Received MAP_INFO (pid=17)", has_map_info,
               f"xsize={map_info_data.get('xsize')}, ysize={map_info_data.get('ysize')}" if map_info_data else "not received")

        has_tiles = 15 in received_pids      # PID_TILE_INFO
        has_begin_turn = 128 in received_pids  # PID_BEGIN_TURN
        has_units = 63 in received_pids       # PID_UNIT_INFO
        report("Received TILE_INFO (pid=15)", has_tiles)
        report("Received BEGIN_TURN (pid=128)", has_begin_turn)
        report("Received UNIT_INFO (pid=63)", has_units)
        report("Total packets after game start", True,
               f"{packets_received} packets, all pids={sorted(received_pids)}")
    else:
        report("Send player_ready", False,
               f"skipped: join_reply_ok={join_reply_ok}, player_id={my_player_id}")

    await ws.close()
    report("WebSocket close", True)


# ---------------------------------------------------------------------------
# 4. GameClient Integration Test
# ---------------------------------------------------------------------------
async def test_game_client(session: aiohttp.ClientSession):
    print("\n=== 4. GameClient Integration Test ===")

    # Create a fresh game for this test (don't reuse the one from test 3)
    async with session.post(f"{BASE_URL}/civclientlauncher?action=new") as resp:
        data = await resp.json()
        port = data.get("port", 0)
        report("Create fresh game for GameClient", data.get("result") == "success" and port > 0,
               f"port={port}")
    if port <= 0:
        return

    await asyncio.sleep(2)

    # Import after ensuring config.py has MAX_MESSAGES_KEPT
    from game_client import GameClient

    client = GameClient(username="testclient")
    try:
        await client.join_game(port)
        report("GameClient.join_game()", client.state.connected, f"port={port}")
    except Exception as e:
        report("GameClient.join_game()", False, str(e))
        return

    # Wait for initial game state packets
    await asyncio.sleep(3)
    report("GameClient connected", client.state.connected)
    report("GameClient phase", client.state.phase in ("pregame", "playing"),
           f"phase={client.state.phase}")

    # Check received game state
    has_players = len(client.state.players) > 0
    report("Received player info", has_players, f"{len(client.state.players)} players")

    conn_id_ok = client.state.my_conn_id >= 0
    report("Got connection ID", conn_id_ok, f"conn_id={client.state.my_conn_id}")

    # Test sending chat command
    try:
        await client.send_chat("/list")
        await asyncio.sleep(1)
        report("Send chat command", True, "/list")
    except Exception as e:
        report("Send chat command", False, str(e))

    # Check if we got messages
    msg_count = len(client.state.messages)
    report("Received chat messages", msg_count > 0, f"{msg_count} messages")

    # Send player_ready to start the game
    try:
        await client.player_ready()
        report("Send player_ready", True)
    except Exception as e:
        report("Send player_ready", False, str(e))

    # Wait for game to start and map/unit data to arrive
    for _ in range(10):
        await asyncio.sleep(1)
        if client.state.phase == "playing" and client.state.map_info.get("xsize", 0) > 0:
            break
    report("Game phase after ready", client.state.phase == "playing",
           f"phase={client.state.phase}")

    # Verify map_info after game started
    mi = client.state.map_info
    has_map = mi.get("xsize", 0) > 0 and mi.get("ysize", 0) > 0
    report("GameClient map_info (MAP_INFO pid=17)", has_map,
           f"xsize={mi.get('xsize')}, ysize={mi.get('ysize')}, topology={mi.get('topology_id')}")

    # Verify units after game started
    my_units = client.state.my_units()
    report("GameClient my_units", len(my_units) > 0, f"{len(my_units)} units")

    # Verify tiles received
    tile_count = len(client.state.tiles)
    report("GameClient tiles", tile_count > 0, f"{tile_count} tiles")

    # Get stats
    stats = client.get_ws_stats() if hasattr(client, 'get_ws_stats') else {}
    if stats:
        report("WebSocket stats", True,
               f"msgs={stats.get('total_ws_msgs', 0)}, "
               f"pkts={stats.get('packets_processed', 0)}")

    # Clean close
    await client.close()
    report("GameClient.close()", not client.state.connected)


# ---------------------------------------------------------------------------
# 5. SSE Event Stream Test
# ---------------------------------------------------------------------------
async def test_sse(session: aiohttp.ClientSession):
    print("\n=== 5. SSE Event Stream Test ===")

    try:
        async with session.get(f"{BASE_URL}/game/events", timeout=aiohttp.ClientTimeout(total=5)) as resp:
            report("/game/events SSE connect", resp.status == 200,
                   f"content-type={resp.headers.get('content-type', '')}")
    except asyncio.TimeoutError:
        report("/game/events SSE connect", True, "connected (timeout expected for SSE)")
    except Exception as e:
        report("/game/events SSE connect", False, str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("  XBWorld Game Server — API Integration Tests")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        # 1. REST APIs
        await test_rest_apis(session)

        # 2. Create a game
        port = await test_launcher(session)
        if port <= 0:
            print("\nFATAL: Could not create game server. Aborting.")
            sys.exit(1)

        # Wait for server to be fully ready
        await asyncio.sleep(2)

        # 3. Raw WebSocket test
        await test_websocket_raw(port)

        # Wait before next connection
        await asyncio.sleep(1)

        # 4. GameClient test (creates its own fresh game)
        await test_game_client(session)

        # 5. SSE test
        await test_sse(session)

    # Summary
    global passed, failed
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
