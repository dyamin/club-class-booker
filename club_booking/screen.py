"""Talks to the mirroir-mcp tool over stdio: JSON-RPC, one message per line.

The tool sees one window (the club app) and can tap and swipe in it.
Coordinates are window points; they match the pixels of its screenshots.
"""
from __future__ import annotations

import base64
import io
import json
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, UnidentifiedImageError


class ScreenError(Exception):
    pass


_STATUS_RX = re.compile(r"window:\s*(\d+)x(\d+),\s*pos=\((-?\d+),\s*(-?\d+)\)")


_NOT_THERE = ("is not running", "window not found")   # the tool's answers when it has no window to act on


class MirroirScreen:
    def __init__(self, command: Sequence[str], timeout: float = 90.0,
                 popen: Callable = subprocess.Popen, region_runner: Callable = subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, retries: int = 3, retry_wait: float = 1.5) -> None:
        self.command = list(command)
        self.timeout = timeout
        self._sleep, self.retries, self.retry_wait = sleep, retries, retry_wait
        self._popen = popen
        self._region_runner = region_runner
        self._proc: Optional[Any] = None
        self._reader: Optional[threading.Thread] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._next_id = 0
        self._points: Optional[Tuple[int, int]] = None      # window size in points, read once
        self._last_region: Optional[Tuple[int, int, int, int]] = None

    # -- life cycle -----------------------------------------------------------
    def start(self) -> None:
        self._proc = self._popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "club-booking", "version": "0.1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if self._reader is not None:
            self._reader.join(timeout=2)
        try:
            proc.stdout.close()
        except OSError:
            pass

    def reconnect(self) -> None:
        """Start the tool again, so that it finds the window of an app that was quit and opened again."""
        self.close()
        self._lines = queue.Queue()
        self._points = None
        self._last_region = None
        self.start()

    def __enter__(self) -> "MirroirScreen":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- JSON-RPC plumbing ------------------------------------------------------
    def _read_loop(self) -> None:
        assert self._proc is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: Dict[str, Any]) -> None:
        if self._proc is None:
            raise ScreenError("the screen tool is not running")
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ScreenError(f"could not write to the screen tool: {exc}")

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        wanted = self._next_id
        self._send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params})
        while True:
            try:
                line = self._lines.get(timeout=self.timeout)
            except queue.Empty:
                raise ScreenError(f"no answer from the screen tool within {self.timeout} seconds ({method})")
            if line is None:
                raise ScreenError("the screen tool stopped")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != wanted:
                continue          # a notification or a late answer
            if "error" in message:
                raise ScreenError(f"{method} failed: {message['error']}")
            return message.get("result", {})

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The tool says "Target ... is not running", or "Target ... window not found", for a moment (right after
        the app was brought to the front, or when it lost the window). It says so before it acts: nothing was
        done then (no tap, no picture), so the call is tried again a few times."""
        for attempt in range(self.retries + 1):
            try:
                return self._call_tool_once(name, arguments)
            except ScreenError as exc:
                if not any(m in str(exc) for m in _NOT_THERE) or attempt == self.retries:
                    raise
                self._sleep(self.retry_wait)
        raise ScreenError("unreachable")

    def _call_tool_once(self, name: str, arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            raise ScreenError(f"tool {name} reported an error: {text}")
        return content

    @staticmethod
    def _text(content: List[Dict[str, Any]]) -> str:
        return " ".join(c.get("text", "") for c in content if c.get("type") == "text")

    # -- what the program uses ------------------------------------------------------
    def tap(self, x: int, y: int) -> str:
        return self._text(self._call_tool("tap", {"x": int(x), "y": int(y)}))

    def swipe(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 900) -> str:
        return self._text(self._call_tool("swipe", {"from_x": int(from_x), "from_y": int(from_y),
                                                    "to_x": int(to_x), "to_y": int(to_y),
                                                    "duration_ms": int(duration_ms)}))

    def screenshot(self) -> bytes:
        """The picture of the app window, scaled to window points: the tool returns native pixels (twice as many
        on a Retina screen), and every position the program uses is in points."""
        for item in self._call_tool("screenshot", {}):
            if item.get("type") == "image":
                if self._points is None:
                    _x, _y, w, h = self.window_region()
                    self._points = (w, h)
                return self._in_points(base64.b64decode(item["data"]), self._points)
        raise ScreenError("the screenshot tool returned no image")

    @staticmethod
    def _in_points(data: bytes, size: Tuple[int, int]) -> bytes:
        try:
            img = Image.open(io.BytesIO(data))
            if img.size == size:
                return data
            buf = io.BytesIO()
            img.convert("RGB").resize(size, Image.LANCZOS).save(buf, format="PNG")
            return buf.getvalue()
        except (UnidentifiedImageError, OSError) as exc:
            raise ScreenError(f"the picture is not readable: {exc}") from exc

    def status(self) -> str:
        return self._text(self._call_tool("status", {}))

    def window_region(self) -> Tuple[int, int, int, int]:
        """x, y, width, height of the app window on the screen, from the status text."""
        text = self.status()
        match = _STATUS_RX.search(text)
        if not text.strip().startswith("Connected") or not match:
            if "No window" in text and self._last_region is not None:
                # seen while the calendar editor is open: the tool loses the window, which did not move.
                # A stale region is safe: the readers then find nothing and the step fails with an error.
                return self._last_region
            raise ScreenError(f"the app window is not ready: {text!r}")
        w, h, x, y = (int(g) for g in match.groups())
        self._last_region = (x, y, w, h)
        return self._last_region

    def region_png(self) -> bytes:
        """Screenshot of the screen area of the app window. It also shows pop-up menus.
        screencapture writes native pixels (twice as many on a Retina screen). Taps and text positions are in
        window points, so the image is scaled to the window size."""
        x, y, w, h = self.window_region()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "region.png"
            done = self._region_runner(["/usr/sbin/screencapture", "-x", "-R", f"{x},{y},{w},{h}", str(path)],
                                       capture_output=True, text=True, timeout=30)
            if done.returncode != 0 or not path.exists():
                raise ScreenError(f"screencapture failed: {(done.stderr or '').strip()}")
            data = path.read_bytes()
        return self._in_points(data, (w, h))
