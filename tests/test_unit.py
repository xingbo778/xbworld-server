"""
Unit tests for xbworld-server — no running server or freeciv binary required.

Tests the internal logic of each module: config, EventBus, ServerManager
(mocked), ws_proxy validation, game_client packet dispatch & tile math.

Usage:
    pytest tests/test_unit.py -v
"""

import asyncio
import json
import struct

import pytest


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------
class TestConfig:
    def test_default_values(self):
        from config import (
            SERVER_PORT, FREECIV_VERSION, MAJOR_VERSION,
            MINOR_VERSION, PATCH_VERSION, MAX_MESSAGES_KEPT,
        )
        assert isinstance(SERVER_PORT, int) and SERVER_PORT > 0
        assert FREECIV_VERSION == "+Freeciv.Web.Devel-3.4"
        assert MAJOR_VERSION == 3
        assert MINOR_VERSION == 3
        assert PATCH_VERSION == 90
        assert MAX_MESSAGES_KEPT == 200

    def test_urls_contain_host_and_port(self):
        from config import LAUNCHER_URL, WS_BASE_URL
        assert "/civclientlauncher" in LAUNCHER_URL
        assert "/civsocket" in WS_BASE_URL


# ---------------------------------------------------------------------------
# ws_proxy.py — validate_username
# ---------------------------------------------------------------------------
class TestValidateUsername:
    def test_valid_usernames(self):
        from ws_proxy import validate_username
        assert validate_username("alice") is True
        assert validate_username("bob123") is True
        assert validate_username("player1") is True
        assert validate_username("abc") is True

    def test_too_short(self):
        from ws_proxy import validate_username
        assert validate_username("ab") is False
        assert validate_username("a") is False
        assert validate_username("") is False

    def test_too_long(self):
        from ws_proxy import validate_username
        assert validate_username("a" * 32) is False
        assert validate_username("a" * 50) is False

    def test_reserved_name(self):
        from ws_proxy import validate_username
        assert validate_username("pbem") is False
        assert validate_username("PBEM") is False
        assert validate_username("Pbem") is False

    def test_invalid_characters(self):
        from ws_proxy import validate_username
        assert validate_username("user name") is False
        assert validate_username("user-name") is False
        assert validate_username("user_name") is False
        assert validate_username("123user") is False  # must start with letter

    def test_none(self):
        from ws_proxy import validate_username
        assert validate_username(None) is False


# ---------------------------------------------------------------------------
# server.py — EventBus
# ---------------------------------------------------------------------------
class TestEventBus:
    def test_publish_and_subscribe(self):
        from server import EventBus
        bus = EventBus(max_history=10)
        q = bus.subscribe()
        bus.publish({"type": "test", "data": 1})
        assert not q.empty()
        evt = q.get_nowait()
        assert evt["type"] == "test"
        assert evt["data"] == 1

    def test_history_replay_on_subscribe(self):
        from server import EventBus
        bus = EventBus(max_history=10)
        for i in range(5):
            bus.publish({"i": i})
        q = bus.subscribe()
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert len(events) == 5
        assert events[0]["i"] == 0

    def test_history_limited(self):
        from server import EventBus
        bus = EventBus(max_history=3)
        for i in range(10):
            bus.publish({"i": i})
        assert len(bus._history) == 3
        assert bus._history[0]["i"] == 7

    def test_history_replay_capped_at_20(self):
        """New subscribers get at most last 20 events from history."""
        from server import EventBus
        bus = EventBus(max_history=50)
        for i in range(50):
            bus.publish({"i": i})
        q = bus.subscribe()
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert len(events) == 20
        assert events[0]["i"] == 30  # last 20 of 50

    def test_unsubscribe(self):
        from server import EventBus
        bus = EventBus()
        q = bus.subscribe()
        assert q in bus._subscribers
        bus.unsubscribe(q)
        assert q not in bus._subscribers

    def test_full_queue_drops_subscriber(self):
        from server import EventBus
        bus = EventBus(max_history=0)
        q = bus.subscribe()
        # Fill the queue (maxsize=100)
        for i in range(100):
            bus.publish({"i": i})
        # Queue is full, next publish should drop this subscriber
        bus.publish({"i": 999})
        assert q not in bus._subscribers

    def test_multiple_subscribers(self):
        from server import EventBus
        bus = EventBus(max_history=0)
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish({"x": 1})
        assert q1.get_nowait()["x"] == 1
        assert q2.get_nowait()["x"] == 1


# ---------------------------------------------------------------------------
# server.py — ServerManager (port finding only, no subprocess)
# ---------------------------------------------------------------------------
class TestServerManagerPorts:
    def test_find_free_port(self):
        from server import ServerManager
        mgr = ServerManager()
        port = mgr._find_free_port(start=16000, end=16010)
        assert 16000 <= port < 16010

    def test_status_empty(self):
        from server import ServerManager
        mgr = ServerManager()
        s = mgr.status()
        assert s == {"total": 0, "single": 0, "multi": 0, "ports": []}


