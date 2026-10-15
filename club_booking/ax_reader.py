"""Reads the club app's screen as text, through accessibility: exact words, no picture, no API call.

The class list comes from here. The picture reader (club_booking.vision) stays as the fallback and still reads the
confirmation dialogs, whose accessibility text is not known yet.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .ax import AxClient, AxDump, AxElement, AxError
from .models import CardState, ClassCard, normalize_name
from .vision import ListRead, infer_datetime

_TIME = re.compile(r"שעה:\s*(\d{1,2}):(\d{2})\s*עד\s*(\d{1,2}):(\d{2})")
_DATE_LINE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_DATE_IN = re.compile(r"(\d{1,2})/(\d{1,2})")
_CLOCK_IN = re.compile(r"(\d{1,2}):(\d{2})")
_SKIP_LINES = {"הסר מהמועדפים", "הוסף למועדפים", "|"}
_CLOSE_WORDS = ("סגור", "אישור", "הבנתי", "סגירה", "מצויין, תודה", "מצוין, תודה")   # the last two: the waitlist message
_ACTIONS = {"הרשמה": CardState.OPEN, "הרשמה להמתנה": CardState.FULL, "תזכורת": CardState.NOT_OPEN,
            "ביטול": CardState.REGISTERED, "הסרה מהמתנה": CardState.WAITLISTED}   # the last two: on a booked card
_BACK = "חזרה לעמוד קודם"
# an attendee on a class page: a first name and an initial ("קרן י."), no digits or colons
_NAME = re.compile(r"^[^\s\d:/]+(?: [^\s\d:/]+)? \S\.$")


def _texts(dump: AxDump) -> List[str]:
    return [t for e in dump.elements for t in (e.desc, e.value) if t]


def screen_kind(dump: AxDump) -> str:
    """favorites | booked | details | login | code | locked | update | selector | other.
    A popup wins over the screen behind it."""
    texts = _texts(dump)
    joined = "\n".join(texts)
    if "גרסה חדשה של האפליקציה" in joined:
        return "update"
    if "ננעל למשך" in joined:
        return "locked"
    if "אימות כניסה" in joined or "הקוד שקיבלת" in joined:
        return "code"
    if "שלחו לי קוד" in joined or "כניסה עם טלפון" in joined:
        return "login"
    if "בחירת מועדון" in joined:
        return "selector"
    if any(t == _BACK for t in texts) and "תיאור השיעור" in joined:
        return "details"
    if any(t.startswith("שיעורים מועדפים") for t in texts):
        return "favorites"
    if any(t.startswith("שיעורים קרובים") for t in texts):
        return "booked"
    return "other"


def selector_rows(dump: AxDump) -> List[Tuple[str, int]]:
    """(text, middle line) of every profile row of the open person selector, top to bottom; [] on any other screen.
    A row is a button whose text has " - " (the club, a dash, the person). Used by the setup wizard."""
    if screen_kind(dump) != "selector":
        return []
    return [(e.desc, int(e.y + e.h / 2)) for e in sorted(dump.elements, key=lambda e: e.y)
            if e.role == "AXButton" and " - " in e.desc]


def attendee_count(dump: AxDump) -> int:
    """How many people are registered, from the names on a class page (verified on the real app, 2026-10-13)."""
    below = next((e.y for e in dump.elements if e.desc.startswith("משך השיעור")), None)
    return sum(1 for e in dump.elements
               if e.role == "AXStaticText" and _NAME.match(e.desc.strip()) and (below is None or e.y > below))


def _viewport(dump: AxDump) -> Tuple[float, float]:
    areas = [e for e in dump.elements if e.role == "AXScrollArea"]
    if areas:
        area = max(areas, key=lambda e: e.w * e.h)
        return area.y, area.y + area.h
    return 0.0, dump.window.h


def _parse_when(text: str, now: datetime, tz: ZoneInfo) -> Optional[datetime]:
    """A date and a time in either order: '15/10 07:45' or '07:45 15/10'."""
    date, clock = _DATE_IN.search(text), _CLOCK_IN.search(text)
    if not date or not clock:
        return None
    try:
        return infer_datetime(int(date.group(1)), int(date.group(2)), int(clock.group(1)), int(clock.group(2)), now, tz)
    except ValueError:
        return None


def _parse_card(desc: str, now: datetime, tz: ZoneInfo) -> Optional[Tuple[str, datetime, datetime, str]]:
    lines = [ln.strip() for ln in desc.split("\n") if ln.strip()]
    date = next((m for m in (_DATE_LINE.match(ln) for ln in lines) if m), None)
    clock = _TIME.search(desc)
    if not date or not clock:
        return None
    day, month = int(date.group(1)), int(date.group(2))
    try:
        start = infer_datetime(day, month, int(clock.group(1)), int(clock.group(2)), now, tz)
        end = infer_datetime(day, month, int(clock.group(3)), int(clock.group(4)), now, tz)
    except ValueError:
        return None
    if end <= start:
        end += timedelta(days=1)
    after_time = lines[next(i for i, ln in enumerate(lines) if "שעה:" in ln) + 1:]
    rest = [ln for ln in after_time if ln not in _SKIP_LINES]
    title = rest[0] if rest else ""
    trainer = next((i for i, ln in enumerate(rest) if ln.startswith("מאמן")), None)
    status = " ".join(rest[trainer + 1:] if trainer is not None else rest[1:])
    return title, start, end, status


def list_from_dump(dump: AxDump, now: datetime, tz: ZoneInfo) -> List[ClassCard]:
    """Every class card in the dump. A card cut off by the edge has its text but no button position: the button
    is given only when it lies fully inside the list area."""
    if screen_kind(dump) not in ("favorites", "booked"):
        return []
    top, bottom = _viewport(dump)
    marks = [i for i, e in enumerate(dump.elements) if e.role == "AXButton" and "שעה:" in e.desc]
    cards: List[ClassCard] = []
    for n, i in enumerate(marks):
        parsed = _parse_card(dump.elements[i].desc, now, tz)
        if parsed is None:
            continue
        title, start, end, status = parsed
        stop = marks[n + 1] if n + 1 < len(marks) else len(dump.elements)
        action: Optional[AxElement] = next((e for e in dump.elements[i + 1:stop]
                                            if e.role == "AXButton" and e.desc in _ACTIONS), None)
        opens_shown: Optional[datetime] = None
        if "פתיחת הרשמה" in status:
            state = CardState.NOT_OPEN
            opens_shown = _parse_when(status.split("פתיחת הרשמה", 1)[1], now, tz)
        elif status.strip() == "רשום" or "נרשמת" in status:
            state = CardState.REGISTERED
        elif "בהמתנה" in status:
            state = CardState.WAITLISTED
        elif "סגור להרשמה" in status:
            state = CardState.UNKNOWN
        elif action is not None:
            state = _ACTIONS[action.desc]
        else:
            state = CardState.UNKNOWN
        button: Optional[Tuple[int, int]] = None
        if action is not None and action.h > 0 and action.y >= top and action.y + action.h <= bottom:
            button = (int(action.x + action.w / 2), int(action.y + action.h / 2))
        cards.append(ClassCard(title, start, end, state, button, opens_shown))
    return cards


class AxReader:
    """Same interface as Vision for the Runner: read_list(png), read_dialog(png), warnings, usage; plus scroll_page()."""

    def __init__(self, ax: AxClient, fallback: Any, tz: ZoneInfo,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self.ax = ax
        self.fallback = fallback
        self.tz = tz
        self.now_fn = now_fn
        self.warnings = fallback.warnings      # the same objects: the Runner reads and clears them
        self.usage = fallback.usage

    def read_list(self, png: Optional[bytes] = None) -> ListRead:
        try:
            dump = self.ax.dump()
        except AxError as exc:
            self.warnings.append(f"accessibility did not work, the picture reader was used: {exc}")
            return self.fallback.read_list(png)
        kind = screen_kind(dump)
        return ListRead(kind, list_from_dump(dump, self.now_fn(), self.tz) if kind in ("favorites", "booked") else [])

    def read_attendees(self) -> Optional[int]:
        """How many people registered, from the class page that is open. None when it cannot be read."""
        try:
            dump = self.ax.dump()
        except AxError:
            return None
        return attendee_count(dump) if screen_kind(dump) == "details" else None

    def open_card(self, card: ClassCard) -> None:
        """Open the class page of a card that is on the screen (to count the people who registered)."""
        for e in self.ax.dump().elements:
            if e.role == "AXButton" and "שעה:" in e.desc:
                parsed = _parse_card(e.desc, self.now_fn(), self.tz)
                if parsed and parsed[1] == card.start and normalize_name(parsed[0]) == normalize_name(card.name):
                    self.ax.press("AXButton", e.desc, int(e.y + max(e.h, 1) / 2), 40)
                    return
        raise AxError("the class card is not on the screen")

    def press_card_action(self, card: ClassCard, desc: str) -> None:
        """Press the button of exactly this text (for example "ביטול") inside the card of this class. The card must be
        on the screen exactly once, and the helper presses only inside it: no position is guessed, and the press can
        never land on another card (the cards of a list each have such a button)."""
        found = []
        for e in self.ax.dump().elements:
            if e.role == "AXButton" and "שעה:" in e.desc:
                parsed = _parse_card(e.desc, self.now_fn(), self.tz)
                if parsed and parsed[1] == card.start and normalize_name(parsed[0]) == normalize_name(card.name):
                    found.append(e)
        if len(found) != 1:
            raise AxError(f"the class card is on the screen {len(found)} times, not once")
        self.ax.press_in_card(found[0].desc, desc)

    def read_popup(self) -> Tuple[str, str]:
        """What a message on the screen says, read before anything is pressed: ("success", text) when it reports a
        success ("ההרשמה בוטלה בהצלחה!"), ("unknown", text) for any other message that has a close or confirm button
        (a fee, an "are you sure"), ("none", "") when there is no message."""
        dump = self.ax.dump()
        if not any(e.desc.strip() in _CLOSE_WORDS for e in dump.elements):
            return "none", ""
        text = " | ".join(e.desc.strip() for e in dump.elements if e.role == "AXStaticText" and e.desc.strip())
        return ("success" if "בהצלחה" in text else "unknown"), text

    def back(self) -> None:
        """Press the back button of a class page."""
        for e in self.ax.dump().elements:
            if e.role == "AXButton" and e.desc == _BACK:
                self.ax.press("AXButton", _BACK, int(e.y + e.h / 2), 40)
                return
        raise AxError("no back button on the screen")

    def read_dialog(self, png: bytes):
        return self.fallback.read_dialog(png)

    def scroll_page(self) -> None:
        self.ax.scroll("down")

    def close_popup(self) -> None:
        """Press the close button of a message popup (for example a refusal)."""
        dump = self.ax.dump()
        for e in dump.elements:
            if e.role in ("AXButton", "AXStaticText") and e.desc in _CLOSE_WORDS:
                self.ax.press(e.role, e.desc, int(e.y + e.h / 2), 40)
                return
        raise AxError("no popup with a close button on the screen")
