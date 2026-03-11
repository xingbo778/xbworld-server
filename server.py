#!/usr/bin/env python3
"""
XBWorld Game Server — freeciv engine + WebSocket proxy.

Serves:
- Game server process management (freeciv-server)
- In-process WebSocket proxy (ws_proxy.py)
- Game launcher API
- Metaserver status API
- Static web client files (optional)

Usage:
    python server.py                    # Start server on port 8080
    python server.py --port 8000        # Custom port
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from ws_proxy import handle_civsocket

logger = logging.getLogger("xbworld-server")

PROJECT_ROOT = Path(__file__).resolve().parent
_webapp_env = os.getenv("WEBAPP_DIR", "")
WEBAPP_DIR = Path(_webapp_env) if _webapp_env else Path("/nonexistent")  # Optional: set to serve frontend files


# ---------------------------------------------------------------------------
# Server Process Manager
# ---------------------------------------------------------------------------
class ServerManager:
    """Manages freeciv-server processes.

    The WebSocket proxy is now in-process (ws_proxy.py), so we only need
    to spawn freeciv-server C processes.
    """

    def __init__(self):
        self._servers: dict[int, subprocess.Popen] = {}
        self._log_files: dict[int, object] = {}
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
        """Spawn a freeciv-server. Returns server port."""
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

        log_fh = open(log_file, "w")
        self._servers[port] = subprocess.Popen(
            [freeciv_bin, "--debug", "1", "--port", str(port),
             "--Announce", "none",
             "--read", serv_script],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(data_dir),
        )
        self._log_files[port] = log_fh

        logger.info("Spawned freeciv-server on port %d (pid %d), log=%s",
                     port, self._servers[port].pid, log_file)

        time.sleep(1)
        rc = self._servers[port].poll()
        if rc is not None:
            log_fh.close()
            self._log_files.pop(port, None)
            log_content = log_file.read_text()[:2000] if log_file.exists() else "(no log)"
            logger.error("freeciv-server exited immediately (code %d): %s", rc, log_content)
            self._servers.pop(port, None)
            raise RuntimeError(f"freeciv-server crashed on startup (exit {rc}): {log_content[:500]}")

        return port

    def kill_game(self, port: int):
        proc = self._servers.pop(port, None)
        log_fh = self._log_files.pop(port, None)
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
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass
        # Clear stale tile/city/player cache so a restarted server on this
        # port does not replay data from the previous game to new observers.
        try:
            from ws_proxy import cache_clear_port
            cache_clear_port(port)
        except Exception as e:
            logger.warning("Failed to clear ws_proxy cache for port %d: %s", port, e)

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
# Global instances
# ---------------------------------------------------------------------------
server_mgr = ServerManager()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    server_mgr.kill_all()


app = FastAPI(title="XBWorld Game Server", lifespan=lifespan)

# --- CORS middleware ---
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

    # For observe action, join an existing running game if available
    if action == "observe":
        status = server_mgr.status()
        if status["ports"]:
            port = status["ports"][0]
            return JSONResponse(
                content={"port": port, "result": "success"},
                headers={"result": "success", "port": str(port),
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

    # For observe action with a newly spawned game, auto-start it with AI players
    if action == "observe":
        asyncio.create_task(_autostart_game(port))

    return JSONResponse(
        content={"port": port, "result": "success"},
        headers={"result": "success", "port": str(port),
                 "Access-Control-Expose-Headers": expose},
    )


async def _autostart_game(port: int):
    """Connect internally, start the game, and stay connected until it ends.

    Staying connected prevents freeciv from ending the session when the only
    human player leaves (singleplayer mode). autotoggle handles turns for us.
    """
    from game_client import GameClient
    client = GameClient(username="host")
    try:
        await client.join_game(port)
        await asyncio.sleep(2.0)
        await client.send_chat("/set timeout 60")
        await asyncio.sleep(0.3)
        await client.send_chat("/start")
        logger.info("[autostart] Started game on port %d, host staying connected", port)
        # Stay connected for the lifetime of the game (autotoggle handles turns)
        await client.wait_for_new_turn(timeout=86400.0)
    except Exception as e:
        logger.warning("[autostart] game on port %d ended: %s", port, e)
    finally:
        await client.close()


# --- Server management API ---

@app.get("/servers")
async def api_servers():
    return server_mgr.status()


@app.post("/game/restart")
async def api_game_restart():
    """Kill all running games and start a fresh singleplayer game.
    Used to warm up the tile cache when connecting to a mid-game server."""
    status = server_mgr.status()
    for port in list(status.get("ports", [])):
        try:
            server_mgr.kill_game(port)  # also clears ws_proxy cache for this port
        except Exception as e:
            logger.warning("Failed to kill game on port %d: %s", port, e)
    # Start a fresh singleplayer game
    try:
        port = server_mgr.spawn_game("singleplayer")
        await asyncio.sleep(1.5)
        asyncio.create_task(_autostart_game(port))
        return {"status": "ok", "port": port}
    except Exception as e:
        logger.error("Failed to start new game after restart: %s", e)
        return {"status": "error", "message": str(e)}


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
    parser = argparse.ArgumentParser(description="XBWorld Game Server")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("PORT", "8080")),
                        help="HTTP server port (default $PORT or 8080)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"[XBWorld] Starting game server on {args.host}:{args.port}")
    print(f"[XBWorld] Open http://localhost:{args.port} in your browser")

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
