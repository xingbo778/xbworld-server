"""
Lightweight headless XBWorld client.

Connects via WebSocket to the proxy layer, sends commands as chat messages
or raw packets, and maintains minimal game state from server pushes.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

import aiohttp
import websockets
from websockets.connection import State as WsState

from config import (
    LAUNCHER_URL, WS_BASE_URL,
    FREECIV_VERSION, MAJOR_VERSION, MINOR_VERSION, PATCH_VERSION,
    MAX_MESSAGES_KEPT,
)

logger = logging.getLogger("xbworld-agent")

# ---------------------------------------------------------------------------
# Packet IDs — client-to-server (from generated packets.js)
# ---------------------------------------------------------------------------
PACKET_SERVER_JOIN_REQ = 4
PACKET_CHAT_MSG_REQ = 26
PACKET_CONN_PONG = 89
PACKET_CLIENT_INFO = 119
PACKET_PLAYER_PHASE_DONE = 52
PACKET_PLAYER_RATES = 53
PACKET_PLAYER_RESEARCH = 55
PACKET_PLAYER_TECH_GOAL = 56
PACKET_PLAYER_READY = 11
PACKET_NATION_SELECT_REQ = 10
PACKET_CITY_CHANGE = 35
PACKET_CITY_BUY = 34
PACKET_CITY_SELL = 33
PACKET_UNIT_ORDERS = 73
PACKET_UNIT_DO_ACTION = 84
PACKET_UNIT_SSCS_SET = 71
PACKET_UNIT_CHANGE_ACTIVITY = 222
PACKET_UNIT_GET_ACTIONS = 87

# ---------------------------------------------------------------------------
# Packet IDs — server-to-client (from generated packhand_gen.js)
# ---------------------------------------------------------------------------
PID_SERVER_JOIN_REPLY = 5
PID_CHAT_MSG = 25
PID_CONNECT_MSG = 27
PID_EARLY_CHAT_MSG = 28
PID_GAME_INFO = 16
PID_MAP_INFO = 17
PID_TILE_INFO = 15
PID_CITY_INFO = 31
PID_CITY_SHORT_INFO = 32
PID_CITY_REMOVE = 30
PID_UNIT_INFO = 63
PID_UNIT_SHORT_INFO = 64
PID_UNIT_REMOVE = 62
PID_PLAYER_INFO = 51
PID_PLAYER_REMOVE = 50
PID_RESEARCH_INFO = 60
PID_BEGIN_TURN = 128
PID_END_TURN = 129
PID_NEW_YEAR = 127
PID_START_PHASE = 126
PID_END_PHASE = 125
PID_CONN_PING = 88
PID_CONN_INFO = 115
PID_CALENDAR_INFO = 255
PID_TIMEOUT_INFO = 244
PID_WEB_CITY_INFO_ADDITION = 256
PID_WEB_PLAYER_INFO_ADDITION = 259
PID_RULESET_UNIT = 140
PID_RULESET_TECH = 144
PID_RULESET_BUILDING = 150
PID_RULESET_TERRAIN = 151
PID_RULESET_GOVERNMENT = 145
PID_RULESETS_READY = 225
PID_UNIT_ACTIONS = 90
PID_PAGE_MSG = 110
PID_PROCESSING_STARTED = 0
PID_PROCESSING_FINISHED = 1

GUI_WEB = 7

# Order types for PACKET_UNIT_ORDERS
ORDER_MOVE = 0
ORDER_ACTIVITY = 1
ORDER_FULL_MP = 2
ORDER_ACTION_MOVE = 3
ORDER_PERFORM_ACTION = 4

# Activity types
ACTIVITY_IDLE = 0
ACTIVITY_FORTIFIED = 4
ACTIVITY_SENTRY = 5
ACTIVITY_EXPLORE = 8
ACTIVITY_FORTIFYING = 10

# Action types for PACKET_UNIT_DO_ACTION
ACTION_FOUND_CITY = 27
ACTION_JOIN_CITY = 28
ACTION_ATTACK = 45
ACTION_DISBAND_UNIT = 51
ACTION_FORTIFY = 125
ACTION_COUNT = 139  # "no action" sentinel

# Server-side agent
SSA_NONE = 0
SSA_AUTOEXPLORE = 2

EXTRA_NONE = -1


# ---------------------------------------------------------------------------
# Game State
# ---------------------------------------------------------------------------
@dataclass
class GameState:
    """Minimal game state maintained from server packets."""
    connected: bool = False
    phase: str = "connecting"  # connecting / pregame / playing / game_over
    turn: int = 0
    year: str = ""
    my_player_id: int = -1
    my_conn_id: int = -1

    players: dict[int, dict] = field(default_factory=dict)
    units: dict[int, dict] = field(default_factory=dict)
    cities: dict[int, dict] = field(default_factory=dict)
    tiles: dict[int, dict] = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    map_info: dict = field(default_factory=dict)

    unit_types: dict[int, dict] = field(default_factory=dict)
    techs: dict[int, dict] = field(default_factory=dict)
    buildings: dict[int, dict] = field(default_factory=dict)
    governments: dict[int, dict] = field(default_factory=dict)
    terrains: dict[int, dict] = field(default_factory=dict)

    messages: list[dict] = field(default_factory=list)
    rulesets_ready: bool = False

    def add_message(self, msg: dict):
        self.messages.append(msg)
        if len(self.messages) > MAX_MESSAGES_KEPT:
            self.messages = self.messages[-MAX_MESSAGES_KEPT:]

    def my_player(self) -> Optional[dict]:
        return self.players.get(self.my_player_id)

    def my_units(self) -> dict[int, dict]:
        return {uid: u for uid, u in self.units.items()
                if u.get("owner") == self.my_player_id}

    def my_cities(self) -> dict[int, dict]:
        return {cid: c for cid, c in self.cities.items()
                if c.get("owner") == self.my_player_id}


# ---------------------------------------------------------------------------
# GameClient
# ---------------------------------------------------------------------------
class GameClient:
    """Async WebSocket client for XBWorld."""

    def __init__(self, username: str = "agent"):
        self.username = username
        self.server_port: int = -1
        self.state = GameState()
        self.ws = None  # websockets.WebSocketClientProtocol
        self._session: Optional[aiohttp.ClientSession] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._turn_counter = 0
        self._turn_event = asyncio.Event()
        self._on_turn_callbacks: list[Callable[["GameClient"], Awaitable]] = []
        self._ws_msg_count = 0
        self._ws_msg_rate_start = time.monotonic()
        self._packets_processed = 0

    # -- lifecycle ----------------------------------------------------------

    async def start_new_game(self, game_type: str = "singleplayer"):
        """Request a server port and connect."""
        logger.info("[%s] Starting new %s game...", self.username, game_type)
        self._session = aiohttp.ClientSession()
        port = await self._request_port(game_type)
        if port is None:
            logger.error("[%s] Failed to get server port from civclientlauncher", self.username)
            raise ConnectionError("Failed to get server port from civclientlauncher")
        self.server_port = port
        logger.info("[%s] Got server port %d, connecting WebSocket...", self.username, port)
        await self._connect_ws(port)
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("[%s] Connected to game server on port %s, recv loop started", self.username, port)

    async def join_game(self, civserverport: int):
        """Connect to an existing game server."""
        logger.info("[%s] Joining existing game on port %d...", self.username, civserverport)
        self._session = aiohttp.ClientSession()
        self.server_port = civserverport
        await self._connect_ws(civserverport)
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("[%s] Joined game server on port %d, recv loop started", self.username, civserverport)

    async def close(self):
        logger.info("[%s] Closing client (connected=%s, turn=%d, packets=%d)",
                     self.username, self.state.connected, self.state.turn, self._packets_processed)
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._session:
            await self._session.close()
        self.state.connected = False
        logger.info("[%s] Client closed", self.username)

    # -- sending ------------------------------------------------------------

    async def send_packet(self, packet: dict):
        """Send a raw JSON packet to the server."""
        if self.ws and self.ws.state == WsState.OPEN:
            logger.debug("[%s] >> packet pid=%s", self.username, packet.get("pid"))
            await self.ws.send(json.dumps(packet))
        else:
            logger.warning("[%s] Cannot send packet pid=%s: WebSocket closed/not ready", self.username, packet.get("pid"))

    async def send_chat(self, message: str):
        """Send a chat/command message (e.g. '/set tax 30')."""
        logger.info("[%s] Sending chat: %s", self.username, message[:100])
        await self.send_packet({"pid": PACKET_CHAT_MSG_REQ, "message": message})

    async def end_turn(self):
        logger.info("[%s] Ending turn %d", self.username, self.state.turn)
        await self.send_packet({
            "pid": PACKET_PLAYER_PHASE_DONE,
            "turn": self.state.turn,
        })

    async def set_rates(self, tax: int, luxury: int, science: int):
        await self.send_packet({
            "pid": PACKET_PLAYER_RATES,
            "tax": tax,
            "luxury": luxury,
            "science": science,
        })

    async def set_research(self, tech_id: int):
        await self.send_packet({
            "pid": PACKET_PLAYER_RESEARCH,
            "tech": tech_id,
        })

    async def set_tech_goal(self, tech_id: int):
        await self.send_packet({
            "pid": PACKET_PLAYER_TECH_GOAL,
            "tech": tech_id,
        })

    async def city_change_production(self, city_id: int, kind: int, value: int):
        """Change city production. kind: 0=improvement, 1=unit."""
        await self.send_packet({
            "pid": PACKET_CITY_CHANGE,
            "city_id": city_id,
            "production_kind": kind,
            "production_value": value,
        })

    async def city_buy(self, city_id: int):
        await self.send_packet({"pid": PACKET_CITY_BUY, "city_id": city_id})

    # -- unit actions -------------------------------------------------------

    def _compute_dest_tile(self, src_tile_id: int, direction: int) -> int:
        """Replicate JS mapstep(): compute the destination tile ID from
        src_tile_id and direction using DIR_DX/DIR_DY tables and map wrapping."""
        DIR_DX = [-1, 0, 1, -1, 1, -1, 0, 1]
        DIR_DY = [-1, -1, -1, 0, 0, 1, 1, 1]

        xsize = self.state.map_info.get("xsize", 0)
        ysize = self.state.map_info.get("ysize", 0)
        if xsize == 0 or ysize == 0:
            return src_tile_id

        tile_data = self.state.tiles.get(src_tile_id)
        if tile_data and "x" in tile_data and "y" in tile_data:
            src_x = tile_data["x"]
            src_y = tile_data["y"]
        else:
            src_x = src_tile_id % xsize
            src_y = src_tile_id // xsize

        new_x = src_x + DIR_DX[direction]
        new_y = src_y + DIR_DY[direction]

        topology_id = self.state.map_info.get("topology_id", 0)
        WRAP_X = 1
        if topology_id & WRAP_X:
            if new_x >= xsize:
                new_y -= 1
            elif new_x < 0:
                new_y += 1
            new_x = new_x % xsize
        else:
            if new_x < 0 or new_x >= xsize:
                return src_tile_id

        if new_y < 0 or new_y >= ysize:
            return src_tile_id

        return new_x + new_y * xsize

    async def unit_move(self, unit_id: int, direction: int):
        """Move a unit one step in the given direction (0-7).
        Directions: 0=NW, 1=N, 2=NE, 3=W, 4=E, 5=SW, 6=S, 7=SE."""
        unit = self.state.units.get(unit_id)
        if not unit:
            logger.warning("[%s] unit_move: unit %d not found", self.username, unit_id)
            return
        src_tile = unit.get("tile", 0)
        dest_tile = self._compute_dest_tile(src_tile, direction)
        logger.debug("[%s] unit_move: unit=%d dir=%d src_tile=%d dest_tile=%d mp=%d",
                     self.username, unit_id, direction, src_tile, dest_tile, unit.get("movesleft", 0))
        await self.send_packet({
            "pid": PACKET_UNIT_ORDERS,
            "unit_id": unit_id,
            "src_tile": src_tile,
            "length": 1,
            "repeat": False,
            "vigilant": False,
            "dest_tile": dest_tile,
            "orders": [{
                "order": ORDER_ACTION_MOVE,
                "activity": ACTIVITY_IDLE,
                "target": src_tile,
                "sub_target": 0,
                "action": ACTION_COUNT,
                "dir": direction,
            }],
        })

    async def unit_found_city(self, unit_id: int, city_name: str = ""):
        """Order a settler to found a city on its current tile.

        Sends PACKET_UNIT_DO_ACTION with ACTION_FOUND_CITY. If the settler
        has no movement points left (e.g. it just moved), the action will
        silently fail on the server side.
        """
        unit = self.state.units.get(unit_id)
        if not unit:
            logger.warning("[%s] unit_found_city: unit %d not found", self.username, unit_id)
            return False
        tile = unit.get("tile", 0)
        mp = unit.get("movesleft", 0)
        logger.info("[%s] unit_found_city: unit=%d tile=%d mp=%d name='%s'",
                     self.username, unit_id, tile, mp, city_name)
        if mp <= 0:
            logger.warning("[%s] unit_found_city: settler %d has 0 MP, action will likely fail",
                           self.username, unit_id)

        await self.send_packet({
            "pid": PACKET_UNIT_DO_ACTION,
            "action_type": ACTION_FOUND_CITY,
            "actor_id": unit_id,
            "target_id": tile,
            "sub_tgt_id": 0,
            "name": city_name or "",
        })
        return True

    async def unit_fortify(self, unit_id: int):
        """Order a unit to fortify."""
        await self.send_packet({
            "pid": PACKET_UNIT_CHANGE_ACTIVITY,
            "unit_id": unit_id,
            "activity": ACTIVITY_FORTIFYING,
            "target": EXTRA_NONE,
        })

    async def unit_auto_explore(self, unit_id: int):
        """Set a unit to auto-explore."""
        await self.send_packet({
            "pid": PACKET_UNIT_SSCS_SET,
            "unit_id": unit_id,
            "type": SSA_AUTOEXPLORE,
            "value": 0,
        })

    async def unit_disband(self, unit_id: int):
        """Disband a unit."""
        unit = self.state.units.get(unit_id)
        if not unit:
            return
        await self.send_packet({
            "pid": PACKET_UNIT_DO_ACTION,
            "action_type": ACTION_DISBAND_UNIT,
            "actor_id": unit_id,
            "target_id": unit_id,
            "sub_tgt_id": 0,
            "name": "",
        })

    async def unit_sentry(self, unit_id: int):
        """Put a unit on sentry duty."""
        await self.send_packet({
            "pid": PACKET_UNIT_CHANGE_ACTIVITY,
            "unit_id": unit_id,
            "activity": ACTIVITY_SENTRY,
            "target": EXTRA_NONE,
        })

    # -- player actions -----------------------------------------------------

    async def player_ready(self):
        if self.state.my_conn_id < 0:
            return
        player_num = self.state.my_player_id
        await self.send_packet({
            "pid": PACKET_PLAYER_READY,
            "is_ready": True,
            "player_no": player_num,
        })

    # -- turn waiting -------------------------------------------------------

    def on_turn(self, callback: Callable[["GameClient"], Awaitable]):
        """Register a callback for each new turn."""
        self._on_turn_callbacks.append(callback)

    async def wait_for_new_turn(self, timeout: float = 300.0) -> bool:
        """Wait until a new begin_turn fires. Uses a counter to avoid race conditions."""
        snapshot = self._turn_counter
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._turn_counter == snapshot:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._turn_event.clear()
            try:
                await asyncio.wait_for(self._turn_event.wait(), min(remaining, 5.0))
            except asyncio.TimeoutError:
                pass
            if not self.state.connected:
                return False
        return True

    # -- internal: connection -----------------------------------------------

    async def _request_port(self, game_type: str) -> Optional[int]:
        url = f"{LAUNCHER_URL}?action=new&type={game_type}"
        async with self._session.post(url) as resp:
            port_str = resp.headers.get("port")
            result = resp.headers.get("result")
            if result == "success" and port_str:
                return int(port_str)
            logger.error("civclientlauncher failed: result=%s", result)
            return None

    async def _connect_ws(self, civserverport: int, max_retries: int = 5):
        proxyport = 1000 + civserverport
        ws_url = f"{WS_BASE_URL}/{proxyport}"
        logger.info("[%s] Connecting WebSocket to %s (server port=%d, proxy port=%d)",
                     self.username, ws_url, civserverport, proxyport)

        last_err = None
        for attempt in range(max_retries):
            try:
                self.ws = await websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=60,
                    max_size=None,
                    close_timeout=10,
                )
                logger.info("[%s] WebSocket connected on attempt %d", self.username, attempt + 1)
                break
            except Exception as e:
                last_err = e
                wait = 1.0 * (attempt + 1)
                logger.warning("[%s] WS connect attempt %d/%d failed (%s), retrying in %.1fs",
                               self.username, attempt + 1, max_retries, e, wait)
                await asyncio.sleep(wait)
        else:
            logger.error("[%s] Failed to connect to %s after %d attempts: %s",
                         self.username, ws_url, max_retries, last_err)
            raise ConnectionError(
                f"Failed to connect to {ws_url} after {max_retries} attempts: {last_err}"
            )

        self.state.connected = True
        self.state.phase = "pregame"

        login = {
            "pid": PACKET_SERVER_JOIN_REQ,
            "username": self.username,
            "capability": FREECIV_VERSION,
            "version_label": "-dev",
            "major_version": MAJOR_VERSION,
            "minor_version": MINOR_VERSION,
            "patch_version": PATCH_VERSION,
            "port": civserverport,
            "password": "",
        }
        logger.info("[%s] Sending login packet to server", self.username)
        await self.ws.send(json.dumps(login))

    # -- internal: receive loop ---------------------------------------------

    async def _recv_loop(self):
        logger.info("[%s] recv_loop started", self.username)
        try:
            async for raw in self.ws:
                self._ws_msg_count += 1
                if self._ws_msg_count % 500 == 0:
                    stats = self.get_ws_stats()
                    logger.info("[%s] WS stats: %d msgs, %d pkts, %.1f msg/s, uptime=%.0fs",
                                self.username, stats["total_ws_msgs"], stats["packets_processed"],
                                stats["ws_msg_rate_per_s"], stats["uptime_s"])
                if isinstance(raw, str):
                    try:
                        packets = json.loads(raw)
                        if isinstance(packets, list):
                            for pkt in packets:
                                if pkt:
                                    self._handle_packet(pkt)
                        elif isinstance(packets, dict):
                            self._handle_packet(packets)
                    except json.JSONDecodeError:
                        logger.warning("[%s] Bad JSON from server: %s", self.username, raw[:200])
                else:
                    logger.debug("[%s] Non-text WS message (type=%s, len=%d)",
                                 self.username, type(raw).__name__, len(raw) if raw else 0)
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("[%s] WebSocket connection closed: code=%s reason=%s",
                           self.username, e.code, e.reason)
        except asyncio.CancelledError:
            logger.debug("[%s] recv_loop cancelled", self.username)
        except Exception as e:
            logger.error("[%s] recv_loop error: %s", self.username, e, exc_info=True)
        finally:
            self.state.connected = False
            stats = self.get_ws_stats()
            logger.info("[%s] WebSocket closed. Final stats: %d msgs, %d pkts processed, turn=%d",
                        self.username, stats["total_ws_msgs"], stats["packets_processed"], self.state.turn)

    # -- internal: packet dispatch ------------------------------------------

    def get_ws_stats(self) -> dict:
        elapsed = time.monotonic() - self._ws_msg_rate_start
        rate = self._ws_msg_count / elapsed if elapsed > 0 else 0
        return {
            "total_ws_msgs": self._ws_msg_count,
            "packets_processed": self._packets_processed,
            "ws_msg_rate_per_s": round(rate, 1),
            "uptime_s": round(elapsed, 1),
        }

    def _handle_packet(self, pkt: dict):
        self._packets_processed += 1
        pid = pkt.get("pid")
        handler = self._handlers.get(pid)
        if handler:
            handler(self, pkt)

    def _on_server_join_reply(self, pkt: dict):
        if pkt.get("you_can_join"):
            self.state.my_conn_id = pkt.get("conn_id", -1)
            logger.info("[%s] Server accepted join, conn_id=%d", self.username, self.state.my_conn_id)
            client_info = {
                "pid": PACKET_CLIENT_INFO,
                "gui": GUI_WEB,
                "emerg_version": 0,
                "distribution": "",
            }
            asyncio.create_task(self.send_packet(client_info))
        else:
            logger.error("[%s] Server REJECTED join: %s", self.username, pkt.get("message"))

    def _on_conn_info(self, pkt: dict):
        if pkt.get("id") == self.state.my_conn_id:
            player_num = pkt.get("player_num", -1)
            if player_num >= 0:
                self.state.my_player_id = player_num
                logger.info("[%s] Assigned player_id=%d", self.username, player_num)

    def _on_conn_ping(self, pkt: dict):
        asyncio.create_task(self.send_packet({"pid": PACKET_CONN_PONG}))

    def _on_game_info(self, pkt: dict):
        new_turn = pkt.get("turn", self.state.turn)
        if new_turn != self.state.turn:
            logger.debug("[%s] game_info: turn changed %d -> %d", self.username, self.state.turn, new_turn)
        self.state.turn = new_turn

    def _on_calendar_info(self, pkt: dict):
        self.state.year = pkt.get("calendar_fragment_name", "")

    def _on_map_info(self, pkt: dict):
        self.state.map_info = {
            "xsize": pkt.get("xsize"),
            "ysize": pkt.get("ysize"),
            "topology_id": pkt.get("topology_id"),
        }

    def _on_chat_msg(self, pkt: dict):
        text = pkt.get("message", "")
        self.state.add_message({"type": "chat", "text": text, "turn": self.state.turn})
        logger.debug("[chat] %s", text)

    def _on_connect_msg(self, pkt: dict):
        text = pkt.get("message", "")
        self.state.add_message({"type": "connect", "text": text})
        logger.debug("[connect] %s", text)

    def _on_city_info(self, pkt: dict):
        cid = pkt.get("id")
        if cid is not None:
            name = pkt.get("name", "")
            if "%" in name:
                from urllib.parse import unquote
                pkt["name"] = unquote(name)
            self.state.cities[cid] = pkt

    def _on_city_short_info(self, pkt: dict):
        cid = pkt.get("id")
        if cid is not None:
            existing = self.state.cities.get(cid, {})
            existing.update(pkt)
            self.state.cities[cid] = existing

    def _on_city_remove(self, pkt: dict):
        cid = pkt.get("city_id")
        removed = self.state.cities.pop(cid, None)
        if removed:
            logger.info("[%s] City removed: id=%d name=%s", self.username, cid, removed.get("name", "?"))

    def _on_unit_info(self, pkt: dict):
        uid = pkt.get("id")
        if uid is not None:
            self.state.units[uid] = pkt

    def _on_unit_short_info(self, pkt: dict):
        uid = pkt.get("id")
        if uid is not None:
            existing = self.state.units.get(uid, {})
            existing.update(pkt)
            self.state.units[uid] = existing

    def _on_unit_remove(self, pkt: dict):
        uid = pkt.get("unit_id")
        removed = self.state.units.pop(uid, None)
        if removed and removed.get("owner") == self.state.my_player_id:
            type_name = self.state.unit_types.get(removed.get("type", -1), {}).get("name", "?")
            logger.info("[%s] My unit removed: id=%d type=%s tile=%d",
                        self.username, uid, type_name, removed.get("tile", -1))

    def _on_player_info(self, pkt: dict):
        pno = pkt.get("playerno")
        if pno is not None:
            self.state.players[pno] = pkt

    def _on_player_remove(self, pkt: dict):
        pno = pkt.get("playerno")
        self.state.players.pop(pno, None)

    def _on_research_info(self, pkt: dict):
        old_researching = self.state.research.get("researching", -1)
        new_researching = pkt.get("researching", -1)
        if old_researching != new_researching:
            old_name = self.state.techs.get(old_researching, {}).get("name", "none")
            new_name = self.state.techs.get(new_researching, {}).get("name", "none")
            logger.info("[%s] Research changed: %s -> %s (bulbs=%d/%d)",
                        self.username, old_name, new_name,
                        pkt.get("bulbs_researched", 0), pkt.get("researching_cost", 0))
            if old_researching >= 0 and new_researching < 0:
                logger.info("[%s] Tech '%s' completed! Auto-picking next research...",
                            self.username, old_name)
                asyncio.create_task(self._auto_pick_research(pkt))
        self.state.research = pkt

    async def _auto_pick_research(self, research_pkt: dict):
        """Auto-pick next research when current one completes."""
        inventions = research_pkt.get("inventions", [])
        PRIORITY_TECHS = [
            "Alphabet", "Bronze Working", "Pottery", "Masonry",
            "Code of Laws", "Warrior Code", "Ceremonial Burial",
            "The Wheel", "Horseback Riding", "Iron Working",
            "Writing", "Literacy", "Map Making", "Currency",
            "Construction", "Republic", "Mathematics",
        ]
        for tech_name in PRIORITY_TECHS:
            for tid, t in self.state.techs.items():
                if t.get("name", "").lower() == tech_name.lower():
                    if tid < len(inventions) and inventions[tid] == 1:
                        break
                    logger.info("[%s] Auto-researching: %s", self.username, t.get("name"))
                    await self.set_research(tid)
                    return
        for tid, t in self.state.techs.items():
            if tid < len(inventions) and inventions[tid] != 1 and t.get("name", ""):
                logger.info("[%s] Auto-researching (fallback): %s", self.username, t.get("name"))
                await self.set_research(tid)
                return

    def _on_begin_turn(self, pkt: dict):
        self.state.phase = "playing"
        self._turn_counter += 1
        logger.info("[%s] BEGIN_TURN: turn=%d counter=%d players=%d units=%d cities=%d",
                     self.username, self.state.turn, self._turn_counter,
                     len(self.state.players), len(self.state.my_units()), len(self.state.my_cities()))
        self._turn_event.set()
        for cb in self._on_turn_callbacks:
            asyncio.create_task(cb(self))

    def _on_end_turn(self, pkt: dict):
        logger.info("[%s] END_TURN: turn=%d", self.username, self.state.turn)
        self.state.phase = "waiting"

    def _on_new_year(self, pkt: dict):
        pass

    def _on_ruleset_unit(self, pkt: dict):
        uid = pkt.get("id")
        if uid is not None:
            name = pkt.get("name", "")
            if name.startswith("?unit:"):
                pkt["name"] = name[6:]
            self.state.unit_types[uid] = pkt

    def _on_ruleset_tech(self, pkt: dict):
        tid = pkt.get("id")
        if tid is not None:
            name = pkt.get("name", "")
            if name.startswith("?tech:"):
                pkt["name"] = name[6:]
            self.state.techs[tid] = pkt

    def _on_ruleset_building(self, pkt: dict):
        bid = pkt.get("id")
        if bid is not None:
            self.state.buildings[bid] = pkt

    def _on_ruleset_government(self, pkt: dict):
        gid = pkt.get("id")
        if gid is not None:
            self.state.governments[gid] = pkt

    def _on_rulesets_ready(self, pkt: dict):
        self.state.rulesets_ready = True
        logger.info("Rulesets loaded: %d unit types, %d techs, %d buildings, %d terrains",
                     len(self.state.unit_types), len(self.state.techs),
                     len(self.state.buildings), len(self.state.terrains))

    def _on_tile_info(self, pkt: dict):
        tile_id = pkt.get("tile")
        if tile_id is not None:
            self.state.tiles[tile_id] = pkt

    def _on_ruleset_terrain(self, pkt: dict):
        tid = pkt.get("id")
        if tid is not None:
            name = pkt.get("name", "")
            if name.startswith("?terrain:"):
                pkt["name"] = name[9:]
            self.state.terrains[tid] = pkt

    def _on_web_city_info_addition(self, pkt: dict):
        cid = pkt.get("id")
        if cid is not None and cid in self.state.cities:
            self.state.cities[cid]["_web_extra"] = pkt

    def _on_web_player_info_addition(self, pkt: dict):
        pno = pkt.get("playerno")
        if pno is not None and pno in self.state.players:
            self.state.players[pno]["_web_extra"] = pkt

    def _on_page_msg(self, pkt: dict):
        text = pkt.get("message", "")
        self.state.add_message({"type": "page", "text": text, "turn": self.state.turn})

    _handlers: dict[int, Callable] = {
        PID_SERVER_JOIN_REPLY: _on_server_join_reply,
        PID_CONN_INFO: _on_conn_info,
        PID_CONN_PING: _on_conn_ping,
        PID_GAME_INFO: _on_game_info,
        PID_CALENDAR_INFO: _on_calendar_info,
        PID_MAP_INFO: _on_map_info,
        PID_CHAT_MSG: _on_chat_msg,
        PID_CONNECT_MSG: _on_connect_msg,
        PID_EARLY_CHAT_MSG: _on_chat_msg,
        PID_TILE_INFO: _on_tile_info,
        PID_CITY_INFO: _on_city_info,
        PID_CITY_SHORT_INFO: _on_city_short_info,
        PID_CITY_REMOVE: _on_city_remove,
        PID_UNIT_INFO: _on_unit_info,
        PID_UNIT_SHORT_INFO: _on_unit_short_info,
        PID_UNIT_REMOVE: _on_unit_remove,
        PID_PLAYER_INFO: _on_player_info,
        PID_PLAYER_REMOVE: _on_player_remove,
        PID_RESEARCH_INFO: _on_research_info,
        PID_BEGIN_TURN: _on_begin_turn,
        PID_END_TURN: _on_end_turn,
        PID_NEW_YEAR: _on_new_year,
        PID_RULESET_UNIT: _on_ruleset_unit,
        PID_RULESET_TECH: _on_ruleset_tech,
        PID_RULESET_BUILDING: _on_ruleset_building,
        PID_RULESET_TERRAIN: _on_ruleset_terrain,
        PID_RULESET_GOVERNMENT: _on_ruleset_government,
        PID_RULESETS_READY: _on_rulesets_ready,
        PID_WEB_CITY_INFO_ADDITION: _on_web_city_info_addition,
        PID_WEB_PLAYER_INFO_ADDITION: _on_web_player_info_addition,
        PID_PAGE_MSG: _on_page_msg,
        PID_PROCESSING_STARTED: lambda self, pkt: None,
        PID_PROCESSING_FINISHED: lambda self, pkt: None,
    }