# ---------------------------------------------------------------------------
# game_client.py — GameState
# ---------------------------------------------------------------------------
class TestGameState:
    def test_initial_state(self):
        from game_client import GameState
        gs = GameState()
        assert gs.connected is False
        assert gs.phase == "connecting"
        assert gs.turn == 0
        assert gs.my_player_id == -1

    def test_add_message_capping(self):
        from game_client import GameState
        gs = GameState()
        for i in range(250):
            gs.add_message({"i": i})
        assert len(gs.messages) == 200
        assert gs.messages[0]["i"] == 50  # oldest kept

    def test_my_units_filters_by_owner(self):
        from game_client import GameState
        gs = GameState()
        gs.my_player_id = 0
        gs.units = {
            1: {"id": 1, "owner": 0, "type": 1},
            2: {"id": 2, "owner": 1, "type": 2},
            3: {"id": 3, "owner": 0, "type": 1},
        }
        mine = gs.my_units()
        assert set(mine.keys()) == {1, 3}

    def test_my_cities_filters_by_owner(self):
        from game_client import GameState
        gs = GameState()
        gs.my_player_id = 2
        gs.cities = {
            10: {"id": 10, "owner": 2, "name": "A"},
            11: {"id": 11, "owner": 0, "name": "B"},
        }
        mine = gs.my_cities()
        assert set(mine.keys()) == {10}

    def test_my_player(self):
        from game_client import GameState
        gs = GameState()
        gs.my_player_id = 1
        gs.players = {0: {"playerno": 0}, 1: {"playerno": 1}}
        assert gs.my_player()["playerno"] == 1

    def test_my_player_not_found(self):
        from game_client import GameState
        gs = GameState()
        assert gs.my_player() is None


# ---------------------------------------------------------------------------
# game_client.py — Packet dispatch
# ---------------------------------------------------------------------------
class TestGameClientPacketHandlers:
    def _make_client(self):
        from game_client import GameClient
        client = GameClient(username="test")
        client.state.connected = True
        return client

    @pytest.mark.asyncio
    async def test_server_join_reply_accepted(self):
        client = self._make_client()
        client._handle_packet({"pid": 5, "you_can_join": True, "conn_id": 42})
        assert client.state.my_conn_id == 42

    def test_server_join_reply_rejected(self):
        client = self._make_client()
        client._handle_packet({"pid": 5, "you_can_join": False, "message": "Denied"})
        assert client.state.my_conn_id == -1

    def test_conn_info_assigns_player_id(self):
        client = self._make_client()
        client.state.my_conn_id = 42
        client._handle_packet({"pid": 115, "id": 42, "player_num": 3})
        assert client.state.my_player_id == 3

    def test_conn_info_ignores_other_connections(self):
        client = self._make_client()
        client.state.my_conn_id = 42
        client._handle_packet({"pid": 115, "id": 99, "player_num": 5})
        assert client.state.my_player_id == -1

    def test_game_info_updates_turn(self):
        client = self._make_client()
        client._handle_packet({"pid": 16, "turn": 10})
        assert client.state.turn == 10

    def test_map_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 17, "xsize": 80, "ysize": 50, "topology_id": 1})
        assert client.state.map_info["xsize"] == 80
        assert client.state.map_info["ysize"] == 50

    def test_player_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 51, "playerno": 0, "name": "Alice"})
        assert 0 in client.state.players
        assert client.state.players[0]["name"] == "Alice"

    def test_player_remove(self):
        client = self._make_client()
        client.state.players[5] = {"playerno": 5}
        client._handle_packet({"pid": 50, "playerno": 5})
        assert 5 not in client.state.players

    def test_unit_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 63, "id": 100, "type": 1, "tile": 55, "owner": 0})
        assert client.state.units[100]["tile"] == 55

    def test_unit_short_info_merges(self):
        client = self._make_client()
        client.state.units[100] = {"id": 100, "type": 1, "tile": 55}
        client._handle_packet({"pid": 64, "id": 100, "tile": 60})
        assert client.state.units[100]["tile"] == 60
        assert client.state.units[100]["type"] == 1  # preserved

    def test_unit_remove(self):
        client = self._make_client()
        client.state.units[100] = {"id": 100, "owner": 0}
        client._handle_packet({"pid": 62, "unit_id": 100})
        assert 100 not in client.state.units

    def test_city_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 31, "id": 10, "name": "Rome", "owner": 0})
        assert client.state.cities[10]["name"] == "Rome"

    def test_city_info_url_decodes_name(self):
        client = self._make_client()
        client._handle_packet({"pid": 31, "id": 10, "name": "New%20York", "owner": 0})
        assert client.state.cities[10]["name"] == "New York"

    def test_city_short_info_merges(self):
        client = self._make_client()
        client.state.cities[10] = {"id": 10, "name": "Rome", "size": 1}
        client._handle_packet({"pid": 32, "id": 10, "size": 3})
        assert client.state.cities[10]["size"] == 3
        assert client.state.cities[10]["name"] == "Rome"

    def test_city_remove(self):
        client = self._make_client()
        client.state.cities[10] = {"id": 10, "name": "Rome"}
        client._handle_packet({"pid": 30, "city_id": 10})
        assert 10 not in client.state.cities

    def test_chat_msg(self):
        client = self._make_client()
        client._handle_packet({"pid": 25, "message": "hello world"})
        assert len(client.state.messages) == 1
        assert client.state.messages[0]["text"] == "hello world"

    def test_research_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 60, "researching": 5, "bulbs_researched": 10, "researching_cost": 50})
        assert client.state.research["researching"] == 5

    def test_begin_turn(self):
        client = self._make_client()
        client._handle_packet({"pid": 128})
        assert client.state.phase == "playing"
        assert client._turn_counter == 1

    def test_end_turn(self):
        client = self._make_client()
        client._handle_packet({"pid": 129})
        assert client.state.phase == "waiting"

    def test_ruleset_unit_strips_prefix(self):
        client = self._make_client()
        client._handle_packet({"pid": 140, "id": 0, "name": "?unit:Warriors"})
        assert client.state.unit_types[0]["name"] == "Warriors"

    def test_ruleset_tech_strips_prefix(self):
        client = self._make_client()
        client._handle_packet({"pid": 144, "id": 0, "name": "?tech:Alphabet"})
        assert client.state.techs[0]["name"] == "Alphabet"

    def test_ruleset_terrain_strips_prefix(self):
        client = self._make_client()
        client._handle_packet({"pid": 151, "id": 0, "name": "?terrain:Plains"})
        assert client.state.terrains[0]["name"] == "Plains"

    def test_ruleset_building(self):
        client = self._make_client()
        client._handle_packet({"pid": 150, "id": 0, "name": "Granary"})
        assert client.state.buildings[0]["name"] == "Granary"

    def test_ruleset_government(self):
        client = self._make_client()
        client._handle_packet({"pid": 145, "id": 0, "name": "Despotism"})
        assert client.state.governments[0]["name"] == "Despotism"

    def test_rulesets_ready(self):
        client = self._make_client()
        client._handle_packet({"pid": 225})
        assert client.state.rulesets_ready is True

    def test_tile_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 15, "tile": 42, "terrain": 3, "x": 2, "y": 1})
        assert client.state.tiles[42]["terrain"] == 3

    def test_calendar_info(self):
        client = self._make_client()
        client._handle_packet({"pid": 255, "calendar_fragment_name": "4000 BC"})
        assert client.state.year == "4000 BC"

    def test_page_msg(self):
        client = self._make_client()
        client._handle_packet({"pid": 110, "message": "Welcome!"})
        assert client.state.messages[0]["type"] == "page"

    def test_unknown_pid_ignored(self):
        client = self._make_client()
        client._handle_packet({"pid": 9999, "data": "whatever"})
        # Should not crash

    def test_packets_processed_counter(self):
        client = self._make_client()
        for i in range(5):
            client._handle_packet({"pid": 16, "turn": i})
        assert client._packets_processed == 5


