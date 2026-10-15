"""Calendar events. The app's own button opens Apple's event editor; this module drives it,
then checks and renames the saved event with AppleScript."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Tuple

from .config import Coords, Person
from .models import ClassCard
from .ocr import TextHit, find_text
from .osa import osa_for, run_applescript

calendar_osa = osa_for("Calendar")      # starts Calendar first: a scheduled job cannot launch it by itself


class CalendarError(Exception):
    pass


@dataclass(frozen=True)
class EventInfo:
    uid: str
    title: str
    start: str
    end: str
    alarms: Tuple[int, ...]   # minutes relative to the start; -60 means one hour before


# argv: 1 calendar name, 2 year, 3 month, 4 day, 5 hour, 6 minute
_DATE = '''
set d1 to current date
set day of d1 to 1
set year of d1 to (item 2 of argv) as integer
set month of d1 to (item 3 of argv) as integer
set day of d1 to (item 4 of argv) as integer
set time of d1 to ((item 5 of argv) as integer) * hours + ((item 6 of argv) as integer) * minutes
set d2 to d1 + (1 * minutes)
'''

_FIND = '''
on run argv
''' + _DATE + '''
    set out to ""
    tell application "Calendar"
        repeat with c in (every calendar whose name is (item 1 of argv))
            set evs to (every event of c whose start date is greater than or equal to d1 and start date is less than d2)
            repeat with e in evs
                set alarmText to ""
                repeat with a in (every display alarm of e)
                    set alarmText to alarmText & (trigger interval of a) & ","
                end repeat
                set out to out & (uid of e) & tab & (summary of e) & tab & ((start date of e) as string) & tab & ((end date of e) as string) & tab & alarmText & linefeed
            end repeat
        end repeat
    end tell
    return out
end run
'''

# argv: 1 calendar name, 2 uid, 3 new title
_RENAME = '''
on run argv
    tell application "Calendar"
        repeat with c in (every calendar whose name is (item 1 of argv))
            set found to (every event of c whose uid is (item 2 of argv))
            if (count of found) is 1 then
                set summary of (item 1 of found) to (item 3 of argv)
                return "ok"
            end if
        end repeat
    end tell
    return "not found"
end run
'''


def _date_args(calendar: str, start: datetime) -> List[str]:
    return [calendar, str(start.year), str(start.month), str(start.day), str(start.hour), str(start.minute)]


def find_events(calendar: str, start: datetime, osa: Callable = calendar_osa) -> List[EventInfo]:
    """Events in one calendar that start at exactly this minute."""
    out = osa(_FIND, _date_args(calendar, start))
    events: List[EventInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        alarms = tuple(int(float(a)) for a in parts[4].split(",") if a.strip())
        events.append(EventInfo(parts[0], parts[1], parts[2], parts[3], alarms))
    return events


def rename_event(calendar: str, uid: str, new_title: str, osa: Callable = calendar_osa) -> None:
    if osa(_RENAME, [calendar, uid, new_title]) != "ok":
        raise CalendarError("could not rename the calendar event")


def name_pattern(calendar: str) -> str:
    """Pop-up items are cut at the window edge, and the chosen value shows a bullet in front,
    so match the first four letters anywhere in the text."""
    return re.escape(calendar[:4])


class CalendarService:
    """Adds one class to the right calendar with an alert one hour before."""

    def __init__(self, screen, coords: Coords, ocr_fn: Callable[[bytes], List[TextHit]],
                 osa: Callable = calendar_osa, sleep: Callable[[float], None] = time.sleep,
                 settle: float = 3.0) -> None:
        self.screen = screen
        self.coords = coords
        self.ocr_fn = ocr_fn
        self.osa = osa
        self.sleep = sleep
        self.settle = settle

    # -- public ---------------------------------------------------------------
    def add(self, person: Person, card: ClassCard) -> EventInfo:
        before = {e.uid for e in find_events(person.calendar, card.start, self.osa)}
        try:
            self._through_the_app(person.calendar)
        except Exception as exc:
            if not self._cancel_editor():
                raise CalendarError(f"{exc}; the calendar editor is still open") from exc
            raise
        event = self._the_new_event(person.calendar, card.start, before)
        if -60 not in event.alarms:
            raise CalendarError(f"the event has no alert one hour before (alarms: {event.alarms})")
        if person.title_prefix:
            rename_event(person.calendar, event.uid, f"{person.title_prefix}{event.title}", self.osa)
        return event

    # -- the app's editor -------------------------------------------------------
    def _through_the_app(self, calendar: str) -> None:
        self.screen.tap(*self.coords.dialog_save_calendar)
        self.sleep(self.settle + 2)
        if find_text(self.ocr_fn(self.screen.region_png()), r"^New Event") is None:
            raise CalendarError("the calendar editor did not open")
        self._choose(r"^Calendar$", name_pattern(calendar))
        self._choose(r"^Alert$", r"^1 hour")
        hits = self.ocr_fn(self.screen.region_png())
        if find_text(hits, name_pattern(calendar), min_x=900) is None:
            raise CalendarError(f"the calendar {calendar!r} is not selected")
        if find_text(hits, r"^1 hour before", min_x=800) is None:
            raise CalendarError("the alert 1 hour before is not selected")
        self.screen.tap(*self.coords.editor_save)
        self.sleep(self.settle)
        if not self._wait_editor_closed():
            raise CalendarError("the calendar editor did not close after Save")

    def _choose(self, row_pattern: str, item_pattern: str) -> None:
        """Open the pop-up menu on a row of the editor and click an item."""
        row = find_text(self.ocr_fn(self.screen.region_png()), row_pattern)
        if row is None:
            raise CalendarError(f"row {row_pattern!r} not found in the editor")
        for _ in range(3):   # the first click may only focus the window
            self.screen.tap(self.coords.editor_value_x, row.y)
            self.sleep(2)
            hits = self.ocr_fn(self.screen.region_png())
            item = next((h for h in sorted(hits, key=lambda h: h.y)
                         if h.x >= 900 and abs(h.y - row.y) > 8 and re.search(item_pattern, h.text, re.IGNORECASE)), None)
            if item is not None:
                self.screen.tap(item.x, item.y)
                self.sleep(2)
                return
        raise CalendarError(f"menu item {item_pattern!r} not found")

    def _editor_open(self) -> bool:
        return find_text(self.ocr_fn(self.screen.region_png()), r"^New Event") is not None

    def _wait_editor_closed(self) -> bool:
        for _ in range(3):
            if not self._editor_open():
                return True
            self.sleep(2)
        return False

    def _cancel_editor(self) -> bool:
        """Cancel the editor, but only while it is on the screen: a tap at its Cancel position means something
        else in the club app. Returns True when no editor is left open."""
        try:
            if not self._editor_open():
                return True
            self.screen.tap(*self.coords.editor_cancel)
            self.sleep(2)
            return self._wait_editor_closed()
        except Exception:
            return False

    # -- after saving ---------------------------------------------------------------
    def _the_new_event(self, calendar: str, start: datetime, before: set) -> EventInfo:
        for _ in range(5):   # the new event may take a moment to show up
            new = [e for e in find_events(calendar, start, self.osa) if e.uid not in before]
            if new:
                break
            self.sleep(2)
        if len(new) != 1:
            raise CalendarError(f"expected exactly 1 new event in {calendar!r}, found {len(new)}")
        return new[0]


_NAMES = '''
on run argv
    set out to ""
    tell application "Calendar"
        repeat with c in every calendar
            set out to out & (name of c) & linefeed
        end repeat
    end tell
    return out
end run
'''


def calendar_names(osa: Callable = calendar_osa) -> List[str]:
    return [line.strip() for line in osa(_NAMES, []).splitlines() if line.strip()]
