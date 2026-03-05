#!/usr/bin/env python3
"""Test: 2 agents join the same multiplayer game, verify independent state."""

import asyncio
import logging

from game_client import GameClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("test-multi")


async def test():
    client1 = GameClient(username="alpha")
    client2 = GameClient(username="beta")

    try:
        print("[Test] Agent 'alpha' requesting multiplayer server...")
        await client1.start_new_game("multiplayer")
        print(f"[Test] alpha connected: {client1.state.connected}, conn_id={client1.state.my_conn_id}")

        await asyncio.sleep(2)

        port = -1
        for msg in client1.state.messages:
            text = msg.get("text", "")
            if "Server at port" in text:
                import re
                m = re.search(r"port (\d+)", text)
                if m:
                    port = int(m.group(1))
                    break

        if port < 0:
            print("[Test] FAILED: Could not extract server port from messages")
            print(f"  Messages: {client1.state.messages}")
            return

        print(f"[Test] Server port: {port}")
        print(f"[Test] Agent 'beta' joining same server on port {port}...")
        await client2.join_game(port)
        await asyncio.sleep(2)

        print(f"[Test] beta connected: {client2.state.connected}, conn_id={client2.state.my_conn_id}")

        assert client1.state.connected, "alpha not connected"
        assert client2.state.connected, "beta not connected"
        assert client1.state.my_conn_id != client2.state.my_conn_id, "Same conn_id!"

        print("[Test] Configuring game...")
        await client1.send_chat("/set aifill 3")
        await asyncio.sleep(0.5)
        await client1.send_chat("/set timeout 0")
        await asyncio.sleep(0.5)

        print("[Test] Starting game (both players must be ready)...")
        await client1.send_chat("/start")
        await asyncio.sleep(0.5)
        await client2.send_chat("/start")

        for i in range(10):
            await asyncio.sleep(1)
            if client1.state.turn >= 1:
                break
            if i == 9:
                print("[Test] Game still not started after 10s, dumping messages:")
                for m in client1.state.messages:
                    print(f"  {m.get('text', '')[:120]}")

        print(f"[Test] alpha: player_id={client1.state.my_player_id}, turn={client1.state.turn}, "
              f"phase={client1.state.phase}, units={len(client1.state.my_units())}")
        print(f"[Test] beta:  player_id={client2.state.my_player_id}, turn={client2.state.turn}, "
              f"phase={client2.state.phase}, units={len(client2.state.my_units())}")

        assert client1.state.my_player_id >= 0, "alpha has no player"
        assert client2.state.my_player_id >= 0, "beta has no player"
        assert client1.state.my_player_id != client2.state.my_player_id, "Same player_id!"
        assert client1.state.turn >= 1, f"Game didn't start (turn={client1.state.turn})"

        alpha_units = client1.state.my_units()
        beta_units = client2.state.my_units()
        print(f"[Test] alpha owns {len(alpha_units)} units, beta owns {len(beta_units)} units")

        assert len(alpha_units) > 0, "alpha has no units"
        assert len(beta_units) > 0, "beta has no units"

        alpha_unit_ids = set(alpha_units.keys())
        beta_unit_ids = set(beta_units.keys())
        assert alpha_unit_ids.isdisjoint(beta_unit_ids), "Units overlap between players!"

        print("\n[Test] Both agents ending turn...")
        await client1.end_turn()
        await asyncio.sleep(0.5)
        await client2.end_turn()
        await asyncio.sleep(3)

        print(f"[Test] alpha turn={client1.state.turn}, beta turn={client2.state.turn}")
        assert client1.state.turn >= 2, "Turn didn't advance"

        print("\n[Test] SUCCESS - Multi-agent multiplayer works!")
        print(f"  alpha: player {client1.state.my_player_id}, {len(client1.state.my_units())} units")
        print(f"  beta:  player {client2.state.my_player_id}, {len(client2.state.my_units())} units")
        print(f"  Total players in game: {len(client1.state.players)}")

    except AssertionError as e:
        print(f"[Test] ASSERTION FAILED: {e}")
    except Exception as e:
        print(f"[Test] FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client1.close()
        await client2.close()


if __name__ == "__main__":
    asyncio.run(test())
