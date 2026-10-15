"""Pure functions: when does each class open, and what is due now."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from .models import CardState, ClassCard


@dataclass(frozen=True)
class Opening:
    person: str          # person key, for example "sam"
    card_key: str        # ClassCard.key
    name: str
    start: datetime
    opens_at: datetime

    @property
    def ident(self) -> str:
        return f"{self.person}|{self.card_key}"


@dataclass(frozen=True)
class Plan:
    created_at: datetime
    openings: Tuple[Opening, ...]


def openings_from_cards(person: str, cards: Iterable[ClassCard], now: datetime,
                        horizon_days: int) -> List[Opening]:
    """Cards that are not open yet and start within the horizon, earliest opening first."""
    limit = now + timedelta(days=horizon_days)
    found = [Opening(person, c.key, c.name, c.start, c.opens_at)
             for c in cards
             if c.state == CardState.NOT_OPEN and now < c.start <= limit]
    return sorted(found, key=lambda o: (o.opens_at, o.person, o.card_key))


def due(openings: Sequence[Opening], now: datetime, handled: Set[str]) -> List[Opening]:
    """Openings whose time has come, whose class has not started, and that nobody handled yet."""
    return [o for o in openings if o.opens_at <= now and o.start > now and o.ident not in handled]


def next_opening(openings: Sequence[Opening], now: datetime, handled: Set[str]) -> Optional[Opening]:
    """The earliest opening still in the future, for any person."""
    future = [o for o in openings if o.opens_at > now and o.ident not in handled]
    return min(future, key=lambda o: o.opens_at) if future else None


def persons_in(openings: Iterable[Opening]) -> List[str]:
    """Person keys in order of first appearance, without repeats."""
    seen: List[str] = []
    for o in openings:
        if o.person not in seen:
            seen.append(o.person)
    return seen


def plan_is_stale(plan: Optional[Plan], now: datetime, max_age_hours: int) -> bool:
    return plan is None or now - plan.created_at > timedelta(hours=max_age_hours)
