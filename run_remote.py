#!/usr/bin/env python3
"""
Run a local agent connecting to the remote Railway XBWorld server.

This script:
1. Sets environment variables to point at the remote server
2. Requests a new game via /civclientlauncher
3. Configures aifill (7 AI bots) and starts the game
4. Runs the agent game loop indefinitely

Usage:
    python run_remote.py
    python run_remote.py --join 6003          # join existing game
    python run_remote.py --username MyAgent    # custom name
    python run_remote.py --aifill 4           # fewer AI bots
"""

import os
import sys

# --- Configure environment BEFORE importing anything from the project ---
os.environ["XBWORLD_HOST"] = os.environ.get(
    "XBWORLD_HOST", "xbworld-production.up.railway.app"
)
os.environ["XBWORLD_TLS"] = "1"
os.environ["PORT"] = "443"

# LLM: use OpenAI-compatible API if OPENAI_API_KEY is set,
# otherwise fall back to whatever is in config.py
if os.environ.get("OPENAI_API_KEY") and not os.environ.get("COMPASS_API_KEY"):
    os.environ.setdefault("LLM_MODEL", "gpt-4.1-mini")
    os.environ.setdefault("LLM_API_KEY", os.environ["OPENAI_API_KEY"])
    os.environ.setdefault("LLM_BASE_URL", os.environ.get(
        "OPENAI_BASE_URL", "https://api.openai.com/v1"
    ))

# Now import project modules (they read env vars at import time)
import argparse
import asyncio
import logging

from game_client import GameClient
from agent import XBWorldAgent
from config import LAUNCHER_URL, WS_BASE_URL, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("run_remote")


async def setup_game(client: GameClient, aifill: int = 8):
    """Wait for connection and configure a new game with AI bots."""
    for i in range(10):
        await asyncio.sleep(1)
        if client.state.connected:
            break
    else:
        logger.error("Failed to connect after 10 seconds")
        return False

    logger.info("Connected. Configuring game (aifill=%d)...", aifill)
    await client.send_chat(f"/set aifill {aifill}")
    await asyncio.sleep(0.5)
    await client.send_chat("/set timeout 0")
    await asyncio.sleep(0.5)

    logger.info("Starting game...")
    await client.send_chat("/start")

    # Wait for game to actually start (turn >= 1)
    for i in range(30):
        await asyncio.sleep(1)
        if client.state.turn >= 1:
            logger.info("Game started! Turn %d", client.state.turn)
            return True

    logger.warning("Game did not start within 30s (turn=%d, phase=%s)",
                    client.state.turn, client.state.phase)
    return client.state.turn >= 1


async def main():
    parser = argparse.ArgumentParser(description="XBWorld Remote Agent")
    parser.add_argument("--join", type=int, default=None,
                        help="Join an existing game on this port")
    parser.add_argument("--username", type=str, default="agent",
                        help="Player username (default: agent)")
    parser.add_argument("--aifill", type=int, default=8,
                        help="Total players including agent (default: 8 = 1 agent + 7 AI)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("=== XBWorld Remote Agent ===")
    logger.info("Server: %s", LAUNCHER_URL)
    logger.info("WebSocket: %s", WS_BASE_URL)
    logger.info("LLM: model=%s base_url=%s", LLM_MODEL, LLM_BASE_URL)
    logger.info("Username: %s, Aifill: %d", args.username, args.aifill)

    client = GameClient(username=args.username)

    try:
        if args.join:
            logger.info("Joining existing game on port %d...", args.join)
            await client.join_game(args.join)
        else:
            logger.info("Starting new game...")
            await client.start_new_game("singleplayer")

        if not args.join:
            ok = await setup_game(client, aifill=args.aifill)
            if not ok:
                logger.error("Failed to setup game, exiting")
                return

        agent = XBWorldAgent(client, name=args.username)
        logger.info("Agent created, starting game loop...")
        logger.info("Game port: %d", client.server_port)

        await agent.run_game_loop()

    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)...")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
    finally:
        await client.close()
        logger.info("Client closed. Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
