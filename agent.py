"""
LLM Agent for XBWorld.

Runs an autonomous loop: each turn, analyzes state and executes actions via
LLM function-calling.  Accepts natural language commands from the user at any
time.  The LLM provider is pluggable (see ``llm_providers`` module).
The decision engine is also pluggable (see ``decision_engine`` module).
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict

import aiohttp

from config import LLM_MODEL, LLM_API_KEY, LLM_BASE_URL, TURN_TIMEOUT_SECONDS
from game_client import GameClient
from agent_tools import (
    TOOL_REGISTRY,
    execute_tool,
    get_game_overview,
    get_my_cities,
    get_my_units,
    get_research_status,
    get_visible_enemies,
)
from llm_providers import create_provider
from state_api import game_state_to_json

logger = logging.getLogger("xbworld-agent")


class PerfTracker:
    """Tracks per-turn performance metrics for the agent."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._turn_start: float = 0
        self._current_turn: int = 0
        self._llm_total: float = 0
        self._tool_total: float = 0
        self._llm_calls: int = 0
        self._tool_calls: int = 0
        self._conv_size: int = 0
        self._ws_msgs: int = 0
        self.turn_history: list[dict] = []

    def start_turn(self, turn: int):
        self._turn_start = time.monotonic()
        self._current_turn = turn
        self._llm_total = 0
        self._tool_total = 0
        self._llm_calls = 0
        self._tool_calls = 0
        self._ws_msgs = 0

    def record_llm(self, elapsed: float, conv_size: int):
        self._llm_total += elapsed
        self._llm_calls += 1
        self._conv_size = conv_size

    def record_tool(self, name: str, elapsed: float):
        self._tool_total += elapsed
        self._tool_calls += 1

    def record_ws_msg(self):
        self._ws_msgs += 1

    def end_turn(self) -> dict:
        total = time.monotonic() - self._turn_start if self._turn_start else 0
        idle = max(0, total - self._llm_total - self._tool_total)
        summary = {
            "agent": self.agent_name,
            "turn": self._current_turn,
            "total_s": round(total, 2),
            "llm_s": round(self._llm_total, 2),
            "tool_s": round(self._tool_total, 2),
            "idle_s": round(idle, 2),
            "llm_calls": self._llm_calls,
            "tool_calls": self._tool_calls,
            "conv_msgs": self._conv_size,
            "ws_msgs": self._ws_msgs,
        }
        self.turn_history.append(summary)
        if len(self.turn_history) > 100:
            self.turn_history = self.turn_history[-100:]
        return summary

    def checkpoint_summary(self, every_n: int = 5) -> str | None:
        """Return a summary string every N turns, or None."""
        if not self.turn_history or self._current_turn % every_n != 0:
            return None
        recent = self.turn_history[-every_n:]
        avg_total = sum(t["total_s"] for t in recent) / len(recent)
        avg_llm = sum(t["llm_s"] for t in recent) / len(recent)
        avg_tool = sum(t["tool_s"] for t in recent) / len(recent)
        avg_idle = sum(t["idle_s"] for t in recent) / len(recent)
        avg_conv = sum(t["conv_msgs"] for t in recent) / len(recent)
        return (
            f"[PERF {self.agent_name}] Turns {recent[0]['turn']}-{recent[-1]['turn']}: "
            f"avg total={avg_total:.1f}s llm={avg_llm:.1f}s tool={avg_tool:.1f}s "
            f"idle={avg_idle:.1f}s conv_size={avg_conv:.0f}"
        )

