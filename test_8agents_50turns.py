#!/usr/bin/env python3
"""
Test: 8 LLM agents playing 50 turns.
Checkpoints every 5 turns with detailed status.
Enhanced with per-turn timing, direction tracking, city founding rates,
combat tracking, and pass/fail criteria.
"""

import asyncio
import json
import logging
import os
import sys
import time

from config import NGINX_HOST, NGINX_PORT
from game_client import GameClient
from agent import XBWorldAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test-8agents")

AGENT_CONFIGS = [
    {"name": "alpha",   "strategy": "aggressive military expansion"},
    {"name": "beta",    "strategy": "defensive turtle with science focus"},
    {"name": "gamma",   "strategy": "economic growth and city building"},
    {"name": "delta",   "strategy": "balanced play with diplomacy"},
    {"name": "epsilon", "strategy": "rapid expansion and exploration"},
    {"name": "zeta",    "strategy": "military tech rush"},
    {"name": "eta",     "strategy": "wonder building and culture"},
    {"name": "theta",   "strategy": "naval power and coastal cities"},
]

STRATEGY_PROMPT = """You are an expert XBWorld player AI agent named "{name}". \
You control a civilization and make strategic decisions each turn.

Your strategic personality: {strategy}

Capabilities: query state, send commands, change production, set research, \
move units, found cities, fortify, explore, disband, sentry, end turns.

Play autonomously following your personality. Be concise. Act fast."""

TARGET_TURNS = 50
CHECKPOINT_INTERVAL = 5

PASS_CRITERIA = {
    "min_cities_by_turn_20": 2,
    "max_stuck_seconds": 60,
    "max_error_rate_pct": 5.0,
    "all_reach_turn": TARGET_TURNS,
}


class AgentMetrics:
    """Per-agent metrics tracker for enhanced logging."""

    def __init__(self, name: str):
        self.name = name
        self.turn_times: list[dict] = []
        self.move_log: list[dict] = []
        self.city_found_attempts: int = 0
        self.city_found_successes: int = 0
        self.combat_events: list[dict] = []
        self.last_turn_seen: int = 0
        self.last_turn_time: float = time.time()
        self.max_stuck_s: float = 0.0

    def record_turn_start(self, turn: int):
        self.turn_times.append({
            "turn": turn,
            "start": time.monotonic(),
            "end": None,
            "duration_s": None,
        })
        now = time.time()
        if turn == self.last_turn_seen:
            stuck = now - self.last_turn_time
            self.max_stuck_s = max(self.max_stuck_s, stuck)
        else:
            self.last_turn_seen = turn
            self.last_turn_time = now

    def record_turn_end(self, turn: int):
        for entry in reversed(self.turn_times):
            if entry["turn"] == turn and entry["end"] is None:
                entry["end"] = time.monotonic()
                entry["duration_s"] = round(entry["end"] - entry["start"], 2)
                break

    def update_stuck_check(self, current_turn: int):
        now = time.time()
        if current_turn == self.last_turn_seen:
            stuck = now - self.last_turn_time
            self.max_stuck_s = max(self.max_stuck_s, stuck)
        else:
            self.last_turn_seen = current_turn
            self.last_turn_time = now

    def summary(self) -> dict:
        completed = [t for t in self.turn_times if t["duration_s"] is not None]
        avg_turn = (sum(t["duration_s"] for t in completed) / len(completed)) if completed else 0
        return {
            "name": self.name,
            "turns_recorded": len(self.turn_times),
            "avg_turn_duration_s": round(avg_turn, 2),
            "city_found_attempts": self.city_found_attempts,
            "city_found_successes": self.city_found_successes,
            "city_found_rate": (
                round(self.city_found_successes / self.city_found_attempts * 100, 1)
                if self.city_found_attempts > 0 else 0
            ),
            "combat_events": len(self.combat_events),
            "max_stuck_s": round(self.max_stuck_s, 1),
        }


def scan_action_log(agent: XBWorldAgent, metrics: AgentMetrics):
    """Scan agent action log for city founding and combat events."""
    for entry in agent.action_log:
        detail = entry.get("detail", "")
        action = entry.get("action", "")

        if action == "tool_call" and "found_city(" in detail:
            metrics.city_found_attempts += 1
            if "SUCCESS" in detail:
                metrics.city_found_successes += 1

        if action == "tool_call" and ("attack" in detail.lower() or "combat" in detail.lower()):
            metrics.combat_events.append({
                "turn": entry.get("turn"),
                "detail": detail[:200],
            })


MULTIPLAYER_PORT = int(os.getenv("GAME_PORT", "6001"))


async def _find_multiplayer_port() -> int:
    """Return the multiplayer server port (from env or default 6001)."""
    return MULTIPLAYER_PORT


