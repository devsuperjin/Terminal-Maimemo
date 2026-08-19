"""Read-only live test against the real maimemo study service.

Usage:
    uv run python scripts/live_test.py            # token from config
    uv run python scripts/live_test.py TOKEN      # explicit token

This only reads data (precheck, init, states, one word fetch) — it never
submits an answer, so it won't change your study state.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal_maimemo import config
from terminal_maimemo.client import MaimemoClient


async def main(sid: str) -> None:
    client = MaimemoClient(sid)

    print("== precheck ==")
    try:
        pre = await client.precheck()
        print("precheck ok:", pre)
    except Exception as exc:
        print("precheck failed:", exc)
        return

    print("\n== websocket connect + init ==")
    try:
        await client.connect()
    except Exception as exc:
        print("connect failed:", exc)
        return
    print("user:", client.user)
    print("privileges:", client.privileges)
    print("settings:", client.settings)

    print("\n== study states ==")
    states = await client.get_study_states()
    print("states:", states)

    print("\n== fetch one word (read-only) ==")
    word_resp = await client.get_word()
    word = word_resp.get("word") or {}
    print("spelling:", word.get("spelling"))
    print("phonetic_us:", word.get("phonetic_us"))
    print("phonetic_uk:", word.get("phonetic_uk"))
    for it in word_resp.get("interpretations", [])[:3]:
        print("  -", it.get("interpretation"))
    print("progress:", word_resp.get("progress"))

    await client.close()
    print("\nOK - closed.")


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else config.load_sid()
    if not sid:
        print("no sid; pass one as argv or save it with: terminal_maimemo --sid <sid>")
        sys.exit(1)
    asyncio.run(main(sid))
