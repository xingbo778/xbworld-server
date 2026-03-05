"""
Agent tools: functions the LLM can call via function-calling.

Tools are registered with the ``@tool`` decorator which auto-generates the
OpenAI-style JSON schema from the function signature and docstring.  The
``TOOL_REGISTRY`` singleton holds all registered tools and provides both
schema export and dispatch.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Callable, get_type_hints

from game_client import GameClient

logger = logging.getLogger("xbworld-agent")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class ToolEntry:
    name: str
    description: str
    fn: Callable
    parameters: dict
    is_async: bool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(self, name: str, description: str, params: dict | None = None):
        """Decorator to register a tool function."""
        def decorator(fn: Callable):
            schema = params or self._infer_params(fn)
            entry = ToolEntry(
                name=name,
                description=description,
                fn=fn,
                parameters=schema,
                is_async=asyncio.iscoroutinefunction(fn),
            )
            self._tools[name] = entry
            return fn
        return decorator

    def openai_definitions(self) -> list[dict]:
        """Return OpenAI-compatible tool definitions for all registered tools."""
        defs = []
        for entry in self._tools.values():
            defs.append({
                "type": "function",
                "function": {
                    "name": entry.name,
                    "description": entry.description,
                    "parameters": entry.parameters,
                },
            })
        return defs

    async def execute(self, client: GameClient, name: str, args: dict) -> str:
        entry = self._tools.get(name)
        if not entry:
            logger.warning("[tools] Unknown tool requested: %s", name)
            return f"Unknown tool: {name}"
        try:
            logger.debug("[tools] Executing %s(%s)", name, args)
            bound_args = self._bind_args(entry.fn, client, args)
            if entry.is_async:
                result = await entry.fn(**bound_args)
            else:
                result = entry.fn(**bound_args)
            logger.debug("[tools] %s result: %s", name, str(result)[:200])
            return result
        except Exception as e:
            logger.error("[tools] Tool %s(%s) failed: %s", name, args, e, exc_info=True)
            return f"Error: {e}"

    @staticmethod
    def _bind_args(fn: Callable, client: GameClient, args: dict) -> dict:
        sig = inspect.signature(fn)
        bound: dict = {}
        for pname, param in sig.parameters.items():
            if pname == "client":
                bound["client"] = client
            elif pname in args:
                bound[pname] = args[pname]
            elif param.default is not inspect.Parameter.empty:
                bound[pname] = param.default
        return bound

    @staticmethod
    def _infer_params(fn: Callable) -> dict:
        """Infer JSON Schema parameters from type hints."""
        hints = get_type_hints(fn)
        sig = inspect.signature(fn)
        props = {}
        required = []
        type_map = {int: "integer", float: "number", str: "string", bool: "boolean"}
        for pname, param in sig.parameters.items():
            if pname == "client":
                continue
            ptype = hints.get(pname, str)
            json_type = type_map.get(ptype, "string")
            props[pname] = {"type": json_type}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        return {"type": "object", "properties": props, "required": required}


TOOL_REGISTRY = ToolRegistry()
tool = TOOL_REGISTRY.register


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_type_name(client: GameClient, type_id: int) -> str:
    return client.state.unit_types.get(type_id, {}).get("name", f"?unit:{type_id}")


def _tech_name(client: GameClient, tech_id: int) -> str:
    return client.state.techs.get(tech_id, {}).get("name", f"tech_{tech_id}")


def _building_name(client: GameClient, building_id: int) -> str:
    return client.state.buildings.get(building_id, {}).get("name", f"building_{building_id}")


DIRECTION_NAMES = {
    "nw": 0, "northwest": 0,
    "n": 1, "north": 1,
    "ne": 2, "northeast": 2,
    "w": 3, "west": 3,
    "e": 4, "east": 4,
    "sw": 5, "southwest": 5,
    "s": 6, "south": 6,
    "se": 7, "southeast": 7,
}

DIR_DX = [-1, 0, 1, -1, 1, -1, 0, 1]
DIR_DY = [-1, -1, -1, 0, 0, 1, 1, 1]


def _resolve_unit(client: GameClient, unit_id: int):
    unit = client.state.units.get(unit_id)
    if not unit:
        return None, f"Unit {unit_id} not found."
    return unit, _unit_type_name(client, unit.get("type", -1))


# ---------------------------------------------------------------------------
# Query tools
# ---------------------------------------------------------------------------

@tool("get_game_overview", "Get a summary of the current game: turn, gold, cities, units, research.")
def get_game_overview(client: GameClient) -> str:
    s = client.state
    p = s.my_player() or {}
    gold = p.get("gold", "?")
    tax = p.get("tax", "?")
    sci = p.get("science", "?")
    lux = p.get("luxury", "?")
    gov_id = p.get("government", -1)
    gov = client.state.governments.get(gov_id, {}).get("name", "?")
    researching = s.research.get("researching", -1)
    bulbs = s.research.get("bulbs_researched", 0)
    tech_cost = s.research.get("researching_cost", 0)
    tech_nm = _tech_name(client, researching) if researching >= 0 else "None"

    inventions = s.research.get("inventions", [])
    known_techs = [t.get("name", "?") for tid, t in s.techs.items()
                   if tid < len(inventions) and inventions[tid] == 1]

    research_line = f"Researching: {tech_nm} ({bulbs}/{tech_cost} bulbs)"
    if researching < 0 or tech_nm == "None":
        research_line += " ⚠️ NO RESEARCH — call set_research_target NOW!"
    elif tech_cost > 0:
        research_line += f" [{int(bulbs/tech_cost*100)}%]"

    return (
        f"Turn {s.turn} | Phase: {s.phase}\n"
        f"Gold: {gold} | Tax: {tax}% Sci: {sci}% Lux: {lux}%\n"
        f"Government: {gov}\n"
        f"Cities: {len(s.my_cities())} | Units: {len(s.my_units())}\n"
        f"{research_line}\n"
        f"Known techs ({len(known_techs)}): {', '.join(known_techs[:10]) if known_techs else 'None'}\n"
        f"Players: {len(s.players)}"
    )


@tool("get_my_cities", "List all my cities with name, population, and current production.")
def get_my_cities(client: GameClient) -> str:
    lines = []
    for cid, c in client.state.my_cities().items():
        name = c.get("name", "?")
        size = c.get("size", "?")
        pk, pv = c.get("production_kind", -1), c.get("production_value", -1)
        prod = _unit_type_name(client, pv) if pk == 1 else _building_name(client, pv) if pk == 0 else "?"
        lines.append(f"  [{cid}] {name} (pop {size}) producing {prod} ({c.get('shield_stock', 0)} shields)")
    return "My cities:\n" + "\n".join(lines) if lines else "No cities."


@tool("get_my_units", "List all my units with type, location, HP, and movement points.")
def get_my_units(client: GameClient) -> str:
    lines = []
    for uid, u in client.state.my_units().items():
        tn = _unit_type_name(client, u.get("type", -1))
        lines.append(f"  [{uid}] {tn} @ tile {u.get('tile', '?')} HP:{u.get('hp', '?')} MP:{u.get('movesleft', '?')} activity:{u.get('activity', 0)}")
    return "My units:\n" + "\n".join(lines) if lines else "No units."


@tool("get_research_status", "Get current research progress and known techs.")
def get_research_status(client: GameClient) -> str:
    r = client.state.research
    researching = r.get("researching", -1)
    tech_nm = _tech_name(client, researching) if researching >= 0 else "None"
    inventions = r.get("inventions", [])
    known = [t.get("name", f"tech_{tid}") for tid, t in client.state.techs.items()
             if tid < len(inventions) and inventions[tid] == 1]
    bulbs = r.get("bulbs_researched", 0)
    cost = r.get("researching_cost", 0)
    status = f"Researching: {tech_nm} ({bulbs}/{cost} bulbs)"
    if researching < 0 or tech_nm == "None":
        status += "\n⚠️ NO ACTIVE RESEARCH — you MUST call set_research_target now!"
    elif cost > 0:
        pct = int(bulbs / cost * 100)
        status += f" [{pct}% complete]"
    status += f"\nKnown techs: {', '.join(known[:20]) if known else 'None'}"
    if known:
        status += f" ({len(known)} total)"
    return status


@tool("get_visible_enemies", "Get all visible enemy units with type, location, and HP.")
def get_visible_enemies(client: GameClient) -> str:
    my_id = client.state.my_player_id
    enemies = []
    for uid, u in client.state.units.items():
        owner = u.get("owner")
        if owner is not None and owner != my_id:
            tn = _unit_type_name(client, u.get("type", -1))
            owner_name = client.state.players.get(owner, {}).get("name", f"player_{owner}")
            enemies.append(
                f"  [{uid}] {tn} owner={owner_name} @ tile {u.get('tile', '?')} HP:{u.get('hp', '?')}"
            )
    if not enemies:
        return "No visible enemy units."
    return f"Visible enemies ({len(enemies)}):\n" + "\n".join(enemies)


@tool("get_recent_messages", "Get the most recent game messages and chat.",
      params={"type": "object", "properties": {"count": {"type": "integer", "description": "Number of messages"}}, "required": []})
def get_recent_messages(client: GameClient, count: int = 10) -> str:
    msgs = client.state.messages[-count:]
    if not msgs:
        return "No messages."
    return "Recent messages:\n" + "\n".join(f"  [{m.get('type', '?')}] {m.get('text', '')}" for m in msgs)


# ---------------------------------------------------------------------------
# Action tools
# ---------------------------------------------------------------------------

@tool("send_command", "Send a raw XBWorld server command (e.g. '/set tax 30', '/start').",
      params={"type": "object", "properties": {"command": {"type": "string", "description": "Server command"}}, "required": ["command"]})
async def send_command(client: GameClient, command: str) -> str:
    await client.send_chat(command)
    return f"Sent: {command}"


@tool("end_turn", "End the current turn.")
async def end_turn(client: GameClient) -> str:
    await client.end_turn()
    return f"Turn {client.state.turn} ended."


@tool("set_tax_rates", "Set tax/luxury/science rates (must sum to 100).",
      params={"type": "object", "properties": {
          "tax": {"type": "integer", "description": "Tax rate %"},
          "luxury": {"type": "integer", "description": "Luxury rate %"},
          "science": {"type": "integer", "description": "Science rate %"},
      }, "required": ["tax", "luxury", "science"]})
async def set_tax_rates(client: GameClient, tax: int, luxury: int, science: int) -> str:
    if tax + luxury + science != 100:
        return f"Error: rates must sum to 100, got {tax + luxury + science}"
    await client.set_rates(tax, luxury, science)
    return f"Rates set: tax={tax}% luxury={luxury}% science={science}%"


@tool("set_research_target", "Set research target by tech name. Stick with it until completed!",
      params={"type": "object", "properties": {"tech_name": {"type": "string", "description": "Technology name"}}, "required": ["tech_name"]})
async def set_research_target(client: GameClient, tech_name: str) -> str:
    inventions = client.state.research.get("inventions", [])
    for tid, t in client.state.techs.items():
        if t.get("name", "").lower() == tech_name.lower():
            if tid < len(inventions) and inventions[tid] == 1:
                return f"Tech '{t.get('name')}' is already known. Pick a different tech."
            await client.set_research(tid)
            cost = client.state.research.get("researching_cost", 0)
            return f"Now researching: {t.get('name')} (cost: {cost} bulbs). Stick with it until completed!"
    available = [t.get("name", "?") for tid, t in client.state.techs.items()
                 if tid < len(inventions) and inventions[tid] != 1][:10]
    return f"Tech '{tech_name}' not found. Available: {', '.join(available)}"


@tool("change_city_production", "Change what a city is producing by name.",
      params={"type": "object", "properties": {
          "city_id": {"type": "integer", "description": "City ID"},
          "production_name": {"type": "string", "description": "Unit or building name"},
      }, "required": ["city_id", "production_name"]})
async def change_city_production(client: GameClient, city_id: int, production_name: str) -> str:
    pn = production_name.strip().lower()
    city = client.state.cities.get(city_id)
    cur_kind = city.get("production_kind", -1) if city else -1
    cur_value = city.get("production_value", -1) if city else -1

    for uid, ut in client.state.unit_types.items():
        name = ut.get("name", "").lower()
        if name == pn or name == pn + "s" or name.rstrip("s") == pn:
            if cur_kind == 1 and cur_value == uid:
                return f"City {city_id} already producing unit: {ut.get('name')} — no change needed."
            await client.city_change_production(city_id, 1, uid)
            return f"City {city_id} now producing unit: {ut.get('name')}"
    for bid, b in client.state.buildings.items():
        name = b.get("name", "").lower()
        if name == pn or name == pn + "s" or name.rstrip("s") == pn:
            if cur_kind == 0 and cur_value == bid:
                return f"City {city_id} already producing building: {b.get('name')} — no change needed."
            await client.city_change_production(city_id, 0, bid)
            return f"City {city_id} now producing building: {b.get('name')}"
    avail_units = [ut.get("name", "?") for ut in client.state.unit_types.values()][:15]
    return f"Production '{production_name}' not found. Available units: {', '.join(avail_units)}"


@tool("buy_city_production", "Buy the current production in a city instantly with gold.",
      params={"type": "object", "properties": {"city_id": {"type": "integer", "description": "City ID"}}, "required": ["city_id"]})
async def buy_city_production(client: GameClient, city_id: int) -> str:
    await client.city_buy(city_id)
    return f"Bought production in city {city_id}."


# ---------------------------------------------------------------------------
# Unit action tools
# ---------------------------------------------------------------------------

def _terrain_name(client: GameClient, terrain_id: int) -> str:
    return client.state.terrains.get(terrain_id, {}).get("name", f"terrain_{terrain_id}")


@tool("get_tile_info", "Get terrain info for a tile (useful before founding a city).",
      params={"type": "object", "properties": {"tile_id": {"type": "integer", "description": "Tile ID"}}, "required": ["tile_id"]})
def get_tile_info(client: GameClient, tile_id: int) -> str:
    tile = client.state.tiles.get(tile_id)
    if not tile:
        return f"Tile {tile_id} not known (not yet explored)."
    terrain_id = tile.get("terrain", -1)
    terrain = _terrain_name(client, terrain_id)
    continent = tile.get("continent", 0)
    extras = tile.get("extras", [])
    return f"Tile {tile_id}: terrain={terrain}, continent={continent}, extras={extras}"


@tool("move_unit", "Move a unit one tile in a direction: N, NE, E, SE, S, SW, W, NW.",
      params={"type": "object", "properties": {
          "unit_id": {"type": "integer", "description": "Unit ID"},
          "direction": {"type": "string", "description": "Direction: N, NE, E, SE, S, SW, W, NW"},
      }, "required": ["unit_id", "direction"]})
async def move_unit(client: GameClient, unit_id: int, direction: str) -> str:
    d = direction.strip().lower()
    dir_num = DIRECTION_NAMES.get(d)
    if dir_num is None:
        try:
            dir_num = int(d)
        except ValueError:
            return f"Invalid direction '{direction}'. Use: N, NE, E, SE, S, SW, W, NW."
    if not 0 <= dir_num <= 7:
        return f"Direction must be 0-7, got {dir_num}."
    unit, type_name = _resolve_unit(client, unit_id)
    if not unit:
        return type_name
    await client.unit_move(unit_id, dir_num)
    return f"Moved {type_name} [{unit_id}] direction {direction}."


@tool("found_city", "Found a new city with a Settler unit on its current tile. You MUST provide a city_name. Make sure the settler is far enough from existing cities (at least 4 tiles away).",
      params={"type": "object", "properties": {
          "unit_id": {"type": "integer", "description": "Settler unit ID"},
          "city_name": {"type": "string", "description": "Name for the new city (REQUIRED — server rejects empty names)"},
      }, "required": ["unit_id", "city_name"]})
async def found_city(client: GameClient, unit_id: int, city_name: str = "") -> str:
    unit, type_name = _resolve_unit(client, unit_id)
    if not unit:
        return type_name
    if "settler" not in type_name.lower():
        return f"Unit {unit_id} is a {type_name}, not a Settler."
    tile = unit.get("tile")
    mp = unit.get("movesleft", 0)
    if mp <= 0:
        return (f"CANNOT found city: Settler [{unit_id}] has 0 movement points. "
                f"Do NOT move a settler and found a city in the same turn. "
                f"Call end_turn and found the city next turn when it has MP.")

    xsize = client.state.map_info.get("xsize", 0)
    if xsize > 0:
        tile_data = client.state.tiles.get(tile, {})
        tx = tile_data.get("x", tile % xsize if xsize else 0)
        ty = tile_data.get("y", tile // xsize if xsize else 0)
        for cid, c in client.state.my_cities().items():
            ct = c.get("tile", -1)
            ct_data = client.state.tiles.get(ct, {})
            cx = ct_data.get("x", ct % xsize if xsize else 0)
            cy = ct_data.get("y", ct // xsize if xsize else 0)
            dist = abs(tx - cx) + abs(ty - cy)
            if dist < 4:
                return (f"TOO CLOSE: Settler [{unit_id}] at tile {tile} is only {dist} tiles from "
                        f"city '{c.get('name', '?')}'. Move the settler at least 4 tiles away "
                        f"from all existing cities before founding. Use auto_explore_unit or "
                        f"move_unit to relocate it.")

    if not city_name or not city_name.strip():
        city_name = f"City{len(client.state.my_cities()) + 1}"
    from urllib.parse import quote
    encoded_name = quote(city_name.strip(), safe="")
    cities_before = len(client.state.my_cities())
    await client.unit_found_city(unit_id, encoded_name)
    await asyncio.sleep(0.8)
    cities_after = len(client.state.my_cities())
    if cities_after > cities_before:
        return f"SUCCESS: City '{city_name}' founded at tile {tile}. Now have {cities_after} cities."
    if unit_id not in client.state.units:
        return f"SUCCESS: Settler consumed, city '{city_name}' likely founded at tile {tile}."
    tile_data = client.state.tiles.get(tile, {})
    terrain_id = tile_data.get("terrain", -1)
    terrain = _terrain_name(client, terrain_id)
    continent = tile_data.get("continent", 0)
    return (f"FAILED: Settler [{unit_id}] at tile {tile} (MP={mp}, terrain={terrain}, continent={continent}). "
            f"City NOT founded — tile may be invalid (ocean/existing city/too close to another city). "
            f"Move the settler at least 4-5 tiles away from existing cities and try next turn.")


@tool("fortify_unit", "Fortify a military unit for a defense bonus.",
      params={"type": "object", "properties": {"unit_id": {"type": "integer", "description": "Unit ID"}}, "required": ["unit_id"]})
async def fortify_unit(client: GameClient, unit_id: int) -> str:
    unit, type_name = _resolve_unit(client, unit_id)
    if not unit:
        return type_name
    await client.unit_fortify(unit_id)
    return f"{type_name} [{unit_id}] fortifying."


@tool("auto_explore_unit", "Set a unit to auto-explore the map automatically.",
      params={"type": "object", "properties": {"unit_id": {"type": "integer", "description": "Unit ID"}}, "required": ["unit_id"]})
async def auto_explore_unit(client: GameClient, unit_id: int) -> str:
    unit, type_name = _resolve_unit(client, unit_id)
    if not unit:
        return type_name
    await client.unit_auto_explore(unit_id)
    return f"{type_name} [{unit_id}] set to auto-explore."


@tool("disband_unit", "Disband (permanently remove) a unit.",
      params={"type": "object", "properties": {"unit_id": {"type": "integer", "description": "Unit ID"}}, "required": ["unit_id"]})
async def disband_unit(client: GameClient, unit_id: int) -> str:
    unit, type_name = _resolve_unit(client, unit_id)
    if not unit:
        return type_name
    await client.unit_disband(unit_id)
    return f"{type_name} [{unit_id}] disbanded."


@tool("sentry_unit", "Put a unit on sentry duty. Alerts when enemies approach.",
      params={"type": "object", "properties": {"unit_id": {"type": "integer", "description": "Unit ID"}}, "required": ["unit_id"]})
async def sentry_unit(client: GameClient, unit_id: int) -> str:
    unit, type_name = _resolve_unit(client, unit_id)
    if not unit:
        return type_name
    await client.unit_sentry(unit_id)
    return f"{type_name} [{unit_id}] on sentry."


# ---------------------------------------------------------------------------
# Batch tools — reduce round-trips for multi-unit/multi-city actions
# ---------------------------------------------------------------------------

@tool("move_units", "Move multiple units in one call. More efficient than calling move_unit repeatedly.",
      params={"type": "object", "properties": {
          "moves": {"type": "array", "description": "List of {unit_id, direction} objects",
                    "items": {"type": "object",
                              "properties": {
                                  "unit_id": {"type": "integer", "description": "Unit ID"},
                                  "direction": {"type": "string", "description": "Direction: N, NE, E, SE, S, SW, W, NW"},
                              }, "required": ["unit_id", "direction"]}}
      }, "required": ["moves"]})
async def move_units(client: GameClient, moves: list) -> str:
    results = []
    for m in moves:
        uid = m.get("unit_id")
        d = str(m.get("direction", "")).strip().lower()
        dir_num = DIRECTION_NAMES.get(d)
        if dir_num is None:
            results.append(f"Unit {uid}: invalid direction '{m.get('direction')}'")
            continue
        unit, type_name = _resolve_unit(client, uid)
        if not unit:
            results.append(f"Unit {uid}: not found")
            continue
        await client.unit_move(uid, dir_num)
        results.append(f"Unit {uid} ({type_name}): moved {m.get('direction')}")
    return "\n".join(results)


@tool("set_productions", "Set production for multiple cities in one call.",
      params={"type": "object", "properties": {
          "productions": {"type": "array", "description": "List of {city_id, production_name} objects",
                          "items": {"type": "object",
                                    "properties": {
                                        "city_id": {"type": "integer", "description": "City ID"},
                                        "production_name": {"type": "string", "description": "Unit or building name"},
                                    }, "required": ["city_id", "production_name"]}}
      }, "required": ["productions"]})
async def set_productions(client: GameClient, productions: list) -> str:
    results = []
    for p in productions:
        cid = p.get("city_id")
        pname = str(p.get("production_name", "")).strip().lower()
        found = False
        for uid, ut in client.state.unit_types.items():
            name = ut.get("name", "").lower()
            if name == pname or name == pname + "s" or name.rstrip("s") == pname:
                await client.city_change_production(cid, 1, uid)
                results.append(f"City {cid}: now producing {ut.get('name')}")
                found = True
                break
        if not found:
            for bid, b in client.state.buildings.items():
                name = b.get("name", "").lower()
                if name == pname or name == pname + "s" or name.rstrip("s") == pname:
                    await client.city_change_production(cid, 0, bid)
                    results.append(f"City {cid}: now producing {b.get('name')}")
                    found = True
                    break
        if not found:
            results.append(f"City {cid}: '{p.get('production_name')}' not found")
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Dispatch (for backward compatibility)
# ---------------------------------------------------------------------------

async def execute_tool(client: GameClient, tool_name: str, args: dict) -> str:
    return await TOOL_REGISTRY.execute(client, tool_name, args)