# ---------------------------------------------------------------------------
# game_client.py — _compute_dest_tile
# ---------------------------------------------------------------------------
class TestComputeDestTile:
    def _make_client_with_map(self, xsize=80, ysize=50, topology_id=1):
        from game_client import GameClient
        client = GameClient(username="test")
        client.state.map_info = {"xsize": xsize, "ysize": ysize, "topology_id": topology_id}
        return client

    def test_move_east(self):
        """Direction 4 = East: x+1, y+0."""
        client = self._make_client_with_map(xsize=80, ysize=50, topology_id=0)
        # tile at (5,5) = 5 + 5*80 = 405, moving east -> (6,5) = 406
        client.state.tiles[405] = {"x": 5, "y": 5}
        result = client._compute_dest_tile(405, 4)
        assert result == 406

    def test_move_north(self):
        """Direction 1 = North: x+0, y-1."""
        client = self._make_client_with_map(xsize=80, ysize=50, topology_id=0)
        client.state.tiles[405] = {"x": 5, "y": 5}
        result = client._compute_dest_tile(405, 1)
        assert result == 5 + 4 * 80  # (5, 4)

    def test_wrap_x(self):
        """Wrapping X: moving east from x=79 wraps to x=0."""
        client = self._make_client_with_map(xsize=80, ysize=50, topology_id=1)
        tile_id = 79 + 5 * 80  # (79, 5) = 479
        client.state.tiles[tile_id] = {"x": 79, "y": 5}
        result = client._compute_dest_tile(tile_id, 4)  # East
        # new_x=80 -> wraps to 0, new_y=5-1=4 (iso adjustment)
        assert result == 0 + 4 * 80  # (0, 4)

    def test_no_wrap_x(self):
        """Without X wrapping, moving off edge returns same tile."""
        client = self._make_client_with_map(xsize=80, ysize=50, topology_id=0)
        tile_id = 79 + 5 * 80  # (79, 5)
        client.state.tiles[tile_id] = {"x": 79, "y": 5}
        result = client._compute_dest_tile(tile_id, 4)  # East off edge
        assert result == tile_id  # clamped

    def test_no_map_returns_same_tile(self):
        """With no map info, returns same tile."""
        from game_client import GameClient
        client = GameClient(username="test")
        result = client._compute_dest_tile(100, 4)
        assert result == 100

    def test_move_south(self):
        """Direction 6 = South: x+0, y+1."""
        client = self._make_client_with_map(xsize=10, ysize=10, topology_id=0)
        tile_id = 5 + 3 * 10  # (5, 3) = 35
        client.state.tiles[tile_id] = {"x": 5, "y": 3}
        result = client._compute_dest_tile(tile_id, 6)
        assert result == 5 + 4 * 10  # (5, 4) = 45

    def test_clamp_y_top(self):
        """Moving north from y=0 clamps."""
        client = self._make_client_with_map(xsize=10, ysize=10, topology_id=0)
        tile_id = 5  # (5, 0)
        client.state.tiles[tile_id] = {"x": 5, "y": 0}
        result = client._compute_dest_tile(tile_id, 1)  # North
        assert result == tile_id

    def test_clamp_y_bottom(self):
        """Moving south from y=max clamps."""
        client = self._make_client_with_map(xsize=10, ysize=10, topology_id=0)
        tile_id = 5 + 9 * 10  # (5, 9) = 95
        client.state.tiles[tile_id] = {"x": 5, "y": 9}
        result = client._compute_dest_tile(tile_id, 6)  # South
        assert result == tile_id

    def test_fallback_to_tile_id_math(self):
        """Without tile data, falls back to tile_id % xsize."""
        client = self._make_client_with_map(xsize=10, ysize=10, topology_id=0)
        # tile_id 35 = (5, 3), move East -> (6, 3) = 36
        result = client._compute_dest_tile(35, 4)
        assert result == 36


