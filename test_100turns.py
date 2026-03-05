#!/usr/bin/env python3
"""
Test: 8 LLM agents playing 100 turns with checkpoints every 5 turns.

Based on test_8agents_50turns.py but extended to 100 turns with enhanced
logging, issue detection, and automatic problem reporting.
"""

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

from config import NGINX_HOST, NGINX_PORT
from game_client import GameClient
from agent import XBWorldAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test-100turns")

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

CRITICAL RULES (follow strictly every turn):
1. RESEARCH: Always keep a research target active. If "Researching: None" or \
"NO RESEARCH", call set_research_target IMMEDIATELY. Never switch research \
mid-way — stick with it until completed. Good path: Alphabet -> Code of Laws -> Republic.
2. PRODUCTION: Build a balanced mix — not just Warriors! After 2-3 Warriors, \
build Granary, Temple, Marketplace, Workers, or Settlers (when city size >= 3).
3. CITY FOUNDING: Move settlers at least 4-5 tiles from existing cities before \
founding. If found_city fails, move further away and try next turn.
4. TAX RATES: Keep science at 60%+ (e.g. tax=10 lux=30 sci=60).
5. EFFICIENCY: Use auto_explore_unit for scouts. Use batch tools. Act fast.

Capabilities: send commands, change production, set research, move units, \
found cities, fortify, explore, disband, sentry, end turns.

