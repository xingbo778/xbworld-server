"""Configuration for XBWorld Agent and Server."""

import os

# XBWorld unified server
SERVER_HOST = os.getenv("XBWORLD_HOST", "localhost")
SERVER_PORT = int(os.getenv("PORT", os.getenv("XBWORLD_PORT", "8080")))

_USE_TLS = SERVER_PORT == 443 or os.getenv("XBWORLD_TLS", "").lower() in ("1", "true")
_HTTP_SCHEME = "https" if _USE_TLS else "http"
_WS_SCHEME = "wss" if _USE_TLS else "ws"
_PORT_SUFFIX = "" if SERVER_PORT in (80, 443) else f":{SERVER_PORT}"

LAUNCHER_URL = f"{_HTTP_SCHEME}://{SERVER_HOST}{_PORT_SUFFIX}/civclientlauncher"
WS_BASE_URL = f"{_WS_SCHEME}://{SERVER_HOST}{_PORT_SUFFIX}/civsocket"

# Legacy aliases (for backward compatibility)
NGINX_HOST = SERVER_HOST
NGINX_PORT = SERVER_PORT

# Game protocol (server compatibility — must match freeciv-server)
FREECIV_VERSION = "+Freeciv.Web.Devel-3.3"
MAJOR_VERSION = 3
MINOR_VERSION = 1
PATCH_VERSION = 90

# LLM configuration
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gemini-3-flash-preview")
LLM_API_KEY = os.getenv("COMPASS_API_KEY", os.getenv("LLM_API_KEY", ""))
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://compass.llm.shopee.io/compass-api/v1")

# Agent behavior
MAX_MESSAGES_KEPT = 200
TURN_TIMEOUT_SECONDS = int(os.getenv("TURN_TIMEOUT", "30"))
GAME_TURN_TIMEOUT = int(os.getenv("GAME_TURN_TIMEOUT", "30"))

# Multi-agent HTTP API
API_HOST = os.getenv("XBWORLD_API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("PORT", os.getenv("XBWORLD_API_PORT", "8080")))
