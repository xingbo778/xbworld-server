# Freeciv Engine Upgrade: 3.2 → 3.4

## Overview

Upgrade the freeciv game engine from 3.2.92 (xbworld branch) to 3.3.90-dev (targeting 3.4.0 stable, origin/main).

**Goal**: Support 8 AI agents playing together via the Python GameClient + WebSocket proxy.

## What Changed

### Engine Version
| Item | Before | After |
|------|--------|-------|
| Freeciv version | 3.2.92.3-dev | 3.3.90.10-dev |
| Network capstring | `+Freeciv.Web.Devel-3.3` | `+Freeciv.Web.Devel-3.4` |
| Ruleset format | 40 (RSFORMAT_3_3) | 50 (RSFORMAT_3_4) |
| Server binary | freeciv-web | freeciv-web (same name) |

### Upstream Changes (1109 commits)
- **10+ crash fixes**: nullptr dereferences, dead unit contacts, illegal action lists
- **AI improvements**: Better war countdown, autoworker shelter-seeking, paratrooper counting
- **New features**: Tiledefs, government flags, superspecialists, TiledefConnected requirement
- **Protocol**: 5 new packets (516-520), 4 removed web packets (287-290)
- **Code modernization**: NULL→nullptr throughout, Lua 5.4.8→5.5 support

### Breaking Changes Applied

#### Ruleset (11 files in `data/xbworld/`)
1. `format_version = 40` → `50` (all .ruleset files)
2. Capstring: `+Freeciv-ruleset-3.3-Devel-2023.Feb.24` → `+Freeciv-ruleset-3.4-Devel-2025.Jan.17`
3. Removed `civil_war_enabled = TRUE` from game.ruleset (now via effects)
4. Removed `homeless_gold_upkeep = FALSE` from game.ruleset (deprecated)
5. Fixed `low_firepower_pearl_harbour` → `low_firepower_pearl_harbor`
6. Added `[control]` section to governments.ruleset (required for gov flags)
7. Converted `Upkeep_Factor` → `Upkeep_Pct` (value × 100: 1→100)

#### Python Layer (`config.py`)
- `FREECIV_VERSION`: `+Freeciv.Web.Devel-3.3` → `+Freeciv.Web.Devel-3.4`
- `MAJOR/MINOR/PATCH_VERSION`: `3.1.90` → `3.3.90`

#### No changes needed in:
- `game_client.py` — All packet IDs (4, 5, 11, 15-17, 25, 31, 51, 63, 115, 128, etc.) are unchanged
- `ws_proxy.py` — TCP framing protocol is identical
- `server.py` — Game process management unchanged

### Freeciv-Web Patches Applied
These patches from the original xbworld branch were cherry-picked onto origin/main:

| Patch | Purpose | Status |
|-------|---------|--------|
| featured_text (web format) | `<>` tags instead of `[]`, `font` instead of `c` | ✅ Clean |
| text_fixes | Web player naming (username = player name) | ✅ Clean |
| combat veterancy fix | Fix combat promotion chance | ✅ Clean |
| metachange | Meta server adjustments | ✅ Clean |
| savegame web | Web save/load with user directories | ✅ Conflict resolved |
| server_password | Web auth support | ✅ Clean |
| longturn | Long turn settings | ✅ Clean |
| meson_webperimental | Build webperimental ruleset | ✅ Clean |
| webgl_vision_cheat | Full map visibility in web mode | ✅ Clean |
| capstring | `+Freeciv.Web.Devel-3.4` | ✅ Manual edit |

### Patches Skipped (not needed for agents)
| Patch | Reason |
|-------|--------|
| goto_fcweb | Adds WEB_GOTO_PATH packets (287-290) removed in 3.4. Only used by web frontend. |
| maphand_ch | Adds web map info packets, also uses removed web packets. Frontend-only. |
| unit_server_side_agent | Minor optimization, conflict in unithand.c. |
| scorelog_filenames | Minor, conflict in report.c. |
| load_command_confirmation | Minor, .orig file conflict. |
| endgame-mapimg | Endgame map image, conflict in srv_main.c. |
| stdsounds_format | Sound format, irrelevant for agents. |
| RevertAmplio2ExtraUnits | Tileset, irrelevant for server. |
| action-selection-airlift | Frontend UI, irrelevant for agents. |

## Network Protocol Changes

### Packet IDs (unchanged for GameClient)
All packet IDs used by `game_client.py` remain identical between 3.3 and 3.4:
- Login: 4 (JOIN_REQ), 5 (JOIN_REPLY), 115 (CONN_INFO)
- Game: 16 (GAME_INFO), 17 (MAP_INFO), 128 (BEGIN_TURN), 129 (END_TURN)
- Units: 63 (UNIT_INFO), 64 (UNIT_SHORT_INFO), 62 (UNIT_REMOVE)
- Cities: 31 (CITY_INFO), 32 (CITY_SHORT_INFO), 30 (CITY_REMOVE)
- Players: 51 (PLAYER_INFO), 60 (RESEARCH_INFO)
- Rulesets: 140 (UNIT), 144 (TECH), 145 (GOV), 150 (BUILDING), 151 (TERRAIN)