# ---------------------------------------------------------------------------
# game_client.py — WebSocket stats
# ---------------------------------------------------------------------------
class TestGameClientStats:
    def test_get_ws_stats(self):
        from game_client import GameClient
        client = GameClient(username="test")
        client._ws_msg_count = 100
        client._packets_processed = 50
        stats = client.get_ws_stats()
        assert stats["total_ws_msgs"] == 100
        assert stats["packets_processed"] == 50
        assert "ws_msg_rate_per_s" in stats
        assert "uptime_s" in stats


# ---------------------------------------------------------------------------
# ws_proxy.py — TCP framing protocol
# ---------------------------------------------------------------------------
class TestTcpFraming:
    def test_frame_encoding(self):
        """Verify the 2-byte-length + payload + NUL framing."""
        message = '{"pid":4,"username":"test"}'
        encoded = message.encode("utf-8")
        # Frame: 2-byte big-endian (len + 3), then payload, then NUL
        header = struct.pack(">H", len(encoded) + 3)
        frame = header + encoded + b"\0"

        # Decode it back
        (size,) = struct.unpack(">H", frame[:2])
        body = frame[2:]
        assert size == len(encoded) + 3
        assert body[-1] == 0
        assert body[:-1].decode("utf-8") == message

    def test_frame_sizes(self):
        """Various payload sizes produce correct frame headers."""
        for payload_len in [1, 100, 1000, 10000]:
            payload = b"x" * payload_len
            header = struct.pack(">H", payload_len + 3)
            (decoded_size,) = struct.unpack(">H", header)
            assert decoded_size == payload_len + 3


# ---------------------------------------------------------------------------
# Helpers shared by ws_proxy cache + CivBridge tests
# ---------------------------------------------------------------------------

def _make_tcp_frame(payload: dict) -> bytes:
    """Encode a dict as a freeciv TCP frame: 2-byte length + JSON + NUL."""
    text = json.dumps(payload).encode("utf-8")
    header = struct.pack(">H", len(text) + 3)
    return header + text + b"\0"


class _MockReader:
    """Fake asyncio.StreamReader that serves pre-built frames then raises EOF."""

    def __init__(self, *frames: bytes):
        self._data = b"".join(frames)
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise asyncio.IncompleteReadError(
                partial=self._data[self._pos:], expected=n
            )
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


