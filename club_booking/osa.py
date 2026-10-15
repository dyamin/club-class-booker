"""Runs AppleScript. Values are passed as arguments, never pasted into the script text."""
from __future__ import annotations

import os
import time
import subprocess
import tempfile
from typing import Callable, Sequence


class AppleScriptError(Exception):
    pass


def ensure_running(name: str, process: str, runner: Callable = subprocess.run,
                   sleep: Callable[[float], None] = time.sleep, wait: float = 20.0) -> None:
    """Start an app, hidden, when it does not run. A scheduled (launchd) job cannot start Calendar with an Apple
    event: it gets 'Application isn't running' (-600). Terminal can, which hid the problem."""
    def running() -> bool:
        return runner(["/usr/bin/pgrep", "-x", process], capture_output=True, text=True).returncode == 0

    if running():
        return
    runner(["/usr/bin/open", "-g", "-a", name], capture_output=True, text=True)
    for _ in range(max(1, int(wait))):
        if running():
            return
        sleep(1)
    raise AppleScriptError(f"{name} did not start")


def osa_for(name: str, process: str = "", ensure: Callable = ensure_running, runner: Callable = None):
    """An AppleScript runner that makes sure the app is running first."""
    run = runner or run_applescript

    def osa(script: str, args: Sequence[str] = ()) -> str:
        ensure(name, process or name)
        return run(script, args)

    return osa


def run_applescript(script: str, args: Sequence[str] = (), runner: Callable = subprocess.run,
                    timeout: int = 90) -> str:
    """Run a script that starts with 'on run argv'. Returns what the script returns."""
    fd, path = tempfile.mkstemp(suffix=".applescript")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
        try:
            done = runner(["/usr/bin/osascript", path, *[str(a) for a in args]],
                          capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AppleScriptError(f"could not run osascript: {exc}")
    finally:
        os.unlink(path)
    if done.returncode != 0:
        raise AppleScriptError((done.stderr or "").strip() or "osascript failed")
    return (done.stdout or "").strip()