### New Packets (not used by GameClient, informational)
- 516: EDIT_FOGOFWAR_STATE
- 517/518: SYNC_SERIAL / SYNC_SERIAL_REPLY
- 519: RULESET_GOV_FLAG
- 520: RULESET_TILEDEF

### Modified Packet Fields
- `GAME_INFO` (16): Removed `homeless_gold_upkeep`, `civil_war_enabled`
- `CITY_INFO` (31): Added `wl_cb` (worklist cancel behavior), `acquire_type`
- `RULESET_UNIT` (140): Added `spectype_id`
- `RULESET_GOVERNMENT` (145): Added `flags` (gov flags bitvector)

The GameClient doesn't reference any removed fields and gracefully ignores unknown new fields (JSON dict access).

## Build Instructions

```bash
# Clean build from xbworld-3.4 branch
cd freeciv
rm -rf build
./prepare_freeciv.sh

# Copy updated rulesets to install directory
cp freeciv/data/xbworld/*.ruleset ~/freeciv/share/freeciv/xbworld/
cp freeciv/data/xbworld/*.lua ~/freeciv/share/freeciv/xbworld/
```

## Testing

### Verified Working
- ✅ freeciv-web binary builds successfully (meson + ninja)
- ✅ xbworld ruleset loads without errors
- ✅ Game creation via REST API (`POST /civclientlauncher?action=new`)
- ✅ WebSocket proxy handshake (login → join_reply → conn_info)
- ✅ GameClient full lifecycle: connect → login → ready → game start → playing
- ✅ Map generation: 84×56 tiles, 6 starting units
- ✅ Packet processing: 8209 packets in 3 seconds

### Test Command
```bash
# Start server
PORT=8090 python server.py &

# Create game
curl -X POST "http://localhost:8090/civclientlauncher?action=new"

# Test with GameClient
PORT=8090 XBWORLD_HOST=localhost python -c "
import asyncio
from game_client import GameClient

async def test():
    client = GameClient(username='test34')
    await client.join_game(6000)
    for i in range(15):
        await asyncio.sleep(1)
        if client.state.my_conn_id >= 0:
            print(f'Connected! player_id={client.state.my_player_id}')
            break
    await client.player_ready()
    for i in range(20):
        await asyncio.sleep(1)
        if client.state.phase == 'playing':
            print(f'Game started! Units: {len(client.state.my_units())}')
            break
    await client.close()

asyncio.run(test())
"
```

## 8-Agent Multiplayer Setup

To run 8 agents playing together:

```bash
# 1. Start server
python server.py &

# 2. Create a multiplayer game
curl -X POST "http://localhost:8080/civclientlauncher?action=multi"
# Returns: {"port": 6000, "result": "success"}

# 3. Connect 8 agents
PORT=8080 XBWORLD_HOST=localhost python -c "
import asyncio
from game_client import GameClient

async def run_agents():
    agents = []
    for i in range(8):
        agent = GameClient(username=f'agent{i}')
        await agent.join_game(6000)
        agents.append(agent)
        await asyncio.sleep(0.5)

    # Wait for all to connect
    await asyncio.sleep(5)

    # All ready
    for agent in agents:
        await agent.player_ready()

    # Game loop
    while True:
        await asyncio.sleep(1)
        for agent in agents:
            if agent.state.phase == 'playing':
                # Agent logic here: move units, build cities, research...
                await agent.end_turn()

asyncio.run(run_agents())
"
```

### Key Configuration for 8 Players
In `data/pubscript_multiplayer.serv`:
- `set aifill 0` — No AI fill, all 8 slots for agents
- `set timeout 60` — 60 second turn timer
- `set minplayers 2` — Minimum 2 to start
- `set generator FAIR` — Fair start positions

Or in `data/pubscript_singleplayer.serv`:
- `set aifill 8` — Fill remaining with AI (agents replace AI on connect)

## Branch Structure

```
origin/main (freeciv 3.3.90-dev → 3.4.0)
  └── xbworld-3.4 (our branch)
        ├── Apply freeciv-web patches (8 patches)
        ├── Remove .orig backup files
        ├── Apply webgl_vision_cheat
        └── Add xbworld ruleset + capstring for 3.4

Server repo (upgrade-freeciv-3.4 branch):
  ├── Update config.py for 3.4 protocol
  └── Update freeciv submodule pointer
```

## Risk Notes

- The `Can't use definition: invalid value for option 'format': 'png'` warning on startup is cosmetic (mapimg format), does not affect gameplay
- `webgl_vision_cheat` gives full map visibility — useful for agents but changes game dynamics
- Some Lua API functions changed in 3.4 (new methods added, deprecation warnings). The xbworld `script.lua` and `parser.lua` should be tested if they use Lua extensively
