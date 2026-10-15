"""Reads and drives the club app through macOS accessibility, with the small Swift helper tools/bin/ax.

Positions are in window points (the top-left corner of the window is 0,0), like the taps of the screen tool.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List


class AxError(Exception):
    pass


@dataclass(frozen=True)
class AxElement:
    role: str
    desc: str
    value: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class AxDump:
    window: AxElement
    elements: List[AxElement]


def _element(raw: Any) -> AxElement:
    return AxElement(str(raw["role"]), str(raw["desc"]), str(raw["value"]),
                     float(raw["x"]), float(raw["y"]), float(raw["w"]), float(raw["h"]))


def parse_dump(text: str) -> AxDump:
    try:
        raw = json.loads(text)
        return AxDump(_element(raw["window"]), [_element(e) for e in raw["elements"]])
    except (ValueError, KeyError, TypeError) as exc:
        raise AxError(f"the accessibility helper gave an answer that cannot be read: {exc}") from exc


class AxClient:
    def __init__(self, binary: Path, process: str, runner: Callable = subprocess.run, timeout: float = 30.0) -> None:
        self.binary = Path(binary)
        self.process = process
        self.runner = runner
        self.timeout = timeout

    def _run(self, *args: str) -> str:
        try:
            done = self.runner([str(self.binary), *args], capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AxError(f"could not run the accessibility helper: {exc}") from exc
        if done.returncode != 0:
            raise AxError((done.stderr or "").strip() or "the accessibility helper failed")
        return done.stdout or ""

    def dump(self) -> AxDump:
        return parse_dump(self._run("dump", self.process))

    def press(self, role: str, desc: str, y: float, tolerance: int = 25) -> None:
        """Press the element of this role and exact description whose center is nearest to y (window points)."""
        self._run("press", self.process, role, desc, str(int(round(y))), str(int(tolerance)))

    def press_in_card(self, card_desc: str, button_desc: str) -> None:
        """Press the button of that exact text inside the one card of that exact text. The helper refuses when the card
        is not on the screen exactly once, or has no such button: a press can never land on another card."""
        self._run("press-in-card", self.process, card_desc, button_desc)

    def scroll(self, direction: str) -> None:
        if direction not in ("down", "up"):
            raise AxError(f"cannot scroll {direction!r}")
        self._run("scroll", self.process, direction)
