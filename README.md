# XBWorld Backend

XBWorld backend server — FastAPI game orchestrator + WebSocket proxy + freeciv C engine.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  XBWorld Backend                      │
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
│        │ freeciv-server  │                           │
│        │ (port 6000+)    │                           │
│        └────────────────┘                            │
└──────────────────────────────────────────────────────┘
```

## Quick Start

### Docker

```bash
docker build -t xbworld-backend .
docker run -p 8080:8080 xbworld-backend
```

Or with docker-compose:

```bash
docker-compose up
```

### Local Development

1. Build the freeciv C server:

```bash
cd freeciv
./prepare_freeciv.sh
```

2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. Run the server:

```bash
python server.py
```

Auto-start a game with AI agents:

```bash
python server.py --agents 4
```

## API Endpoints

### Game Management

| Method | Path | Description |
|--------|------|-------------|
| POST | `/game/create` | Create game with agents |
| GET | `/game/status` | Status of all agents |
| DELETE | `/game` | Shut down game |
| GET | `/game/events` | SSE stream of real-time events |

### Agent Control

| Method | Path | Description |
|--------|------|-------------|
| GET | `/agents/{name}/state` | Agent status summary |
| POST | `/agents/{name}/command` | Natural language command |
| POST | `/agents/{name}/actions` | Direct tool execution |
| GET | `/agents/{name}/log` | Action log |

### WebSocket Proxy

| Path | Description |
|------|-------------|
| `ws://host:port/civsocket/{port}` | WebSocket proxy to freeciv-server |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Server listen port |
| `FREECIV_BIN` | `~/freeciv/bin/freeciv-web` | Path to freeciv binary |
| `FREECIV_DATA_PATH` | `~/freeciv/share/freeciv/` | Path to freeciv data |
| `LLM_MODEL` | `openai/gemini-3-flash-preview` | LLM model for agents |
| `LLM_API_KEY` | | API key for LLM provider |
| `LLM_BASE_URL` | | LLM API base URL |
| `GAME_TURN_TIMEOUT` | `30` | Turn timeout in seconds |
| `WEBAPP_DIR` | | Optional path to frontend webapp files |

## Project Structure

```
xbworld-backend/
├── server.py              # FastAPI server (main entry point)
├── ws_proxy.py            # WebSocket-to-TCP proxy
├── standalone_proxy.py    # Standalone WS proxy (aiohttp)
├── agent.py               # AI agent logic
├── agent_tools.py         # Agent tool definitions
├── game_client.py         # Freeciv game client
├── decision_engine.py     # Decision engine interface
├── llm_providers.py       # LLM provider integrations
├── config.py              # Configuration
├── state_api.py           # Game state API
├── main.py                # Single-agent CLI runner
├── multi_main.py          # Multi-agent CLI runner
├── run_remote.py          # Remote game runner
├── requirements.txt       # Python dependencies
├── data/                  # Server scripts (.serv files)
├── freeciv/               # Freeciv C engine (git submodule)
├── static/                # Observer UI
├── Dockerfile             # Docker build
└── docker-compose.yml     # Docker compose config
```

## Connecting Frontend

The frontend (xbworld-web) connects to this backend via:
- HTTP API calls to `http://backend-host:8080/...`
- WebSocket connections to `ws://backend-host:8080/civsocket/{port}`

Set the `BACKEND_URL` environment variable in the frontend to point to this backend.
