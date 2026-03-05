"""
Pluggable decision engine abstraction for XBWorld agents.

Decouples decision-making from the game loop so that different strategies
(LLM, rule-based, RL, external API) can be swapped without changing the
agent or game client code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("xbworld-agent")


@dataclass
class ToolCall:
    name: str
    args: dict


class DecisionEngine(ABC):
    """Base class for all decision engines."""

    engine_type: str = "base"

    @abstractmethod
    async def decide(
        self,
        state: dict,
        available_tools: list[dict],
        context: dict | None = None,
    ) -> list[ToolCall]:
        """Given structured game state and tool schemas, return actions to take.

        Args:
            state: Structured game state from ``state_api.game_state_to_json``.
            available_tools: OpenAI-format tool definitions.
            context: Optional extra context (e.g. user command, turn number).

        Returns:
            List of tool calls to execute.
        """

    async def on_results(
        self,
        results: list[dict],
        state: dict,
        available_tools: list[dict],
    ) -> list[ToolCall] | None:
        """Process tool execution results, optionally return follow-up actions.

        Default implementation returns None (no follow-up).
        """
        return None

    async def close(self):
        """Clean up resources."""


class LLMEngine(DecisionEngine):
    """LLM-based decision engine using function calling.

    This wraps the existing LLM provider + conversation logic from the
    original XBWorldAgent, extracted into a reusable engine.
    """

    engine_type = "llm"

    def __init__(self, provider, system_prompt: str, llm_model: str):
        self.provider = provider
        self.llm_model = llm_model
        self.conversation: list[dict] = [{"role": "system", "content": system_prompt}]
        self._http_session = None
        self._max_iterations = 5

    async def _get_session(self):
        import aiohttp
        from config import TURN_TIMEOUT_SECONDS
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(
                total=TURN_TIMEOUT_SECONDS,
                sock_read=TURN_TIMEOUT_SECONDS,
            )
            self._http_session = aiohttp.ClientSession(timeout=timeout)
        return self._http_session

    async def decide(self, state, available_tools, context=None):
        turn = state.get("turn", "?")
        prompt_parts = [
            f"Turn {turn}. Issue ALL actions in ONE batch, then call end_turn. "
            f"Do NOT call query tools — state is below. Be fast.\n"
        ]
        prompt_parts.append(_format_state_for_llm(state))

        if context and context.get("user_command"):
            prompt_parts.append(f"\n[User Command]\n{context['user_command']}")

        self.conversation.append({"role": "user", "content": "\n".join(prompt_parts)})

        all_calls = []
        for _ in range(self._max_iterations):
            session = await self._get_session()
            data = await self.provider.call(session, self.conversation, available_tools)
            parsed = self.provider.parse_response(data)
            if parsed is None:
                while self.conversation and self.conversation[-1].get("role") in ("tool", "assistant"):
                    self.conversation.pop()
                break

            text = parsed.get("text", "")
            func_calls = parsed.get("tool_calls", [])
            raw_assistant = parsed.get("raw_assistant")
            self.conversation.append(raw_assistant or {"role": "assistant", "content": text})

            if func_calls:
                all_calls.extend(ToolCall(fc["name"], fc.get("args", {})) for fc in func_calls)
                break

            break

        self._trim_conversation()
        return all_calls

    async def on_results(self, results, state, available_tools):
        tool_msg = self.provider.format_tool_results(
            results,
            [{"name": r["name"]} for r in results],
        )
        self.conversation.append(tool_msg)

        session = await self._get_session()
        data = await self.provider.call(session, self.conversation, available_tools)
        parsed = self.provider.parse_response(data)
        if not parsed:
            return None

        text = parsed.get("text", "")
        func_calls = parsed.get("tool_calls", [])
        raw_assistant = parsed.get("raw_assistant")
        self.conversation.append(raw_assistant or {"role": "assistant", "content": text})

        if func_calls:
            return [ToolCall(fc["name"], fc.get("args", {})) for fc in func_calls]
        return None

    def _trim_conversation(self):
        if len(self.conversation) > 16:
            self.conversation = [self.conversation[0]] + self.conversation[-10:]

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()


class RuleBasedEngine(DecisionEngine):
    """Simple rule-based agent for testing and baseline comparison.

    Follows a fixed priority: found cities > explore > fortify > end turn.
    """

    engine_type = "rule_based"

    async def decide(self, state, available_tools, context=None):
        actions: list[ToolCall] = []
        my_units = state.get("units", [])
        my_cities = state.get("cities", [])

        for unit in my_units:
            unit_type = unit.get("type_name", "").lower()
            unit_id = unit.get("id")
            mp = unit.get("movesleft", 0)
            activity = unit.get("activity", 0)

            if mp <= 0:
                continue

            if "settler" in unit_type and len(my_cities) < 4:
                actions.append(ToolCall("found_city", {
                    "unit_id": unit_id,
                    "city_name": f"City{len(my_cities) + 1}",
                }))
            elif activity == 0:
                actions.append(ToolCall("auto_explore_unit", {"unit_id": unit_id}))

        if not state.get("research", {}).get("researching_name"):
            actions.append(ToolCall("set_research_target", {"tech_name": "Alphabet"}))

        actions.append(ToolCall("end_turn", {}))
        return actions


class ExternalEngine(DecisionEngine):
    """Placeholder engine for externally-controlled agents.

    External agents call the /agents/{name}/actions API directly.
    This engine does nothing autonomously — it just ends the turn
    if no external actions were received.
    """

    engine_type = "external"

    def __init__(self):
        self._pending_actions: list[ToolCall] = []

    def submit_actions(self, actions: list[ToolCall]):
        self._pending_actions.extend(actions)

    async def decide(self, state, available_tools, context=None):
        if self._pending_actions:
            actions = self._pending_actions[:]
            self._pending_actions.clear()
            return actions
        return [ToolCall("end_turn", {})]


def _format_state_for_llm(state: dict) -> str:
    """Convert structured state to concise text for LLM consumption."""
    lines = []
    p = state.get("player", {})
    lines.append(
        f"Turn {state.get('turn')} | Phase: {state.get('phase')}\n"
        f"Gold: {p.get('gold', '?')} | Tax: {p.get('tax', '?')}% "
        f"Sci: {p.get('science', '?')}% Lux: {p.get('luxury', '?')}%\n"
        f"Government: {p.get('government', '?')}"
    )

    cities = state.get("cities", [])
    if cities:
        city_lines = [f"  [{c['id']}] {c['name']} (pop {c['size']}) producing {c.get('production', '?')}"
                      for c in cities]
        lines.append("My cities:\n" + "\n".join(city_lines))
    else:
        lines.append("No cities.")

    units = state.get("units", [])
    if units:
        unit_lines = [f"  [{u['id']}] {u['type_name']} @ tile {u['tile']} HP:{u['hp']} MP:{u['movesleft']}"
                      for u in units]
        lines.append("My units:\n" + "\n".join(unit_lines))
    else:
        lines.append("No units.")

    research = state.get("research", {})
    lines.append(
        f"Researching: {research.get('researching_name', 'None')} "
        f"({research.get('bulbs_researched', 0)}/{research.get('researching_cost', 0)})"
    )

    enemies = state.get("visible_enemies", [])
    if enemies:
        enemy_lines = [f"  [{e['id']}] {e['type_name']} owner={e['owner_name']} @ tile {e['tile']}"
                       for e in enemies]
        lines.append(f"Visible enemies ({len(enemies)}):\n" + "\n".join(enemy_lines))

    return "\n".join(lines)
