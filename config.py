"""Configuration for XBWorld Game Server."""

import os

# Server
SERVER_HOST = os.getenv("XBWORLD_HOST", "localhost")
SERVER_PORT = int(os.getenv("PORT", os.getenv("XBWORLD_PORT", "8080")))

_USE_TLS = SERVER_PORT == 443 or os.getenv("XBWORLD_TLS", "").lower() in ("1", "true")
_HTTP_SCHEME = "https" if _USE_TLS else "http"
_WS_SCHEME = "wss" if _USE_TLS else "ws"
_PORT_SUFFIX = "" if SERVER_PORT in (80, 443) else f":{SERVER_PORT}"

LAUNCHER_URL = f"{_HTTP_SCHEME}://{SERVER_HOST}{_PORT_SUFFIX}/civclientlauncher"
WS_BASE_URL = f"{_WS_SCHEME}://{SERVER_HOST}{_PORT_SUFFIX}/civsocket"

# Game protocol (must match freeciv-server)
FREECIV_VERSION = "+Freeciv.Web.Devel-3.3"
MAJOR_VERSION = 3
MINOR_VERSION = 1
PATCH_VERSION = 90
