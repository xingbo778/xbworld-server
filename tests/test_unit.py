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
        assert FREECIV_VERSION == "+Freeciv.Web.Devel-3.3"
        assert MAJOR_VERSION == 3
        assert MINOR_VERSION == 1
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