DEFAULT_SYSTEM_PROMPT = """\
You are an expert XBWorld player AI agent. You control a civilization and \
make strategic decisions each turn.

Your capabilities:
- Query game state (cities, units, research, messages)
- Send server commands (e.g. /set tax 30, /start, /save)
- Change city production, set research targets, adjust tax rates
- Move units, found cities, fortify, explore, disband, sentry
- Use batch tools (move_units, set_productions) for efficiency
- End turns when done

CRITICAL RULES (follow strictly):
1. RESEARCH: Always keep a research target active. If "Researching: None", \
call set_research_target IMMEDIATELY before doing anything else. \
Good early techs: Alphabet -> Code of Laws -> Republic. \
NEVER change research mid-way unless the current tech is completed. \
Stick with your chosen tech until it finishes.
2. CITY PRODUCTION: Build a balanced mix — not just Warriors! Priority: \
   - Granary (food growth), Temple (culture/happiness), Marketplace (gold) \
   - Workers (for terrain improvement) \
   - Settlers (when city size >= 3, to expand) \
   - Phalanx/Archers for defense, Warriors only for early exploration \
   After building 2-3 Warriors, switch to Granary or other buildings.
3. CITY FOUNDING: When founding a city with a Settler: \
   - Move the settler at least 4-5 tiles away from existing cities \
   - Do NOT found a city on the same turn you move the settler \
   - Check the tile is on land (not ocean) and not adjacent to another city \
   - If found_city FAILS, move the settler further away and try next turn
4. TAX RATES: Set science to at least 60% (e.g. tax=10 luxury=30 science=60) \
   to ensure research progress. Adjust only if gold is negative.
5. EXPLORATION: Set Warriors and Explorers to auto_explore_unit early. \
   Don't manually move every unit each turn — it wastes time.
6. EFFICIENCY: Issue ALL actions in one batch, then call end_turn. \
   Prefer move_units/set_productions batch tools over individual calls.

When no instructions are given, play autonomously and report what you did.
Always be concise. Respond in the same language as the user."""


