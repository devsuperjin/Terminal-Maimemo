"""Regression tests for study-screen keyboard sequencing."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from terminal_maimemo.screens import StudyScreen


class _UnexpectedClientCall:
    async def submit_response(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("grading submitted before Space revealed the answer")


async def _check_numbers_before_space_are_ignored() -> None:
    for response in range(1, 5):
        screen = object.__new__(StudyScreen)
        screen._busy = False
        screen.current = {"word": {"id": "w1"}}
        screen.revealed = False
        screen.client = _UnexpectedClientCall()

        await screen._grade(response)


asyncio.run(_check_numbers_before_space_are_ignored())
print("ok: 1-4 are ignored until Space reveals the answer")

assert StudyScreen.MEMORY_HISTORY_COLORS[1] == "#82d9be"
assert StudyScreen.MEMORY_HISTORY_COLORS[2] == "#f4c96b"
assert StudyScreen.MEMORY_HISTORY_COLORS[3] == "#f2a38f"
assert StudyScreen._format_history_day(-1) == "Today"
assert StudyScreen._format_history_day(458) == "D458"
print("ok: memory history keeps compact colored day markers")
