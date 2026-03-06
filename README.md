# XBWorld Backend

XBWorld backend server — FastAPI game orchestrator + WebSocket proxy for the freeciv C game engine.

## Architecture

```
Browser / AI Agent
       │
       ▼
┌──────────────────────────────────────────────────────┐
│                  xbworld-server                       │
│                                                      │
│  ┌──────────────┐   ┌──────────────┐                │
│  │ FastAPI       │   │ WS Proxy     │                │
│  │ (port 8080)   │   │ (in-process) │                │
│  │ REST + SSE    │   │ WS ↔ TCP     │                │
│  └──────┬───────┘   └──────┬───────┘                │
│         │                  │                         │
│         └──────┬───────────┘                         │
│                │                                     │
│        ┌───────▼────────┐                            │
│        │ freeciv-server  │  (C binary, one per game) │
│        │ (port 6000+)    │                           │
│        └────────────────┘                            │
└──────────────────────────────────────────────────────┘
```

**How it works:**

1. **FastAPI server** (`server.py`) listens on port 8080 and provides REST APIs for game management, an SSE event stream for the observer UI, and serves static files.

2. **Game launcher** (`POST /civclientlauncher`) spawns a freeciv-server C process on an available port (6000-6100). Each game gets its own process with its own TCP port.

3. **WebSocket proxy** (`ws_proxy.py`) runs in-process inside FastAPI. Browser clients connect via `ws://host:8080/civsocket/{port}` and the proxy bridges WebSocket JSON frames to/from the freeciv-server's TCP protocol (2-byte big-endian length header + UTF-8 JSON + NUL terminator).

4. **Game client** (`game_client.py`) is a headless Python client that connects through the same WebSocket proxy. It maintains game state (units, cities, map, research, rulesets) from server packets and provides methods for sending game commands (move units, found cities, set research, etc.). Used by AI agents.

5. **Observer UI** (`/observer`) is a single-page dashboard that connects to the SSE event stream and displays agent activity, game state, and a live map (via iframe to the web client).

## Quick Start

### Docker (recommended)

```bash
docker build -t xbworld-backend .
docker run -p 8080:8080 xbworld-backend
```

Or with docker-compose:

```bash
docker-compose up
```

### Local Development

1. Install build dependencies and build the freeciv C server:

```bash
# Ubuntu/Debian
sudo apt install build-essential meson ninja-build pkg-config \
    libcurl4-openssl-dev libjansson-dev libicu-dev liblzma-dev \
    libzstd-dev libsqlite3-dev zlib1g-dev

cd freeciv && ./prepare_freeciv.sh
```

2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. Run the server:

```bash
python server.py                # Start on port 8080
python server.py --port 9000    # Custom port
python server.py -v             # Verbose logging
```

## API Endpoints

### Game Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/civclientlauncher?action=new` | Spawn a new freeciv-server, returns `{"port": 6000, "result": "success"}` |
| `POST` | `/civclientlauncher?civserverport=N` | Connect to existing server on port N |
| `GET` | `/servers` | List active game servers: `{"total": 1, "ports": [6000]}` |
| `GET` | `/meta/status` | Metaserver status: `ok;total;single;multi` |
| `GET` | `/game/events` | SSE stream of real-time game events |

### Legacy Client Compatibility

| Method | Path | Response |
|--------|------|----------|
| `POST/GET` | `/validate_user` | `user_does_not_exist` |
| `POST/GET` | `/login_user` | `OK` |
| `GET` | `/motd.js` | Message of the day JavaScript |

### WebSocket Proxy

| Path | Description |
|------|-------------|
| `ws://host:port/civsocket/{proxy_port}` | WebSocket-to-TCP bridge to freeciv-server |

**Protocol:** Client sends a login JSON packet as the first message containing `username` and `port` (the actual freeciv-server port). Subsequent messages are forwarded as raw JSON to the game server. Server responses arrive as JSON arrays.

### Static Pages

| Path | Description |
|------|-------------|
| `/` | Redirects to observer |
| `/observer` | AI game observer dashboard (SSE-powered) |

## Testing

```bash
# Unit tests (no server needed, tests internal logic)
pytest tests/test_unit.py -v

# Integration tests (requires running server + freeciv binary)
python server.py &
pytest tests/test_integration.py -v --timeout=120

# Legacy integration test script
python test_api.py
```

**Test coverage:**
- 65 unit tests: config validation, EventBus pub/sub, username validation, GameState filtering, all 30+ packet handlers, tile coordinate math, TCP framing protocol
- 15 integration tests: REST API endpoints, game creation/launcher, WebSocket proxy handshake through game start, full GameClient lifecycle (connect → login → ready → play → close), SSE event stream

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Server listen port |
| `FREECIV_BIN` | `~/freeciv/bin/freeciv-web` | Path to freeciv binary |
| `FREECIV_DATA_PATH` | `~/freeciv/share/freeciv/` | Path to freeciv data |
| `WEBAPP_DIR` | | Optional path to frontend webapp files |
| `XBWORLD_HOST` | `localhost` | Host for config.py URL generation |
| `XBWORLD_TLS` | | Set to `1` or `true` for HTTPS/WSS URLs |

## Project Structure

```
xbworld-server/
├── server.py              # FastAPI server + ServerManager + EventBus
├── ws_proxy.py            # In-process WebSocket-to-TCP proxy (CivBridge)
├── standalone_proxy.py    # Standalone aiohttp WS proxy (for nginx setups)
├── game_client.py         # Headless freeciv client (GameState + GameClient)
├── config.py              # URL/protocol configuration
├── test_api.py            # Legacy integration test script
├── tests/
│   ├── test_unit.py       # 65 unit tests (no server needed)
│   └── test_integration.py # 15 integration tests (needs running server)
├── data/
│   ├── web.serv           # Base server settings
│   ├── pubscript_singleplayer.serv  # Singleplayer game config
│   └── pubscript_multiplayer.serv   # Multiplayer game config
├── static/
│   ├── index.html         # Redirect to observer
│   └── observer.html      # AI game observer dashboard
├── freeciv/               # Freeciv C engine source (git submodule)
│   ├── prepare_freeciv.sh # Build script
│   └── freeciv/           # Source code (xingbo778/freeciv xbworld branch)
├── requirements.txt       # Python: aiohttp, fastapi, uvicorn, websockets
├── Dockerfile             # Multi-stage: build freeciv + slim Python runtime
├── docker-compose.yml     # Docker compose config
├── railway.toml           # Railway deployment config
└── pytest.ini             # Pytest configuration
```

## Connecting Frontend

The frontend (xbworld-web) connects to this backend via:
- HTTP API calls to `http://backend-host:8080/...`
- WebSocket connections to `ws://backend-host:8080/civsocket/{port}`

Set the `BACKEND_URL` environment variable in the frontend to point to this backend.
