"""
Async WebSocket-to-TCP proxy for freeciv-server.

Pure asyncio WebSocket-to-TCP bridge. Runs as a module inside the
FastAPI process (server.py).

Protocol:
  Browser ←→ WebSocket (JSON text frames)
  Proxy   ←→ TCP (2-byte big-endian length + UTF-8 JSON + NUL terminator)
"""

import asyncio
import itertools
import json
import logging
import re
import time
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("xbworld-proxy")

CONNECTION_LIMIT = 1000
_connections: dict[str, "CivBridge"] = {}

# ---------------------------------------------------------------------------
# Per-port tile cache: captures MAP_INFO (pid=17) and TILE_INFO (pid=15)
# from the first (host) connection and replays them to later browser clients
# that join mid-game and don't receive an initial tile dump from the server.
# ---------------------------------------------------------------------------
PID_PROCESSING_STARTED = 0
PID_PROCESSING_FINISHED = 1
PID_MAP_INFO = 17
PID_TILE_INFO = 15
PID_CITY_INFO = 31
PID_CITY_REMOVE = 30
PID_PLAYER_INFO = 51
PID_PLAYER_REMOVE = 50

# Boolean lookup table for pids that need targeted field extraction.
# Replaces frozenset (requires hashing) with a direct array index — O(1)
# with no hash computation.  Freeciv pid values are always small non-negative
# integers; size 256 covers all currently-defined pids with ample headroom.
_PIDS_NEEDING_EXTRACT: list[bool] = [False] * 256
for _p in (PID_CITY_INFO, PID_CITY_REMOVE, PID_PLAYER_INFO, PID_PLAYER_REMOVE):
    _PIDS_NEEDING_EXTRACT[_p] = True

# Fast pid extraction without full JSON parse.  Pattern matches the very
# first "pid" key in the freeciv JSON frame, which is always at the start.
_PID_RE = re.compile(r'"pid"\s*:\s*(-?\d+)')

# Targeted field extractors — avoid json.loads for city/player packets.
# These rely on freeciv's compact JSON format where integer fields appear as
# "key":value (no spaces around colon, integer value, no string quoting).
_CITY_ID_RE    = re.compile(r'"id"\s*:\s*(\d+)')
_CITY_REM_RE   = re.compile(r'"city_id"\s*:\s*(\d+)')
# Combined PLAYER_INFO extractor: one scan instead of three separate re.search()
# calls.  Named groups let us identify which field each match captured.
_PLAYER_ALL_RE = re.compile(
    r'"playerno"\s*:\s*(?P<no>\d+)'
    r'|"name"\s*:\s*"(?P<name>[^"]*)"'
    r'|"ai_control"\s*:\s*(?P<ai>true|false|1|0)'
)
# Still needed for PLAYER_REMOVE (only needs playerno):
_PLAYER_NO_RE  = re.compile(r'"playerno"\s*:\s*(\d+)')

_tile_cache: dict[int, dict] = {}  # server_port -> {map_info, tiles, cities, locked, tiles_prefix}

_USERNAME_RE = re.compile(r"[a-z][a-z0-9_]*")

# Per-port player registry used to pick an AI player for the /take command.
# Keyed by (server_port, playerno); value is {"name": str, "ai": bool}.
_player_cache: dict[int, dict[int, dict]] = {}


def _cache_feed_raw(server_port: int, pid: int, packet_json: str) -> None:
    """Store a MAP_INFO or TILE_INFO packet in the tile cache."""
    cache = _tile_cache.get(server_port)
    if cache and cache.get("locked"):
        return  # tile cache is complete — don't overwrite with stale tile/map updates
    if pid == PID_MAP_INFO:
        if cache is None:
            cache = {"map_info": None, "tiles": [], "cities": {}, "locked": False}
            _tile_cache[server_port] = cache
        cache["map_info"] = packet_json
        logger.info("[tile-cache:%d] cached MAP_INFO", server_port)
    elif pid == PID_TILE_INFO:
        if cache is None:
            cache = {"map_info": None, "tiles": [], "cities": {}, "locked": False}
            _tile_cache[server_port] = cache
        cache["tiles"].append(packet_json)
        if len(cache["tiles"]) % 500 == 0:
            logger.info("[tile-cache:%d] cached %d TILE_INFO packets",
                        server_port, len(cache["tiles"]))


