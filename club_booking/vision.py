"""Reads the Hebrew screens of the club app with the Claude API (image in, JSON out)."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .models import CardState, ClassCard


class VisionError(Exception):
    pass


def make_client(api_key: str):
    """Build the SDK client. The import is here so that tests do not need the package."""
    import anthropic
    # base_url is explicit: an inherited ANTHROPIC_BASE_URL must not receive the key
    return anthropic.Anthropic(api_key=api_key, base_url="https://api.anthropic.com", max_retries=2, timeout=90.0)


_STATES = [s.value for s in CardState]

SCREENS = ["favorites", "schedule", "selector", "login", "other"]

CLASS_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "screen": {"type": "string", "enum": SCREENS},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "state": {"type": "string", "enum": _STATES},
                    "opens": {"type": "string"},
                    "button_x": {"type": "integer"},
                    "button_y": {"type": "integer"},
                },
                "required": ["title", "date", "start", "end", "state", "opens", "button_x", "button_y"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["screen", "cards"],
    "additionalProperties": False,
}

CLASS_LIST_PROMPT = """This is a screenshot (1024x800 pixels) of a gym club app. The text is Hebrew, written right to left.
First say which screen it is, as 'screen':
  favorites: the Classes tab with the sub-tab 'מועדפים' (favorites) selected, showing a list of class cards.
  schedule: the Classes tab with the sub-tab 'לו"ז שיעורים' (schedule) selected, with day chips above the class cards.
  selector: a sheet that lists the people of the family with one check mark.
  login: the sign-in screen with a phone number field and the button 'שלחו לי קוד', or the screen where the code is typed ('אימות כניסה').
  other: anything else (home, the booked tab 'מוזמנים', a dialog, a class details page, a calendar editor).
Then list every class card whose title line, date and time line, and bottom area are fully visible. For each card give:
- title: the class name exactly as written.
- date: day and month as DD/MM, from the line like 'שלישי 13/10 | 08:50 - 09:35'.
- start and end: HH:MM in 24 hours, from the same line.
- state, decided from the bottom area of the card:
  open: a blue button 'הרשמה' (only that word).
  full: a blue button 'הרשמה להמתנה'.
  not_open: a line 'פתיחת הרשמה' (registration opens) and a blue button 'תזכורת'.
  registered: the text 'נרשמת' with a check mark, or a grey 'רשום'.
  waitlisted: the text 'בהמתנה' with a queue position.
  unknown: none of these, for example a grey 'סגור להרשמה' (registration closed).
