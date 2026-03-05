"""
Pluggable LLM provider abstraction.

Each provider translates between a common conversation format (OpenAI-style
messages) and the provider's native API.  Providers are stateless — the HTTP
session is passed in from the caller so it can be reused across calls.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

logger = logging.getLogger("xbworld-agent")


class LLMProvider(ABC):
    """Base class for LLM providers."""

    name: str = "base"

    @abstractmethod
    async def call(
        self,
        session: aiohttp.ClientSession,
        messages: list[dict],
        tool_definitions: list[dict],
    ) -> dict:
        """Send a chat-completion request and return the raw JSON response."""

    @abstractmethod
    def parse_response(self, data: dict | None) -> dict | None:
        """Parse the raw response into a common format.

        Returns ``None`` on failure, otherwise::

            {
                "text": str,
                "tool_calls": [{"name": str, "args": dict}, ...],
                "raw_assistant": <provider-specific message to append to conversation>,
            }
        """

    @abstractmethod
    def format_tool_results(
        self,
        results: list[dict],
        original_calls: list[dict],
    ) -> dict:
        """Format tool execution results into a conversation message."""


# ---------------------------------------------------------------------------
# Gemini (native generateContent via Compass or direct)
# ---------------------------------------------------------------------------

class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model.removeprefix("openai/")
        self.api_key = api_key
        self.base_url = base_url

    async def call(self, session, messages, tool_definitions):
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        system_text, contents = self._to_contents(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "tools": [{"functionDeclarations": self._to_declarations(tool_definitions)}],
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}

        logger.debug("[gemini] POST %s (contents=%d, tools=%d)", url, len(contents), len(tool_definitions))
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error("[gemini] HTTP %d: %s", resp.status, text[:300])
                raise RuntimeError(f"Gemini HTTP {resp.status}: {text[:500]}")
            data = await resp.json()
            candidates = data.get("candidates", [])
            logger.debug("[gemini] Response: %d candidates", len(candidates))
            return data

    def parse_response(self, data):
        if not data:
            return None
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None

        func_calls = []
        text_parts = []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                func_calls.append({"name": fc.get("name", ""), "args": fc.get("args", {})})
            if "text" in p:
                text_parts.append(p["text"])

        return {
            "text": "\n".join(t for t in text_parts if t),
            "tool_calls": func_calls,
            "raw_assistant": {
                "role": "assistant",
                "content": "\n".join(t for t in text_parts if t),
                "_gemini_parts": parts,
            },
        }

    def format_tool_results(self, results, original_calls):
        response_parts = []
        for r in results:
            response_parts.append({
                "functionResponse": {
                    "name": r["name"],
                    "response": {"result": r["result"]},
                }
            })
        return {
            "role": "tool",
            "_gemini_response_parts": response_parts,
            "content": "",
        }

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _to_contents(messages: list[dict]) -> tuple[str, list[dict]]:
        system_text = ""
        contents: list[dict] = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                system_text = msg.get("content", "")
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": msg.get("content", "")}]})
            elif role == "assistant":
                parts = msg.get("_gemini_parts")
                if not parts:
                    c = msg.get("content", "")
                    parts = [{"text": c}] if c else []
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                rp = msg.get("_gemini_response_parts")
                if rp:
                    contents.append({"role": "user", "parts": rp})
                else:
                    fn = msg.get("_fn_name", "unknown")
                    contents.append({
                        "role": "user",
                        "parts": [{"functionResponse": {"name": fn, "response": {"result": msg.get("content", "")}}}],
                    })
        return system_text, contents

    @staticmethod
    def _to_declarations(tool_defs: list[dict]) -> list[dict]:
        decls = []
        for t in tool_defs:
            fn = t.get("function", {})
            params = fn.get("parameters", {})
            gp = GeminiProvider._convert_params(params)
            decls.append({"name": fn["name"], "description": fn.get("description", ""), "parameters": gp})
        return decls

    @staticmethod
    def _convert_params(params: dict) -> dict:
        result: dict[str, Any] = {}
        if "type" in params:
            result["type"] = params["type"].upper()
        if "properties" in params:
            result["properties"] = {}
            for k, v in params["properties"].items():
                prop: dict[str, Any] = {}
                if "type" in v:
                    prop["type"] = v["type"].upper()
                if "description" in v:
                    prop["description"] = v["description"]
                if "enum" in v:
                    prop["enum"] = v["enum"]
                result["properties"][k] = prop
        if "required" in params:
            result["required"] = params["required"]
        return result


# ---------------------------------------------------------------------------
# OpenAI-compatible (works with any OpenAI-API provider)
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def call(self, session, messages, tool_definitions):
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        clean_msgs = []
        for m in messages:
            role = m["role"]
            if role == "assistant" and m.get("tool_calls"):
                # Assistant message with tool_calls: content can be null
                clean = {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": m["tool_calls"],
                }
            elif role == "tool":
                # Tool response message: must have tool_call_id
                clean = {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }
            else:
                clean = {"role": role, "content": m.get("content", "")}
            clean_msgs.append(clean)

        body = {
            "model": self.model,
            "messages": clean_msgs,
            "tools": tool_definitions,
        }

        logger.debug("[openai] POST %s model=%s (msgs=%d, tools=%d)", url, self.model, len(clean_msgs), len(tool_definitions))
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error("[openai] HTTP %d: %s", resp.status, text[:300])
                raise RuntimeError(f"OpenAI HTTP {resp.status}: {text[:500]}")
            data = await resp.json()
            choices = data.get("choices", [])
            usage = data.get("usage", {})
            logger.debug("[openai] Response: %d choices, usage=%s", len(choices), usage)
            return data

    def parse_response(self, data):
        if not data:
            return None
        choices = data.get("choices", [])
        if not choices:
            return None
        msg = choices[0].get("message", {})
        text = msg.get("content", "") or ""
        tool_calls_raw = msg.get("tool_calls", [])
        tool_calls = []
        for tc in tool_calls_raw:
            fn = tc.get("function", {})
            import json
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"name": fn.get("name", ""), "args": args, "id": tc.get("id")})

        return {
            "text": text,
            "tool_calls": tool_calls,
            "raw_assistant": msg,
        }

    def format_tool_results(self, results, original_calls):
        """Return a list of tool messages, one per tool_call_id.

        OpenAI requires each tool_call_id from the assistant message to have
        a corresponding tool-role response message.
        """
        msgs = []
        for i, fc in enumerate(original_calls):
            tool_call_id = fc.get("id", "")
            # Match result by index (results and original_calls are aligned)
            if i < len(results):
                content = f"{results[i]['name']}: {results[i]['result']}"
            else:
                content = "(no result)"
            msgs.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            })
        return msgs


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_provider(model: str, api_key: str, base_url: str) -> LLMProvider:
    """Auto-detect provider from model name or base URL."""
    ml = model.lower()
    if "gemini" in ml or "generateContent" in base_url:
        return GeminiProvider(model, api_key, base_url)
    return OpenAIProvider(model, api_key, base_url)
