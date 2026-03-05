#!/usr/bin/env python3
"""
Multi-Agent XBWorld — multiple LLM agents playing as different players.

Usage:
    # 3 agents with default names
    python multi_main.py --agents 3

    # Named agents with strategy hints
    python multi_main.py --agents alpha:aggressive,beta:defensive,gamma:economic

    # From JSON config
    python multi_main.py --config agents.json

    # Join existing game
    python multi_main.py --agents 2 --join 6001

agents.json example:
[
  {"name": "alpha", "strategy": "aggressive military expansion"},
  {"name": "beta",  "strategy": "defensive turtle with science focus"},
  {"name": "gamma", "strategy": "economic and diplomatic", "llm_model": "gpt-4o-mini"}
]
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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from config import (
    LAUNCHER_URL, API_HOST, API_PORT, NGINX_HOST, NGINX_PORT,
    LLM_MODEL, LLM_API_KEY, LLM_BASE_URL, GAME_TURN_TIMEOUT,
)
from game_client import GameClient
from agent import XBWorldAgent, DEFAULT_SYSTEM_PROMPT
from agent_tools import TOOL_REGISTRY, execute_tool
from state_api import game_state_to_json, StateTracker
from decision_engine import ToolCall, ExternalEngine

logger = logging.getLogger("xbworld-multi")

STRATEGY_PROMPT_TEMPLATE = """You are an expert XBWorld player AI agent named "{name}". You control a civilization and make strategic decisions each turn.

Your strategic personality: {strategy}

Your capabilities:
- Query game state (cities, units, research, messages)
- Send server commands (e.g. /set tax 30, /start, /save)
- Change city production, set research targets, adjust tax rates
- End turns when done

When the user gives you instructions in natural language, interpret them and execute the appropriate actions.
When no instructions are given, play autonomously following your strategic personality.

Always be concise in your reports. Use the tools to gather information before making decisions.
Respond in the same language as the user (Chinese if they speak Chinese, English if English)."""


# ---------------------------------------------------------------------------
# Global game state shared across the API
# ---------------------------------------------------------------------------
def _find_free_port(start: int = 6000, end: int = 6100) -> int:
    """Find a free TCP port in the given range."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in {start}-{end}")