- opens: for a not_open card, the time and date written after 'פתיחת הרשמה:' (the app writes 'HH:MM DD/MM'), given as 'DD/MM HH:MM'. Use an empty string for every other card.
- button_x, button_y: pixel position of the center of the card's main blue button text ('הרשמה', 'הרשמה להמתנה' or 'תזכורת'). Use 0 and 0 when the state is registered, waitlisted or unknown.
Skip any card that is cut off at the top or the bottom edge. Do not invent cards. Return only JSON."""

DIALOG_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["booked", "waitlisted", "cancelled", "refused", "calendar_editor", "list", "other"]},
        "title": {"type": "string"},
        "date": {"type": "string"},
        "start": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["kind", "title", "date", "start", "message"],
    "additionalProperties": False,
}

DIALOG_PROMPT = """This is a screenshot (1024x800 pixels) of a gym club app. The text is Hebrew, right to left. Say what is on the screen.
kind: 'booked' when a confirmation says 'זהו, נרשמת!' (you are registered); 'waitlisted' when a confirmation says you joined a waitlist; 'cancelled' when it says the registration was cancelled ('ההרשמה בוטלה בהצלחה'); 'refused' when a message popup says the registration was NOT accepted (for example the age does not fit, the class is not for this member, no credit left); 'calendar_editor' when a calendar 'New Event' editor is shown; 'list' for a normal class list or home screen; 'other' for anything else.
When a confirmation names a class, give title, date as DD/MM, and start as HH:MM. For 'refused' give the text of the popup in message. Otherwise give empty strings. Return only JSON."""


@dataclass(frozen=True)
class ListRead:
    screen: str                    # one of SCREENS; "other" when the answer did not say
    cards: List[ClassCard]


@dataclass(frozen=True)
class DialogRead:
    kind: str
    title: str
    date: str
    start: str
    message: str = ""        # the text of a refusal popup


def infer_datetime(day: int, month: int, hour: int, minute: int, now: datetime, tz: ZoneInfo) -> datetime:
    """The app shows no year. Pick the year that puts the date closest to now."""
    best: Optional[datetime] = None
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            cand = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            continue
        if best is None or abs(cand - now) < abs(best - now):
            best = cand
    if best is None:
        raise ValueError(f"not a date: {day}/{month}")
    return best


def _parse_dm(text: str) -> Tuple[int, int]:
    day, month = text.strip().split("/")[:2]
    return int(day), int(month)


def _parse_hm(text: str) -> Tuple[int, int]:
    hour, minute = text.strip().split(":")[:2]
    return int(hour), int(minute)


class Vision:
    def __init__(self, client: Any, model: str, tz: ZoneInfo,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self.client = client
        self.model = model
        self.tz = tz
        self.now_fn = now_fn
        self.warnings: List[str] = []
        self.usage: Dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def _ask(self, png: bytes, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        data = base64.standard_b64encode(png).decode("ascii")
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                    {"type": "text", "text": prompt},
                ]}],
            )
        except Exception as exc:  # network, key, rate limit, server error: the SDK already retried
            raise VisionError(f"API call failed: {type(exc).__name__}: {exc}") from exc
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        if getattr(response, "stop_reason", None) == "refusal":
            raise VisionError("the model declined to read the screenshot")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
        if text is None:
            raise VisionError("the answer has no text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisionError(f"the answer is not valid JSON: {exc}") from exc

    def read_list(self, png: bytes) -> ListRead:
        """The kind of screen and the cards on it. A card that cannot be understood is skipped and noted in
        warnings. The caller must check the screen kind before it trusts the cards."""
        raw = self._ask(png, CLASS_LIST_PROMPT, CLASS_LIST_SCHEMA)
        now = self.now_fn()
        cards: List[ClassCard] = []
        for item in raw.get("cards", []):
            try:
                day, month = _parse_dm(item["date"])
                sh, sm = _parse_hm(item["start"])
                eh, em = _parse_hm(item["end"])
                start = infer_datetime(day, month, sh, sm, now, self.tz)
                end = infer_datetime(day, month, eh, em, now, self.tz)
                if end <= start:
                    end = end + timedelta(days=1)
                state = CardState(item["state"])
                button = (item["button_x"], item["button_y"]) if (item["button_x"] or item["button_y"]) else None
                opens_shown = None
                if item["opens"].strip():
                    try:
                        parts = item["opens"].split()
                        dm = next((p for p in parts if "/" in p), "")     # either order: DD/MM HH:MM or HH:MM DD/MM
                        hm = next((p for p in parts if ":" in p), "")
                        od, om = _parse_dm(dm)
                        oh, omin = _parse_hm(hm)
                        opens_shown = infer_datetime(od, om, oh, omin, now, self.tz)
                    except ValueError as exc:
                        self.warnings.append(f"opening time not understood: {item['opens']!r} ({exc})")
                if button is None and state == CardState.OPEN:        # the program taps only open cards
                    self.warnings.append(f"no button position for the card {item['title'].strip()!r}")
                cards.append(ClassCard(item["title"].strip(), start, end, state, button, opens_shown))
            except (KeyError, ValueError, TypeError) as exc:
                self.warnings.append(f"skipped a card: {item!r} ({exc})")
        screen = raw.get("screen")
        return ListRead(screen if screen in SCREENS else "other", cards)

    def read_dialog(self, png: bytes) -> DialogRead:
        raw = self._ask(png, DIALOG_PROMPT, DIALOG_SCHEMA)
        return DialogRead(raw["kind"], raw["title"], raw["date"], raw["start"], raw.get("message", ""))
