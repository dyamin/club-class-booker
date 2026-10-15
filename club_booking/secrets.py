"""The API key lives in the macOS Keychain. Never in a file, a log, or a message."""
from __future__ import annotations

import subprocess
from typing import Callable


class SecretError(Exception):
    pass


def store_secret(service: str, value: str, account: str = "", runner: Callable = subprocess.run) -> None:
    """Add (or update) a Keychain item. The value is on the command line of the `security` tool for a moment: that is the
    tool's only way to take it without a terminal."""
    import getpass
    account = account or getpass.getuser()
    try:
        done = runner(["/usr/bin/security", "add-generic-password", "-a", account, "-s", service, "-w", value, "-U"],
                      capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretError(f"could not run the Keychain tool: {exc}")
    if done.returncode != 0:
        raise SecretError(f"the Keychain refused to store '{service}': {(done.stderr or '').strip()[:120]}")


def get_api_key(service: str, runner: Callable = subprocess.run) -> str:
    try:
        done = runner(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                      capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretError(f"could not run the Keychain tool: {exc}")
    key = (done.stdout or "").strip()
    if done.returncode != 0 or not key:
        raise SecretError(
            f"no API key in the Keychain under the service name '{service}'. "
            f"Add it with: security add-generic-password -a \"$USER\" -s {service} -w")
    return key
