"""Login of the club app, with a guard against the app's lock.

The app locks the login for 10 minutes after 3 code requests. Until 2026-10-15 the program never logged in by itself;
the user CHANGED that rule on 2026-10-15 ("it should be independent"): `autologin.py` now uses this helper when
`auto_login` is on in the config. Do not remove that feature as a violation of the old rule. This module is also the
supervised login for a person (or Claude) at the Mac.
Rules: a counted step runs once; it is written to the file BEFORE it is done; the third request in 10 minutes is refused.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from .ax_reader import screen_kind

FIELD = "מספר הטלפון שלך"
SEND = "שלחו לי קוד"
CLOSE = "סגור"


class LoginError(Exception):
    pass


class LoginBlocked(LoginError):
    """The login is locked, or the next step would make the app lock it."""


class LoginGuard:
    def __init__(self, path: Path, max_requests: int = 3, window: timedelta = timedelta(minutes=10)) -> None:
        self.path = Path(path)
        self.max_requests = max_requests
        self.window = window

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def _times(self, key: str) -> List[datetime]:
        out: List[datetime] = []
        for item in self._load().get(key, []):
            try:
                out.append(datetime.fromisoformat(item))
            except (TypeError, ValueError):
                continue
        return out

    def requests(self) -> List[datetime]:
        return self._times("requests")

    def _add(self, key: str, now: datetime) -> None:
        data = self._load()
        data[key] = [t.isoformat() for t in self._times(key) if now - t < self.window] + [now.isoformat()]
        self._save(data)

    def record_request(self, now: datetime) -> None:
        self._add("requests", now)

    def record_entry(self, now: datetime) -> None:
        self._add("entries", now)

    def record_lock(self, now: datetime) -> None:
        data = self._load()
        data["locked_until"] = (now + self.window).isoformat()
        self._save(data)

    def _wait(self, key: str, now: datetime) -> Optional[timedelta]:
        recent = sorted(t for t in self._times(key) if now - t < self.window)
        allowed = self.max_requests - 1            # one is always left free: the user may need it
        if len(recent) >= allowed:
            return recent[len(recent) - allowed] + self.window - now
        return None

    def check(self, now: datetime, key: str = "requests") -> Optional[timedelta]:
        """None when the next step is allowed, otherwise how long to wait."""
        try:
            locked_until = datetime.fromisoformat(self._load().get("locked_until", ""))
            if now < locked_until:
                return locked_until - now
        except (TypeError, ValueError):
            pass
        return self._wait(key, now)

    def check_entry(self, now: datetime) -> Optional[timedelta]:
        return self.check(now, "entries")


def _minutes(wait: timedelta) -> int:
    return int(wait.total_seconds() // 60) + 1


class LoginHelper:
    def __init__(self, ax, guard: LoginGuard, type_into: Callable[[str, bool], bool],
                 sleep: Callable[[float], None] = time.sleep,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 poll: float = 1.0, timeout: float = 25.0,
                 read_field: Callable[[], str] = lambda: "") -> None:
        """type_into(text, clear) must bring the club app to the front, check it, and type, in one step;
        it returns False when it typed nothing. read_field() returns the text of the phone field, read from the screen."""
        self.ax, self.guard = ax, guard
        self.type_into, self.read_field = type_into, read_field
        self.sleep, self.now_fn, self.poll, self.timeout = sleep, now_fn, poll, timeout

    def _wait_for(self, done: Callable[[str], bool]) -> str:
        """Look at the screen every `poll` seconds, for at most `timeout` seconds, until done(kind) is true.
        The app can need more than a few seconds to answer. Only looks: it never presses anything."""
        kind, waited = "unknown", 0.0
        while True:
            self.sleep(self.poll)
            waited += self.poll
            kind = screen_kind(self.ax.dump())
            if done(kind) or waited >= self.timeout:
                return kind

    def _type(self, text: str, clear: bool = False) -> None:
        """Typing goes to the front app, and other apps keep taking the front: the step checks and types at once,
        and is tried again a few times. If it never gets the club app in front, nothing is typed."""
        for _ in range(6):
            if self.type_into(text, clear):
                return
            self.sleep(0.5)
        raise LoginError("the club app did not come to the front: nothing was typed")

    @staticmethod
    def _center(dump, desc: str) -> int:
        for e in dump.elements:
            if e.role == "AXButton" and e.desc == desc:
                return int(e.y + e.h / 2)
        raise LoginError(f"the button {desc!r} is not on the screen")

    def _close_popup(self) -> None:
        for role in ("AXStaticText", "AXButton"):
            try:
                self.ax.press(role, CLOSE, 752, 40)
                return
            except Exception:
                continue

    def request_code(self, phone: str) -> str:
        """Fill the phone number and press 'send me a code' ONCE. Returns code_screen, or unknown."""
        dump = self.ax.dump()
        kind = screen_kind(dump)
        if kind == "code":
            return "code_screen"
        if kind != "login":
            raise LoginError(f"the app does not show the login screen (it shows: {kind})")
        now = self.now_fn()
        wait = self.guard.check(now)
        if wait:
            raise LoginBlocked(f"too many code requests: wait {_minutes(wait)} minutes")
        field_y, send_y = self._center(dump, FIELD), self._center(dump, SEND)
        self.ax.press("AXButton", FIELD, field_y, 30)
        self._type(phone, clear=True)          # the field may hold text from an earlier try
        shown = ""
        for attempt in range(4):             # the last digit can be drawn after the first picture was taken
            try:
                shown = self.read_field()
            except Exception as exc:                        # for example the OCR helper crashed: nothing was sent
                raise LoginError(f"could not read the phone field ({type(exc).__name__}: {exc}): nothing was sent")
            if re.sub(r"[\s\-]", "", shown) == phone:      # accessibility does not show the text: the screen is read
                break
            self.sleep(0.5)
        else:
            raise LoginError(f"the field shows {shown!r}, not the number: nothing was sent")
        self.guard.record_request(now)                      # written before the press
        self.ax.press("AXButton", SEND, send_y, 30)          # exactly once, never repeated
        after = self._wait_for(lambda kind: kind in ("code", "locked", "update"))
        if after == "code":
            return "code_screen"
        if after == "locked":
            self.guard.record_lock(self.now_fn())
            self._close_popup()
            raise LoginBlocked("the app locked the login for 10 minutes")
        return "unknown"

    def enter_code(self, code: str) -> str:
        """Type the 6 digits on the code screen. Returns logged_in, or unknown."""
        if not re.fullmatch(r"\d{6}", code or ""):
            raise LoginError("the code must be exactly 6 digits")
        if screen_kind(self.ax.dump()) != "code":
            raise LoginError("the app does not show the code screen")
        now = self.now_fn()
        wait = self.guard.check_entry(now)
        if wait:
            raise LoginBlocked(f"too many codes typed: wait {_minutes(wait)} minutes")
        self.guard.record_entry(now)
        self._type(code)
        after = self._wait_for(lambda kind: kind != "code")
        return "unknown" if after in ("login", "code", "locked", "update") else "logged_in"
