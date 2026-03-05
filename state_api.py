"""
Structured game state API for XBWorld.

Provides JSON-serializable state representations and turn deltas,
optimized for both LLM consumption and external agent APIs.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from game_client import GameClient

logger = logging.getLogger("xbworld-agent")


def _unit_type_name(client: GameClient, type_id: int) -> str:
    return client.state.unit_types.get(type_id, {}).get("name", f"unit_{type_id}")


def _tech_name(client: GameClient, tech_id: int) -> str:
    return client.state.techs.get(tech_id, {}).get("name", f"tech_{tech_id}")


def _building_name(client: GameClient, building_id: int) -> str:
    return client.state.buildings.get(building_id, {}).get("name", f"building_{building_id}")


def _terrain_name(client: GameClient, terrain_id: int) -> str:
    return client.state.terrains.get(terrain_id, {}).get("name", f"terrain_{terrain_id}")


def _city_json(client: GameClient, city: dict) -> dict:
    pk = city.get("production_kind", -1)
    pv = city.get("production_value", -1)
    if pk == 1:
        prod = _unit_type_name(client, pv)
    elif pk == 0:
        prod = _building_name(client, pv)
    else:
        prod = "?"
    return {
        "id": city.get("id"),
        "name": city.get("name", "?"),
        "size": city.get("size", 0),
        "tile": city.get("tile"),
        "owner": city.get("owner"),
        "production": prod,
        "production_kind": pk,
        "production_value": pv,
        "shield_stock": city.get("shield_stock", 0),
        "food_stock": city.get("food_stock", 0),
    }


def _unit_json(client: GameClient, unit: dict) -> dict:
    return {
        "id": unit.get("id"),
        "type": unit.get("type"),
        "type_name": _unit_type_name(client, unit.get("type", -1)),
        "tile": unit.get("tile"),
        "owner": unit.get("owner"),
        "owner_name": client.state.players.get(unit.get("owner", -1), {}).get("name", "?"),
        "hp": unit.get("hp", 0),
        "movesleft": unit.get("movesleft", 0),
        "activity": unit.get("activity", 0),
        "veteran": unit.get("veteran", 0),
    }


def _research_json(client: GameClient) -> dict:
    r = client.state.research
    researching = r.get("researching", -1)
    inventions = r.get("inventions", [])
    known = [t.get("name", f"tech_{tid}") for tid, t in client.state.techs.items()
             if tid < len(inventions) and inventions[tid] == 1]
    return {
        "researching_id": researching,
        "researching_name": _tech_name(client, researching) if researching >= 0 else None,
        "bulbs_researched": r.get("bulbs_researched", 0),
        "researching_cost": r.get("researching_cost", 0),
        "known_techs": known[:30],
    }


def _diplomacy_json(client: GameClient) -> list[dict]:
    my_id = client.state.my_player_id
    result = []
    for pid, p in client.state.players.items():
        if pid == my_id:
            continue
        result.append({
            "player_id": pid,
            "name": p.get("name", f"player_{pid}"),
            "is_alive": p.get("is_alive", True),
            "nation": p.get("nation"),
        })
    return result


def game_state_to_json(client: GameClient) -> dict:
    """Convert the full game state to a structured JSON dict."""
    s = client.state
    p = s.my_player() or {}
    gov_id = p.get("government", -1)
    gov_name = client.state.governments.get(gov_id, {}).get("name", "?")

    return {
        "turn": s.turn,
        "year": s.year,
        "phase": s.phase,
        "player": {
            "id": s.my_player_id,
            "gold": p.get("gold", 0),
            "tax": p.get("tax", 0),
            "science": p.get("science", 0),
            "luxury": p.get("luxury", 0),
            "government": gov_name,
            "government_id": gov_id,
        },
        "cities": [_city_json(client, c) for c in s.my_cities().values()],
        "units": [_unit_json(client, u) for u in s.my_units().values()],
        "research": _research_json(client),
        "visible_enemies": [
            _unit_json(client, u) for u in s.units.values()
            if u.get("owner") != s.my_player_id
        ],
        "diplomacy": _diplomacy_json(client),
        "map": {
            "xsize": s.map_info.get("xsize", 0),
            "ysize": s.map_info.get("ysize", 0),
            "known_tiles": len(s.tiles),
        },
        "stats": {
            "total_cities": len(s.my_cities()),
            "total_units": len(s.my_units()),
            "total_players": len(s.players),
        },
    }


def compute_turn_delta(prev_state: dict | None, curr_state: dict) -> dict:
    """Compute what changed between two state snapshots.

    Returns a dict describing new/lost cities, new/lost units, research
    progress, gold change, and enemy movements.
    """
    if prev_state is None:
        return {"is_first": True, **curr_state}

    delta: dict[str, Any] = {
        "turn": curr_state["turn"],
        "prev_turn": prev_state["turn"],
    }

    prev_gold = prev_state.get("player", {}).get("gold", 0)
    curr_gold = curr_state.get("player", {}).get("gold", 0)
    delta["gold_change"] = curr_gold - prev_gold

    prev_city_ids = {c["id"] for c in prev_state.get("cities", [])}
    curr_city_ids = {c["id"] for c in curr_state.get("cities", [])}
    delta["new_cities"] = [c for c in curr_state.get("cities", []) if c["id"] not in prev_city_ids]
    delta["lost_cities"] = [cid for cid in prev_city_ids if cid not in curr_city_ids]

    prev_unit_ids = {u["id"] for u in prev_state.get("units", [])}
    curr_unit_ids = {u["id"] for u in curr_state.get("units", [])}
    delta["new_units"] = [u for u in curr_state.get("units", []) if u["id"] not in prev_unit_ids]
    delta["lost_units"] = [uid for uid in prev_unit_ids if uid not in curr_unit_ids]

    prev_research = prev_state.get("research", {}).get("researching_name")
    curr_research = curr_state.get("research", {}).get("researching_name")
    if prev_research != curr_research:
        delta["research_changed"] = {"from": prev_research, "to": curr_research}

    prev_enemies = {e["id"] for e in prev_state.get("visible_enemies", [])}
    curr_enemies = {e["id"] for e in curr_state.get("visible_enemies", [])}
    delta["new_enemies_visible"] = [
        e for e in curr_state.get("visible_enemies", []) if e["id"] not in prev_enemies
    ]
    delta["enemies_disappeared"] = len(prev_enemies - curr_enemies)

    return delta


class StateTracker:
    """Tracks per-agent state snapshots for delta computation."""

    def __init__(self):
        self._snapshots: dict[str, dict] = {}

    def snapshot(self, agent_name: str, client: GameClient) -> tuple[dict, dict]:
        """Take a snapshot and return (current_state, delta_from_previous)."""
        current = game_state_to_json(client)
        prev = self._snapshots.get(agent_name)
        delta = compute_turn_delta(prev, current)
        self._snapshots[agent_name] = copy.deepcopy(current)
        return current, delta
