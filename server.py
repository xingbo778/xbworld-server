#!/usr/bin/env python3
"""
Unified XBWorld server — a single FastAPI process serving everything.

Serves:
- Static web client files
- Game launcher API
- Metaserver status API
- AI agent management API
- In-process WebSocket proxy (ws_proxy.py)
- Game server process management

Usage:
    python server.py                    # Start server on port 8080
    python server.py --port 8000        # Custom port
    python server.py --agents 4         # Auto-start a 4-agent game
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from config import (
    LLM_MODEL, LLM_API_KEY, LLM_BASE_URL,
)
from game_client import GameClient
from agent import XBWorldAgent, DEFAULT_SYSTEM_PROMPT
from ws_proxy import handle_civsocket

logger = logging.getLogger("xbworld-server")

PROJECT_ROOT = Path(__file__).resolve().parent
_webapp_env = os.getenv("WEBAPP_DIR", "")
WEBAPP_DIR = Path(_webapp_env) if _webapp_env else Path("/nonexistent")  # Optional: set to serve frontend files

STRATEGY_PROMPT_TEMPLATE = """You are an expert XBWorld player AI agent named "{name}". You control a civilization and make strategic decisions each turn.

Your strategic personality: {strategy}

Your capabilities:
- Query game state (cities, units, research, messages)
- Send server commands (e.g. /set tax 30, /start, /save)
- Change city production, set research targets, adjust tax rates
- Move units, found cities, fortify, explore, disband, sentry
- End turns when done