class _MockWS:
    """Fake FastAPI WebSocket that captures sent text."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class _MockWriter:
    """Fake asyncio.StreamWriter that captures written bytes."""

    def __init__(self):
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


# ---------------------------------------------------------------------------
# ws_proxy.py — city cache (_cache_feed_city / _cache_remove_city)
# ---------------------------------------------------------------------------

class TestCityCache:
    """Tests for the per-port city cache that backs observer replay."""

    PORT = 19010

    def setup_method(self):
        import ws_proxy
        ws_proxy._tile_cache.pop(self.PORT, None)

    def teardown_method(self):
        import ws_proxy
        ws_proxy._tile_cache.pop(self.PORT, None)

    def test_feed_city_creates_entry(self):
        from ws_proxy import _cache_feed_city, _tile_cache
        _cache_feed_city(self.PORT, 10, '{"pid":31,"id":10,"name":"Rome"}')
        assert 10 in _tile_cache[self.PORT]["cities"]
        assert "Rome" in _tile_cache[self.PORT]["cities"][10]

    def test_feed_city_overwrites_stale_entry(self):
        """Second CITY_INFO for same ID replaces the first."""
        from ws_proxy import _cache_feed_city, _tile_cache
        _cache_feed_city(self.PORT, 10, '{"pid":31,"id":10,"name":"Rome"}')
        _cache_feed_city(self.PORT, 10, '{"pid":31,"id":10,"name":"Roma"}')
        assert "Roma" in _tile_cache[self.PORT]["cities"][10]
        assert "Rome" not in _tile_cache[self.PORT]["cities"][10]

    def test_feed_city_multiple_ids(self):
        from ws_proxy import _cache_feed_city, _tile_cache
        _cache_feed_city(self.PORT, 1, '{"pid":31,"id":1}')
        _cache_feed_city(self.PORT, 2, '{"pid":31,"id":2}')
        assert len(_tile_cache[self.PORT]["cities"]) == 2

    def test_remove_city_deletes_entry(self):
        from ws_proxy import _cache_feed_city, _cache_remove_city, _tile_cache
        _cache_feed_city(self.PORT, 10, '{"pid":31,"id":10}')
        _cache_remove_city(self.PORT, 10)
        assert 10 not in _tile_cache[self.PORT]["cities"]

    def test_remove_nonexistent_city_is_noop(self):
        from ws_proxy import _cache_remove_city
        _cache_remove_city(self.PORT, 9999)  # must not raise

    def test_city_updated_after_tile_cache_locked(self):
        """City cache keeps updating even after the tile portion is locked."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles": ['{"pid":15}'],
            "cities": {},
            "locked": True,  # tiles frozen
        }
        ws_proxy._cache_feed_city(self.PORT, 5, '{"pid":31,"id":5,"name":"Athens"}')
        assert 5 in ws_proxy._tile_cache[self.PORT]["cities"]

    def test_replay_includes_city_packets(self):
        """_cache_get_replay embeds current city state in the bundle."""
        from ws_proxy import _cache_feed_city, _cache_get_replay, _tile_cache
        _tile_cache[self.PORT] = {
            "map_info": '{"pid":17,"xsize":10}',
            "tiles": ['{"pid":15,"tile":0}'],
            "cities": {},
            "locked": True,
        }
        _cache_feed_city(self.PORT, 7, '{"pid":31,"id":7,"name":"Carthage"}')
        replay = _cache_get_replay(self.PORT)
        assert replay is not None
        bundle = json.loads(replay)
        pids = [p["pid"] for p in bundle]
        assert 31 in pids  # CITY_INFO present
        city_pkts = [p for p in bundle if p["pid"] == 31]
        assert city_pkts[0]["name"] == "Carthage"

    def test_replay_wraps_with_processing_sentinels(self):
        """Replay bundle starts with PROCESSING_STARTED and ends with PROCESSING_FINISHED."""
        from ws_proxy import _cache_get_replay, _tile_cache
        _tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles": ['{"pid":15}'],
            "cities": {},
            "locked": True,
        }
        bundle = json.loads(_cache_get_replay(self.PORT))
        assert bundle[0]["pid"] == 0   # PROCESSING_STARTED
        assert bundle[-1]["pid"] == 1  # PROCESSING_FINISHED

    def test_replay_none_when_no_tiles(self):
        """No replay is produced if tile cache is still empty."""
        from ws_proxy import _cache_get_replay, _tile_cache
        _tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles": [],
            "cities": {},
            "locked": False,
        }
        assert _cache_get_replay(self.PORT) is None

    def test_replay_city_snapshot_at_call_time(self):
        """Cities added after locking appear in replay (snapshot is live)."""
        from ws_proxy import _cache_feed_city, _cache_get_replay, _tile_cache
        _tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles": ['{"pid":15}'],
            "cities": {},
            "locked": True,
        }
        _cache_feed_city(self.PORT, 1, '{"pid":31,"id":1,"name":"First"}')
        first_replay = json.loads(_cache_get_replay(self.PORT))
        _cache_feed_city(self.PORT, 2, '{"pid":31,"id":2,"name":"Second"}')
        second_replay = json.loads(_cache_get_replay(self.PORT))
        first_names  = {p.get("name") for p in first_replay  if p.get("pid") == 31}
        second_names = {p.get("name") for p in second_replay if p.get("pid") == 31}
        assert "First"  in first_names
        assert "Second" not in first_names   # not yet present at first call
        assert "Second" in second_names      # present at second call


# ---------------------------------------------------------------------------
# ws_proxy.py — player cache (_cache_feed_player / _cache_get_ai_player_name)
# ---------------------------------------------------------------------------

class TestPlayerCache:
    """Tests for per-port player registry used to pick the /take target."""

    PORT = 19020

    def setup_method(self):
        import ws_proxy
        ws_proxy._player_cache.pop(self.PORT, None)

    def teardown_method(self):
        import ws_proxy
        ws_proxy._player_cache.pop(self.PORT, None)

    def test_feed_player_stores_entry(self):
        from ws_proxy import _cache_feed_player, _player_cache
        _cache_feed_player(self.PORT, 0, "Romans", True)
        assert _player_cache[self.PORT][0] == {"name": "Romans", "ai": True}

    def test_feed_player_overwrites_on_update(self):
        from ws_proxy import _cache_feed_player, _player_cache
        _cache_feed_player(self.PORT, 0, "Romans", True)
        _cache_feed_player(self.PORT, 0, "Romans", False)  # ai_control changed
        assert _player_cache[self.PORT][0]["ai"] is False

    def test_remove_player_deletes_entry(self):
        from ws_proxy import _cache_feed_player, _cache_remove_player, _player_cache
        _cache_feed_player(self.PORT, 0, "Romans", True)
        _cache_remove_player(self.PORT, 0)
        assert 0 not in _player_cache.get(self.PORT, {})

    def test_remove_nonexistent_player_is_noop(self):
        from ws_proxy import _cache_remove_player
        _cache_remove_player(self.PORT, 9999)  # must not raise

    def test_get_ai_name_prefers_ai_flag(self):
        """ai_control=True player takes priority over non-AI player."""
        from ws_proxy import _cache_feed_player, _cache_get_ai_player_name
        _cache_feed_player(self.PORT, 0, "HumanPlayer", False)
        _cache_feed_player(self.PORT, 1, "AiCiv",       True)
        assert _cache_get_ai_player_name(self.PORT) == "AiCiv"

    def test_get_ai_name_falls_back_to_any_named_player(self):
        """If no AI player, return first player that has a name."""
        from ws_proxy import _cache_feed_player, _cache_get_ai_player_name
        _cache_feed_player(self.PORT, 0, "HumanPlayer", False)
        assert _cache_get_ai_player_name(self.PORT) == "HumanPlayer"

    def test_get_ai_name_skips_empty_names(self):
        """Players with empty name strings are not returned."""
        from ws_proxy import _cache_feed_player, _cache_get_ai_player_name
        _cache_feed_player(self.PORT, 0, "",       True)   # AI but no name
        _cache_feed_player(self.PORT, 1, "Romans", True)
        assert _cache_get_ai_player_name(self.PORT) == "Romans"

    def test_get_ai_name_returns_none_when_empty(self):
        from ws_proxy import _cache_get_ai_player_name
        assert _cache_get_ai_player_name(self.PORT) is None

    def test_get_ai_name_returns_none_when_all_names_empty(self):
        from ws_proxy import _cache_feed_player, _cache_get_ai_player_name
        _cache_feed_player(self.PORT, 0, "", True)
        _cache_feed_player(self.PORT, 1, "", False)
        assert _cache_get_ai_player_name(self.PORT) is None


