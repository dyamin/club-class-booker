"""Wakes the program up, with launchd (the macOS job scheduler). Nothing runs on a timer.

Three kinds of agent, all running `python -m club_booking tick`. They are persistent: files in ~/Library/LaunchAgents
that launchd loads at every login, so they survive a restart.
  opening    one agent for EVERY planned opening (the time a class opens: 24 hours before it starts), installed in advance.
             The program wakes at that minute and not before. If the Mac is asleep then, launchd runs it at the next wake.
  login      runs once at login (not on a timer): a catch-up after the Mac was off, and the place where the agents are
             installed again when the plan changed.
  wake-test  a one-time agent that proves the wake-up works (see `arm-test`).

launchd cannot wake a sleeping Mac by itself. To wake the Mac from sleep at those times macOS needs root:
see wake_commands(), the user runs it.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence

TICK_LABEL = "com.club-booking.tick"
OPENING_PREFIX = "com.club-booking.opening."
TEST_LABEL = "com.club-booking.wake-test"
_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
PRUNE_AFTER = timedelta(minutes=60)     # an opening agent waits up to 20 minutes for the unlock, then runs up to 20


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def opening_label(when: datetime) -> str:
    return f"{OPENING_PREFIX}{when:%Y%m%d%H%M}"


def label_time(label: str) -> Optional[datetime]:
    if not label.startswith(OPENING_PREFIX):
        return None
    try:
        return datetime.strptime(label[len(OPENING_PREFIX):], "%Y%m%d%H%M")
    except ValueError:
        return None


def wake_command(when: datetime) -> str:
    """The command that makes the Mac wake up two minutes before `when`. It needs sudo."""
    wake = when - timedelta(minutes=2)
    return f'sudo pmset schedule wake "{wake:%m/%d/%y %H:%M:%S}"'


def wake_commands(times: Sequence[datetime]) -> str:
    """One command line for every opening time (each once, in order): the Mac wakes two minutes before each. The user
    runs them (macOS needs root). A woken Mac that is locked still needs the user's unlock."""
    return "\n".join(wake_command(t) for t in sorted(set(times)))


class LaunchAgents:
    def __init__(self, python: Path, repo: Path, log: Path, uid: Optional[int] = None,
                 runner: Callable = subprocess.run, directory: Optional[Path] = None) -> None:
        self.python = Path(python)
        self.repo = Path(repo)
        self.log = Path(log)
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner
        self.dir = directory or agents_dir()

    # -- plist content -------------------------------------------------------------
    def _base(self, label: str, extra_args: List[str]) -> dict:
        return {
            "Label": label,
            "ProgramArguments": [str(self.python), "-m", "club_booking", "tick", *extra_args],
            "WorkingDirectory": str(self.repo),
            "StandardOutPath": str(self.log),
            "StandardErrorPath": str(self.log),
            "EnvironmentVariables": {"PATH": _PATH},
        }

    def tick_plist(self) -> dict:
        data = self._base(TICK_LABEL, [])
        data["RunAtLoad"] = True              # once at login; never on a timer
        return data

    def opening_plist(self, label: str, when: datetime, extra_args: Optional[List[str]] = None) -> dict:
        data = self._base(label, extra_args or [])
        data["StartCalendarInterval"] = {"Month": when.month, "Day": when.day, "Hour": when.hour, "Minute": when.minute}
        return data

    # -- launchctl ---------------------------------------------------------------------
    def _path(self, label: str) -> Path:
        return self.dir / f"{label}.plist"

    def _load(self, label: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(label)
        with path.open("wb") as fh:
            plistlib.dump(data, fh)
        self.runner(["launchctl", "bootout", f"gui/{self.uid}/{label}"], capture_output=True, text=True)
        done = self.runner(["launchctl", "bootstrap", f"gui/{self.uid}", str(path)], capture_output=True, text=True)
        if done.returncode != 0:
            path.unlink(missing_ok=True)      # a plist left behind would count as armed for ever
            raise RuntimeError(f"launchctl bootstrap failed for {label}: {(done.stderr or '').strip()}")

    def _unload(self, label: str) -> None:
        self.runner(["launchctl", "bootout", f"gui/{self.uid}/{label}"], capture_output=True, text=True)
        self._path(label).unlink(missing_ok=True)

    # -- what the program asks for -----------------------------------------------------
    def is_installed(self) -> bool:
        """The scheduler is installed when the tick agent is. Before that, nothing may be armed."""
        return self._path(TICK_LABEL).exists()

    def install_tick(self) -> None:
        self._load(TICK_LABEL, self.tick_plist())

    def opening_labels(self) -> List[str]:
        return sorted(p.stem for p in self.dir.glob(f"{OPENING_PREFIX}*.plist")) if self.dir.exists() else []

    def sync_openings(self, times: Sequence[datetime], running_label: Optional[str] = None,
                      now: Optional[datetime] = None) -> List[str]:
        """Make the opening agents match the plan: one agent for every future time, none for a time that is no longer
        planned. Only the difference is loaded or removed.

        An agent whose time has come may be booking right now: boot it out and the job stops in the middle of a tap.
        So it stays until prune() removes it an hour later. `running_label` (the job that calls this) stays for the same
        reason: a job must not boot itself out. Returns the labels that are armed for the given times."""
        now = now or datetime.now()
        wanted = {opening_label(t): t for t in times if t > now}
        for label, when in sorted(wanted.items()):
            if label not in self.opening_labels():
                self._load(label, self.opening_plist(label, when))
        for other in self.opening_labels():
            if other in wanted or other == running_label:
                continue
            at = label_time(other)
            if at is None or at > now:
                self._unload(other)
        return sorted(wanted)

    def prune(self, now: datetime, running_label: Optional[str] = None) -> None:
        """Remove opening agents that fired more than half an hour ago (they would fire again next year)."""
        naive = now.replace(tzinfo=None)
        for label in self.opening_labels():
            when = label_time(label)
            if label != running_label and when is not None and when < naive - PRUNE_AFTER:
                self._unload(label)
        test_at = self.test_time(now.year)
        if TEST_LABEL != running_label and test_at is not None and test_at < naive:
            self._unload(TEST_LABEL)

    def test_time(self, year: int) -> Optional[datetime]:
        path = self._path(TEST_LABEL)
        if not path.exists():
            return None
        try:
            when = plistlib.loads(path.read_bytes())["StartCalendarInterval"]
            return datetime(year, when["Month"], when["Day"], when["Hour"], when["Minute"])
        except (KeyError, ValueError, plistlib.InvalidFileException):
            return None

    def disarm_openings(self, running_label: Optional[str] = None, now: Optional[datetime] = None) -> None:
        """Remove the opening agents. With `now`, agents whose time has come stay: they may be booking."""
        for label in self.opening_labels():
            if label == running_label:
                continue
            at = label_time(label)
            if now is not None and at is not None and at <= now:
                continue
            self._unload(label)

    def arm_test(self, when: datetime) -> None:
        self._load(TEST_LABEL, self.opening_plist(TEST_LABEL, when, ["--dry-run", "--notify"]))

    def remove_test(self) -> None:
        self._unload(TEST_LABEL)

    def uninstall(self) -> None:
        self._unload(TICK_LABEL)
        self.remove_test()
        self.disarm_openings()