async def run_test():
    clients: list[GameClient] = []
    agents: list[XBWorldAgent] = []
    metrics: dict[str, AgentMetrics] = {}

    logger.info("=== Starting 8-agent test for %d turns ===", TARGET_TURNS)

    port = await _find_multiplayer_port()
    logger.info("Using game server on port %d", port)

    first = GameClient(username=AGENT_CONFIGS[0]["name"])
    await first.join_game(port)
    clients.append(first)
    logger.info("First agent connected to port %d", port)
    logger.info("Observe: http://%s:%d/webclient/?action=observe&civserverport=%d",
                NGINX_HOST, NGINX_PORT, port)

    await asyncio.sleep(5)

    for cfg in AGENT_CONFIGS[1:]:
        c = GameClient(username=cfg["name"])
        await c.join_game(port)
        clients.append(c)
        await asyncio.sleep(5)

    for i, cfg in enumerate(AGENT_CONFIGS):
        prompt = STRATEGY_PROMPT.format(**cfg)
        agent = XBWorldAgent(clients[i], name=cfg["name"], system_prompt=prompt)
        agents.append(agent)
        metrics[cfg["name"]] = AgentMetrics(cfg["name"])

    await first.send_chat("/set timeout 0")
    await asyncio.sleep(0.5)

    for c in clients:
        await c.send_chat("/start")
        await asyncio.sleep(0.3)

    for i in range(20):
        await asyncio.sleep(1)
        if any(c.state.turn >= 1 for c in clients):
            logger.info("Game started! Turn 1 detected after %ds", i + 1)
            break
    else:
        logger.error("Game did not start within 20s!")
        for c in clients:
            await c.close()
        return

    tasks = []
    for agent in agents:
        tasks.append(asyncio.create_task(agent.run_game_loop()))

    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    checkpoint_file = os.path.join(log_dir, "checkpoint_report.jsonl")
    with open(checkpoint_file, "w") as f:
        f.write("")

    t0 = time.time()
    last_checkpoint_turn = 0

    while True:
        await asyncio.sleep(3)

        active = [a for a in agents if a.client.state.connected]
        if not active:
            logger.error("All agents disconnected!")
            break
        turns = {a.name: a.client.state.turn for a in active}
        min_turn = min(turns.values())
        max_turn = max(turns.values())

        for a in active:
            m = metrics[a.name]
            m.update_stuck_check(a.client.state.turn)

        if min_turn >= TARGET_TURNS:
            logger.info("=== All connected agents reached turn %d. Test complete! ===", TARGET_TURNS)
            break

        elapsed = time.time() - t0
        if elapsed > 3600:
            logger.error("=== TIMEOUT: 1 hour elapsed, stopping at turn %d ===", max_turn)
            break

        disconnected = [a.name for a in agents if not a.client.state.connected]
        if disconnected:
            logger.warning("Agents disconnected: %s at turn %d", disconnected, max_turn)

        checkpoint_turn = (min_turn // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
        if checkpoint_turn > last_checkpoint_turn and checkpoint_turn > 0:
            last_checkpoint_turn = checkpoint_turn
            for a in agents:
                scan_action_log(a, metrics[a.name])
            report = generate_checkpoint(agents, metrics, checkpoint_turn, elapsed)
            logger.info("\n%s", format_checkpoint(report))
            with open(checkpoint_file, "a") as f:
                f.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")

    for a in agents:
        scan_action_log(a, metrics[a.name])

    final_report = generate_checkpoint(agents, metrics, max_turn, time.time() - t0)
    pass_fail = evaluate_pass_fail(agents, metrics, max_turn)
    final_report["pass_fail"] = pass_fail

    logger.info("\n=== FINAL REPORT ===\n%s", format_checkpoint(final_report))
    logger.info("\n=== PASS/FAIL CRITERIA ===\n%s", format_pass_fail(pass_fail))

    with open(checkpoint_file, "a") as f:
        f.write(json.dumps({"final": True, **final_report}, ensure_ascii=False, default=str) + "\n")

    for t in tasks:
        t.cancel()
    for a in agents:
        await a.close()
    for c in clients:
        await c.close()


def evaluate_pass_fail(agents: list[XBWorldAgent], metrics: dict[str, AgentMetrics],
                       final_turn: int) -> dict:
    """Evaluate pass/fail criteria and return results."""
    results = {"passed": True, "checks": []}

    cities_by_20 = {}
    for a in agents:
        cities = len(a.client.state.my_cities())
        turn = a.client.state.turn
        cities_by_20[a.name] = {"cities": cities, "turn": turn}

    min_turn_reached = min(a.client.state.turn for a in agents)
    all_reached_target = min_turn_reached >= PASS_CRITERIA["all_reach_turn"]
    results["checks"].append({
        "name": "all_agents_reach_turn_50",
        "passed": all_reached_target,
        "detail": f"min_turn={min_turn_reached}, target={PASS_CRITERIA['all_reach_turn']}",
    })
    if not all_reached_target:
        results["passed"] = False

    for a in agents:
        m = metrics[a.name]
        stuck_ok = m.max_stuck_s <= PASS_CRITERIA["max_stuck_seconds"]
        results["checks"].append({
            "name": f"{a.name}_not_stuck",
            "passed": stuck_ok,
            "detail": f"max_stuck={m.max_stuck_s:.1f}s, limit={PASS_CRITERIA['max_stuck_seconds']}s",
        })
        if not stuck_ok:
            results["passed"] = False

    for a in agents:
        tool_calls = [e for e in a.action_log if e.get("action") == "tool_call"]
        errors = [e for e in a.action_log if "error" in e.get("action", "")]
        total = len(tool_calls) + len(errors)
        error_rate = (len(errors) / total * 100) if total > 0 else 0
        err_ok = error_rate <= PASS_CRITERIA["max_error_rate_pct"]
        results["checks"].append({
            "name": f"{a.name}_error_rate",
            "passed": err_ok,
            "detail": f"errors={len(errors)}/{total} ({error_rate:.1f}%), limit={PASS_CRITERIA['max_error_rate_pct']}%",
        })
        if not err_ok:
            results["passed"] = False

    if min_turn_reached >= 20:
        for a in agents:
            cities = len(a.client.state.my_cities())
            city_ok = cities >= PASS_CRITERIA["min_cities_by_turn_20"]
            results["checks"].append({
                "name": f"{a.name}_cities_by_turn_20",
                "passed": city_ok,
                "detail": f"cities={cities}, required={PASS_CRITERIA['min_cities_by_turn_20']}",
            })
            if not city_ok:
                results["passed"] = False

    return results


def format_pass_fail(pf: dict) -> str:
    lines = [f"Overall: {'PASS' if pf['passed'] else 'FAIL'}"]
    for check in pf["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        lines.append(f"  [{status}] {check['name']}: {check['detail']}")
    return "\n".join(lines)


def generate_checkpoint(agents: list[XBWorldAgent], metrics: dict[str, AgentMetrics],
                        turn: int, elapsed: float) -> dict:
    agent_data = []
    for a in agents:
        s = a.client.state
        p = s.my_player() or {}
        my_cities = s.my_cities()
        my_units = s.my_units()

        tool_calls = [e for e in a.action_log if e.get("action") == "tool_call"]
        errors = [e for e in a.action_log if "error" in e.get("action", "")]
        timeouts = [e for e in a.action_log if e.get("action") == "timeout"]

        unit_types = {}
        for u in my_units.values():
            tn = a.client.state.unit_types.get(u.get("type", -1), {}).get("name", "?")
            unit_types[tn] = unit_types.get(tn, 0) + 1

        m = metrics.get(a.name)
        m_summary = m.summary() if m else {}

        enemy_units = {uid: u for uid, u in s.units.items()
                       if u.get("owner") != s.my_player_id and u.get("owner") is not None}

        agent_data.append({
            "name": a.name,
            "turn": s.turn,
            "phase": s.phase,
            "connected": s.connected,
            "gold": p.get("gold", 0),
            "tax": p.get("tax", 0),
            "science": p.get("science", 0),
            "luxury": p.get("luxury", 0),
            "cities": len(my_cities),
            "city_names": [c.get("name", "?") for c in my_cities.values()],
            "units": len(my_units),
            "unit_types": unit_types,
            "visible_enemies": len(enemy_units),
            "total_tool_calls": len(tool_calls),
            "total_errors": len(errors),
            "total_timeouts": len(timeouts),
            "last_report": a.last_report[:200] if a.last_report else "",
            "metrics": m_summary,
        })

    return {
        "checkpoint_turn": turn,
        "elapsed_s": round(elapsed, 1),
        "agents": agent_data,
    }


def format_checkpoint(report: dict) -> str:
    lines = [f"{'='*70}",
             f"CHECKPOINT @ Turn {report['checkpoint_turn']} ({report['elapsed_s']}s elapsed)",
             f"{'='*70}"]
    for a in report["agents"]:
        m = a.get("metrics", {})
        lines.append(
            f"  {a['name']:10s} | turn={a['turn']:3d} | gold={a.get('gold',0):5} | "
            f"cities={a['cities']} | units={a['units']} | enemies={a.get('visible_enemies', 0)} | "
            f"tools={a['total_tool_calls']} err={a['total_errors']} timeout={a['total_timeouts']}"
        )
        if a["city_names"]:
            lines.append(f"{'':14s}cities: {', '.join(a['city_names'][:5])}")
        if a["unit_types"]:
            ut_str = ", ".join(f"{k}:{v}" for k, v in a["unit_types"].items())
            lines.append(f"{'':14s}units: {ut_str}")
        if m:
            city_rate = m.get("city_found_rate", 0)
            avg_turn = m.get("avg_turn_duration_s", 0)
            stuck = m.get("max_stuck_s", 0)
            combat = m.get("combat_events", 0)
            lines.append(
                f"{'':14s}perf: avg_turn={avg_turn}s | city_rate={city_rate}% | "
                f"max_stuck={stuck}s | combats={combat}"
            )
    lines.append(f"{'='*70}")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        asyncio.run(run_test())
    except KeyboardInterrupt:
        print("\nInterrupted.")