class EventBus:
    """Simple pub/sub for SSE game events."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event: dict):
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class GameOrchestrator:
    def __init__(self):
        self.agents: dict[str, XBWorldAgent] = {}
        self.clients: dict[str, GameClient] = {}
        self.external_clients: dict[str, GameClient] = {}
        self.server_port: int = -1
        self._tasks: list[asyncio.Task] = []
        self._server_proc: subprocess.Popen | None = None
        self._proxy_proc: subprocess.Popen | None = None
        self.events = EventBus()
        self.state_tracker = StateTracker()

    def _spawn_server(self, port: int) -> None:
        """Start a freeciv-server + standalone aiohttp WebSocket proxy for CLI mode.

        When running via server.py, the WebSocket proxy is in-process and
        only the C server needs to be spawned. In standalone CLI mode we
        launch the aiohttp-based standalone_proxy.py as a separate process.
        """
        freeciv_bin = os.path.expanduser("~/freeciv/bin/freeciv-web")
        freeciv_data = os.path.expanduser("~/freeciv/share/freeciv/")
        project_root = Path(__file__).resolve().parent
        proxy_script = Path(__file__).resolve().parent / "standalone_proxy.py"
        log_dir = project_root / "logs"
        log_dir.mkdir(exist_ok=True)

        env = {**os.environ, "FREECIV_DATA_PATH": freeciv_data}

        proxy_port = 1000 + port
        self._proxy_proc = subprocess.Popen(
            [sys.executable, str(proxy_script), str(proxy_port)],
            stdout=open(log_dir / f"proxy-{proxy_port}.log", "w"),
            stderr=subprocess.STDOUT,
            env=env,
        )

        serv_script = str(project_root / "data" / "pubscript_multiplayer.serv")
        self._server_proc = subprocess.Popen(
            [freeciv_bin, "--debug", "1", "--port", str(port),
             "--Announce", "none", "--exit-on-end", "--quitidle", "120",
             "--read", serv_script],
            stdout=open(log_dir / f"server-{port}.log", "w"),
            stderr=subprocess.STDOUT,
            env=env,
        )
        logger.info("Spawned freeciv-server on port %d, proxy on %d (pids %d, %d)",
                     port, proxy_port, self._server_proc.pid, self._proxy_proc.pid)

    def _kill_spawned(self):
        for proc, label in [(self._server_proc, "server"), (self._proxy_proc, "proxy")]:
            if proc and proc.poll() is None:
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                logger.info("Stopped spawned %s (pid %d)", label, proc.pid)
        self._server_proc = None
        self._proxy_proc = None

    async def create_game(self, agent_configs: list[dict],
                          server_port: int = None,
                          aifill: int = 0,
                          standalone: bool = False,
                          turn_timeout: int | None = None):
        """Create a multiplayer game and connect all agents.

        If *standalone* is True, spawn freeciv-server + proxy directly
        (no Tomcat / publite2 / MariaDB needed).
        """
        if self.agents:
            await self.shutdown()

        first_client = GameClient(username=agent_configs[0]["name"])

        if server_port:
            self.server_port = server_port
            await first_client.join_game(server_port)
        elif standalone:
            port = _find_free_port()
            self._spawn_server(port)
            await asyncio.sleep(2)
            self.server_port = port
            await first_client.join_game(port)
        else:
            await first_client.start_new_game("multiplayer")
            self.server_port = first_client.server_port

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
                                 event_bus=self.events)
            self.agents[cfg["name"]] = agent

        first_name = agent_configs[0]["name"]
        first_client = self.clients[first_name]
        total_players = len(agent_configs) + aifill
        if aifill > 0:
            await first_client.send_chat(f"/set aifill {total_players}")
            await asyncio.sleep(0.5)
        effective_timeout = turn_timeout if turn_timeout is not None else GAME_TURN_TIMEOUT
        await first_client.send_chat(f"/set timeout {effective_timeout}")
        await asyncio.sleep(0.5)

        logger.info("All %d agents connected to port %d. Starting game...",
                     len(agent_configs), self.server_port)

        for name, client in self.clients.items():
            await client.send_chat("/start")
            await asyncio.sleep(0.3)

        for i in range(15):
            await asyncio.sleep(1)
            turns = {n: c.state.turn for n, c in self.clients.items()}
            logger.debug("Waiting for game start... %s", turns)
            if any(t >= 1 for t in turns.values()):
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
        for client in self.external_clients.values():
            await client.close()
        self.clients.clear()
        self.external_clients.clear()
        self.agents.clear()
        self._kill_spawned()
        self.server_port = -1

    def get_agent(self, name: str) -> XBWorldAgent:
        agent = self.agents.get(name)
        if not agent:
            raise KeyError(f"Agent '{name}' not found. Available: {list(self.agents.keys())}")
        return agent

    def get_client(self, name: str) -> GameClient:
        """Get a GameClient by name — works for both managed agents and external agents."""
        if name in self.clients:
            return self.clients[name]
        if name in self.external_clients:
            return self.external_clients[name]
        raise KeyError(f"Client '{name}' not found. Available: {list(self.clients.keys()) + list(self.external_clients.keys())}")


orchestrator = GameOrchestrator()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await orchestrator.shutdown()


app = FastAPI(title="XBWorld Multi-Agent API", lifespan=lifespan)

# P1-7: Serve static observer UI (no Tomcat needed)
from fastapi.staticfiles import StaticFiles
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/observe", StaticFiles(directory=str(_static_dir), html=True), name="observer")


@app.post("/game/create")
async def api_create_game(body: dict):
    """Create a new multiplayer game with the specified agents.

    Body:
        agents: list of {name, strategy?, llm_model?}
        aifill: int (optional, number of AI players to add)
        server_port: int (optional, join existing server)
        turn_timeout: int (optional, server-side turn timeout in seconds)
    """
    agent_configs = body.get("agents", [])
    if not agent_configs:
        raise HTTPException(400, "Must provide at least one agent config")

    for i, cfg in enumerate(agent_configs):
        if isinstance(cfg, str):
            agent_configs[i] = {"name": cfg}
        elif "name" not in cfg:
            raise HTTPException(400, f"Agent config at index {i} missing 'name'")

    names = [c["name"] for c in agent_configs]
    if len(names) != len(set(names)):
        raise HTTPException(400, "Agent names must be unique")

    try:
        await orchestrator.create_game(
            agent_configs,
            server_port=body.get("server_port"),
            aifill=body.get("aifill", 0),
            turn_timeout=body.get("turn_timeout"),
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    port = orchestrator.server_port
    return {
        "status": "ok",
        "server_port": port,
        "observe_url": f"http://{NGINX_HOST}:{NGINX_PORT}/webclient/?action=observe&civserverport={port}",
        "agents": names,
    }


@app.get("/game/status")
async def api_game_status():
    """Get status of all agents."""
    if not orchestrator.agents:
        return {"status": "no_game", "agents": []}
    port = orchestrator.server_port
    return {
        "status": "running",
        "server_port": port,
        "observe_url": f"http://{NGINX_HOST}:{NGINX_PORT}/webclient/?action=observe&civserverport={port}",
        "agents": [a.get_status() for a in orchestrator.agents.values()],
    }


@app.delete("/game")
async def api_delete_game():
    """Shut down the current game and disconnect all agents."""
    await orchestrator.shutdown()
    return {"status": "ok"}


@app.get("/agents/{name}/state")
async def api_agent_state(name: str):
    """Get detailed state for a specific agent."""
    try:
        agent = orchestrator.get_agent(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return agent.get_status()


@app.post("/agents/{name}/command")
async def api_agent_command(name: str, body: dict):
    """Send a natural language command to a specific agent.

    Body: {"command": "research Alphabet"}
    """
    try:
        agent = orchestrator.get_agent(name)
    except KeyError as e:
        raise HTTPException(404, str(e))

    command = body.get("command", "")
    if not command:
        raise HTTPException(400, "Must provide 'command' field")

    result = await agent.submit_command(command)
    return {"status": "ok", "message": result}


@app.get("/agents/{name}/log")
async def api_agent_log(name: str, limit: int = 50):
    """Get the action log for a specific agent."""
    try:
        agent = orchestrator.get_agent(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"name": name, "log": agent.action_log[-limit:]}


# ---------------------------------------------------------------------------
# P0-1: Agent Connect API — let external agents join a running game
# ---------------------------------------------------------------------------

@app.post("/game/join")
async def api_join_game(body: dict):
    """External agent joins the running game.

    Body: {username: str}
    Returns: {ws_url, server_port, tools, proxy_port}
    """
    if orchestrator.server_port < 0:
        raise HTTPException(400, "No game running. Create one first with POST /game/create")

    username = body.get("username", "").strip()
    if not username:
        raise HTTPException(400, "Must provide 'username'")
    if username in orchestrator.clients or username in orchestrator.external_clients:
        raise HTTPException(409, f"Username '{username}' already in use")

    client = GameClient(username=username)
    try:
        await client.join_game(orchestrator.server_port)
    except Exception as e:
        raise HTTPException(500, f"Failed to connect: {e}")

    orchestrator.external_clients[username] = client
    proxy_port = 1000 + orchestrator.server_port
    return {
        "status": "ok",
        "username": username,
        "server_port": orchestrator.server_port,
        "proxy_port": proxy_port,
        "ws_url": f"ws://{NGINX_HOST}:{NGINX_PORT}/civsocket/{proxy_port}",
        "tools": TOOL_REGISTRY.openai_definitions(),
    }


@app.get("/game/tools")
async def api_game_tools():
    """List all available tools with their JSON schemas."""
    return {"tools": TOOL_REGISTRY.openai_definitions()}


# ---------------------------------------------------------------------------
# P0-2: Direct Tool Execution API — bypass LLM for programmatic agents
# ---------------------------------------------------------------------------

@app.post("/agents/{name}/actions")
async def api_agent_actions(name: str, body: dict):
    """Execute tool calls directly, bypassing the LLM.

    Body: {actions: [{name: "move_unit", args: {unit_id: 1, direction: "N"}}, ...]}
    Returns: {results: [{name, args, result, success}, ...]}
    """
    try:
        client = orchestrator.get_client(name)
    except KeyError as e:
        raise HTTPException(404, str(e))

    actions = body.get("actions", [])
    if not actions:
        raise HTTPException(400, "Must provide at least one action")

    results = []
    for action in actions:
        action_name = action.get("name", "")
        action_args = action.get("args", {})
        result = await execute_tool(client, action_name, action_args)
        success = not result.lower().startswith("error") and "not found" not in result.lower()
        results.append({
            "name": action_name,
            "args": action_args,
            "result": result,
            "success": success,
        })
        orchestrator.events.publish({
            "type": "agent_action",
            "agent": name,
            "tool": action_name,
            "args": action_args,
            "result": result[:200],
            "success": success,
        })

    return {"results": results}


@app.post("/agents/{name}/end_turn")
async def api_agent_end_turn(name: str):
    """End the current turn for an agent."""
    try:
        client = orchestrator.get_client(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    await client.end_turn()
    return {"status": "ok", "turn": client.state.turn}


# ---------------------------------------------------------------------------
# P1-5: Structured Game State API
# ---------------------------------------------------------------------------

@app.get("/agents/{name}/state/json")
async def api_agent_state_json(name: str):
    """Get full structured game state for an agent."""
    try:
        client = orchestrator.get_client(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return game_state_to_json(client)


@app.get("/agents/{name}/state/delta")
async def api_agent_state_delta(name: str):
    """Get state changes since last query for an agent."""
    try:
        client = orchestrator.get_client(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    current, delta = orchestrator.state_tracker.snapshot(name, client)
    return {"current": current, "delta": delta}


@app.get("/game/state")
async def api_game_state():
    """Get global game state overview."""
    if orchestrator.server_port < 0:
        return {"status": "no_game"}
    states = {}
    for name, client in {**orchestrator.clients, **orchestrator.external_clients}.items():
        states[name] = game_state_to_json(client)
    return {
        "status": "running",
        "server_port": orchestrator.server_port,
        "agents": states,
    }


# ---------------------------------------------------------------------------
# P1-6: Observer Event Stream (SSE)
# ---------------------------------------------------------------------------

@app.get("/game/events")
async def api_game_events():
    """Server-Sent Events stream of game events.

    Events: turn_start, agent_action, city_founded, unit_moved, agent_report, game_state
    """
    queue = orchestrator.events.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            orchestrator.events.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def parse_agents_arg(agents_str: str) -> list[dict]:
    """Parse --agents argument.

    Formats:
        "3"                                -> 3 agents with default names
        "alpha,beta,gamma"                 -> named agents
        "alpha:aggressive,beta:defensive"  -> named agents with strategies
    """
    if agents_str.isdigit():
        n = int(agents_str)
        names = [f"agent{i+1}" for i in range(n)]
        return [{"name": name} for name in names]

    configs = []
    for part in agents_str.split(","):
        part = part.strip()
        if ":" in part:
            name, strategy = part.split(":", 1)
            configs.append({"name": name.strip(), "strategy": strategy.strip()})
        else:
            configs.append({"name": part})
    return configs


async def run_with_cli(agent_configs: list[dict], server_port: int = None,
                       aifill: int = 0, standalone: bool = False):
    """Run multi-agent game from CLI (no HTTP API)."""
    await orchestrator.create_game(agent_configs, server_port=server_port,
                                   aifill=aifill, standalone=standalone)

    port = orchestrator.server_port
    print(f"\n[Multi] Game running with {len(agent_configs)} agents on port {port}", flush=True)
    print(f"[Multi] Observe URL: http://localhost:8000/webclient/?action=observe&civserverport={port}", flush=True)
    print("[Multi] Type '<agent_name> <command>' to send commands (e.g. 'alpha research Alphabet')", flush=True)
    print("[Multi] Type 'status' to see all agents. Ctrl+C to quit.\n", flush=True)

    interactive = sys.stdin.isatty()

    if interactive:
        loop = asyncio.get_event_loop()
        try:
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                if line == "status":
                    for name, agent in orchestrator.agents.items():
                        s = agent.get_status()
                        print(f"  [{name}] turn={s['turn']} gold={s['gold']} "
                              f"cities={s['cities']} units={s['units']} phase={s['phase']}", flush=True)
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0] in orchestrator.agents:
                    agent_name, cmd = parts
                    await orchestrator.agents[agent_name].submit_command(cmd)
                else:
                    for agent in orchestrator.agents.values():
                        await agent.submit_command(line)
        except (EOFError, KeyboardInterrupt):
            pass
    else:
        try:
            await asyncio.gather(*orchestrator._tasks)
        except asyncio.CancelledError:
            pass

    await orchestrator.shutdown()


async def main():
    parser = argparse.ArgumentParser(description="XBWorld Multi-Agent")
    parser.add_argument("--agents", type=str, default="2",
                        help="Agent spec: count, names, or name:strategy pairs")
    parser.add_argument("--config", type=str, default=None,
                        help="JSON config file for agents")
    parser.add_argument("--join", type=int, default=None,
                        help="Join an existing game server on this port")
    parser.add_argument("--aifill", type=int, default=0,
                        help="Number of AI players to add beyond the agents")
    parser.add_argument("--api", action="store_true",
                        help="Start HTTP API server instead of CLI mode")
    parser.add_argument("--api-port", type=int, default=None,
                        help="HTTP API port (default from config)")
    parser.add_argument("--standalone", action="store_true",
                        help="Spawn freeciv-server directly (no Tomcat/publite2 needed)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.api:
        port = args.api_port or API_PORT
        print(f"[Multi] Starting HTTP API on {API_HOST}:{port}")
        print(f"[Multi] POST /game/create to start a game")
        config = uvicorn.Config(app, host=API_HOST, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
    else:
        if args.config:
            with open(args.config) as f:
                agent_configs = json.load(f)
        else:
            agent_configs = parse_agents_arg(args.agents)

        print(f"[Multi] Agents: {[c['name'] for c in agent_configs]}")

        try:
            await run_with_cli(agent_configs, server_port=args.join,
                               aifill=args.aifill, standalone=args.standalone)
        except KeyboardInterrupt:
            print("\n[Multi] Shutting down...")
            await orchestrator.shutdown()
        except Exception as e:
            print(f"\n[Error] {e}")
            logging.exception("Fatal error")
            await orchestrator.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