When no instructions are given, play autonomously following your strategic personality.
Always be concise. Respond in the same language as the user."""


# ---------------------------------------------------------------------------
# Server Process Manager (replaces publite2)
# ---------------------------------------------------------------------------
class ServerManager:
    """Manages freeciv-server processes.

    The WebSocket proxy is now in-process (ws_proxy.py), so we only need
    to spawn freeciv-server C processes.
    """

    def __init__(self):
        self._servers: dict[int, subprocess.Popen] = {}
        self._log_dir = PROJECT_ROOT / "logs"
        self._log_dir.mkdir(exist_ok=True)

    def _find_free_port(self, start: int = 6000, end: int = 6100) -> int:
        for port in range(start, end):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError(f"No free port in {start}-{end}")

    def spawn_game(self, game_type: str = "multiplayer") -> int:
        """Spawn a freeciv-server. Returns server port.

        No separate proxy process is needed — the WebSocket proxy runs
        in-process via ws_proxy.py.
        """
        port = self._find_free_port()
        logger.info("Found free port %d for %s game", port, game_type)

        freeciv_bin = os.getenv("FREECIV_BIN", os.path.expanduser("~/freeciv/bin/freeciv-web"))
        freeciv_data = os.getenv("FREECIV_DATA_PATH", os.path.expanduser("~/freeciv/share/freeciv/"))
        logger.info("freeciv binary: %s, data: %s", freeciv_bin, freeciv_data)

        env = {**os.environ, "FREECIV_DATA_PATH": freeciv_data}

        data_dir = PROJECT_ROOT / "data"
        serv_script = str(data_dir / f"pubscript_{game_type}.serv")
        log_file = self._log_dir / f"server-{port}.log"

        if not Path(freeciv_bin).exists():
            raise RuntimeError(f"freeciv binary not found: {freeciv_bin}")
        if not Path(serv_script).exists():
            raise RuntimeError(f"serv script not found: {serv_script}")

        self._servers[port] = subprocess.Popen(
            [freeciv_bin, "--debug", "1", "--port", str(port),
             "--Announce", "none",
             "--read", serv_script],
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(data_dir),
        )

        logger.info("Spawned freeciv-server on port %d (pid %d), log=%s",
                     port, self._servers[port].pid, log_file)

        time.sleep(1)
        rc = self._servers[port].poll()
        if rc is not None:
            log_content = log_file.read_text()[:2000] if log_file.exists() else "(no log)"
            logger.error("freeciv-server exited immediately (code %d): %s", rc, log_content)
            self._servers.pop(port, None)
            raise RuntimeError(f"freeciv-server crashed on startup (exit {rc}): {log_content[:500]}")

        return port

    def kill_game(self, port: int):
        proc = self._servers.pop(port, None)
        if proc and proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            logger.info("Stopped server for port %d", port)

    def kill_all(self):
        for port in list(self._servers.keys()):
            self.kill_game(port)

    def status(self) -> dict:
        active = []
        for port, proc in list(self._servers.items()):
            if proc.poll() is None:
                active.append(port)
            else:
                self._servers.pop(port, None)
        return {
            "total": len(active),
            "single": 0,
            "multi": len(active),
            "ports": active,
        }


# ---------------------------------------------------------------------------
# Event Bus for SSE observer
# ---------------------------------------------------------------------------
class EventBus:
    """Simple pub/sub for server-sent events to observer clients."""

    def __init__(self, max_history: int = 200):
        self._subscribers: list[asyncio.Queue] = []
        self._history: list[dict] = []
        self._max_history = max_history

    def publish(self, event: dict):
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        for evt in self._history[-20:]:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                break
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)


event_bus = EventBus()


# ---------------------------------------------------------------------------
# Agent Orchestrator
# ---------------------------------------------------------------------------
class AgentOrchestrator:
    def __init__(self, server_mgr: ServerManager):
        self.server_mgr = server_mgr
        self.agents: dict[str, XBWorldAgent] = {}
        self.clients: dict[str, GameClient] = {}
        self.server_port: int = -1
        self._tasks: list[asyncio.Task] = []

    async def create_game(self, agent_configs: list[dict], server_port: int = None,
                          aifill: int = 0):
        logger.info("Creating game with %d agents, server_port=%s, aifill=%d",
                     len(agent_configs), server_port, aifill)
        if self.agents:
            logger.info("Shutting down existing game before creating new one")
            await self.shutdown()

        first_client = GameClient(username=agent_configs[0]["name"])

        if server_port:
            self.server_port = server_port
            await first_client.join_game(server_port)
        else:
            port = self.server_mgr.spawn_game("multiplayer")
            await asyncio.sleep(2)
            self.server_port = port
            await first_client.join_game(port)

        self.clients[agent_configs[0]["name"]] = first_client
        await asyncio.sleep(2)

        if not first_client.state.connected:
            raise ConnectionError("First agent failed to connect")

        for cfg in agent_configs[1:]:
            client = GameClient(username=cfg["name"])
            await client.join_game(self.server_port)
            self.clients[cfg["name"]] = client
            await asyncio.sleep(1)

        for cfg in agent_configs:
            client = self.clients[cfg["name"]]
            strategy = cfg.get("strategy", "balanced play")
            llm_model = cfg.get("llm_model")
            prompt = STRATEGY_PROMPT_TEMPLATE.format(name=cfg["name"], strategy=strategy)
            agent = XBWorldAgent(client, name=cfg["name"],
                                 system_prompt=prompt, llm_model=llm_model,
                                 event_bus=event_bus)
            self.agents[cfg["name"]] = agent

        first_client = self.clients[agent_configs[0]["name"]]
        total_players = len(agent_configs) + aifill
        if aifill > 0:
            await first_client.send_chat(f"/set aifill {total_players}")
            await asyncio.sleep(0.5)
        await first_client.send_chat("/set timeout 0")
        await asyncio.sleep(0.5)

        for name, client in self.clients.items():
            await client.send_chat("/start")
            await asyncio.sleep(0.3)

        for i in range(15):
            await asyncio.sleep(1)
            if any(c.state.turn >= 1 for c in self.clients.values()):
                break

        for name, agent in self.agents.items():
            task = asyncio.create_task(agent.run_game_loop())
            self._tasks.append(task)
            logger.info("Agent '%s' game loop started", name)

    async def shutdown(self):
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for agent in self.agents.values():
            await agent.close()
        for client in self.clients.values():
            await client.close()
        self.clients.clear()
        self.agents.clear()
        if self.server_port > 0:
            self.server_mgr.kill_game(self.server_port)
        self.server_port = -1


# ---------------------------------------------------------------------------
# Global instances
# ---------------------------------------------------------------------------
server_mgr = ServerManager()
orchestrator = AgentOrchestrator(server_mgr)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await orchestrator.shutdown()
    server_mgr.kill_all()


app = FastAPI(title="XBWorld Server", lifespan=lifespan)

# --- CORS middleware (allow local frontend to connect to remote backend) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["port", "result"],
)


# --- Metaserver API ---

@app.get("/meta/status", response_class=PlainTextResponse)
async def meta_status():
    """Metaserver status endpoint.
    Returns semicolon-separated: ok;total;single;multi"""
    s = server_mgr.status()
    return f"ok;{s['total']};{s['single']};{s['multi']}"




# --- User validation stubs (legacy client expects these) ---

@app.post("/validate_user", response_class=PlainTextResponse)
@app.get("/validate_user", response_class=PlainTextResponse)
async def validate_user(request: Request):
    return "user_does_not_exist"


@app.post("/login_user", response_class=PlainTextResponse)
@app.get("/login_user", response_class=PlainTextResponse)
async def login_user(request: Request):
    return "OK"


# --- Game Launcher API ---

@app.post("/civclientlauncher")
async def civclient_launcher(request: Request):
    """Launch a new game server or connect to existing one.
    Compatible with the JS client which reads 'port' and 'result' from response headers."""
    params = dict(request.query_params)
    action = params.get("action", "new")
    existing_port = params.get("civserverport")

    expose = "port, result"

    if existing_port:
        return JSONResponse(
            content={"port": int(existing_port), "result": "success"},
            headers={"result": "success", "port": str(existing_port),
                     "Access-Control-Expose-Headers": expose},
        )

    game_type = "multiplayer" if action == "multi" else "singleplayer"

    try:
        port = server_mgr.spawn_game(game_type)
    except Exception as e:
        return JSONResponse(
            content={"error": str(e)},
            headers={"result": "error", "Access-Control-Expose-Headers": expose},
            status_code=500,
        )

    await asyncio.sleep(1.5)
    return JSONResponse(
        content={"port": port, "result": "success"},
        headers={"result": "success", "port": str(port),
                 "Access-Control-Expose-Headers": expose},
    )


# --- Agent Management API ---

@app.post("/game/create")
async def api_create_game(body: dict):
    agent_configs = body.get("agents", [])
    if not agent_configs:
        raise HTTPException(400, "Must provide at least one agent config")

    for i, cfg in enumerate(agent_configs):
        if isinstance(cfg, str):
            agent_configs[i] = {"name": cfg}
        elif "name" not in cfg:
            raise HTTPException(400, f"Agent config at index {i} missing 'name'")

    try:
        await orchestrator.create_game(
            agent_configs,
            server_port=body.get("server_port"),
            aifill=body.get("aifill", 0),
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    return {
        "status": "ok",
        "server_port": orchestrator.server_port,
        "observe_url": f"/webclient/index.html?action=observe&civserverport={orchestrator.server_port}",
        "agents": [c["name"] for c in agent_configs],
    }


@app.get("/game/status")
async def api_game_status():
    if not orchestrator.agents:
        return {"status": "no_game", "agents": []}
    return {
        "status": "running",
        "server_port": orchestrator.server_port,
        "agents": [a.get_status() for a in orchestrator.agents.values()],
    }


@app.delete("/game")
async def api_delete_game():
    await orchestrator.shutdown()
    return {"status": "ok"}


@app.get("/agents/{name}/state")
async def api_agent_state(name: str):
    agent = orchestrator.agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    return agent.get_status()


@app.post("/agents/{name}/command")
async def api_agent_command(name: str, body: dict):
    agent = orchestrator.agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    command = body.get("command", "")
    if not command:
        raise HTTPException(400, "Must provide 'command' field")
    result = await agent.submit_command(command)
    return {"status": "ok", "message": result}


@app.get("/agents/{name}/log")
async def api_agent_log(name: str, limit: int = 50):
    agent = orchestrator.agents.get(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    return {"name": name, "log": agent.action_log[-limit:]}


# --- Server management API ---

@app.get("/servers")
async def api_servers():
    return server_mgr.status()


# --- SSE Event Stream for Observer ---

@app.get("/game/events")
async def game_events():
    """Server-Sent Events stream for the observer UI."""
    queue = event_bus.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Observer UI ---

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/observer", response_class=HTMLResponse)
@app.get("/observer.html", response_class=HTMLResponse)
async def observer_page():
    """Serve the AI game observer dashboard."""
    observer_path = STATIC_DIR / "observer.html"
    if observer_path.exists():
        return observer_path.read_text()
    return HTMLResponse("<h1>Observer not found</h1>", status_code=404)


# --- In-process WebSocket proxy ---

@app.websocket("/civsocket/{proxy_port}")
async def ws_civsocket(ws: WebSocket, proxy_port: int):
    """WebSocket proxy endpoint — bridges browser to freeciv-server via TCP.
    The proxy_port in the URL is kept for client compatibility but the actual
    server port is determined from the login packet."""
    await handle_civsocket(ws, proxy_port)


# --- Static file serving ---
# Serve the legacy web client directly from the webapp directory.
# The legacy client uses webclient.min.js (pre-built JS bundle) with jQuery
# and the 2D Canvas renderer — no Vite build needed.

if WEBAPP_DIR.exists():
    logger.info("Serving legacy web client from %s", WEBAPP_DIR)
    for subdir in ["css", "javascript", "images", "static", "fonts",
                    "textures", "tileset", "music", "docs"]:
        path = WEBAPP_DIR / subdir
        if path.exists():
            app.mount(f"/{subdir}", StaticFiles(directory=str(path)), name=subdir)
            app.mount(f"/src/main/webapp/{subdir}", StaticFiles(directory=str(path)), name=f"compat_{subdir}")
    webclient_dir = WEBAPP_DIR / "webclient"
    if webclient_dir.exists():
        app.mount("/webclient", StaticFiles(directory=str(webclient_dir), html=True), name="webclient")


@app.get("/motd.js")
async def motd_js():
    motd_path = WEBAPP_DIR / "motd.js"
    if motd_path.exists():
        return PlainTextResponse(motd_path.read_text(), media_type="application/javascript")
    return PlainTextResponse("var defined_motd = 'Welcome to XBWorld!';", media_type="application/javascript")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the game client index page."""
    raw_index = WEBAPP_DIR / "webclient" / "index.html"
    if raw_index.exists():
        return raw_index.read_text()
    return HTMLResponse("<h1>XBWorld</h1><p><a href='/webclient/index.html'>Launch Game</a></p>")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="XBWorld Unified Server")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("PORT", "8080")),
                        help="HTTP server port (default $PORT or 8080)")
    parser.add_argument("--agents", type=int, default=0,
                        help="Auto-start a game with N agents")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"[XBWorld] Starting server on {args.host}:{args.port}")
    print(f"[XBWorld] Open http://localhost:{args.port} in your browser")
    if args.agents > 0:
        print(f"[XBWorld] Will auto-start {args.agents}-agent game after server is ready")

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    async def run():
        task = asyncio.create_task(server.serve())
        if args.agents > 0:
            await asyncio.sleep(3)
            agent_configs = [{"name": f"agent{i+1}"} for i in range(args.agents)]
            try:
                await orchestrator.create_game(agent_configs)
                print(f"[XBWorld] Game started with {args.agents} agents on port {orchestrator.server_port}")
            except Exception as e:
                print(f"[XBWorld] Failed to auto-start game: {e}")
        await task

    asyncio.run(run())


if __name__ == "__main__":
    main()