Play autonomously following your personality. Be concise. Act fast."""

TARGET_TURNS = 100
CHECKPOINT_INTERVAL = 5
MAX_TEST_DURATION_S = 7200  # 2 hours

PASS_CRITERIA = {
    "min_cities_by_turn_20": 1,
    "min_cities_by_turn_50": 2,
    "min_techs_by_turn_50": 2,
    "max_stuck_seconds": 90,
    "max_error_rate_pct": 10.0,
    "all_reach_turn": TARGET_TURNS,
}


class AgentMetrics:
    """Per-agent metrics tracker."""

    def __init__(self, name: str):
        self.name = name
        self.turn_times: list[dict] = []
        self.city_found_attempts: int = 0
        self.city_found_successes: int = 0
        self.combat_events: list[dict] = []
        self.errors: list[dict] = []
        self.timeouts: list[dict] = []
        self.last_turn_seen: int = 0
        self.last_turn_time: float = time.time()
        self.max_stuck_s: float = 0.0
        self.llm_errors: int = 0
        self.tool_errors: int = 0
        self.disconnects: int = 0
        self.turn_durations: list[float] = []

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
                self.turn_durations.append(entry["duration_s"])
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
        p95_turn = sorted(self.turn_durations)[int(len(self.turn_durations) * 0.95)] if len(self.turn_durations) > 5 else 0
        return {
            "name": self.name,
            "turns_recorded": len(self.turn_times),
            "avg_turn_duration_s": round(avg_turn, 2),
            "p95_turn_duration_s": round(p95_turn, 2),
            "city_found_attempts": self.city_found_attempts,
            "city_found_successes": self.city_found_successes,
            "city_found_rate": (
                round(self.city_found_successes / self.city_found_attempts * 100, 1)
                if self.city_found_attempts > 0 else 0
            ),
            "combat_events": len(self.combat_events),
            "max_stuck_s": round(self.max_stuck_s, 1),
            "llm_errors": self.llm_errors,
            "tool_errors": self.tool_errors,
            "disconnects": self.disconnects,
        }


def scan_action_log(agent: XBWorldAgent, metrics: AgentMetrics):
    """Scan agent action log for events."""
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

        if "llm_error" in action:
            metrics.llm_errors += 1
            metrics.errors.append({"turn": entry.get("turn"), "type": "llm", "detail": detail[:200]})

        if action == "tool_call" and detail.lower().startswith("error"):
            metrics.tool_errors += 1

        if action == "timeout":
            metrics.timeouts.append({"turn": entry.get("turn"), "detail": detail[:200]})


def detect_issues(agents: list[XBWorldAgent], metrics: dict[str, AgentMetrics],
                  checkpoint_turn: int) -> list[dict]:
    """Detect potential issues at checkpoint."""
    issues = []

    for a in agents:
        m = metrics[a.name]
        turn = a.client.state.turn

        if not a.client.state.connected:
            issues.append({
                "severity": "CRITICAL",
                "agent": a.name,
                "issue": "DISCONNECTED",
                "detail": f"Agent disconnected at turn {turn}",
            })
            m.disconnects += 1

        if m.max_stuck_s > 60:
            issues.append({
                "severity": "WARNING",
                "agent": a.name,
                "issue": "STUCK",
                "detail": f"Agent stuck for {m.max_stuck_s:.1f}s at turn {turn}",
            })

        if turn < checkpoint_turn - 10:
            issues.append({
                "severity": "WARNING",
                "agent": a.name,
                "issue": "LAGGING",
                "detail": f"Agent at turn {turn}, checkpoint is {checkpoint_turn}",
            })

        tool_calls = [e for e in a.action_log if e.get("action") == "tool_call"]
        errors = [e for e in a.action_log if "error" in e.get("action", "")]
        total = len(tool_calls) + len(errors)
        if total > 0:
            error_rate = len(errors) / total * 100
            if error_rate > 15:
                issues.append({
                    "severity": "WARNING",
                    "agent": a.name,
                    "issue": "HIGH_ERROR_RATE",
                    "detail": f"Error rate {error_rate:.1f}% ({len(errors)}/{total})",
                })

        if checkpoint_turn >= 30 and len(a.client.state.my_cities()) == 0:
            issues.append({
                "severity": "WARNING",
                "agent": a.name,
                "issue": "NO_CITIES",
                "detail": f"Agent has 0 cities at turn {turn}",
            })

        recent_timeouts = [t for t in m.timeouts if t.get("turn", 0) >= checkpoint_turn - 5]
        if len(recent_timeouts) >= 3:
            issues.append({
                "severity": "WARNING",
                "agent": a.name,
                "issue": "FREQUENT_TIMEOUTS",
                "detail": f"{len(recent_timeouts)} timeouts in last 5 turns",
            })

    return issues


MULTIPLAYER_PORT = int(os.getenv("GAME_PORT", "6001"))


async def run_test():
    clients: list[GameClient] = []
    agents: list[XBWorldAgent] = []
    metrics: dict[str, AgentMetrics] = {}

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    file_handler = logging.FileHandler(log_dir / "test_100turns.log", mode="w")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(file_handler)

    logger.info("=" * 70)
    logger.info("=== Starting 100-turn test with %d agents ===", len(AGENT_CONFIGS))
    logger.info("=== Target: %d turns, checkpoint every %d turns ===", TARGET_TURNS, CHECKPOINT_INTERVAL)
    logger.info("=== Max duration: %ds ===", MAX_TEST_DURATION_S)
    logger.info("=" * 70)

    port = MULTIPLAYER_PORT
    logger.info("Using game server on port %d", port)

    first = GameClient(username=AGENT_CONFIGS[0]["name"])
    try:
        await first.join_game(port)
    except Exception as e:
        logger.error("Failed to connect first agent: %s", e)
        logger.error("Make sure the game server is running on port %d", port)
        logger.error("Start with: python server.py --agents 0")
        return
    clients.append(first)
    logger.info("First agent '%s' connected to port %d", AGENT_CONFIGS[0]["name"], port)
    logger.info("Observe: http://%s:%d/webclient/?action=observe&civserverport=%d",
                NGINX_HOST, NGINX_PORT, port)

    await asyncio.sleep(2)

    for cfg in AGENT_CONFIGS[1:]:
        logger.info("Connecting agent '%s'...", cfg["name"])
        c = GameClient(username=cfg["name"])
        try:
            await c.join_game(port)
        except Exception as e:
            logger.error("Failed to connect agent '%s': %s", cfg["name"], e)
            for cl in clients:
                await cl.close()
            return
        clients.append(c)
        await asyncio.sleep(0.8)

    logger.info("All %d agents connected", len(clients))

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

    logger.info("Waiting for game to start...")
    for i in range(30):
        await asyncio.sleep(1)
        if any(c.state.turn >= 1 for c in clients):
            logger.info("Game started! Turn 1 detected after %ds", i + 1)
            break
    else:
        logger.error("Game did not start within 30s!")
        for c in clients:
            await c.close()
        return

    tasks = []
    for agent in agents:
        tasks.append(asyncio.create_task(agent.run_game_loop()))
        logger.info("Agent '%s' game loop started", agent.name)

    checkpoint_file = log_dir / "checkpoint_100turns.jsonl"
    issues_file = log_dir / "issues_100turns.jsonl"
    with open(checkpoint_file, "w") as f:
        f.write("")
    with open(issues_file, "w") as f:
        f.write("")

    t0 = time.time()
    last_checkpoint_turn = 0
    all_issues: list[dict] = []

    logger.info("=" * 70)
    logger.info("Test loop started. Monitoring agents...")
    logger.info("=" * 70)

    while True:
        await asyncio.sleep(3)

        turns = {a.name: a.client.state.turn for a in agents}
        min_turn = min(turns.values())
        max_turn = max(turns.values())

        for a in agents:
            m = metrics[a.name]
            m.update_stuck_check(a.client.state.turn)

        if min_turn >= TARGET_TURNS:
            logger.info("=" * 70)
            logger.info("=== ALL agents reached turn %d. Test COMPLETE! ===", TARGET_TURNS)
            logger.info("=" * 70)
            break

        elapsed = time.time() - t0
        if elapsed > MAX_TEST_DURATION_S:
            logger.error("=" * 70)
            logger.error("=== TIMEOUT: %.0fs elapsed, stopping at turn %d ===", elapsed, max_turn)
            logger.error("=" * 70)
            break

        if not all(c.state.connected for c in clients):
            disconnected = [a.name for a in agents if not a.client.state.connected]
            logger.error("Agents disconnected: %s at turn %d", disconnected, max_turn)
            still_connected = [a for a in agents if a.client.state.connected]
            if not still_connected:
                logger.error("All agents disconnected, stopping test")
                break
            logger.warning("Continuing with %d/%d connected agents", len(still_connected), len(agents))

        checkpoint_turn = (min_turn // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
        if checkpoint_turn > last_checkpoint_turn and checkpoint_turn > 0:
            last_checkpoint_turn = checkpoint_turn

            for a in agents:
                scan_action_log(a, metrics[a.name])

            report = generate_checkpoint(agents, metrics, checkpoint_turn, elapsed)
            logger.info("\n%s", format_checkpoint(report))

            issues = detect_issues(agents, metrics, checkpoint_turn)
            if issues:
                all_issues.extend(issues)
                logger.warning("--- ISSUES DETECTED at turn %d ---", checkpoint_turn)
                for issue in issues:
                    logger.warning("  [%s] %s: %s - %s",
                                   issue["severity"], issue["agent"], issue["issue"], issue["detail"])
                    with open(issues_file, "a") as f:
                        f.write(json.dumps({"checkpoint_turn": checkpoint_turn, **issue},
                                           ensure_ascii=False) + "\n")
                logger.warning("--- END ISSUES ---")
            else:
                logger.info("No issues detected at turn %d", checkpoint_turn)

            with open(checkpoint_file, "a") as f:
                report["issues"] = issues
                f.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")

    for a in agents:
        scan_action_log(a, metrics[a.name])

    elapsed_total = time.time() - t0
    final_report = generate_checkpoint(agents, metrics, max_turn, elapsed_total)
    pass_fail = evaluate_pass_fail(agents, metrics, max_turn)
    final_report["pass_fail"] = pass_fail
    final_report["total_issues"] = len(all_issues)
    final_report["issue_summary"] = summarize_issues(all_issues)

    logger.info("\n" + "=" * 70)
    logger.info("FINAL REPORT")
    logger.info("=" * 70)
    logger.info("\n%s", format_checkpoint(final_report))
    logger.info("\n=== PASS/FAIL CRITERIA ===\n%s", format_pass_fail(pass_fail))
    logger.info("\n=== ISSUE SUMMARY ===")
    logger.info("Total issues detected: %d", len(all_issues))
    for category, count in final_report["issue_summary"].items():
        logger.info("  %s: %d", category, count)

    with open(checkpoint_file, "a") as f:
        f.write(json.dumps({"final": True, **final_report}, ensure_ascii=False, default=str) + "\n")

    for t in tasks:
        t.cancel()
    for a in agents:
        await a.close()
    for c in clients:
        await c.close()

    logger.info("Test finished. Duration: %.1fs", elapsed_total)
    logger.info("Checkpoint log: %s", checkpoint_file)
    logger.info("Issues log: %s", issues_file)
    logger.info("Detailed log: %s", log_dir / "test_100turns.log")

    if pass_fail["passed"]:
        logger.info("RESULT: PASS")
    else:
        logger.error("RESULT: FAIL")
        for check in pass_fail["checks"]:
            if not check["passed"]:
                logger.error("  FAILED: %s - %s", check["name"], check["detail"])


def summarize_issues(issues: list[dict]) -> dict:
    """Summarize issues by category."""
    summary = defaultdict(int)
    for issue in issues:
        key = f"{issue.get('severity', '?')}_{issue.get('issue', '?')}"
        summary[key] += 1
    return dict(summary)


def evaluate_pass_fail(agents: list[XBWorldAgent], metrics: dict[str, AgentMetrics],
                       final_turn: int) -> dict:
    """Evaluate pass/fail criteria."""
    results = {"passed": True, "checks": []}

    min_turn_reached = min(a.client.state.turn for a in agents)
    all_reached_target = min_turn_reached >= PASS_CRITERIA["all_reach_turn"]
    results["checks"].append({
        "name": f"all_agents_reach_turn_{TARGET_TURNS}",
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

    if min_turn_reached >= 50:
        for a in agents:
            cities = len(a.client.state.my_cities())
            city_ok = cities >= PASS_CRITERIA["min_cities_by_turn_50"]
            results["checks"].append({
                "name": f"{a.name}_cities_by_turn_50",
                "passed": city_ok,
                "detail": f"cities={cities}, required={PASS_CRITERIA['min_cities_by_turn_50']}",
            })
            if not city_ok:
                results["passed"] = False

        for a in agents:
            s = a.client.state
            inventions = s.research.get("inventions", [])
            known = len([t for tid, t in s.techs.items()
                         if tid < len(inventions) and inventions[tid] == 1])
            tech_ok = known >= PASS_CRITERIA["min_techs_by_turn_50"]
            results["checks"].append({
                "name": f"{a.name}_techs_by_turn_50",
                "passed": tech_ok,
                "detail": f"known_techs={known}, required={PASS_CRITERIA['min_techs_by_turn_50']}",
            })
            if not tech_ok:
                results["passed"] = False

    for a in agents:
        connected_ok = a.client.state.connected
        results["checks"].append({
            "name": f"{a.name}_connected",
            "passed": connected_ok,
            "detail": f"connected={connected_ok}",
        })
        if not connected_ok:
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

        ws_stats = a.client.get_ws_stats()

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
            "ws_stats": ws_stats,
            "known_techs": len([t for tid, t in s.techs.items()
                                if tid < len(s.research.get("inventions", []))
                                and s.research.get("inventions", [])[tid] == 1]),
        })

    return {
        "checkpoint_turn": turn,
        "elapsed_s": round(elapsed, 1),
        "agents": agent_data,
    }


def format_checkpoint(report: dict) -> str:
    lines = [f"{'='*80}",
             f"CHECKPOINT @ Turn {report['checkpoint_turn']} ({report['elapsed_s']}s elapsed)",
             f"{'='*80}"]
    for a in report["agents"]:
        m = a.get("metrics", {})
        ws = a.get("ws_stats", {})
        lines.append(
            f"  {a['name']:10s} | turn={a['turn']:3d} | gold={a.get('gold',0):5} | "
            f"cities={a['cities']} | units={a['units']} | enemies={a.get('visible_enemies', 0)} | "
            f"tools={a['total_tool_calls']} err={a['total_errors']} timeout={a['total_timeouts']} | "
            f"techs={a.get('known_techs', 0)}"
        )
        if a["city_names"]:
            lines.append(f"{'':14s}cities: {', '.join(a['city_names'][:8])}")
        if a["unit_types"]:
            ut_str = ", ".join(f"{k}:{v}" for k, v in a["unit_types"].items())
            lines.append(f"{'':14s}units: {ut_str}")
        if m:
            avg_turn = m.get("avg_turn_duration_s", 0)
            p95_turn = m.get("p95_turn_duration_s", 0)
            city_rate = m.get("city_found_rate", 0)
            stuck = m.get("max_stuck_s", 0)
            combat = m.get("combat_events", 0)
            llm_err = m.get("llm_errors", 0)
            lines.append(
                f"{'':14s}perf: avg_turn={avg_turn}s p95={p95_turn}s | city_rate={city_rate}% | "
                f"max_stuck={stuck}s | combats={combat} | llm_err={llm_err}"
            )
        if ws:
            lines.append(
                f"{'':14s}ws: msgs={ws.get('total_ws_msgs', 0)} pkts={ws.get('packets_processed', 0)} "
                f"rate={ws.get('ws_msg_rate_per_s', 0)}/s uptime={ws.get('uptime_s', 0)}s"
            )
    lines.append(f"{'='*80}")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        asyncio.run(run_test())
    except KeyboardInterrupt:
        print("\nInterrupted.")
