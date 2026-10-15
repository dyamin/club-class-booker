"""Plain data types shared by the whole program."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Tuple


class CardState(str, Enum):
    """What a class card shows, decided by its button."""

    OPEN = "open"              # button: הרשמה
    FULL = "full"              # button: הרשמה להמתנה
    NOT_OPEN = "not_open"      # button: תזכורת (registration opens later)
    REGISTERED = "registered"  # shows: נרשמת / רשום
    WAITLISTED = "waitlisted"  # shows: בהמתנה
    UNKNOWN = "unknown"


def normalize_name(text: str) -> str:
    """A class name without case, spaces, punctuation, or vowel marks. The reader can write the same
    Hebrew name a little differently each time (an apostrophe or a geresh); the key must not change."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text).casefold() if ch.isalnum())


def opening_time(start: datetime) -> datetime:
    """Registration opens exactly 24 hours before the class starts (absolute time)."""
    utc = start.astimezone(timezone.utc) - timedelta(hours=24)
    return utc.astimezone(start.tzinfo)


@dataclass(frozen=True)
class ClassCard:
    name: str
    start: datetime
    end: datetime
    state: CardState
    button: Optional[Tuple[int, int]] = None  # rough center of the main button, window points
    opens_shown: Optional[datetime] = None    # the opening time the card itself shows, if any

    @property
    def key(self) -> str:
        return f"{self.start.strftime('%Y-%m-%dT%H:%M')}|{normalize_name(self.name)}"

    @property
    def opens_at(self) -> datetime:
        """The time the card states. Without one, 24 hours before the start."""
        return self.opens_shown if self.opens_shown is not None else opening_time(self.start)
