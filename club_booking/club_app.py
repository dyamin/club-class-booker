"""Starts, activates and restarts the Mac app that runs the club app."""
from __future__ import annotations

import subprocess
import time
from typing import Callable


class ClubApp:
    def __init__(self, name: str, process: str, runner: Callable = subprocess.run,
                 sleep: Callable[[float], None] = time.sleep, quit_wait: float = 10.0) -> None:
        self.name = name
        self.process = process
        self.runner = runner
        self.sleep = sleep
        self.quit_wait = quit_wait

    # `tell application ... to activate` returned without an error and did nothing on macOS 27.2 (beta, 2026-10-15): the app
    # stayed behind the other windows and the taps went to them. System Events can put the process in front.
    _FRONT = ["tell application (item 1 of argv) to activate", "delay 0.3",
              'tell application "System Events"',
              "    set frontmost of process (item 2 of argv) to true",
              # a running app with no window (closed by hand) cannot be clicked: open it again
              '    if (count of windows of process (item 2 of argv)) is 0 then',
              '        do shell script "/usr/bin/open -a " & quoted form of (item 1 of argv)',
              "        delay 1.5",                      # the window needs a moment to come to this desktop
              "    end if",
              "end tell"]

    def _osascript(self, *lines: str):
        # the app name and the process are arguments, not part of the script
        cmd = ["/usr/bin/osascript", "-e", "on run argv"]
        for line in lines:
            cmd += ["-e", line]
        return self.runner(cmd + ["-e", "end run", self.name, self.process], capture_output=True, text=True, timeout=30)

    def _running(self) -> bool:
        return self.runner(["/usr/bin/pgrep", "-x", self.process], capture_output=True, text=True).returncode == 0

    def activate(self) -> None:
        """Bring the app to the front. The first click on a window that is not in front only does this."""
        self._osascript(*self._FRONT)

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
