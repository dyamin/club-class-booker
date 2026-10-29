"""Settings: the three people, screen positions, and paths. Loaded from a JSON file."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Dict, Tuple

Point = Tuple[int, int]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Person:
    key: str
    first_name: str
    selector_row_y: int
    calendar: str
    title_prefix: str = ""
    asks: bool = False     # ask which class to keep when this person has several in a day or overlapping ones (R16, R17)
    asks_overlaps: bool = False    # ask only about OVERLAPPING classes (the club refuses two at once; the sons use this)
    age_refusal_ok: bool = False   # the club refuses this person for age but lets them in: no message, and the class
                                   # still gets a calendar event (2026-10-19)


@dataclass(frozen=True)
class Coords:
    """Positions in the club app window (1024x800 points)."""

    selector_open: Point = (520, 68)
    selector_close: Point = (1001, 70)
    selector_row_x: int = 512
    register_x: int = 512             # the column of the register button on a class card
    register_tolerance: int = 60      # a button further from register_x than this is not tapped
    tab_classes: Point = (640, 775)
    subtab_schedule: Point = (860, 108)
    subtab_favorites: Point = (512, 108)
    subtab_booked: Point = (170, 108)         # "מוזמנים": every class the profile is registered for
    favorites_underline: Point = (512, 126)     # the blue line under the selected sub-tab
    schedule_underline: Point = (860, 126)
    booked_underline: Point = (170, 126)
    dialog_save_calendar: Point = (459, 757)
    dialog_ok: Point = (579, 757)
    editor_save: Point = (999, 65)
    editor_cancel: Point = (24, 65)
    editor_value_x: int = 969
    scroll_from: Point = (250, 680)
    scroll_to: Point = (250, 300)


@dataclass(frozen=True)
class Config:
    home: Path
    repo: Path
    people: Tuple[Person, ...]
    coords: Coords = Coords()
    timezone: str = "Asia/Jerusalem"
    model: str = "claude-opus-5-5"
    keychain_service: str = "club-booking-anthropic"
    mirroir_binary: Path = Path("tools/bin/mirroir-mcp")
    ocr_binary: Path = Path("tools/bin/ocr")
    horizon_days: int = 7
    max_scroll_screens: int = 12
    settle_seconds: float = 6.0
    replan_after_hours: int = 48       # a plan older than this is refreshed, right after an opening run, when the user is away
    reminders_list: str = "Club bookings"
    countdown_seconds: int = 15
    max_open_retries: int = 5
    max_run_seconds: int = 1200
    max_person_failures: int = 3      # failed runs per person per day before the program gives up for the day
    max_replans_per_day: int = 2      # how often a stale plan is rebuilt in one day
    replan_force_hours: int = 168     # a plan older than this is rebuilt even if the user is at the Mac (once a week)
    min_idle_seconds: float = 600.0   # the user must have been away this long before the program does anything non-urgent
    max_booking_rounds: int = 12      # bookings plus one last look per person and run
    keep_evidence_days: int = 30      # older evidence pictures are deleted
    club_app_name: str = "Runner 2"    # the Mac app that runs the club app, and its process name
    club_app_process: str = "Runner"
    max_app_restarts: int = 1         # per person and run: quit and open the app again when it is stuck
    app_restart_wait_seconds: float = 20.0
    app_start_wait_seconds: float = 8.0      # after the app (or its window) was started: time to show its home screen
    quit_app_after_run: bool = False         # quit the club app when a booking session is over (the account stays logged in)
    reader: str = "ax"                # ax = read the list through accessibility (picture reader as fallback); vision = pictures only
    ax_binary: Path = Path("tools/bin/ax")
    phone_keychain_service: str = "club-booking-phone"      # optional: the number for `login request`
    calendar_audit: str = "off"       # off | report (say what is missing) | write (add the missing events)
    questions: str = "off"            # off | on: waitlist, questions on the iPhone, cancel what the user drops (R14-R19)
    auto_login: bool = False          # the program logs the club app in by itself (needs Text Message Forwarding): see autologin.py
    sms_sender: str = "Hashmonaim"    # the sender of the login SMS (case, dots and spaces do not matter)
    sms_wait_seconds: int = 120       # how long to wait for the SMS after the code was requested
    messages_db: Path = Path.home() / "Library" / "Messages" / "chat.db"
    questions_several: bool = True    # ask which to keep when a person who asks has several classes in a day (R16); the user turned it off on 2026-10-15

    def person(self, key: str) -> Person:
        for p in self.people:
            if p.key == key:
                return p
        raise ConfigError(f"unknown person: {key!r}")

    @property
    def home_person(self) -> Person:
        """The first person in the list. The app is left on this profile after a run."""
        return self.people[0]


_SCALARS = {
    "timezone": str, "model": str, "keychain_service": str, "horizon_days": int,
    "max_scroll_screens": int, "settle_seconds": float, "replan_after_hours": int, "min_idle_seconds": float,
    "reminders_list": str, "countdown_seconds": int, "max_open_retries": int,
    "max_run_seconds": int, "max_person_failures": int, "max_replans_per_day": int,
    "replan_force_hours": int, "max_booking_rounds": int, "keep_evidence_days": int,
    "club_app_name": str, "club_app_process": str, "max_app_restarts": int, "app_restart_wait_seconds": float,
    "app_start_wait_seconds": float,
    "reader": str, "phone_keychain_service": str, "calendar_audit": str, "questions": str,
    "sms_sender": str, "sms_wait_seconds": int,
}


def load_config(path: Path, repo: Path) -> Config:
    """Read a JSON config file. Paths in the file are relative to the repo folder."""
    try:
        raw: Dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}. Make it with: python -m club_booking setup")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}")

    if not raw.get("people"):
        raise ConfigError("config needs a non-empty 'people' list")
    people = []
    for item in raw["people"]:
        try:
            people.append(Person(**item))
        except TypeError as exc:
            raise ConfigError(f"bad person entry {item!r}: {exc}")
    keys = [p.key for p in people]
    if len(set(keys)) != len(keys):
        raise ConfigError("person keys must be unique")

    coords = Coords()
    if "coords" in raw:
        allowed = {f.name for f in fields(Coords)}
        unknown = set(raw["coords"]) - allowed
        if unknown:
            raise ConfigError(f"unknown coords: {sorted(unknown)}")
        coords = replace(coords, **{k: (tuple(v) if isinstance(v, list) else v) for k, v in raw["coords"].items()})

    kwargs: Dict[str, Any] = {}
    for name, typ in _SCALARS.items():
        if name in raw:
            kwargs[name] = typ(raw[name])
    for name in ("mirroir_binary", "ocr_binary", "ax_binary", "messages_db"):
        if name in raw:
            kwargs[name] = Path(raw[name])
    for name in ("auto_login", "questions_several", "quit_app_after_run"):
        if name in raw:
            if not isinstance(raw[name], bool):
                raise ConfigError(f"{name} must be true or false, not {raw[name]!r}")
            kwargs[name] = raw[name]

    cfg = Config(home=Path(raw.get("home", "")) if raw.get("home") else default_home(),
                 repo=Path(repo), people=tuple(people), coords=coords, **kwargs)
    if cfg.calendar_audit not in ("off", "report", "write"):
        raise ConfigError(f"calendar_audit must be off, report or write, not {cfg.calendar_audit!r}")
    if cfg.questions not in ("off", "on"):
        raise ConfigError(f"questions must be off or on, not {cfg.questions!r}")
    return cfg


def default_home() -> Path:
    return Path.home() / "Library" / "Application Support" / "club-booking"


def resolve(cfg: Config, p: Path) -> Path:
    """A path from the config: absolute as is, otherwise relative to the repo folder."""
    return p if p.is_absolute() else cfg.repo / p