def _cache_feed_city(server_port: int, city_id: int, packet_json: str) -> None:
    """Update a city in the game-state cache.

    City data is NOT locked — it is always kept current so that a late-joining
    observer receives an up-to-date city snapshot even after the tile cache has
    been locked.  Each city is stored by its integer ID so updates overwrite
    stale entries without growing unboundedly.

    ``cities_joined`` is cleared on every update so that _cache_get_replay()
    rebuilds the joined string lazily on the next observer join.
    """
    cache = _tile_cache.get(server_port)
    if cache is None:
        cache = {"map_info": None, "tiles": [], "cities": {}, "locked": False}
        _tile_cache[server_port] = cache
    # "cities" is always initialised to {} in new caches; no setdefault needed.
    cache["cities"][city_id] = packet_json
    cache.pop("cities_joined", None)  # invalidate lazy join cache


def _cache_remove_city(server_port: int, city_id: int) -> None:
    """Remove a city from the cache when it is destroyed."""
    cache = _tile_cache.get(server_port)
    if cache:
        cache["cities"].pop(city_id, None)
        cache.pop("cities_joined", None)  # invalidate lazy join cache


def _cache_feed_player(server_port: int, playerno: int, name: str, ai_control: bool) -> None:
    """Store or update a player entry in the per-port player registry."""
    if server_port not in _player_cache:
        _player_cache[server_port] = {}
    _player_cache[server_port][playerno] = {"name": name, "ai": ai_control}


def _cache_remove_player(server_port: int, playerno: int) -> None:
    """Remove a player from the registry (slot removed or disconnected)."""
    _player_cache.get(server_port, {}).pop(playerno, None)


def _cache_get_ai_player_name(server_port: int) -> Optional[str]:
    """Return the name of the first AI player with a non-empty name.

    Mirrors the browser JS logic in clientCore.ts::requestObserveGame:
      1. prefer a player flagged ai_control=True
      2. fall back to the first player with any non-empty name
    Returns None when no suitable player is known yet.

    Single-pass implementation: records the first named player as a fallback
    while scanning for an AI-controlled one, avoiding a second full iteration.
    """
    players = _player_cache.get(server_port, {})
    fallback: Optional[str] = None
    for entry in players.values():
        if entry["ai"] and entry["name"]:
            return entry["name"]
        if fallback is None and entry["name"]:
            fallback = entry["name"]
    return fallback


def _cache_lock(server_port: int) -> bool:
    """Lock the tile portion of the cache (no more MAP_INFO / TILE_INFO updates).

    City data continues to be updated after locking — only tiles are frozen.
    Pre-computes the immutable tiles prefix string so _cache_get_replay() only
    needs to append the (smaller, dynamic) city list on each observer join.

    Returns True if the cache was just locked by this call, False otherwise
    (already locked or insufficient data).  The caller can use the return value
    to update its local mirror without a second dict lookup.
    """
    cache = _tile_cache.get(server_port)
    if cache and not cache.get("locked") and cache.get("map_info") and cache.get("tiles"):
        cache["locked"] = True
        # Build the invariant part of the replay once: PROCESSING_STARTED +
        # MAP_INFO + all TILE_INFO packets, joined ready for embedding in [...]
        # itertools.chain avoids allocating a merged list (tiles can be 5000+).
        cache["tiles_prefix"] = ",".join(
            itertools.chain(['{"pid":0}', cache["map_info"]], cache["tiles"])
        )
        logger.info("[tile-cache:%d] locked (%d tiles, %d cities so far)",
                    server_port, len(cache["tiles"]), len(cache.get("cities", {})))
        return True
    return False


