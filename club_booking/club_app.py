"""Starts, activates and restarts the Mac app that runs the club app."""
from __future__ import annotations

import subprocess
import time
from typing import Callable, Optional, Tuple


class ClubApp:
    def __init__(self, name: str, process: str, runner: Callable = subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, quit_wait: float = 10.0,
                 size: Tuple[int, int] = (1024, 800)) -> None:
        self.name = name
        self.process = process
        self.runner = runner
        self.sleep = sleep
        self.quit_wait = quit_wait
        self.size = size             # every position the program taps was measured in a window of this size

    # `tell application ... to activate` returned without an error and did nothing on macOS 27.2 (beta, 2026-10-15): the app
    # stayed behind the other windows and the taps went to them. System Events can put the process in front.
    _FRONT = ["tell application (item 1 of argv) to activate", "delay 0.3",
              'tell application "System Events"',
              "    set frontmost of process (item 2 of argv) to true",
              "end tell"]
    # How many windows the process has. A running app with no window (closed by hand) cannot be clicked.
    _WINDOWS = ['tell application "System Events" to return count of windows of process (item 2 of argv)']
    WINDOW_WAIT_LOOKS = 10                            # looks, 0.5 seconds apart, for the window to come back
    _SIZE = ['tell application "System Events"',
             "    set z to size of window 1 of process (item 2 of argv)",
             '    return (item 1 of z as text) & "x" & (item 2 of z as text)',
             "end tell"]
    SIZE_TOLERANCE = 4                                # points: the system may round

    def _osascript(self, *lines: str, timeout: float = 30):
        # the app name and the process are arguments, not part of the script
        cmd = ["/usr/bin/osascript", "-e", "on run argv"]
        for line in lines:
            cmd += ["-e", line]
        return self.runner(cmd + ["-e", "end run", self.name, self.process], capture_output=True, text=True, timeout=timeout)

    def _window_count(self) -> int:
        """The number of windows of the app, or -1 when that cannot be read (then nothing is opened on a guess)."""
        try:
            done = self._osascript(*self._WINDOWS, timeout=15)
            return int((done.stdout or "").strip()) if done.returncode == 0 else -1
        except (subprocess.TimeoutExpired, ValueError):
            return -1

    def _reopen_window(self) -> None:
        """A running app with no window is opened again, once, and given a few seconds to show its window. This comes
        BEFORE `tell application ... to activate`, which hangs while the app has no window (2026-10-22: every run of an
        hour failed on its 30-second timeout and never reached the `open -a`)."""
        if self._window_count() != 0:
            return
        self.runner(["/usr/bin/open", "-a", self.name], capture_output=True, text=True, timeout=20)
        for _ in range(self.WINDOW_WAIT_LOOKS):
            if self._window_count() > 0:
                return
            self.sleep(0.5)

    def _running(self) -> bool:
        return self.runner(["/usr/bin/pgrep", "-x", self.process], capture_output=True, text=True).returncode == 0

    def _window_size(self) -> Optional[Tuple[int, int]]:
        try:
            done = self._osascript(*self._SIZE, timeout=15)
            width, _, height = (done.stdout or "").strip().partition("x")
            return (int(width), int(height)) if done.returncode == 0 else None
        except (subprocess.TimeoutExpired, ValueError):
            return None

    def _fit_window(self) -> None:
        """Set the window to the size that every tap position was measured in. A window that the program reopened (or
        that came back after a restart) can be much bigger: on 2026-10-22 it was 2560x1320, the person selector could not
        be closed, and the run failed for everyone. A size that cannot be read is not set on a guess. Never raises."""
        try:
            if self._window_count() < 1:
                return
            now = self._window_size()
            if now is None or all(abs(a - b) <= self.SIZE_TOLERANCE for a, b in zip(now, self.size)):
                return
            self._osascript('tell application "System Events" to set size of window 1 of process (item 2 of argv) '
                            f"to {{{self.size[0]}, {self.size[1]}}}", timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def activate(self) -> None:
        """Bring the app to the front. The first click on a window that is not in front only does this."""
        self._reopen_window()
        self._osascript(*self._FRONT)
        self._fit_window()

    def can_control(self) -> str:
        """Empty when this program can bring the app to the front, else the reason. A scheduled job needs the
        Automation permission for the app; activate() ignores errors on purpose, so it cannot tell."""
        try:
            done = self._osascript(*self._FRONT)
        except subprocess.TimeoutExpired:
            return f"{self.name} did not answer within 30 seconds"
        return "" if done.returncode == 0 else ((done.stderr or "").strip() or "osascript failed")

    def restart(self) -> None:
        """Quit the app, end it if it does not quit (a stuck app may not answer), and open it again.
        The session may be lost: the app can come back on its login screen."""
        self._osascript("tell application (item 1 of argv) to quit")
        for _ in range(max(1, int(self.quit_wait))):
            if not self._running():
                break
            self.sleep(1)
        else:
            self.runner(["/usr/bin/pkill", "-x", self.process], capture_output=True, text=True)
            self.sleep(1)
        self.runner(["/usr/bin/open", "-a", self.name], capture_output=True, text=True)
        for _ in range(self.WINDOW_WAIT_LOOKS):          # the new process needs a moment to show its window
            if self._window_count() > 0:
                break
            self.sleep(0.5)
        self._fit_window()