class XBWorldAgent:
    def __init__(self, client: GameClient, name: str = "Agent",
                 system_prompt: str = None, llm_model: str = None,
                 engine=None, event_bus=None):
        self.client = client
        self.name = name
        self.llm_model = llm_model or LLM_MODEL
        prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.conversation: list[dict] = [{"role": "system", "content": prompt}]
        self._command_queue: asyncio.Queue[str] = asyncio.Queue()
        self.action_log: list[dict] = []
        self.last_report: str = ""
        self._http_session: aiohttp.ClientSession | None = None
        self._provider = create_provider(self.llm_model, LLM_API_KEY, LLM_BASE_URL)
        self.perf = PerfTracker(name)
        self.engine = engine
        self._event_bus = event_bus

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(
                total=TURN_TIMEOUT_SECONDS,
                sock_read=TURN_TIMEOUT_SECONDS,
            )
            self._http_session = aiohttp.ClientSession(timeout=timeout)
        return self._http_session

    async def close(self):
        logger.info("[%s] Closing agent (turn=%d, actions=%d)", self.name, self.client.state.turn, len(self.action_log))
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        if self.engine:
            await self.engine.close()
        logger.info("[%s] Agent closed", self.name)

    def _publish_event(self, event_type: str, data: dict = None):
        """Publish an event to the event bus if available."""
        if self._event_bus:
            event = {"type": event_type, "agent": self.name, "turn": self.client.state.turn}
            if data:
                event.update(data)
            self._event_bus.publish(event)

    def _log_action(self, action: str, detail: str = ""):
        entry = {
            "time": time.time(),
            "turn": self.client.state.turn,
            "action": action,
            "detail": detail,
        }
        self.action_log.append(entry)
        if len(self.action_log) > 500:
            self.action_log = self.action_log[-500:]

    def _log_llm_detail(self, event_type: str, data: dict):
        """Append a detailed JSON log entry to the agent's log file."""
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{self.name.lower()}_llm.jsonl")
        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "turn": self.client.state.turn,
            "phase": self.client.state.phase,
            "event": event_type,
            **data,
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    async def submit_command(self, command: str) -> str:
        """Submit a natural language command from external callers (API/stdin).
        Returns immediately; the command is processed on the next opportunity."""
        self._command_queue.put_nowait(command)
        self._log_action("command_received", command)
        return f"Command queued for {self.name}: {command}"

    async def run_game_loop(self):
        """Main game loop — waits for turns and processes them.
        Does NOT read stdin; use submit_command() for external input."""
        logger.info("[%s] Game loop started (phase=%s, turn=%d, connected=%s)",
                     self.name, self.client.state.phase, self.client.state.turn, self.client.state.connected)
        last_counter = self.client._turn_counter

        if self.client.state.phase == "playing" and self.client.state.turn >= 1:
            logger.info("[%s] Processing initial turn %d", self.name, self.client.state.turn)
            try:
                await self._run_turn_with_timeout()
            except Exception as e:
                logger.error("[%s] Error on initial turn: %s", self.name, e, exc_info=True)

        while self.client.state.connected:
            if self.client._turn_counter > last_counter:
                last_counter = self.client._turn_counter
                logger.info("[%s] Processing turn %d (counter=%d)",
                            self.name, self.client.state.turn, last_counter)
            else:
                got_turn = await self.client.wait_for_new_turn(timeout=30)
                if not got_turn:
                    if not self.client.state.connected:
                        break
                    user_cmd = self._drain_command()
                    if user_cmd:
                        try:
                            await self._handle_user_command(user_cmd)
                        except Exception as e:
                            logger.error("[%s] Error handling command: %s", self.name, e)
                    continue
                last_counter = self.client._turn_counter
                logger.info("[%s] Processing turn %d (counter=%d)",
                            self.name, self.client.state.turn, last_counter)

            user_cmd = self._drain_command()
            try:
                if user_cmd:
                    await self._handle_user_command(user_cmd)
                else:
                    await self._run_turn_with_timeout()
            except Exception as e:
                logger.error("[%s] Error on turn %d: %s", self.name, self.client.state.turn, e)

    async def _run_turn_with_timeout(self):
        """Run autonomous turn with a hard timeout."""
        turn = self.client.state.turn
        logger.debug("[%s] _run_turn_with_timeout: turn=%d timeout=%ds", self.name, turn, TURN_TIMEOUT_SECONDS)
        try:
            await asyncio.wait_for(self._autonomous_turn(), timeout=TURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("[%s] Turn %d TIMED OUT after %ds, force ending",
                           self.name, turn, TURN_TIMEOUT_SECONDS)
            self._log_action("timeout", f"turn {turn}")
            try:
                await self.client.end_turn()
            except Exception as e:
                logger.error("[%s] Failed to force-end turn after timeout: %s", self.name, e)

    def _drain_command(self):
        """Get the latest command, discarding older ones if multiple queued."""
        cmd = None
        try:
            while True:
                cmd = self._command_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        return cmd

    async def _handle_user_command(self, command: str):
        """Process a natural language command from the user."""
        self._log_action("user_command", command)
        state_summary = get_game_overview(self.client)
        self.conversation.append({
            "role": "user",
            "content": f"[Game State]\n{state_summary}\n\n[User Command]\n{command}",
        })
        await self._llm_loop()

    async def _autonomous_turn(self):
        """Let the agent play a turn autonomously."""
        turn_before = self.client.state.turn
        self.perf.start_turn(turn_before)
        self._log_action("autonomous_turn", f"turn {turn_before}")
        self._publish_event("turn_start", {"year": self.client.state.year})

        p = self.client.state.my_player() or {}
        logger.info("[%s] === TURN %d START === gold=%s cities=%d units=%d phase=%s",
                     self.name, turn_before, p.get("gold", "?"),
                     len(self.client.state.my_cities()), len(self.client.state.my_units()),
                     self.client.state.phase)

        overview = get_game_overview(self.client)
        cities = get_my_cities(self.client)
        units = get_my_units(self.client)
        research = get_research_status(self.client)
        enemies = get_visible_enemies(self.client)
        self._log_llm_detail("turn_start", {
            "turn": turn_before,
            "overview": overview,
            "cities": cities,
            "units": units,
            "research": research,
            "enemies": enemies,
        })

        research_hint = ""
        if "Researching: None" in research or "Researching: tech_" in research:
            research_hint = (
                "\n⚠️ WARNING: No active research! Call set_research_target FIRST. "
                "Good choices: Alphabet, Bronze Working, Pottery, Code of Laws, Republic."
            )

        production_hint = ""
        city_count = len(self.client.state.my_cities())
        if city_count > 0 and "Warriors" in cities and turn_before > 10:
            production_hint = (
                "\n💡 TIP: Consider building Granary, Temple, or Marketplace instead of Warriors. "
                "Build Settlers when city size >= 3 to expand."
            )

        self.conversation.append({
            "role": "user",
            "content": (
                f"Turn {turn_before}. Issue ALL actions in ONE batch, then call end_turn. "
                f"Do NOT call query tools — state is below. Be fast."
                f"{research_hint}{production_hint}\n\n"
                f"{overview}\n{cities}\n{units}\n{research}\n{enemies}"
            ),
        })
        await self._llm_loop()

        if self.client.state.turn == turn_before and self.client.state.phase == "playing":
            logger.info("[%s] Auto-ending turn %d (LLM didn't end it)", self.name, turn_before)
            self._log_action("auto_end_turn", f"turn {turn_before}")
            await self.client.end_turn()

        perf_summary = self.perf.end_turn()
        self._log_llm_detail("turn_perf", perf_summary)

        p = self.client.state.my_player() or {}
        logger.info("[%s] === TURN %d END === perf: total=%.1fs llm=%.1fs tool=%.1fs idle=%.1fs "
                     "calls=%d/%d | gold=%s cities=%d units=%d",
                     self.name, turn_before,
                     perf_summary["total_s"], perf_summary["llm_s"],
                     perf_summary["tool_s"], perf_summary["idle_s"],
                     perf_summary["llm_calls"], perf_summary["tool_calls"],
                     p.get("gold", "?"), len(self.client.state.my_cities()),
                     len(self.client.state.my_units()))

        checkpoint = self.perf.checkpoint_summary(every_n=5)
        if checkpoint:
            logger.info(checkpoint)

    async def _llm_call(self) -> dict | None:
        """Call the LLM via the configured provider. Returns raw provider response."""
        session = await self._get_http_session()
        tool_defs = TOOL_REGISTRY.openai_definitions()

        t0 = time.monotonic()
        logger.info("[%s] LLM call start (msgs=%d)", self.name, len(self.conversation))
        self._log_llm_detail("request", {
            "provider": self._provider.name,
            "model": self.llm_model,
            "num_messages": len(self.conversation),
        })

        data = await self._provider.call(session, self.conversation, tool_defs)

        elapsed = time.monotonic() - t0
        self.perf.record_llm(elapsed, len(self.conversation))
        logger.info("[%s] LLM call done in %.1fs", self.name, elapsed)
        self._log_llm_detail("response", {
            "elapsed_s": round(elapsed, 1),
            "raw_keys": list(data.keys()) if data else [],
        })
        return data

    async def _llm_loop(self):
        """Call LLM with tools, execute tool calls, repeat until done."""
        max_iterations = 5
        for iteration in range(max_iterations):
            logger.debug("[%s] LLM loop iteration %d/%d", self.name, iteration + 1, max_iterations)
            try:
                data = await self._llm_call()
            except Exception as e:
                logger.error("[%s] LLM call failed on iteration %d: %s", self.name, iteration + 1, e, exc_info=True)
                self._log_action("llm_error", str(e))
                break

            parsed = self._provider.parse_response(data)
            if parsed is None:
                logger.warning("[%s] LLM returned unparseable response on iteration %d, rolling back",
                               self.name, iteration + 1)
                self._log_action("llm_empty", "unparseable response")
                while self.conversation and self.conversation[-1].get("role") in ("tool", "assistant"):
                    self.conversation.pop()
                break

            text_content = parsed.get("text", "")
            func_calls = parsed.get("tool_calls", [])
            raw_assistant = parsed.get("raw_assistant")
            logger.info("[%s] LLM response: text=%d chars, tool_calls=%d",
                        self.name, len(text_content), len(func_calls))

            self.conversation.append(raw_assistant or {
                "role": "assistant", "content": text_content,
            })

            if func_calls:
                tool_results = []
                for fc in func_calls:
                    fn_name = fc["name"]
                    fn_args = fc.get("args", {})
                    t_tool = time.monotonic()
                    result = await execute_tool(self.client, fn_name, fn_args)
                    tool_elapsed = time.monotonic() - t_tool
                    self.perf.record_tool(fn_name, tool_elapsed)
                    logger.info("[%s] TOOL %s(%s) -> %s (%.2fs)", self.name, fn_name, fn_args, result[:200], tool_elapsed)
                    self._log_action("tool_call", f"{fn_name}({fn_args}) -> {result[:200]}")
                    self._log_llm_detail("tool_exec", {
                        "function": fn_name, "args": fn_args,
                        "result": result[:500],
                        "elapsed_s": round(tool_elapsed, 3),
                    })
                    tool_results.append({"name": fn_name, "result": result})
                    self._publish_event("agent_action", {
                        "tool": fn_name, "args": fn_args,
                        "result": result[:200],
                    })

                tool_msg = self._provider.format_tool_results(tool_results, func_calls)
                if isinstance(tool_msg, list):
                    self.conversation.extend(tool_msg)
                else:
                    self.conversation.append(tool_msg)
                continue

            if text_content:
                self.last_report = text_content
                self._log_action("report", text_content[:300])
                print(f"\n[{self.name} Turn {self.client.state.turn}] {text_content}")

            break

        self._trim_conversation()

    def _trim_conversation(self):
        """Keep conversation manageable by trimming old messages.
        Aggressive trimming reduces LLM token usage — the #1 bottleneck.

        For OpenAI compatibility, we must ensure:
        1. Every 'tool' message follows an 'assistant' message with tool_calls
        2. Every tool_call_id in an assistant message has a matching tool response
        """
        if len(self.conversation) <= 16:
            return

        system_msg = self.conversation[0] if self.conversation and self.conversation[0].get("role") == "system" else None
        recent = self.conversation[-10:]

        # Find the first valid start: skip orphaned tool messages at the beginning
        start = 0
        while start < len(recent) and recent[start].get("role") == "tool":
            start += 1

        # Also ensure we don't start with an assistant message that has tool_calls
        # without the corresponding tool responses
        cleaned = recent[start:]

        if system_msg:
            self.conversation = [system_msg] + cleaned
        else:
            self.conversation = cleaned

    def get_status(self) -> dict:
        """Return a JSON-serializable status summary for API consumers."""
        s = self.client.state
        p = s.my_player() or {}

        researching_id = s.research.get("researching", -1)
        tech_name = s.techs.get(researching_id, {}).get("name", "None") if researching_id >= 0 else "None"
        inventions = s.research.get("inventions", [])
        known_techs = [t.get("name", "?") for tid, t in s.techs.items()
                       if tid < len(inventions) and inventions[tid] == 1]

        status = {
            "name": self.name,
            "username": self.client.username,
            "connected": s.connected,
            "phase": s.phase,
            "turn": s.turn,
            "player_id": s.my_player_id,
            "gold": p.get("gold"),
            "tax": p.get("tax"),
            "science": p.get("science"),
            "luxury": p.get("luxury"),
            "cities": len(s.my_cities()),
            "units": len(s.my_units()),
            "researching": tech_name,
            "bulbs": s.research.get("bulbs_researched", 0),
            "tech_cost": s.research.get("researching_cost", 0),
            "known_techs": len(known_techs),
            "last_report": self.last_report,
        }
        if self.perf.turn_history:
            last = self.perf.turn_history[-1]
            status["perf"] = {
                "last_turn_total_s": last["total_s"],
                "last_turn_llm_s": last["llm_s"],
                "last_turn_tool_s": last["tool_s"],
                "avg_turn_s": round(
                    sum(t["total_s"] for t in self.perf.turn_history) / len(self.perf.turn_history), 2
                ),
            }
        return status