def _cache_get_replay(server_port: int) -> Optional[str]:
    """Return a WebSocket message containing cached MAP_INFO + TILE_INFO + CITY_INFO, or None.

    The city snapshot is taken at call time so it reflects the latest known
    state; tiles are frozen at lock time.  Packet order mirrors what the server
    sends during a normal initial state dump:
        PROCESSING_STARTED → MAP_INFO → TILE_INFO* → CITY_INFO* → PROCESSING_FINISHED

    Uses the pre-built ``tiles_prefix`` string (set in _cache_lock) so the
    expensive join over thousands of tile packets only happens once, not on
    every observer join.
    """
    cache = _tile_cache.get(server_port)
    if not cache:
        return None
    prefix = cache.get("tiles_prefix")
    if not prefix:
        # Fallback for caches locked before this optimisation was deployed.
        if not cache.get("map_info") or not cache.get("tiles"):
            return None
        prefix = ",".join(['{"pid":0}', cache["map_info"]] + cache["tiles"])
    cities = cache.get("cities", {})
    if cities:
        # Lazily cache the joined city string; rebuilt only when a city
        # is added or removed (cache["cities_joined"] is cleared on change).
        cities_joined = cache.get("cities_joined")
        if cities_joined is None:
            cities_joined = ",".join(cities.values())
            cache["cities_joined"] = cities_joined
        return f'[{prefix},{cities_joined},{{"pid":1}}]'
    return f'[{prefix},{{"pid":1}}]'


def cache_clear_port(port: int) -> None:
    """Remove all cached state for a game server port.

    Must be called when a freeciv-server process is killed or restarted so
    that the next connection on the same port does not receive stale tile,
    city, or player data from the previous game session.
    """
    _tile_cache.pop(port, None)
    _player_cache.pop(port, None)
    logger.info("[tile-cache:%d] cache cleared (server stopped)", port)


