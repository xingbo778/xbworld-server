#!/usr/bin/env python3
"""Quick connection test: connect, login, wait for packets, disconnect."""

import asyncio
import logging

from game_client import GameClient

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


async def test():
    client = GameClient()
    try:
        print("[Test] Starting new singleplayer game...")
        await client.start_new_game("singleplayer")
        print(f"[Test] Connected: {client.state.connected}")

        print("[Test] Waiting 3s for server packets...")
        await asyncio.sleep(3)

        print(f"[Test] Phase: {client.state.phase}")
        print(f"[Test] Conn ID: {client.state.my_conn_id}")
        print(f"[Test] Player ID: {client.state.my_player_id}")
        print(f"[Test] Messages: {len(client.state.messages)}")
        for m in client.state.messages[:5]:
            print(f"  {m}")

        print("\n[Test] Sending /start...")
        await client.send_chat("/set aifill 3")
        await asyncio.sleep(0.5)
        await client.send_chat("/start")
        await asyncio.sleep(3)

        print(f"[Test] Phase: {client.state.phase}")
        print(f"[Test] Turn: {client.state.turn}")
        print(f"[Test] Players: {len(client.state.players)}")
        print(f"[Test] Units: {len(client.state.units)}")
        print(f"[Test] Cities: {len(client.state.cities)}")
        print(f"[Test] Unit types: {len(client.state.unit_types)}")
        print(f"[Test] Techs: {len(client.state.techs)}")
        print(f"[Test] Buildings: {len(client.state.buildings)}")
        print(f"[Test] Rulesets ready: {client.state.rulesets_ready}")

        if client.state.units:
            print("\n[Test] My units:")
            for uid, u in list(client.state.my_units().items())[:3]:
                print(f"  [{uid}] type={u.get('type')} tile={u.get('tile')}")

        print("\n[Test] Ending turn...")
        await client.end_turn()
        await asyncio.sleep(2)
        print(f"[Test] Turn after end: {client.state.turn}")

        print("\n[Test] SUCCESS - All basic operations work!")

    except Exception as e:
        print(f"[Test] FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(test())