# ---------------------------------------------------------------------------
# ws_proxy.py — CivBridge observer /take trigger
# ---------------------------------------------------------------------------

class TestCivBridgeObserverTake:
    """Tests for the proactive /take sent to late-joining observers.

    These tests exercise _server_reader_loop() end-to-end with mocked
    WebSocket and TCP connections so that no live server is needed.
    """

    PORT = 19030

    def setup_method(self):
        import ws_proxy
        ws_proxy._tile_cache.pop(self.PORT, None)
        ws_proxy._player_cache.pop(self.PORT, None)

    def teardown_method(self):
        import ws_proxy
        ws_proxy._tile_cache.pop(self.PORT, None)
        ws_proxy._player_cache.pop(self.PORT, None)

    def _locked_tile_cache(self):
        """Return a tile cache dict that appears already locked (prior connection)."""
        return {
            "map_info": '{"pid":17,"xsize":10,"ysize":10}',
            "tiles":    ['{"pid":15,"tile":0}'],
            "cities":   {},
            "locked":   True,
        }

    def _make_bridge(self):
        import ws_proxy
        mock_ws     = _MockWS()
        mock_writer = _MockWriter()
        bridge = ws_proxy.CivBridge(mock_ws, "observer", self.PORT, "key-test")
        bridge._writer = mock_writer
        return bridge, mock_ws, mock_writer

    def _tcp_written_str(self, mock_writer: _MockWriter) -> str:
        return mock_writer.written.decode("utf-8", errors="ignore")

    # -- unit check on __init__ flag --

    def test_take_sent_flag_initialises_false(self):
        bridge, _, _ = self._make_bridge()
        assert bridge._take_sent is False

    # -- /take is sent for late observers --

    @pytest.mark.asyncio
    async def test_late_observer_sends_take_after_injection(self):
        """When tile cache was already locked, proxy sends /take <ai> over TCP."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        ws_proxy._player_cache[self.PORT] = {
            0: {"name": "Romans", "ai": True}
        }
        bridge, _, mock_writer = self._make_bridge()
        bridge._reader = _MockReader(_make_tcp_frame({"pid": 1}))  # PROCESSING_FINISHED

        await bridge._server_reader_loop()

        written = self._tcp_written_str(mock_writer)
        assert "/take Romans" in written, (
            f"Expected /take Romans in TCP output; got: {written!r}"
        )

    @pytest.mark.asyncio
    async def test_late_observer_take_uses_ai_player_not_human(self):
        """/take targets the AI player even when a human player is also cached."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        ws_proxy._player_cache[self.PORT] = {
            0: {"name": "HumanSlot", "ai": False},
            1: {"name": "AiCiv",     "ai": True},
        }
        bridge, _, mock_writer = self._make_bridge()
        bridge._reader = _MockReader(_make_tcp_frame({"pid": 1}))

        await bridge._server_reader_loop()

        written = self._tcp_written_str(mock_writer)
        assert "/take AiCiv"    in written
        assert "/take HumanSlot" not in written

    # -- /take is NOT sent for the first (host) connection --

    @pytest.mark.asyncio
    async def test_first_connection_does_not_send_take(self):
        """The host connection populates the tile cache — it must not trigger /take."""
        import ws_proxy
        # Cache exists but is NOT yet locked (this is the first connection)
        ws_proxy._tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles":    ['{"pid":15}'],
            "cities":   {},
            "locked":   False,
        }
        ws_proxy._player_cache[self.PORT] = {0: {"name": "Romans", "ai": True}}

        bridge, _, mock_writer = self._make_bridge()
        bridge._reader = _MockReader(_make_tcp_frame({"pid": 1}))

        await bridge._server_reader_loop()

        written = self._tcp_written_str(mock_writer)
        assert "/take" not in written, (
            f"Expected no /take for first connection; got: {written!r}"
        )

    # -- idempotency: /take sent exactly once --

    @pytest.mark.asyncio
    async def test_take_sent_flag_prevents_duplicate(self):
        """Multiple PROCESSING_FINISHED packets produce exactly one /take."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        ws_proxy._player_cache[self.PORT] = {0: {"name": "Romans", "ai": True}}

        bridge, _, mock_writer = self._make_bridge()
        bridge._reader = _MockReader(
            _make_tcp_frame({"pid": 1}),   # first  PROCESSING_FINISHED → /take sent
            _make_tcp_frame({"pid": 1}),   # second PROCESSING_FINISHED → no resend
        )

        await bridge._server_reader_loop()

        written = self._tcp_written_str(mock_writer)
        assert written.count("/take Romans") == 1, (
            f"Expected exactly one /take; got {written.count('/take Romans')} in: {written!r}"
        )

    # -- graceful degradation when player cache is empty --

    @pytest.mark.asyncio
    async def test_no_take_when_player_cache_empty(self):
        """If no player name is cached yet, /take is skipped gracefully."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        # _player_cache intentionally left empty

        bridge, _, mock_writer = self._make_bridge()
        bridge._reader = _MockReader(_make_tcp_frame({"pid": 1}))

        await bridge._server_reader_loop()

        written = self._tcp_written_str(mock_writer)
        assert "/take" not in written

    # -- PLAYER_INFO packets flowing through the loop populate the registry --

    @pytest.mark.asyncio
    async def test_player_info_in_stream_populates_cache_and_triggers_take(self):
        """PLAYER_INFO arriving before PROCESSING_FINISHED is used for /take."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        # No player cache pre-seeded — player info arrives in the stream

        bridge, _, mock_writer = self._make_bridge()
        bridge._reader = _MockReader(
            _make_tcp_frame(
                {"pid": 51, "playerno": 0, "name": "Greeks", "ai_control": True}
            ),
            _make_tcp_frame({"pid": 1}),   # PROCESSING_FINISHED
        )

        await bridge._server_reader_loop()

        # Player cache populated
        players = ws_proxy._player_cache.get(self.PORT, {})
        assert players.get(0, {}).get("name") == "Greeks"

        # /take used the name from the stream
        written = self._tcp_written_str(mock_writer)
        assert "/take Greeks" in written

    @pytest.mark.asyncio
    async def test_player_remove_in_stream_deletes_from_cache(self):
        """PLAYER_REMOVE cleans up the player registry."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        ws_proxy._player_cache[self.PORT] = {0: {"name": "Romans", "ai": True}}

        bridge, _, _ = self._make_bridge()
        bridge._reader = _MockReader(
            _make_tcp_frame({"pid": 50, "playerno": 0}),  # PLAYER_REMOVE
            _make_tcp_frame({"pid": 1}),
        )

        await bridge._server_reader_loop()

        players = ws_proxy._player_cache.get(self.PORT, {})
        assert 0 not in players

    # -- CITY_INFO packets flowing through the loop populate the city cache --

    @pytest.mark.asyncio
    async def test_city_info_in_stream_populates_city_cache(self):
        """CITY_INFO packets arriving in the stream update the city cache."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles":    ['{"pid":15}'],
            "cities":   {},
            "locked":   False,   # host connection: populates cache
        }

        bridge, _, _ = self._make_bridge()
        bridge._reader = _MockReader(
            _make_tcp_frame({"pid": 31, "id": 5, "name": "Sparta", "owner": 0}),
            _make_tcp_frame({"pid": 1}),
        )

        await bridge._server_reader_loop()

        cities = ws_proxy._tile_cache.get(self.PORT, {}).get("cities", {})
        assert 5 in cities
        assert "Sparta" in cities[5]

    @pytest.mark.asyncio
    async def test_city_remove_in_stream_evicts_from_cache(self):
        """CITY_REMOVE cleans up the city entry."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles":    ['{"pid":15}'],
            "cities":   {5: '{"pid":31,"id":5,"name":"Sparta"}'},
            "locked":   True,
        }

        bridge, _, _ = self._make_bridge()
        bridge._reader = _MockReader(
            _make_tcp_frame({"pid": 30, "city_id": 5}),  # CITY_REMOVE
            _make_tcp_frame({"pid": 1}),
        )

        await bridge._server_reader_loop()

        cities = ws_proxy._tile_cache.get(self.PORT, {}).get("cities", {})
        assert 5 not in cities

    # -- tile+city bundle injected to client WebSocket --

    @pytest.mark.asyncio
    async def test_injection_bundle_sent_to_websocket(self):
        """The replay bundle (tiles + cities) is sent to the WebSocket client."""
        import ws_proxy
        ws_proxy._tile_cache[self.PORT] = self._locked_tile_cache()
        ws_proxy._tile_cache[self.PORT]["cities"] = {
            3: '{"pid":31,"id":3,"name":"Carthage"}'
        }
        ws_proxy._player_cache[self.PORT] = {0: {"name": "Romans", "ai": True}}

        bridge, mock_ws, _ = self._make_bridge()
        bridge._reader = _MockReader(_make_tcp_frame({"pid": 1}))

        await bridge._server_reader_loop()

        # Find the replay bundle (largest sent text, contains pid=31)
        replay_msg = next(
            (s for s in mock_ws.sent if '"pid": 31' in s or '"pid":31' in s),
            None,
        )
        assert replay_msg is not None, "Expected a WebSocket message containing CITY_INFO"
        bundle = json.loads(replay_msg)
        city_names = [p.get("name") for p in bundle if p.get("pid") == 31]
        assert "Carthage" in city_names


