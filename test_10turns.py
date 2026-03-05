#!/usr/bin/env python3
"""10-turn smoke test: start a game, play 10 turns with basic actions, verify stability."""

import asyncio
import logging
import time

from game_client import GameClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("test-10turns")

TARGET_TURNS = 10


async def play_turn(client: GameClient, turn: int):
    """Simple turn: move units toward unexplored tiles, end turn."""
    my_units = client.state.my_units()
    for uid, unit in my_units.items():
        tile = unit.get("tile")
        if tile is not None:
            # Try moving each unit in a direction based on turn number
            dx = [0, 1, 1, 0, -1, -1, -1, 0, 1][turn % 9]
            dy = [1, 1, 0, -1, -1, 0, 1, 1, -1][turn % 9]
            try:
                await client.unit_action(uid, "move", dx=dx, dy=dy)
            except Exception:
                pass
    await client.end_turn()


async def test():
    client = GameClient()
    start_time = time.time()
    errors = []

    try:
        log.info("Starting singleplayer game...")
        await client.start_new_game("singleplayer")
        log.info("Connected: conn_id=%s player_id=%s", client.state.my_conn_id, client.state.my_player_id)

        await asyncio.sleep(2)

        log.info("Setting up game: aifill=3, timeout=0")
        await client.send_chat("/set aifill 3")
        await asyncio.sleep(0.3)
        await client.send_chat("/set timeout 0")
        await asyncio.sleep(0.3)
        await client.send_chat("/start")
        await asyncio.sleep(2)

        if not client.state.rulesets_ready:
            errors.append("Rulesets not loaded")
            log.error("FAIL: Rulesets not loaded")
            return

        log.info("Game started: turn=%d players=%d units=%d",
                 client.state.turn, len(client.state.players), len(client.state.units))

        # Play turns
        for target_turn in range(1, TARGET_TURNS + 1):
            turn_start = time.time()

            # Wait for our turn
            timeout = 15
            while client.state.turn < target_turn and (time.time() - turn_start) < timeout:
                await asyncio.sleep(0.2)

            if client.state.turn < target_turn:
                errors.append(f"Stuck at turn {client.state.turn}, expected {target_turn}")
                log.error("FAIL: Stuck at turn %d (expected %d)", client.state.turn, target_turn)
                break

            my_units = client.state.my_units()
            my_cities = {cid: c for cid, c in client.state.cities.items()
                         if c.get("owner") == client.state.my_player_id}

            log.info("Turn %d: units=%d cities=%d elapsed=%.1fs",
                     target_turn, len(my_units), len(my_cities),
                     time.time() - start_time)

            await play_turn(client, target_turn)
            await asyncio.sleep(0.5)

        # Final checks
        final_turn = client.state.turn
        elapsed = time.time() - start_time
        total_units = len(client.state.my_units())
        total_cities = len({cid: c for cid, c in client.state.cities.items()
                           if c.get("owner") == client.state.my_player_id})

        log.info("=" * 60)
        log.info("RESULTS:")
        log.info("  Final turn: %d / %d", final_turn, TARGET_TURNS)
        log.info("  Units: %d", total_units)
        log.info("  Cities: %d", total_cities)
        log.info("  Players: %d", len(client.state.players))
        log.info("  Elapsed: %.1fs", elapsed)
        log.info("  Packets processed: %d", getattr(client.state, 'packets_processed', -1))
        log.info("  Errors: %d", len(errors))
        log.info("=" * 60)

        if final_turn < TARGET_TURNS:
            errors.append(f"Only reached turn {final_turn}/{TARGET_TURNS}")

        if errors:
            log.error("FAILED: %s", "; ".join(errors))
        else:
            log.info("PASSED: 10-turn smoke test completed successfully")

    except Exception as e:
        log.error("FAILED with exception: %s", e)
        import traceback
        traceback.print_exc()
        errors.append(str(e))
    finally:
        await client.close()

    return len(errors) == 0


if __name__ == "__main__":
    success = asyncio.run(test())
    exit(0 if success else 1)
