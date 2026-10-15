"""Calendar audit: every class a person is registered for has an event in that person's calendar.

A class the user registers by hand, or a class whose calendar step failed, has no event. The audit finds those and
adds the event with Calendar scripting (AppleScript). The app's own "save to calendar" button is not available
here: it exists only in the confirmation dialog right after a booking.

Rules that keep it safe:
- An event counts when it is in the person's calendar and starts at the same minute as the class. The end and the
  title are not compared (nobody knows what the app's own editor makes, and a second event is worse than none).
  A repeating event is listed with the date of its first occurrence, so it also counts when its time of day is
  the class's. In the shared calendar of the sons the title must also start with the person's own prefix, so one
  son's event never stands for the other's.
- An event without any person's prefix at that time is reported and nothing is added: it is probably the app's own
  event after a failed rename, and a second one would be a duplicate.
- A class is done once it is checked (event found, added, or reported). The audit never adds it again, so an event
  the user deleted on purpose does not come back.

A class that the user unregisters loses its event (the user's decision of 2026-10-14). Deleting is hard to undo, so
`remove_dropped` is strict:
- only a class this audit handled (it is in `calendar_done`), and only while it is still in the future;
- only when the booked tab of the person was read to its end, and the class is on no card of that reading (a
  waitlisted class counts as there);
- only on the second reading, at least 30 minutes after the first: one wrong reading deletes nothing;
- only an event that starts at the class's minute, in the person's calendar, with the person's title prefix and the
  class's name (compared the way the class key is); exactly one such event, and never a repeating one;
- never more than 3 classes of one person at once: more looks like a misreading, and the user is told;
- the delete is counted before it is tried and stops after 3 failures.
After the delete the class is no longer "done": if the user registers again, the audit adds an event again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .calendar_app import CalendarError, calendar_osa
from .config import Person
from .models import CardState, ClassCard, normalize_name
from .state import Store

MAX_TRIES = 3          # failed attempts per class, or per calendar and day; after that it is left alone
MIN_GAP = timedelta(minutes=30)     # a class must be missing from two readings this far apart before its event goes
MAX_REMOVALS = 3                    # per person and pass: more looks like a misreading of the list
_STARTED = "%Y-%m-%dT%H:%M"

_HELPERS = '''
on makeDate(y, mo, d, hh, mi)
    set r to current date
    set day of r to 1
    set year of r to y
    set month of r to mo
    set day of r to d
    set time of r to hh * hours + mi * minutes
    return r
end makeDate

on parts(d)
    set t to time of d
    return ((year of d) as text) & tab & ((month of d) as integer) & tab & (day of d) & tab & (t div 3600) & tab & ((t mod 3600) div 60)
end parts
'''

# argv: 1 calendar name, 2-6 from (year month day hour minute), 7-11 until (same). Output: one event per line:
# uid, title, start (5 numbers), end (5 numbers), alarms (minutes relative to the start, comma separated)
_LIST = _HELPERS + '''
on run argv
    set d1 to my makeDate((item 2 of argv) as integer, (item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer, (item 6 of argv) as integer)
    set d2 to my makeDate((item 7 of argv) as integer, (item 8 of argv) as integer, (item 9 of argv) as integer, (item 10 of argv) as integer, (item 11 of argv) as integer)
    set out to ""
    tell application "Calendar"
        repeat with c in (every calendar whose name is (item 1 of argv))
            set evs to (every event of c whose start date is greater than or equal to d1 and start date is less than d2)
            repeat with e in evs
                set alarmText to ""
                repeat with a in (every display alarm of e)
                    set alarmText to alarmText & ((trigger interval of a) as integer) & ","
                end repeat
                set titleText to ""
                try
                    set titleText to (summary of e) as text
                end try
                set out to out & (uid of e) & tab & titleText & tab & my parts(start date of e) & tab & my parts(end date of e) & tab & alarmText & linefeed
            end repeat
        end repeat
    end tell
    return out
end run
'''

# argv: 1 calendar name, 2 title, 3-7 start (year month day hour minute), 8 length in minutes.
# Everything comes in as an argument: a title with a quote or a geresh must not become script text.
_CREATE = _HELPERS + '''
on run argv
    set d1 to my makeDate((item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer, (item 6 of argv) as integer, (item 7 of argv) as integer)
    set d2 to d1 + ((item 8 of argv) as integer) * minutes
    tell application "Calendar"
        set found to (every calendar whose name is (item 1 of argv))
        if (count of found) is not 1 then return "calendar: " & (count of found)
        set c to item 1 of found
        set e to make new event at end of events of c with properties {summary:(item 2 of argv), start date:d1, end date:d2}
        tell e to make new display alarm at end of display alarms with properties {trigger interval:-60}
        set alarmText to ""
        repeat with a in (every display alarm of e)
            set alarmText to alarmText & ((trigger interval of a) as integer) & ","
        end repeat
        return "ok" & tab & (uid of e) & tab & alarmText
    end tell
end run
'''

# argv: 1 calendar name, 2 uid, 3-7 start (year month day hour minute). Output: "deleted", or why nothing was deleted.
# The filter runs once, into a variable (see ScriptSpeedTests). Exactly one event, and never a repeating one: a series
# has one uid for all its weeks.
_DELETE = _HELPERS + '''
on run argv
    set d1 to my makeDate((item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer, (item 6 of argv) as integer, (item 7 of argv) as integer)
    set d2 to d1 + 1 * minutes
    tell application "Calendar"
        set found to (every calendar whose name is (item 1 of argv))
        if (count of found) is not 1 then return "calendar: " & (count of found)
        set c to item 1 of found
        set evs to (every event of c whose start date is greater than or equal to d1 and start date is less than d2 and uid is (item 2 of argv))
        if (count of evs) is not 1 then return "events: " & (count of evs)
        set e to item 1 of evs
        set rule to ""
        try
            set rule to (recurrence of e) as text
        end try
        if rule is not "" and rule is not "missing value" then return "repeating"
        delete e
        return "deleted"
    end tell
end run
'''


@dataclass(frozen=True)
class CalEvent:
    uid: str
    title: str
    start: datetime          # local time, without a zone: the way Calendar shows it
    end: datetime
    alarms: Tuple[int, ...]  # minutes relative to the start; -60 is one hour before


@dataclass(frozen=True)
class Finding:
    person: str
    name: str
    start: datetime
    status: str              # added | would_add | removed | would_remove | unprefixed | check | failed
    detail: str = ""


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)


def _args(moment: datetime) -> List[str]:
    return [str(moment.year), str(moment.month), str(moment.day), str(moment.hour), str(moment.minute)]


def list_events(calendar: str, since: datetime, until: datetime, osa: Callable = calendar_osa) -> List[CalEvent]:
    """Events of one calendar that start in [since, until). A damaged line is skipped."""
    out = osa(_LIST, [calendar, *_args(_naive(since)), *_args(_naive(until))])
    events: List[CalEvent] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 12:
            parts.append("")        # run_applescript strips the output: the empty alarm field of the last line is lost
        if len(parts) != 13:
            continue
        try:
            numbers = [int(p) for p in parts[2:12]]
            alarms = tuple(int(float(a)) for a in parts[12].split(",") if a.strip())
            events.append(CalEvent(parts[0], parts[1], datetime(*numbers[:5]), datetime(*numbers[5:]), alarms))
        except ValueError:
            continue
    return events


def create_event(calendar: str, title: str, start: datetime, end: datetime,
                 osa: Callable = calendar_osa) -> Tuple[str, Tuple[int, ...]]:
    """Add one event with an alert one hour before. Returns its uid and the alerts read back from the new event
    (read inside the same script: a second pass over the calendar would cost another 20 seconds)."""
    start, end = _naive(start), _naive(end)
    minutes = int((end - start).total_seconds() // 60)
    answer = osa(_CREATE, [calendar, title, *_args(start), str(minutes)]).strip()
    parts = answer.split("\t")
    if parts[0] != "ok" or len(parts) < 2 or not parts[1]:
        raise CalendarError(f"Calendar did not create the event ({answer or 'no answer'})")
    try:
        alarms = tuple(int(float(a)) for a in (parts[2] if len(parts) > 2 else "").split(",") if a.strip())
    except ValueError:
        alarms = ()
    return parts[1], alarms


def delete_event(calendar: str, uid: str, start: datetime, osa: Callable = calendar_osa) -> None:
    """Delete one event, found by its uid and its start. Raises CalendarError unless Calendar says it deleted it."""
    answer = osa(_DELETE, [calendar, uid, *_args(_naive(start))]).strip()
    if answer != "deleted":
        raise CalendarError(f"Calendar did not delete the event ({answer or 'no answer'})")


def format_findings(findings: Sequence[Finding], names: Dict[str, str], mode: str) -> Optional[Tuple[str, str]]:
    """(title, body) for the phone, or None when there is nothing to say."""
    if not findings:
        return None
    lines: List[str] = []
    added = would = check = removal = 0
    for f in findings:
        who = names.get(f.person, f.person)
        when = f"{f.start:%a %d/%m %H:%M}"
        if f.status in ("added", "removed"):
            continue                       # what the user expected: it is in the log, not a message
        elif f.status == "would_add":
            would += 1
            lines.append(f"Calendar: would add {who} - {f.name}, {when}")
        elif f.status == "would_remove":
            removal += 1
            lines.append(f"Calendar: would delete the event of {who} - {f.name}, {when}")
        else:
            check += 1
            what = who if f.name == "calendar" else f"{who} - {f.name}, {when}"
            lines.append(f"Check: {what}: " + (f"calendar problem: {f.detail}" if f.status == "failed" else f.detail))
    plural = lambda n, word: f"{n} {word}" + ("" if n == 1 else "s")     # noqa: E731
    if mode == "report":
        head = "Club booking: calendar check (report only)"
        parts = (([f"{plural(would, 'event')} missing"] if would else [])
                 + ([f"{plural(removal, 'event')} to delete"] if removal else [])
                 + ([f"{check} to check"] if check else []))
        title = head + (", " + ", ".join(parts) if parts else "")
    else:
        title = f"Club booking: calendar check, {check} to check"
    return (title, "\n".join(lines)) if lines else None


class CalendarAudit:
    """mode: "off" (does nothing), "report" (says what it would add), "write" (adds)."""

    def __init__(self, store: Store, people: Sequence[Person], osa: Callable = calendar_osa, mode: str = "off",
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self.store, self.people, self.osa, self.mode, self.now_fn = store, people, osa, mode, now_fn
        self._prefixes = [p.title_prefix for p in people if p.title_prefix]

    def check(self, person: Person, cards: Sequence[ClassCard], horizon_days: int) -> List[Finding]:
        return self.check_all([(person, cards)], horizon_days)

    def check_all(self, entries: Sequence[Tuple[Person, Sequence[ClassCard]]], horizon_days: int) -> List[Finding]:
        """Look at the classes each person is registered for. One pass per calendar (a pass over a big calendar
        takes about 20 seconds, and the two sons share one). Never raises: a problem is a finding."""
        if self.mode not in ("report", "write"):
            return []
        now = self.now_fn()
        horizon = now + timedelta(days=horizon_days)
        done = self.store.calendar_done()
        by_calendar: Dict[str, List[Tuple[Person, str, ClassCard]]] = {}
        for person, cards in entries:
            for card in sorted(cards, key=lambda c: c.start):
                ident = f"{person.key}|{card.key}"
                if (card.state == CardState.REGISTERED and now < card.start <= horizon and ident not in done
                        and self.store.attempt_count(f"calendar|{ident}") < MAX_TRIES):
                    by_calendar.setdefault(person.calendar, []).append((person, ident, card))
        findings: List[Finding] = []
        for calendar, items in by_calendar.items():
            starts = [card.start for _, _, card in items]
            since = _naive(min(starts))
            tries_key = f"calendar-list|{calendar}|{now:%Y%m%d}"
            if self.store.attempt_count(tries_key) >= MAX_TRIES:
                continue                    # a broken Calendar costs up to 90 seconds a run: stop for today
            try:
                events = list_events(calendar, since, _naive(max(starts)) + timedelta(minutes=1), self.osa)
            except Exception as exc:
                self.store.bump_attempt(tries_key)
                findings.append(Finding(items[0][0].key, "calendar", min(starts), "failed",
                                        f"could not read the calendar {calendar!r}: {exc}"))
                continue
            for person, ident, card in sorted(items, key=lambda i: i[2].start):
                finding = self._one(person, ident, card, events, since)
                if finding is not None:
                    findings.append(finding)
        return findings

    # -- one class ---------------------------------------------------------------------
    def _one(self, person: Person, ident: str, card: ClassCard, events: List[CalEvent],
             since: datetime) -> "Finding | None":
        start = _naive(card.start)
        # a repeating event is listed with the date of its first occurrence (before the window): its time of day counts
        same = [e for e in events if e.start == start or (e.start < since and e.start.time() == start.time())]
        if person.title_prefix:
            if any(e.title.startswith(person.title_prefix) for e in same):
                self._done(ident, card)
                return None
            bare = [e for e in same if not any(e.title.startswith(p) for p in self._prefixes)]
            if bare:
                self._done(ident, card)
                return Finding(person.key, card.name, card.start, "unprefixed",
                               f"the calendar has {bare[0].title!r} at that time without a name: nothing added")
        elif same:
            self._done(ident, card)
            return None
        if self.mode == "report":
            return Finding(person.key, card.name, card.start, "would_add")
        title = f"{person.title_prefix}{card.name}"
        self.store.bump_attempt(f"calendar|{ident}")        # counted before the write, like every counted action
        try:
            _, alarms = create_event(person.calendar, title, card.start, card.end, self.osa)
        except Exception as exc:
            return Finding(person.key, card.name, card.start, "failed", f"{type(exc).__name__}: {exc}")
        self._done(ident, card)             # the event exists now, also when its alert is wrong
        if -60 not in alarms:
            return Finding(person.key, card.name, card.start, "failed",
                           f"the event has no alert one hour before (alarms: {alarms})")
        return Finding(person.key, card.name, card.start, "added")

    def _done(self, ident: str, card: ClassCard) -> None:
        if self.mode == "write":
            self.store.mark_calendar_done(ident, card.start.strftime(_STARTED), self.now_fn())

    # -- a class the user unregistered ----------------------------------------------------
    def remove_dropped(self, entries: Sequence[Tuple[Person, Sequence[ClassCard]]]) -> List[Finding]:
        """entries: for every person whose booked tab was read TO ITS END, the cards of that reading (any state).
        A class this audit handled that is on none of them is a class the person unregistered from. Never raises."""
        if self.mode not in ("report", "write"):
            return []
        now = self.now_fn()
        local_now = _naive(now)
        write = self.mode == "write"
        findings: List[Finding] = []
        done = self.store.calendar_done_starts()
        dropped = self.store.calendar_dropped()
        ready: Dict[str, List[Tuple[Person, str, datetime]]] = {}       # calendar -> classes to look at
        for person, cards in entries:
            # only a class the person is registered or waitlisted for is "there": a favorite that shows "register" is not
            present = {c.key for c in cards if c.state in (CardState.REGISTERED, CardState.WAITLISTED)}
            mine: List[Tuple[Person, str, datetime]] = []
            for ident, start_text in done.items():
                owner, _, key = ident.partition("|")
                if owner != person.key:
                    continue
                try:
                    start = datetime.strptime(start_text, _STARTED)
                except ValueError:
                    continue
                if start <= local_now:
                    continue                          # a class that has started is history: its event stays
                if key in present:
                    if write and ident in dropped:
                        self.store.clear_calendar_dropped(ident)       # it is there: the earlier reading was wrong
                    continue
                if self.store.attempt_count(f"calendar-delete|{ident}") >= MAX_TRIES:
                    continue
                if write:
                    first = dropped.get(ident)
                    if first is None:
                        self.store.mark_calendar_dropped(ident, now)   # the first reading: only noted
                        continue
                    if now - datetime.fromisoformat(first) < MIN_GAP:
                        continue
                mine.append((person, ident, start))
            if len(mine) > MAX_REMOVALS:
                findings.append(Finding(person.key, "calendar", min(s for _, _, s in mine), "check",
                                        f"{len(mine)} classes look unregistered at once: nothing deleted "
                                        "(probably the list was read badly)"))
                continue
            for item in mine:
                ready.setdefault(item[0].calendar, []).append(item)
        for calendar, items in ready.items():
            starts = [s for _, _, s in items]
            tries_key = f"calendar-list|{calendar}|{now:%Y%m%d}"
            if self.store.attempt_count(tries_key) >= MAX_TRIES:
                continue
            try:
                events = list_events(calendar, min(starts), max(starts) + timedelta(minutes=1), self.osa)
            except Exception as exc:
                self.store.bump_attempt(tries_key)
                findings.append(Finding(items[0][0].key, "calendar", min(starts), "failed",
                                        f"could not read the calendar {calendar!r}: {exc}"))
                continue
            for person, ident, start in sorted(items, key=lambda i: i[2]):
                finding = self._remove_one(person, ident, start, events, write)
                if finding is not None:
                    findings.append(finding)
        return findings

    def _belongs(self, person: Person, title: str, name: str) -> bool:
        prefix = person.title_prefix
        if prefix and not title.startswith(prefix):
            return False
        return normalize_name(title[len(prefix):]) == name

    def _remove_one(self, person: Person, ident: str, start: datetime, events: List[CalEvent],
                    write: bool) -> "Finding | None":
        name = ident.split("|", 2)[2] if ident.count("|") >= 2 else ""
        same = [e for e in events if e.start == start and self._belongs(person, e.title, name)]
        if not same:
            if write:                       # no event of the class is left: nothing to delete, nothing to watch
                self.store.unmark_calendar_done(ident)
                self.store.clear_calendar_dropped(ident)
            return None
        shown = same[0].title[len(person.title_prefix):]
        if len(same) > 1:
            self.store.bump_attempt(f"calendar-delete|{ident}")
            return Finding(person.key, shown, start, "check", f"{len(same)} events match the class: nothing deleted")
        if not write:
            return Finding(person.key, shown, start, "would_remove")
        self.store.bump_attempt(f"calendar-delete|{ident}")        # counted before the write, like every counted action
        try:
            delete_event(person.calendar, same[0].uid, start, self.osa)
        except Exception as exc:
            return Finding(person.key, shown, start, "failed", f"{type(exc).__name__}: {exc}")
        self.store.unmark_calendar_done(ident)
        self.store.clear_calendar_dropped(ident)
        self.store.clear_attempt(f"calendar-delete|{ident}")
        self.store.clear_attempt(f"calendar|{ident}")
        return Finding(person.key, shown, start, "removed")