# ---------------------------------------------------------------------------
# Fast pid extraction (_PID_RE regex) and pre-computed tile prefix
# ---------------------------------------------------------------------------
class TestFastPidExtraction:
    """Tests for the _PID_RE regex used to avoid full json.loads in the hot path."""

    def test_pid_re_matches_simple(self):
        from ws_proxy import _PID_RE
        m = _PID_RE.search('{"pid":15,"x":3,"y":4}')
        assert m is not None
        assert int(m.group(1)) == 15

    def test_pid_re_matches_with_space(self):
        from ws_proxy import _PID_RE
        m = _PID_RE.search('{"pid" : 17, "xsize":20}')
        assert m is not None
        assert int(m.group(1)) == 17

    def test_pid_re_matches_pid_zero(self):
        from ws_proxy import _PID_RE
        m = _PID_RE.search('{"pid":0}')
        assert m is not None
        assert int(m.group(1)) == 0

    def test_pid_re_matches_negative(self):
        from ws_proxy import _PID_RE
        m = _PID_RE.search('{"pid":-1,"data":"x"}')
        assert m is not None
        assert int(m.group(1)) == -1

    def test_pid_re_no_match(self):
        from ws_proxy import _PID_RE
        assert _PID_RE.search('{"data":42}') is None

    def test_pid_re_large_pid(self):
        from ws_proxy import _PID_RE
        m = _PID_RE.search('{"pid":999,"foo":"bar"}')
        assert int(m.group(1)) == 999


