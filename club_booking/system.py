"""Questions about the Mac itself."""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from typing import Callable, Iterator


def screen_locked(runner: Callable = subprocess.run) -> bool:
    """True when the login screen is up. Screenshots and mouse clicks do not work then."""
    done = runner(["/usr/sbin/ioreg", "-n", "Root", "-d1"], capture_output=True, text=True, timeout=15)
    return bool(re.search(r'"CGSSessionScreenIsLocked"\s*=\s*(Yes|1|true)', done.stdout or "", re.IGNORECASE))


def display_asleep() -> bool:
    """True when the main display is asleep. Then a screenshot comes out black or fails, and the club app's window has
    no size for accessibility (seen on 2026-10-13: the runs of 18:58 failed this way). Read through CoreGraphics."""
    import ctypes
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
        cg.CGDisplayIsAsleep.restype = ctypes.c_int
        return bool(cg.CGDisplayIsAsleep(cg.CGMainDisplayID()))
    except Exception:            # a probe that fails must not crash the tick: count the display as awake
        return False


def wake_display(start: Callable = subprocess.Popen) -> None:
    """Say that the user is active: the display turns on. Does not wait for the five seconds to pass."""
    start(["/usr/bin/caffeinate", "-u", "-t", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextlib.contextmanager
def keep_display_awake(start: Callable = subprocess.Popen, pid: int | None = None) -> Iterator[None]:
    """Hold the display on while the program uses the screen. The wake-up above only resets the idle timer: on the
    Sabbath of 2026-10-17 the display went dark about 30 seconds later, in the middle of a run, and the screenshots
    failed. `caffeinate -d` holds it on; `-i` keeps the Mac from idle sleep; `-w` makes it end by itself when this
    process dies. A hold that cannot start never stops the run (the run then fails the old way, in the log)."""
    process = None
    try:
        process = start(["/usr/bin/caffeinate", "-d", "-i", "-w", str(pid if pid is not None else os.getpid())],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        process = None
    try:
        yield
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:           # it ends by itself with this process (-w)
                pass


def screen_unavailable(locked: Callable[[], bool] = screen_locked, asleep: Callable[[], bool] = display_asleep) -> bool:
    """True when the program cannot see or click now: the login screen is up, or the display is asleep. Only looks:
    a display is woken only when a class is due (wake_if_only_asleep), never every few minutes."""
    return locked() or asleep()


def wake_if_only_asleep(locked: Callable[[], bool] = screen_locked, asleep: Callable[[], bool] = display_asleep,
                        wake: Callable[[], None] = wake_display, sleep: Callable[[float], None] = time.sleep) -> bool:
    """True when the screen is usable after a sleeping display was woken. A locked screen needs a password: it is not
    woken. A display that wakes to the login screen is not usable either."""
    if locked():
        return False
    if not asleep():
        return True
    wake()
    sleep(2)
    return not locked() and not asleep()


_TYPE_SCRIPT = [
    "on run argv",
    "tell application (item 1 of argv) to activate",
    "delay 0.3",
    'tell application "System Events"',
    "    set frontmost of process (item 2 of argv) to true",       # `activate` alone did not work on macOS 27.2 (beta)
    "    delay 0.3",
    '    if (name of first application process whose frontmost is true) is not (item 2 of argv) then error "not in front"',
    '    if (item 4 of argv) is "1" then',
    '        keystroke "a" using command down',
    "        key code 51",
    "    end if",
    "    keystroke (item 3 of argv)",
    "end tell",
    "end run",
]


def type_into(app_name: str, process: str, text: str, clear: bool = False, runner: Callable = subprocess.run) -> bool:
    """Bring the app to the front, check that it is in front, optionally select all and delete, and type text.
    All in ONE script, so that the check and the typing are a few milliseconds apart: typing goes to the front app,
    and other apps (the terminal, the editor) keep taking the front. Returns False when nothing was typed.
    The text and the names are arguments of the script, never a part of it."""
    cmd = ["/usr/bin/osascript"] + [part for line in _TYPE_SCRIPT for part in ("-e", line)] + [
        app_name, process, text, "1" if clear else "0"]
    try:
        done = runner(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def system_events_problem(runner: Callable = subprocess.run) -> str:
    """'' when this job may control System Events: the automatic login types the code through it. A scheduled job has
    its own permissions (not Terminal's), so the wake-up test asks from the job. Nothing is typed."""
    try:
        done = runner(["/usr/bin/osascript", "-e",
                       'tell application "System Events" to get name of first application process whose frontmost is true'],
                      capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run osascript: {exc}"
    return "" if done.returncode == 0 else ((done.stderr or "").strip() or "System Events refused")


def idle_seconds(runner: Callable = subprocess.run) -> float:
    """Seconds since the last keyboard or mouse input. A scheduled run waits for the user to be away.
    When the value cannot be read, the user counts as away (infinity): waiting for ever is the worse error."""
    done = runner(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=15)
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', done.stdout or "")
    return int(match.group(1)) / 1e9 if match else float("inf")
