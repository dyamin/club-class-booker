"""Settings the screen tool needs in ~/.mirroir-mcp: which app window, and which tools it may use."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, List

TARGETS = {
    "targets": {"club": {"type": "macos-app", "bundle_id": "il.co.offline.hashmonaim", "process_name": "Runner"}},
    "default_target": "club",
}
PERMISSIONS = {"allow": ["tap", "swipe"], "deny": [], "blockedApps": []}


def init_mirroir_config(home: Path) -> List[str]:
    """Create the two files if they are missing. Never overwrite a file that exists."""
    folder = Path(home) / ".mirroir-mcp"
    folder.mkdir(parents=True, exist_ok=True)
    made: List[str] = []
    for name, data in (("targets.json", TARGETS), ("permissions.json", PERMISSIONS)):
        path = folder / name
        if not path.exists():
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            made.append(str(path))
    return made


def check_mirroir_config(home: Path) -> List[str]:
    """Problems with the two files, as sentences. An empty list means fine."""
    folder = Path(home) / ".mirroir-mcp"
    problems: List[str] = []
    try:
        targets = json.loads((folder / "targets.json").read_text(encoding="utf-8"))
        club = targets["targets"][targets["default_target"]]
        if club.get("bundle_id") != TARGETS["targets"]["club"]["bundle_id"]:
            problems.append(f"targets.json points at {club.get('bundle_id')!r}, not the club app")
    except (OSError, KeyError, json.JSONDecodeError):
        problems.append("targets.json is missing or wrong (run: python -m club_booking init-mirroir)")
    try:
        allow = set(json.loads((folder / "permissions.json").read_text(encoding="utf-8")).get("allow", []))
        if not {"tap", "swipe"} <= allow:
            problems.append("permissions.json must allow the tools tap and swipe")
    except (OSError, json.JSONDecodeError):
        problems.append("permissions.json is missing or wrong (run: python -m club_booking init-mirroir)")
    return problems


def check_mirroir_permissions(binary: Path, runner: Callable = subprocess.run) -> List[str]:
    """Run the screen tool's own check. It reports Screen Recording and Accessibility for the
    program that started it: Terminal when you run it by hand, Python when launchd runs it."""
    try:
        done = runner([str(binary), "doctor"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"could not run the screen tool check: {exc}"]
    problems: List[str] = []
    for line in (done.stdout or "").splitlines():
        if "not permitted" in line:
            problems.append("Screen Recording is not allowed for this program")
        if "not granted" in line:
            problems.append("Accessibility is not allowed for this program")
    return problems
