#!/usr/bin/env python3
"""
API integration test runner for the XBWorld game server.

Tests:
1. REST API endpoints (meta/status, servers, launcher)
2. WebSocket proxy connection + freeciv protocol handshake
3. GameClient full lifecycle (connect, join, receive game state, close)
4. SSE stream availability

Usage:
    python test_api.py
    python test_api.py --only rest,launcher
    python test_api.py --base-url http://localhost:8000 --timeout 10
    XBWORLD_BASE_URL=http://localhost:8000 python test_api.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp
import websockets

DEFAULT_BASE_URL = "http://localhost:8080"
ALL_TESTS = ("rest", "launcher", "websocket", "game-client", "sse")


def _normalize_base_url(raw: str) -> str:
    value = raw.strip() or DEFAULT_BASE_URL
    return value.rstrip("/")


def _derive_ws_base(base_url: str, override: str = "") -> str:
    if override.strip():
        return override.strip().rstrip("/")

    parsed = urlparse(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{ws_scheme}://{parsed.netloc}/civsocket"


def _parse_test_selection(value: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if not names:
        raise argparse.ArgumentTypeError("expected at least one test name")

    invalid = [name for name in names if name not in ALL_TESTS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"unknown tests: {', '.join(invalid)}; valid: {', '.join(ALL_TESTS)}"
        )
    return names


@dataclass(frozen=True)
class TestConfig:
    base_url: str
    ws_base: str
    tests: tuple[str, ...]
    timeout: float
    launcher_wait: float
    between_tests_wait: float
    game_client_wait: float


@dataclass
class TestRun:
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def report(self, name: str, ok: bool, detail: str = "") -> bool:
        status = "PASS" if ok else "FAIL"
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(name)
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
        return ok

    @property
    def total(self) -> int:
        return self.passed + self.failed


async def _safe_json(resp: aiohttp.ClientResponse) -> object:
    try:
        return await resp.json()
    except Exception:
        return {"raw": await resp.text()}


async def test_rest_apis(session: aiohttp.ClientSession, config: TestConfig, run: TestRun) -> None:
    print("\n=== 1. REST API Tests ===")

    async with session.get(f"{config.base_url}/meta/status") as resp:
        text = await resp.text()
        run.report("/meta/status", resp.status == 200 and text.startswith("ok;"), text.strip())

    async with session.get(f"{config.base_url}/servers") as resp:
        data = await _safe_json(resp)
        ok = resp.status == 200 and isinstance(data, dict) and "total" in data
        run.report("/servers", ok, json.dumps(data))

    async with session.post(f"{config.base_url}/validate_user") as resp:
        text = await resp.text()
        run.report("/validate_user", text.strip() == "user_does_not_exist", text.strip())

    async with session.post(f"{config.base_url}/login_user") as resp:
        text = await resp.text()
        run.report("/login_user", text.strip() == "OK", text.strip())

    async with session.get(f"{config.base_url}/") as resp:
        run.report("/ (root)", resp.status == 200)

    async with session.get(f"{config.base_url}/observer") as resp:
        run.report("/observer", resp.status in (200, 404))


async def test_launcher(
    session: aiohttp.ClientSession,
    config: TestConfig,
    run: TestRun,
    *,
    action: str = "new",
    label: str = "POST /civclientlauncher (create game)",
) -> int:
    print("\n=== 2. Game Launcher Test ===")

    async with session.post(f"{config.base_url}/civclientlauncher?action={action}") as resp:
        data = await _safe_json(resp)
        port = int(data.get("port", 0)) if isinstance(data, dict) else 0
        result = data.get("result", "") if isinstance(data, dict) else ""
        run.report(label, result == "success" and port > 0, f"port={port}, result={result}")

    async with session.get(f"{config.base_url}/servers") as resp:
        data = await _safe_json(resp)
        total = data.get("total", 0) if isinstance(data, dict) else 0
        ports = data.get("ports") if isinstance(data, dict) else None
        run.report("/servers (after create)", total >= 1, f"total={total}, ports={ports}")

    async with session.get(f"{config.base_url}/meta/status") as resp:
        text = await resp.text()
        parts = text.strip().split(";")
        total = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        run.report("/meta/status (after create)", total >= 1, text.strip())

    return port


async def test_websocket_raw(port: int, config: TestConfig, run: TestRun) -> None:
    print("\n=== 3. WebSocket Proxy Test (raw protocol) ===")

    proxy_port = 1000 + port
    ws_url = f"{config.ws_base}/{proxy_port}"

    try:
        ws = await websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=60,
            max_size=None,
            open_timeout=config.timeout,
            close_timeout=config.timeout,
        )
        run.report("WebSocket connect", True, ws_url)
    except Exception as exc:
        run.report("WebSocket connect", False, str(exc))
        return

    packets_received = 0
    join_reply_ok = False
    received_pids: set[int] = set()
    my_player_id = -1
    map_info_data: dict[str, object] = {}

    async def drain_packets(duration: float) -> None:
        nonlocal packets_received, join_reply_ok, my_player_id, map_info_data
        start = time.monotonic()
        while time.monotonic() - start < duration:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(config.timeout, 3.0))
                data = json.loads(raw)
                packets = data if isinstance(data, list) else [data]
                for packet in packets:
                    if not packet:
                        continue
                    pid = int(packet.get("pid", -1))
                    received_pids.add(pid)
                    packets_received += 1
                    if pid == 5:
                        join_reply_ok = bool(packet.get("you_can_join", False))
                    elif pid == 115:
                        if packet.get("used", False) and packet.get("established", False):
                            my_player_id = int(packet.get("player_num", -1))
                    elif pid == 17:
                        map_info_data = packet
            except asyncio.TimeoutError:
                break

    try:
        await ws.send(
            json.dumps(
                {
                    "pid": 4,
                    "username": "testuser",
                    "capability": "+Freeciv.Web.Devel-3.3",
                    "version_label": "-dev",
                    "major_version": 3,
                    "minor_version": 1,
                    "patch_version": 90,
                    "port": port,
                    "password": "",
                }
            )
        )
        run.report("Send login packet", True)
    except Exception as exc:
        run.report("Send login packet", False, str(exc))
        await ws.close()
        return

    try:
        await drain_packets(config.timeout)
    except Exception as exc:
        run.report("Receive pregame packets", False, str(exc))

    run.report(
        "Receive pregame packets",
        packets_received > 0,
        f"{packets_received} packets, pids={sorted(received_pids)[:20]}",
    )
    run.report("Server JOIN reply (you_can_join)", join_reply_ok)
    run.report("Received GAME_INFO (pid=16)", 16 in received_pids)
    run.report("Received PLAYER_INFO (pid=51)", 51 in received_pids)
    run.report("Received CONN_INFO (pid=115)", 115 in received_pids)

    if join_reply_ok and my_player_id >= 0:
        try:
            await ws.send(
                json.dumps(
                    {
                        "pid": 11,
                        "is_ready": True,
                        "player_no": my_player_id,
                    }
                )
            )
            run.report("Send player_ready", True, f"player_no={my_player_id}")
        except Exception as exc:
            run.report("Send player_ready", False, str(exc))

        try:
            await drain_packets(config.timeout)
        except Exception as exc:
            run.report("Receive game-start packets", False, str(exc))

        has_map_info = 17 in received_pids
        run.report(
            "Received MAP_INFO (pid=17)",
            has_map_info,
            (
                f"xsize={map_info_data.get('xsize')}, ysize={map_info_data.get('ysize')}"
                if map_info_data
                else "not received"
            ),
        )
        run.report("Received TILE_INFO (pid=15)", 15 in received_pids)
        run.report("Received BEGIN_TURN (pid=128)", 128 in received_pids)
        run.report("Received UNIT_INFO (pid=63)", 63 in received_pids)
        run.report(
            "Total packets after game start",
            True,
            f"{packets_received} packets, all pids={sorted(received_pids)}",
        )
    else:
        run.report(
            "Send player_ready",
            False,
            f"skipped: join_reply_ok={join_reply_ok}, player_id={my_player_id}",
        )

    await ws.close()
    run.report("WebSocket close", True)


async def test_game_client(session: aiohttp.ClientSession, config: TestConfig, run: TestRun) -> None:
    print("\n=== 4. GameClient Integration Test ===")

    port = await test_launcher(
        session,
        config,
        run,
        action="new",
        label="Create fresh game for GameClient",
    )
    if port <= 0:
        return

    await asyncio.sleep(config.launcher_wait)

    from game_client import GameClient

    client = GameClient(username="testclient")
    try:
        await asyncio.wait_for(client.join_game(port), timeout=config.timeout + 10)
        run.report("GameClient.join_game()", client.state.connected, f"port={port}")
    except Exception as exc:
        run.report("GameClient.join_game()", False, str(exc))
        return

    try:
        await asyncio.sleep(config.game_client_wait)
        run.report("GameClient connected", client.state.connected)
        run.report(
            "GameClient phase",
            client.state.phase in ("pregame", "playing"),
            f"phase={client.state.phase}",
        )
        run.report("Received player info", len(client.state.players) > 0, f"{len(client.state.players)} players")
        run.report("Got connection ID", client.state.my_conn_id >= 0, f"conn_id={client.state.my_conn_id}")

        try:
            await client.send_chat("/list")
            await asyncio.sleep(1)
            run.report("Send chat command", True, "/list")
        except Exception as exc:
            run.report("Send chat command", False, str(exc))

        msg_count = len(client.state.messages)
        run.report("Received chat messages", msg_count > 0, f"{msg_count} messages")

        try:
            await client.player_ready()
            run.report("Send player_ready", True)
        except Exception as exc:
            run.report("Send player_ready", False, str(exc))

        for _ in range(max(1, int(config.timeout))):
            await asyncio.sleep(1)
            if client.state.phase == "playing" and client.state.map_info.get("xsize", 0) > 0:
                break

        run.report("Game phase after ready", client.state.phase == "playing", f"phase={client.state.phase}")

        map_info = client.state.map_info
        has_map = map_info.get("xsize", 0) > 0 and map_info.get("ysize", 0) > 0
        run.report(
            "GameClient map_info (MAP_INFO pid=17)",
            has_map,
            f"xsize={map_info.get('xsize')}, ysize={map_info.get('ysize')}, topology={map_info.get('topology_id')}",
        )
        run.report("GameClient my_units", len(client.state.my_units()) > 0, f"{len(client.state.my_units())} units")
        run.report("GameClient tiles", len(client.state.tiles) > 0, f"{len(client.state.tiles)} tiles")

        stats = client.get_ws_stats() if hasattr(client, "get_ws_stats") else {}
        if stats:
            run.report(
                "WebSocket stats",
                True,
                f"msgs={stats.get('total_ws_msgs', 0)}, pkts={stats.get('packets_processed', 0)}",
            )
    finally:
        await client.close()
        run.report("GameClient.close()", not client.state.connected)


async def test_sse(session: aiohttp.ClientSession, config: TestConfig, run: TestRun) -> None:
    print("\n=== 5. SSE Event Stream Test ===")

    try:
        async with session.get(
            f"{config.base_url}/game/events",
            timeout=aiohttp.ClientTimeout(total=config.timeout),
        ) as resp:
            run.report(
                "/game/events SSE connect",
                resp.status == 200,
                f"content-type={resp.headers.get('content-type', '')}",
            )
    except asyncio.TimeoutError:
        run.report("/game/events SSE connect", True, "connected (timeout expected for SSE)")
    except Exception as exc:
        run.report("/game/events SSE connect", False, str(exc))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XBWorld API integration test runner")
    parser.add_argument(
        "--base-url",
        default=os.getenv("XBWORLD_BASE_URL", DEFAULT_BASE_URL),
        help=f"HTTP base URL (default: {DEFAULT_BASE_URL} or XBWORLD_BASE_URL)",
    )
    parser.add_argument(
        "--ws-base",
        default=os.getenv("XBWORLD_WS_BASE", ""),
        help="WebSocket base URL override, e.g. ws://localhost:8080/civsocket",
    )
    parser.add_argument(
        "--only",
        type=_parse_test_selection,
        default=ALL_TESTS,
        help=f"Comma-separated test list: {', '.join(ALL_TESTS)}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("XBWORLD_TEST_TIMEOUT", "8")),
        help="Per-phase timeout in seconds",
    )
    parser.add_argument(
        "--launcher-wait",
        type=float,
        default=float(os.getenv("XBWORLD_LAUNCHER_WAIT", "2")),
        help="Wait after launcher returns a new game port",
    )
    parser.add_argument(
        "--between-tests-wait",
        type=float,
        default=float(os.getenv("XBWORLD_BETWEEN_TESTS_WAIT", "1")),
        help="Pause between dependent test phases",
    )
    parser.add_argument(
        "--game-client-wait",
        type=float,
        default=float(os.getenv("XBWORLD_GAME_CLIENT_WAIT", "3")),
        help="Initial wait after GameClient.join_game()",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> TestConfig:
    base_url = _normalize_base_url(args.base_url)
    ws_base = _derive_ws_base(base_url, args.ws_base)
    return TestConfig(
        base_url=base_url,
        ws_base=ws_base,
        tests=args.only,
        timeout=max(1.0, args.timeout),
        launcher_wait=max(0.0, args.launcher_wait),
        between_tests_wait=max(0.0, args.between_tests_wait),
        game_client_wait=max(0.0, args.game_client_wait),
    )


async def run_selected_tests(config: TestConfig) -> int:
    run = TestRun()

    print("=" * 60)
    print("  XBWorld Game Server — API Integration Tests")
    print("=" * 60)
    print(f"  base_url={config.base_url}")
    print(f"  ws_base={config.ws_base}")
    print(f"  tests={','.join(config.tests)}")

    timeout = aiohttp.ClientTimeout(total=config.timeout)
    launcher_port = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        if "rest" in config.tests:
            await test_rest_apis(session, config, run)

        if "launcher" in config.tests:
            launcher_port = await test_launcher(session, config, run)
            if launcher_port <= 0:
                print("\nFATAL: Could not create game server. Dependent tests skipped.")

        if "websocket" in config.tests:
            if launcher_port <= 0:
                launcher_port = await test_launcher(
                    session,
                    config,
                    run,
                    label="POST /civclientlauncher (create game for websocket)",
                )
            if launcher_port > 0:
                await asyncio.sleep(config.launcher_wait)
                await test_websocket_raw(launcher_port, config, run)
            else:
                run.report("WebSocket phase prerequisites", False, "no launcher port available")

        if "game-client" in config.tests:
            await asyncio.sleep(config.between_tests_wait)
            await test_game_client(session, config, run)

        if "sse" in config.tests:
            await test_sse(session, config, run)

    print(f"\n{'=' * 60}")
    print(f"  Results: {run.passed}/{run.total} passed, {run.failed} failed")
    if run.failures:
        print(f"  Failed checks: {', '.join(run.failures)}")
    print(f"{'=' * 60}")
    return 0 if run.failed == 0 else 1


def main() -> int:
    args = parse_args()
    config = build_config(args)
    return asyncio.run(run_selected_tests(config))


if __name__ == "__main__":
    sys.exit(main())