class TestTilesCachePrefix:
    """Tests for the pre-computed tiles_prefix string set by _cache_lock."""

    PORT = 19020

    def setup_method(self):
        import ws_proxy
        ws_proxy._tile_cache.pop(self.PORT, None)

    def teardown_method(self):
        import ws_proxy
        ws_proxy._tile_cache.pop(self.PORT, None)

    def test_lock_sets_tiles_prefix(self):
        from ws_proxy import _cache_feed_raw, _cache_lock, _tile_cache
        _cache_feed_raw(self.PORT, 17, '{"pid":17}')
        _cache_feed_raw(self.PORT, 15, '{"pid":15,"tile":0}')
        _cache_feed_raw(self.PORT, 15, '{"pid":15,"tile":1}')
        _cache_lock(self.PORT)
        prefix = _tile_cache[self.PORT].get("tiles_prefix")
        assert prefix is not None
        assert '{"pid":0}' in prefix
        assert '{"pid":17}' in prefix
        assert '{"pid":15,"tile":0}' in prefix
        assert '{"pid":15,"tile":1}' in prefix

    def test_replay_uses_prefix_not_tiles_list(self):
        """_cache_get_replay uses tiles_prefix and appends dynamic cities."""
        from ws_proxy import _cache_feed_city, _cache_feed_raw, _cache_lock, _cache_get_replay
        _cache_feed_raw(self.PORT, 17, '{"pid":17}')
        _cache_feed_raw(self.PORT, 15, '{"pid":15,"tile":0}')
        _cache_lock(self.PORT)
        _cache_feed_city(self.PORT, 42, '{"pid":31,"id":42,"name":"Cairo"}')

        replay = _cache_get_replay(self.PORT)
        assert replay is not None
        data = json.loads(replay)
        pids = [p["pid"] for p in data]
        assert pids[0] == 0   # PROCESSING_STARTED
        assert 17 in pids     # MAP_INFO
        assert 15 in pids     # TILE_INFO
        assert 31 in pids     # CITY_INFO
        assert pids[-1] == 1  # PROCESSING_FINISHED
        names = [p.get("name") for p in data if p.get("pid") == 31]
        assert "Cairo" in names

    def test_replay_fallback_without_prefix(self):
        """If tiles_prefix absent (old cache), _cache_get_replay builds it inline."""
        from ws_proxy import _tile_cache, _cache_get_replay
        _tile_cache[self.PORT] = {
            "map_info": '{"pid":17}',
            "tiles":    ['{"pid":15}'],
            "cities":   {},
            "locked":   True,
            # no tiles_prefix key
        }
        replay = _cache_get_replay(self.PORT)
        assert replay is not None
        data = json.loads(replay)
        assert data[0]["pid"] == 0
        assert data[-1]["pid"] == 1

    def test_replay_no_cities(self):
        """Replay with zero cities still has correct structure."""
        from ws_proxy import _cache_feed_raw, _cache_lock, _cache_get_replay
        _cache_feed_raw(self.PORT, 17, '{"pid":17}')
        _cache_feed_raw(self.PORT, 15, '{"pid":15}')
        _cache_lock(self.PORT)
        replay = _cache_get_replay(self.PORT)
        data = json.loads(replay)
        assert data[0]["pid"] == 0
        assert data[-1]["pid"] == 1
        city_pkts = [p for p in data if p.get("pid") == 31]
        assert city_pkts == []

    def test_locked_tile_cache_updates_cities_after_lock(self):
        """Cities added after lock appear in subsequent replays (tiles_prefix unchanged)."""
        from ws_proxy import _cache_feed_raw, _cache_lock, _cache_feed_city, _cache_get_replay, _tile_cache
        _cache_feed_raw(self.PORT, 17, '{"pid":17}')
        _cache_feed_raw(self.PORT, 15, '{"pid":15}')
        _cache_lock(self.PORT)
        prefix_before = _tile_cache[self.PORT].get("tiles_prefix")

        _cache_feed_city(self.PORT, 1, '{"pid":31,"id":1,"name":"Thebes"}')
        _cache_feed_city(self.PORT, 2, '{"pid":31,"id":2,"name":"Memphis"}')

        # tiles_prefix unchanged
        assert _tile_cache[self.PORT].get("tiles_prefix") == prefix_before

        replay = _cache_get_replay(self.PORT)
        data = json.loads(replay)
        names = {p.get("name") for p in data if p.get("pid") == 31}
        assert "Thebes" in names
        assert "Memphis" in names