def validate_username(name: str) -> bool:
    if not name or len(name) <= 2 or len(name) >= 32:
        return False
    lower = name.lower()
    return lower != "pbem" and _USERNAME_RE.fullmatch(lower) is not None


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
        self._tile_cache_injected = False  # whether we've replayed cached tiles
        self._processing_count = 0         # count of PROCESSING_FINISHED seen
        self._take_sent = False            # whether we've sent /take to trigger city resync
        self._tile_cache_locked = False    # local flag: skip _cache_feed_raw once locked

    async def connect_to_server(self, login_packet: str) -> bool:
        logger.info("[proxy:%s] Connecting to civserver at 127.0.0.1:%d", self.username, self.server_port)
        try:
            async with asyncio.timeout(5.0):
                self._reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", self.server_port
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
            header = (len(encoded) + 3).to_bytes(2, 'big')
            self._writer.write(header + encoded + b"\0")
            await self._writer.drain()
        except Exception as e:
            logger.warning("[proxy:%s] Failed to send to civserver: %s", self.username, e)

    async def _server_reader_loop(self):
        """Read packets from freeciv-server TCP and forward to WebSocket client."""
        exit_reason = "unknown"
        # Cache hot-path attributes as locals so CPython uses LOAD_FAST instead
        # of LOAD_FAST+LOAD_ATTR on every iteration.  server_port is immutable;
        # _tile_cache_locked starts False and only ever flips to True, so we
        # can keep a local mirror and sync back to the instance when it changes.
        server_port = self.server_port
        username = self.username
        reader = self._reader
        buf_append = self._send_buffer.append
        _tile_cache_locked = False  # mirrors self._tile_cache_locked
        _tcp_pkt_count = 0          # local counter; synced to self._tcp_pkt_count in finally
        try:
            while not self._stopped and reader:
                # Inline header read — avoids 2 method calls + self._reader LOAD_ATTR per packet.
                try:
                    async with asyncio.timeout(300):
                        header_data = await reader.readexactly(2)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
                    exit_reason = f"header read failed: {type(e).__name__}"
                    break

                packet_size = int.from_bytes(header_data, 'big')
                body_size = packet_size - 2
                if body_size <= 0 or body_size > 32767:
                    logger.error("[proxy:%s] Invalid packet size %d from server", username, body_size)
                    continue

                # Inline body read.
                try:
                    async with asyncio.timeout(300):
                        body = await reader.readexactly(body_size)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
                    exit_reason = f"body read failed: {type(e).__name__}"
                    break

                # Freeciv always appends a NUL terminator (protocol invariant).
                # Strip unconditionally — saves a per-packet byte comparison.
                body = body[:-1]

                _tcp_pkt_count += 1

                # errors="ignore" never raises — try/except is unnecessary overhead.
                text = body.decode("utf-8", errors="ignore")

                # Extract pid: Freeciv packets always start with {"pid":N,...
                # Fast path: text[7:] starts right after the colon in '{"pid":'.
                # text.find(',', 7) is a C-level scan; int() strips whitespace so
                # '{"pid": 31,...' works.  Regex fallback handles edge cases
                # (no comma = single-field packet like {"pid":0}).
                _comma = text.find(',', 7)
                if _comma > 7:
                    try:
                        pid = int(text[7:_comma])
                    except ValueError:
                        _m = _PID_RE.search(text)
                        pid = int(_m.group(1)) if _m else None
                else:
                    _m = _PID_RE.search(text)
                    pid = int(_m.group(1)) if _m else None

                # Feed MAP_INFO and TILE_INFO into tile cache (locked after first batch).
                # _tile_cache_locked check is first so that after locking it short-circuits
                # without evaluating the pid comparisons on every subsequent packet.
                if not _tile_cache_locked and (pid == PID_MAP_INFO or pid == PID_TILE_INFO):
                    _cache_feed_raw(server_port, pid, text)
                elif pid is not None and pid < 256 and _PIDS_NEEDING_EXTRACT[pid]:
                    # Use targeted regex extractors instead of full json.loads.
                    # Feed CITY_INFO into city cache — always updated, never locked
                    if pid == PID_CITY_INFO:
                        _m = _CITY_ID_RE.search(text)
                        if _m:
                            _cache_feed_city(server_port, int(_m.group(1)), text)
                    # Remove destroyed cities from cache
                    elif pid == PID_CITY_REMOVE:
                        _m = _CITY_REM_RE.search(text)
                        if _m:
                            _cache_remove_city(server_port, int(_m.group(1)))
                    # Track players so we can pick an AI player for /take.
                    # Single finditer scan extracts playerno, name, ai_control
                    # in one pass instead of three separate re.search() calls.
                    elif pid == PID_PLAYER_INFO:
                        _p_no: int | None = None
                        _p_name = ""
                        _p_ai = False
                        for _pm in _PLAYER_ALL_RE.finditer(text):
                            lg = _pm.lastgroup
                            if lg == 'no':
                                _p_no = int(_pm.group('no'))
                            elif lg == 'name':
                                _p_name = _pm.group('name')
                            else:
                                _p_ai = _pm.group('ai') in ('true', '1')
                        if _p_no is not None:
                            _cache_feed_player(server_port, _p_no, _p_name, _p_ai)
                    elif pid == PID_PLAYER_REMOVE:
                        _m = _PLAYER_NO_RE.search(text)
                        if _m:
                            _cache_remove_player(server_port, int(_m.group(1)))

                buf_append(text)

                if pid == PID_PROCESSING_FINISHED:
                    self._processing_count += 1
                    # Snapshot the locked flag BEFORE _cache_lock() may set it.
                    # True  → tile cache was already populated by a previous connection
                    #         → this connection is a late-joining observer.
                    # False → this connection is the first one (host / game starter).
                    # Check if cache was already locked by a previous connection
                    # BEFORE attempting to lock it ourselves.  Use local mirror
                    # first (O(1) bool test) before falling back to dict lookup.
                    was_already_locked = _tile_cache_locked or \
                        _tile_cache.get(server_port, {}).get("locked", False)
                    # Lock the tile portion of the cache after the first full batch.
                    # _cache_lock() returns True only when it actually performed the lock.
                    if not _tile_cache_locked:
                        if _cache_lock(server_port) or was_already_locked:
                            _tile_cache_locked = True
                            self._tile_cache_locked = True
                    # Inject cached tiles+cities for late-joining observers only.
                    #
                    # was_already_locked == True  → tile cache was fully populated by a
                    #   previous connection before this one joined.  This connection is a
                    #   late observer: the server will NOT send a full tile dump, so we
                    #   replay the cached state here.
                    # was_already_locked == False → this connection IS the one that
                    #   populated the cache.  It already received every tile/map packet
                    #   through the normal buffer → flush path above; sending the replay
                    #   again would duplicate all tile data over the WebSocket.
                    if not self._tile_cache_injected:
                        self._tile_cache_injected = True  # prevent repeat injection
                        if was_already_locked:
                            replay = _cache_get_replay(server_port)
                            if replay:
                                _cache_snap = _tile_cache.get(server_port, {})
                                logger.info(
                                    "[proxy:%s] Injecting cached tile+city data "
                                    "(port=%d, tiles=%d, cities=%d)",
                                    username, server_port,
                                    len(_cache_snap.get("tiles", [])),
                                    len(_cache_snap.get("cities", {})),
                                )
                                # Flush current buffer first, then inject.
                                flush_ok = await self._flush_to_client()
                                if not flush_ok:
                                    break
                                try:
                                    await self.ws.send_text(replay)
                                    self._ws_send_count += 1
                                except Exception as e:
                                    logger.error("[proxy:%s] Failed to send tile cache: %s", username, e)
                                    self._stopped = True
                                    break
                                # Proactively send /take <ai_player> so freeciv-server
                                # immediately pushes fresh CITY_INFO packets.  This mirrors
                                # clientCore.ts::requestObserveGame but fires before the
                                # browser's own 3-second delayed /take, giving city data
                                # sooner.  The browser's subsequent /take is a no-op.
                                if not self._take_sent:
                                    ai_name = _cache_get_ai_player_name(server_port)
                                    if ai_name:
                                        self._take_sent = True
                                        take_pkt = json.dumps({"pid": 26, "message": f"/take {ai_name}"})
                                        await self._send_to_server(take_pkt)
                                        logger.info(
                                            "[proxy:%s] Sent /take %s to trigger city resync "
                                            "(port=%d)",
                                            username, ai_name, server_port,
                                        )
                                    else:
                                        logger.warning(
                                            "[proxy:%s] Late observer but no AI player name cached yet "
                                            "(port=%d) — skipping /take; browser JS will handle it",
                                            username, server_port,
                                        )
                                continue  # skip normal flush below (already flushed)

                # Flush strategy:
                # - TILE_INFO during initial dump (not yet locked): batch in buffer;
                #   PID_PROCESSING_FINISHED will flush the whole dump in one WebSocket frame,
                #   reducing 5000+ individual frames to 1 during tile sync.
                # - All other packets (and TILE_INFO after lock): flush immediately for
                #   low latency during normal gameplay.
                if not _tile_cache_locked and pid == PID_TILE_INFO:
                    continue  # accumulate; flushed at PROCESSING_FINISHED

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
            logger.warning("[proxy:%s] Server reader error: %s", username, e)
        finally:
            self._tcp_pkt_count = _tcp_pkt_count  # sync local counter back to instance
            logger.info("[proxy:%s] _server_reader_loop exited: reason='%s' tcp_pkts=%d ws_sends=%d",
                         username, exit_reason, self._tcp_pkt_count, self._ws_send_count)
            if not self._stopped:
                logger.info("[proxy:%s] TCP connection from civserver closed (server initiated)", username)
                await self.close()

    async def _flush_to_client(self) -> bool:
        """Flush send buffer to WebSocket client. Returns False if send failed."""
        if not self._send_buffer or self._stopped:
            return True
        # Fast path for single-packet flushes (common during normal gameplay)
        # avoids the list-iteration overhead of ",".join().
        if len(self._send_buffer) == 1:
            packet = f'[{self._send_buffer[0]}]'
        else:
            packet = f'[{",".join(self._send_buffer)}]'
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
