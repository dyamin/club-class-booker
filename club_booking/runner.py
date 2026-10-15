"""One run: for each person select the profile, read the Favorites list, book what is open,
and remember what opens later."""
from __future__ import annotations

import time
import traceback
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .attention import Attention, GuardedScreen, UserActive
from .ax import AxError
from .calendar_app import CalendarError
from .config import Config, Person
from .models import CardState, ClassCard, normalize_name
from .osa import AppleScriptError
from .ocr import OcrError
from .pixels import PixelError, band_changed, blue_at, looks_like_selector, refine_button_center, selected_row
from .planner import Opening, Plan, openings_from_cards
from .screen import ScreenError
from .state import Store
from .vision import DialogRead

MAX_LIST_MOVES = 2                 # how often the list may move between the reading and the tap, per person
TAP_BAND_ABOVE, TAP_BAND_BELOW = 70, 25      # the strip of a card (title to button) that must stay the same
DIALOG_BAND = (720, 790)                     # the strip with the buttons of a confirmation dialog
_KNOWN_ERRORS = (CalendarError, OcrError, AppleScriptError, ScreenError)


class RunError(Exception):
    pass


class StuckError(RunError):
    """The app is not where it should be. Quitting it and opening it again may help."""


class LoggedOutError(RunError):
    """The app shows its login screen. A restart cannot help: the login code comes by SMS to the user."""


LOGGED_OUT = ("the club app is logged out. Open it and log in with your phone number "
              "(the code comes by SMS), then run again")
UPDATE_NEEDED = ("the club app needs an update (a popup asks for it). Update it in the App Store, open it, "
                 "and log in, then run again")
BLOCKED = {"login": LOGGED_OUT, "code": LOGGED_OUT, "locked": LOGGED_OUT, "update": UPDATE_NEEDED}
OVERLAP_REFUSAL = "שיעורים חופפים"          # the club: "you cannot register for two overlapping classes" (seen 2026-10-15)
WINDOW_HIDDEN = ("the club app window is not visible (closed, minimized, or on another desktop): open it and the "
                 "program goes on")           # 2026-10-15: the screen tool then answers "outside the 'club' window (500x500)"
USER_ACTIVE = "the user came back to the Mac: this run stood back"     # no failure, no message (see attention.py)


@dataclass
class Outcome:
    person: str
    name: str
    start: datetime
    status: str            # booked | waitlisted | would_book | full | not_opened | tap_failed | list_moved | unconfirmed | booked_other
    detail: str = ""
    calendar: str = ""     # "ok", "failed: ...", or "" when not tried
    card_key: str = ""     # ClassCard.key of the class that was booked or waitlisted (the questions need it)


@dataclass
class PersonResult:
    person: str
    cards: List[ClassCard] = field(default_factory=list)
    booked: List[ClassCard] = field(default_factory=list)   # the booked tab: every class the person is registered for
    booked_read: bool = False                               # was the booked tab read well (an empty list is then true)
    booked_complete: bool = False                           # ... and to its END (the audit deletes only on such a list)
    outcomes: List[Outcome] = field(default_factory=list)
    error: str = ""
    # overlapping classes of a person who asks: (the class that is, or was, booked; the others left unbooked; was it
    # booked before this run)
    overlaps: List[Tuple[ClassCard, List[ClassCard], bool]] = field(default_factory=list)


@dataclass
class RunResult:
    run_id: str
    people: List[PersonResult]
    plan: Optional[Plan] = None
    warnings: List[str] = field(default_factory=list)     # what the reader could not understand


def _overlap(a: ClassCard, b: ClassCard) -> bool:
    return a.start < b.end and b.start < a.end


def _overlap_groups(cards: List[ClassCard]) -> List[List[ClassCard]]:
    """Classes that overlap each other, directly or through another class, grouped."""
    groups: List[List[ClassCard]] = []
    for c in sorted(cards, key=lambda x: x.start):
        touching = [g for g in groups if any(_overlap(c, m) for m in g)]
        merged = [c] + [m for g in touching for m in g]
        groups = [g for g in groups if g not in touching] + [merged]
    return groups


def _dm(text: str) -> Tuple[int, int]:
    day, month = text.strip().split("/")[:2]
    return int(day), int(month)


def _hm(text: str) -> Tuple[int, int]:
    hour, minute = text.strip().split(":")[:2]
    return int(hour), int(minute)


def _same_title(a: str, b: str) -> bool:
    x, y = normalize_name(a), normalize_name(b)
    return bool(x) and bool(y) and (x in y or y in x)


def dialog_check(dialog: DialogRead, card: ClassCard) -> str:
    """What the screen after the tap says:
    match (the booking of this class), title_differs (a booking on the same day and time under another title),
    other_class (a booking, but not for this day and time), not_booked (not a booking confirmation)."""
    # a full class shows a waitlist confirmation; the app's own text for it has not been seen, so a booking
    # confirmation for the same class is accepted too
    expected = ("waitlisted", "booked") if card.state == CardState.FULL else ("booked",)
    if dialog.kind not in expected:
        return "not_booked"
    if card.state == CardState.FULL and dialog.kind == "waitlisted" and not dialog.date.strip() and not dialog.start.strip():
        return "match"           # the waitlist message may not name the class; the tap was on a verified card
    try:
        day, month = _dm(dialog.date)
        hour, minute = _hm(dialog.start)
    except ValueError:
        return "other_class"
    if (day, month, hour, minute) != (card.start.day, card.start.month, card.start.hour, card.start.minute):
        return "other_class"
    return "match" if _same_title(dialog.title, card.name) else "title_differs"


def dialog_matches(dialog: DialogRead, card: ClassCard) -> bool:
    """The confirmation is a booking, for this day and time, with a similar name."""
    return dialog_check(dialog, card) == "match"


class Runner:
    def __init__(self, cfg: Config, screen, vision, store: Store, calendar,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 sleep: Callable[[float], None] = time.sleep, dry_run: bool = False, club_app=None,
                 attention: Optional[Attention] = None) -> None:
        self.cfg = cfg
        self.attention = attention      # None = nobody is asked: a run the user started and watches
        self.screen = GuardedScreen(screen, attention) if attention is not None else screen
        self.vision = vision
        self.store = store
        self.calendar = calendar
        self.now_fn = now_fn
        self.sleep = sleep
        self.dry_run = dry_run
        self.club_app = club_app        # starts and restarts the app; None = never restart
        self._logged_out = ""
        self._popup_open = False        # a message stayed open after a waitlist join: no restart, no more taps
        self.booked_complete = False    # the last read_booked reached the end of the list (see read_booked)
        self.run_id = uuid.uuid4().hex[:8]
        self._started = now_fn()
        self.rows = [p.selector_row_y for p in cfg.people]

    def _check_budget(self) -> None:
        """A run that hangs must stop. The limit is max_run_seconds in the config."""
        if self.now_fn() - self._started > timedelta(seconds=self.cfg.max_run_seconds):
            raise RunError(f"the run took longer than {self.cfg.max_run_seconds} seconds and was stopped")

    # -- moving around the app -----------------------------------------------------
    def _close_selector(self) -> None:
        """Close the person selector and check that it is closed: its screen shows the children's names."""
        c = self.cfg.coords
        for _ in range(2):
            self.screen.tap(*c.selector_close)
            self.sleep(2)
            if not looks_like_selector(self.screen.screenshot(), self.rows, c.selector_row_x):
                return
        raise StuckError("the person selector did not close")

    def ensure_person(self, person: Person) -> None:
        """Make the app show this person, and verify it by the check mark in the selector."""
        c = self.cfg.coords
        target = self.cfg.people.index(person)
        for _ in range(4):
            png = self.screen.screenshot()
            if not looks_like_selector(png, self.rows, c.selector_row_x):
                self.screen.tap(*c.selector_open)
                self.sleep(2)
                png = self.screen.screenshot()
                if not looks_like_selector(png, self.rows, c.selector_row_x):
                    continue
            if selected_row(png, self.rows, c.selector_row_x) == target:
                self._close_selector()
                return
            self.screen.tap(c.selector_row_x, self.rows[target])
            self.sleep(self.cfg.settle_seconds)
        raise StuckError(f"could not switch the app to {person.first_name}")

    def _stop_if_blocked(self) -> None:
        """One look before taps made without looking. On the login screen the Classes tab (640, 775) lies inside the
        button "send me a code" (y 748-785): a tap there asks for an SMS, and three requests lock the login for ten
        minutes. A session can end at any moment, also right after the selector was checked."""
        kind = self._screen_kind()
        if kind in BLOCKED:
            raise LoggedOutError(BLOCKED[kind])
        if kind == "details":            # a class page left open (a card was pressed, then something went wrong)
            back = getattr(self.vision, "back", None)
            if back is not None:
                try:
                    back()
                    self.sleep(2)
                except Exception as exc:
                    self.store.log("popup_not_closed", run_id=self.run_id, error=f"{type(exc).__name__}: {exc}")

    def go_to_favorites(self) -> None:
        """Open the Favorites list at its top. Switching sub-tabs rebuilds the list from the top.
        A tap can be lost (the first click on a window that is not in front only brings it to the front),
        so the blue line under the Favorites tab is checked, and the taps are made again."""
        c = self.cfg.coords
        for _ in range(3):
            self._stop_if_blocked()
            self.screen.tap(*c.tab_classes)
            self.sleep(2)
            self.screen.tap(*c.subtab_schedule)
            self.sleep(2)
            self.screen.tap(*c.subtab_favorites)
            self.sleep(self.cfg.settle_seconds)
            if blue_at(self.screen.screenshot(), c.favorites_underline):
                return
        raise StuckError("the Favorites tab did not open")

    def go_to_booked(self) -> None:
        """Open the booked sub-tab (every class the profile is registered for). Same care as the Favorites tab."""
        c = self.cfg.coords
        for _ in range(3):
            self._stop_if_blocked()
            self.screen.tap(*c.tab_classes)
            self.sleep(2)
            self.screen.tap(*c.subtab_schedule)
            self.sleep(2)
            self.screen.tap(*c.subtab_booked)
            self.sleep(self.cfg.settle_seconds)
            if blue_at(self.screen.screenshot(), c.booked_underline):
                return
        raise StuckError("the booked tab did not open")

    def read_booked(self, stop_key: Optional[str] = None) -> List[ClassCard]:
        """Every class the open profile is registered for or waitlisted for, from the booked tab. With stop_key the
        reading stops as soon as that class is on the screen (its card is then the one of the last reading)."""
        c0 = self.cfg.coords
        seen: Dict[str, ClassCard] = {}
        without_news = 0
        self.booked_complete = False         # true only when the list ended by itself: two screens in a row with no news
        for _ in range(self.cfg.max_scroll_screens):
            self._check_budget()
            png = self.screen.screenshot()
            if looks_like_selector(png, self.rows, c0.selector_row_x):
                raise StuckError("the person selector is open; its screenshot was not sent to the reader")
            listing = self.vision.read_list(png)
            if listing.screen in BLOCKED:
                raise LoggedOutError(BLOCKED[listing.screen])
            if listing.screen != "booked":
                raise StuckError(f"the app does not show the booked tab (the reader saw: {listing.screen})")
            fresh = [c for c in listing.cards if c.key not in seen]
            for c in listing.cards:
                seen[c.key] = c                      # the newest reading of a card has the button that is on the screen
            if stop_key is not None and stop_key in seen and stop_key in {c.key for c in listing.cards}:
                break
            without_news = 0 if fresh else without_news + 1
            if without_news >= 2:
                self.booked_complete = stop_key is None
                break
            scroll = getattr(self.vision, "scroll_page", None)
            try:
                if scroll is None:
                    raise AttributeError("the reader cannot scroll")
                scroll()
            except Exception as exc:
                if isinstance(exc, AxError) and str(exc) == "no scroll area":
                    self.booked_complete = stop_key is None      # nothing to scroll: all of the list is on this screen
                break          # never swipe here: a swipe that starts on a card opens its class page (lessons row 4)
            self.sleep(self.cfg.settle_seconds)
        return list(seen.values())

    def card_is_open(self, person: Person, key: str) -> bool:
        """True when the class is on the person's Favorites list and its button says "register". Only looks."""
        self.ensure_person(person)
        self.go_to_favorites()
        cards, _ = self._scan(person, set(), everything=True)
        return any(c.key == key and c.state == CardState.OPEN for c in cards)

    def registered_keys(self, person: Person) -> Optional[Set[str]]:
        """The keys of the classes the person is registered or waitlisted for now, from the booked tab. None when the
        booked tab could not be read."""
        try:
            self.ensure_person(person)
            self.go_to_booked()
            return {c.key for c in self.read_booked() if c.state in (CardState.REGISTERED, CardState.WAITLISTED)}
        except LoggedOutError:
            raise
        except Exception as exc:
            self.store.log("booked_read_failed", run_id=self.run_id, person=person.key,
                           error=f"{type(exc).__name__}: {exc}")
            return None

    def cancel(self, person: Person, ident: str) -> str:
        """Give up a booking, or leave a waitlist spot, that the program made itself (spec R18). Returns
        "cancelled", "already_gone", "refused: ..." (nothing was touched) or "problem: ...".

        This is the only place that cancels anything. The guard comes first: only an ident in the program's own
        standing bookings of this very person is ever pressed. The press is by the button's exact text through
        accessibility, never by a guessed position. The attempt is written before the press and is never repeated."""
        row = self.store.standing_bookings().get(ident)
        if row is None or row["person"] != person.key:
            return "refused: the program did not make this booking"
        if self.dry_run:
            return "refused: this is a dry run"
        presser = getattr(self.vision, "press_card_action", None)
        if presser is None:
            return "problem: cancelling needs the accessibility reader"
        key, name = row["card_key"], row["name"]
        desc, expected = ("הסרה מהמתנה", CardState.WAITLISTED) if row["status"] == "waitlisted" \
            else ("ביטול", CardState.REGISTERED)
        stage = "before"             # before | pressed: what a failure must tell
        try:
            self.ensure_person(person)
            self.go_to_booked()
            card = next((c for c in self.read_booked(stop_key=key) if c.key == key), None)
            if card is None:
                self.store.record_booking(self.run_id, person.key, key, name, status="cancelled")
                return "already_gone"
            if card.state != expected:
                return f"problem: the class shows as {card.state.value}, not {expected.value}; nothing was pressed"
            if card.button is None:
                return "problem: the class is cut off by the edge of the screen; nothing was pressed"
            if self.attention is not None:
                self.attention.checkpoint()      # the last place to stand back: nothing is written or pressed yet
            with self._finish_what_was_begun():  # then the cancel is finished, whatever the user does
                self.store.record_booking(self.run_id, person.key, key, name, status="cancel_attempted")
                stage = "written"
                presser(card, desc)
                stage = "pressed"
                self.sleep(2)
                reader = getattr(self.vision, "read_popup", None)
                kind, text = reader() if reader is not None else ("none", "")
                if kind == "unknown":              # a fee, an "are you sure": nobody read it, so nothing is confirmed blind
                    self._save_window_picture(f"{person.key}-cancel-message-{key}")
                    self.store.log("cancel_message", run_id=self.run_id, person=person.key, card=key, text=text)
                    return ("problem: after the press the app shows an unexpected message "
                            f"({text[:80]}); nothing more was pressed, check the app")
                if kind == "success":
                    self._close_popup()
                    self.sleep(1)
                self.go_to_booked()
                after = {c.key: c for c in self.read_booked()}
                if key in after and after[key].state == expected:
                    return "problem: the class still shows as booked after the cancel"
                self.store.record_booking(self.run_id, person.key, key, name, status="cancelled")
                return "cancelled"
        except LoggedOutError:
            raise
        except Exception as exc:
            what = f"{type(exc).__name__}: {exc}"
            if stage == "before":              # nothing was written or pressed: the answer can be tried again
                return f"retry: {what}"
            return f"problem: {'after the press: ' if stage == 'pressed' else ''}{what}"

    def _read_all_booked(self, persons: Sequence[Person], results: List["PersonResult"]) -> None:
        """After every person has been booked: the booked tab of each, for the calendar check and the questions.
        It takes about half a minute per person, so it never sits between two people's bookings. A problem here must
        not fail the person."""
        if self.cfg.calendar_audit == "off" and self.cfg.questions == "off":
            return
        for person, result in zip(persons, results):
            unconfirmed = any(o.status == "unconfirmed" for o in result.outcomes)   # the booked tab tells if it was booked
            if (result.error and not unconfirmed) or self._logged_out:
                continue
            try:
                self._check_budget()
                self.ensure_person(person)
                self.go_to_booked()
                result.booked = self.read_booked()
                result.booked_read = True
                result.booked_complete = self.booked_complete
            except LoggedOutError as exc:
                self._logged_out = str(exc)
                self.store.log("logged_out", run_id=self.run_id, person=person.key)
            except Exception as exc:
                self.store.log("booked_read_failed", run_id=self.run_id, person=person.key,
                               error=f"{type(exc).__name__}: {exc}")

    def _reconcile(self, result: "PersonResult") -> None:
        """A booking whose confirmation was not recognized (2026-10-15 14:03: the screen showed the list after the tap) is
        checked against the booked tab, which was read for this person. A class that is on it IS booked: the person has no
        problem and the user gets no message. A class that is not on it stays a problem."""
        if not result.booked_read:
            return
        held = {c.key for c in result.booked if c.state == CardState.REGISTERED}
        for o in result.outcomes:
            if o.status == "unconfirmed" and o.card_key in held:
                if o.detail and o.detail in result.error:         # the person's error was this failure (and maybe a restore note)
                    result.error = result.error.replace(o.detail, "").strip("; ").strip()
                o.status, o.detail = "booked", "confirmed on the booked tab"
                self.store.record_booking(self.run_id, result.person, o.card_key, o.name, status="confirmed")

    # -- reading the list -----------------------------------------------------------
    def _eligible(self, person: Person, card: ClassCard, skip: Set[str], now: datetime) -> bool:
        limit = now + timedelta(days=self.cfg.horizon_days)
        wanted = (CardState.OPEN, CardState.FULL) if self.cfg.questions == "on" else (CardState.OPEN,)    # FULL: join the waitlist
        if card.state == CardState.FULL and self.store.attempt_count(f"waitlist-fail|{person.key}|{card.key}") >= 3:
            return False                # the waitlist button could not be tapped three times: leave it to the user
        return (card.state in wanted and card.button is not None
                and now < card.start <= limit and f"{person.key}|{card.key}" not in skip)

    def _scan(self, person: Person, skip: Set[str], everything: bool = False
              ) -> Tuple[List[ClassCard], Optional[Tuple[ClassCard, bytes]]]:
        """Read the list from the top. Stop at the first class that should be booked (not when everything is wanted)."""
        now = self.now_fn()
        c0 = self.cfg.coords
        seen: Dict[str, ClassCard] = {}
        previous: Set[str] = set()
        without_news = 0
        for _ in range(self.cfg.max_scroll_screens):
            self._check_budget()
            png = self.screen.screenshot()
            if looks_like_selector(png, self.rows, c0.selector_row_x):
                raise StuckError("the person selector is open; its screenshot was not sent to the reader")
            listing = self.vision.read_list(png)
            if listing.screen in BLOCKED:
                raise LoggedOutError(BLOCKED[listing.screen])
            if listing.screen != "favorites":
                raise StuckError(f"the app does not show the Favorites list (the reader saw: {listing.screen})")
            cards = listing.cards
            keys = {c.key for c in cards}
            if previous and keys and not (previous & keys):
                self.store.log("scan_gap", run_id=self.run_id, person=person.key, before=sorted(previous), now=sorted(keys))
                self._save_window_picture(f"{person.key}-scan-gap")          # what the window looked like at that moment
                raise RunError("two screens of the list have no class in common; classes between them were not seen")
            previous = keys
            fresh = [c for c in cards if c.key not in seen]
            for c in cards:
                seen.setdefault(c.key, c)
            for c in cards:
                if not everything and self._eligible(person, c, skip, now):
                    return list(seen.values()), (c, png)
            without_news = 0 if fresh else without_news + 1
            if without_news >= 2:
                break
            scroll = getattr(self.vision, "scroll_page", None)     # a page scroll by the reader overlaps; a swipe flings
            try:
                if scroll is None:
                    raise AttributeError("the reader cannot scroll")
                scroll()
            except Exception:                  # no accessibility for this job: swipe (the overlap check still guards)
                self.screen.swipe(*c0.scroll_from, *c0.scroll_to)
            self.sleep(self.cfg.settle_seconds)
        return list(seen.values()), None

    def _bring_into_view(self, card: ClassCard) -> bool:
        """Scroll the Favorites list from its top until this class is on the screen. The list is virtual: only the
        classes on the screen can be pressed."""
        self.go_to_favorites()
        for _ in range(self.cfg.max_scroll_screens):
            listing = self.vision.read_list(self.screen.screenshot())
            if listing.screen != "favorites":
                return False
            if any(c.key == card.key for c in listing.cards):
                return True
            scroll = getattr(self.vision, "scroll_page", None)
            if scroll is None:
                return False
            scroll()
            self.sleep(self.cfg.settle_seconds)
        return False

    def _crowd(self, card: ClassCard) -> Optional[int]:
        """How many people registered, from the class page (spec R19). None when the reader cannot count."""
        opener = getattr(self.vision, "open_card", None)
        counter = getattr(self.vision, "read_attendees", None)
        back = getattr(self.vision, "back", None)
        if not (opener and counter and back):
            return None
        count: Optional[int] = None
        try:
            if not self._bring_into_view(card):
                raise RunError("the class could not be brought on the screen")
            opener(card)
            self.sleep(3)
            count = counter()
        except Exception as exc:
            self.store.log("crowd_unknown", run_id=self.run_id, card=card.key, error=f"{type(exc).__name__}: {exc}")
        try:
            back()
            self.sleep(2)
        except Exception as exc:
            self.store.log("popup_not_closed", run_id=self.run_id, error=f"{type(exc).__name__}: {exc}")
        return count

    def _plan_overlaps(self, person: Person, skip: Set[str]) -> List[Tuple[ClassCard, List[ClassCard], bool]]:
        """For a person who asks: classes that overlap are not all booked (spec R17b). Of a group the one that is
        booked already stays; else the more crowded one is booked (R19; the earlier one when the crowd is unknown).
        The others go into `skip`, and the user is asked which to keep."""
        now = self.now_fn()
        cards, _ = self._scan(person, skip, everything=True)
        mine = self.store.standing_bookings()
        eligible = [c for c in cards if self._eligible(person, c, skip, now)]
        booked = [c for c in cards if c.state == CardState.REGISTERED]      # the program's own and the user's
        chosen = self.store.chosen()          # a class the user chose in an answer is booked, not skipped
        plans: List[Tuple[ClassCard, List[ClassCard], bool]] = []
        counted = False
        for group in _overlap_groups(eligible + booked):
            waiting = [c for c in group if c in eligible and f"{person.key}|{c.key}" not in chosen]
            if len(group) < 2 or not waiting:
                continue
            already = sorted([c for c in group if c in booked], key=lambda c: f"{person.key}|{c.key}" not in mine)
            if already:                      # the program's own booking first, else the class the user registered
                default, others, was_booked = already[0], waiting, True
            else:
                crowd = {c.key: self._crowd(c) for c in waiting}
                counted = True
                default = max(waiting, key=lambda c: (crowd[c.key] if crowd[c.key] is not None else -1, -c.start.timestamp()))
                others, was_booked = [c for c in waiting if c is not default], False
            for other in others:
                skip.add(f"{person.key}|{other.key}")
            plans.append((default, others, was_booked))
        if counted:
            self.go_to_favorites()            # the class pages replaced the list: start from its top again
        return plans

    def _scan_once_more(self, person: Person, skip: Set[str]) -> Tuple[List[ClassCard], Optional[Tuple[ClassCard, bytes]]]:
        """_scan, and a second reading from the top when two screens had no class in common (the list moved: a touch of
        the mouse, or a jump). The guard stays: a list that keeps jumping still fails, and no class is skipped silently."""
        try:
            return self._scan(person, skip)
        except RunError as exc:
            if "no class in common" not in str(exc):
                raise
            self.store.log("scan_gap_retry", run_id=self.run_id, person=person.key)
            self.go_to_favorites()
            return self._scan(person, skip)

    def _check_not_empty(self, person: Person) -> None:
        """An empty list is believed only when no class is planned for this person."""
        plan = self.store.load_plan()
        now = self.now_fn()
        planned = [o for o in (plan.openings if plan else ()) if o.person == person.key and o.start > now]
        if planned:
            raise RunError(f"the Favorites list looked empty, but {len(planned)} classes are planned for "
                           f"{person.first_name}; the reading is not trusted")

    # -- booking ----------------------------------------------------------------------
    def _close_dialog(self, shown: bytes) -> None:
        """Tap OK on a booking confirmation, but only while the screen still looks like it. When the bottom strip
        changed (the calendar step can change the screen) the screen is read again first. Never tap blind."""
        try:
            now = self.screen.screenshot()
            if band_changed(shown, now, *DIALOG_BAND) and self.vision.read_dialog(now).kind != "booked":
                self.store.log("dialog_not_closed", run_id=self.run_id, reason="the screen is no longer the confirmation")
                return
            self.screen.tap(*self.cfg.coords.dialog_ok)
            self.sleep(2)
        except Exception as exc:
            self.store.log("dialog_not_closed", run_id=self.run_id, reason=f"{type(exc).__name__}: {exc}")

    def _book(self, person: Person, card: ClassCard, png: bytes) -> Outcome:
        c = self.cfg.coords
        # The reader looked at `png`. Take a new screenshot and tap only if the card did not move since then.
        fresh = self.screen.screenshot()
        if looks_like_selector(fresh, self.rows, c.selector_row_x):
            return Outcome(person.key, card.name, card.start, "list_moved", "the person selector is open")
        top = card.button[1]
        if band_changed(png, fresh, top - TAP_BAND_ABOVE, top + TAP_BAND_BELOW):
            return Outcome(person.key, card.name, card.start, "list_moved", "the list moved after it was read")
        try:
            x, y = refine_button_center(fresh, card.button)
        except PixelError as exc:
            return Outcome(person.key, card.name, card.start, "tap_failed", str(exc))
        if abs(x - c.register_x) > c.register_tolerance:
            return Outcome(person.key, card.name, card.start, "tap_failed",
                           f"the button is not in the register column (x={x}, expected {c.register_x}"
                           f" plus or minus {c.register_tolerance})")
        if self.dry_run:
            return Outcome(person.key, card.name, card.start, "would_book")
        self.store.save_evidence(f"{person.key}-before-{card.key}", fresh)
        if self.attention is not None:
            self.attention.checkpoint()      # the last place to stand back: nothing is written or pressed yet
        # Write ahead: from here on the class counts as booked, whatever happens next. It is never tapped again by itself.
        self.store.record_booking(self.run_id, person.key, card.key, card.name, status="attempted")
        self.store.mark_handled(f"{person.key}|{card.key}")
        with self._finish_what_was_begun():
            self.screen.tap(x, y)
            self.sleep(self.cfg.settle_seconds)
            try:
                return self._after_tap(person, card)
            except Exception as exc:
                return Outcome(person.key, card.name, card.start, "unconfirmed",
                               f"after the tap: {type(exc).__name__}: {exc}; check the app", card_key=card.key)

    def _finish_what_was_begun(self):
        """From the register tap to the closed confirmation the user does not stop the run: a dialog left open is worse
        than two more clicks."""
        return self.attention.hold() if self.attention is not None else nullcontext()

    def _close_popup(self) -> None:
        """Close a message popup with the reader's help (accessibility). Never raises; a popup that stays is found by
        the next step, which does not see the list."""
        closer = getattr(self.vision, "close_popup", None)
        if closer is None:
            return
        try:
            closer()
        except Exception as exc:
            self.store.log("popup_not_closed", run_id=self.run_id, error=f"{type(exc).__name__}: {exc}")

    def _save_window_picture(self, label: str) -> None:
        """Evidence of what the window looked like when a step failed (pop-up menus included). Never raises."""
        try:
            self.store.save_evidence(label, self.screen.region_png())
        except Exception as exc:
            self.store.log("evidence_failed", run_id=self.run_id, error=f"{type(exc).__name__}: {exc}")

    def _after_tap(self, person: Person, card: ClassCard) -> Outcome:
        after = self.screen.screenshot()
        self.store.save_evidence(f"{person.key}-after-{card.key}", after)
        dialog = self.vision.read_dialog(after)
        verdict = dialog_check(dialog, card)
        if verdict == "not_booked" and dialog.kind == "refused":
            # for example Robin is not old enough for a class: remember it, close the popup, go on with the next class
            self.store.mark_refused(f"{person.key}|{card.key}", dialog.message)
            self._close_popup()
            return Outcome(person.key, card.name, card.start, "refused",
                           f"the club did not accept the registration ({dialog.message or 'no reason shown'}); "
                           "it will not be tried again")
        if verdict == "not_booked":
            return Outcome(person.key, card.name, card.start, "unconfirmed",
                           f"after the tap the screen showed {dialog.kind!r}, not a booking confirmation; check the app",
                           card_key=card.key)
        if verdict == "other_class":
            self.store.record_booking(self.run_id, person.key, f"unexpected|{dialog.date} {dialog.start}|{dialog.title}",
                                      dialog.title, status="unexpected")
            self._close_dialog(after)
            return Outcome(person.key, card.name, card.start, "booked_other",
                           f"the confirmation shows {dialog.title!r} on {dialog.date} at {dialog.start}, not {card.name!r} "
                           f"on {card.start:%d/%m} at {card.start:%H:%M}; the app holds that booking now")
        if card.state == CardState.FULL:
            # the class was full: the program is on the waitlist now (spec R14); it keeps the spot and says nothing
            self.store.record_booking(self.run_id, person.key, card.key, card.name, status="waitlisted")
            self._close_popup()
            self.sleep(1)                         # the sheet takes a moment to go
            outcome = Outcome(person.key, card.name, card.start, "waitlisted", "joined the waitlist", card_key=card.key)
            if self._screen_kind() != "favorites":
                self._popup_open = True           # the real waitlist message has never been seen: do not guess
                outcome.detail = "joined the waitlist; a message is still open: check the app"
            return outcome
        self.store.record_booking(self.run_id, person.key, card.key, card.name)
        outcome = Outcome(person.key, card.name, card.start, "booked", card_key=card.key)
        if verdict == "title_differs":
            outcome.detail = f"the confirmation names {dialog.title!r}"
        if self.cfg.calendar_audit == "write":
            outcome.calendar = "deferred"          # the calendar audit adds the event after all bookings
        else:
            try:
                self.calendar.add(person, card)
                outcome.calendar = "ok"
            except Exception as exc:
                outcome.calendar = f"failed: {exc}" if isinstance(exc, _KNOWN_ERRORS) else f"failed: {type(exc).__name__}: {exc}"
                self._save_window_picture(f"{person.key}-calendar-failure-{card.key}")
        self._close_dialog(after)
        return outcome

    def _screen_kind(self) -> str:
        """What the app shows, by one look. The selector is recognised without the reader: its picture shows
        the children's names and is never sent."""
        try:
            png = self.screen.screenshot()
            if looks_like_selector(png, self.rows, self.cfg.coords.selector_row_x):
                return "selector"
            return self.vision.read_list(png).screen
        except Exception:
            return "unknown"

    def _restart_app(self, reason: Exception) -> None:
        if self.attention is not None:
            self.attention.checkpoint()
        self.store.log("app_restart", run_id=self.run_id, reason=str(reason))
        self.club_app.restart()
        self.sleep(self.cfg.app_restart_wait_seconds)
        reconnect = getattr(self.screen, "reconnect", None)
        if reconnect is not None:
            reconnect()           # the screen tool looks for the window of the new process

    def run_person(self, person: Person) -> PersonResult:
        result = PersonResult(person.key)
        # a class booked by the program, seen registered, or refused by the club is never tried again by it
        skip: Set[str] = (set(self.store.booked_idents()) | self.store.seen_registered()
                          | set(self.store.refused())) - self.store.chosen()      # a class the user chose is tried again
        moves = 0
        restarts = 0
        try:
            if self.cfg.questions == "on" and (person.asks or person.asks_overlaps) and not self.dry_run:
                self._check_budget()
                before = set(skip)
                try:
                    self.ensure_person(person)
                    self.go_to_favorites()
                    result.overlaps = self._plan_overlaps(person, skip)
                except LoggedOutError:
                    raise
                except Exception as exc:       # a failure of this new step never stops a booking: no overlap rule this run
                    skip.clear()
                    skip.update(before)
                    result.overlaps = []
                    self.store.log("overlap_plan_failed", run_id=self.run_id, person=person.key,
                                   error=f"{type(exc).__name__}: {exc}")
            for _ in range(self.cfg.max_booking_rounds):
                self._check_budget()
                try:
                    self.ensure_person(person)
                    self.go_to_favorites()
                    cards, pick = self._scan_once_more(person, skip)
                except StuckError as exc:
                    if self.club_app is None:
                        raise
                    kind = self._screen_kind()
                    if kind in BLOCKED:
                        raise LoggedOutError(BLOCKED[kind]) from exc
                    if self._popup_open:
                        raise            # a restart can log the app out: a message that stays open is reported instead
                    restarts += 1
                    if restarts > self.cfg.max_app_restarts:
                        raise
                    self._restart_app(exc)
                    continue
                if pick is None:
                    if not cards:
                        self._check_not_empty(person)
                    result.cards = cards
                    return result
                card, png = pick
                outcome = self._book(person, card, png)
                if outcome.status == "list_moved":
                    moves += 1
                    if moves > MAX_LIST_MOVES:
                        result.error = f"the list moved before every tap ({outcome.detail})"
                        return result
                    continue                      # read the list again from the top
                result.outcomes.append(outcome)
                skip.add(f"{person.key}|{card.key}")
                if outcome.status == "tap_failed" and card.state == CardState.FULL:
                    # no tap was made (the button looked different): this must not block the person's other classes
                    self.store.bump_attempt(f"waitlist-fail|{person.key}|{card.key}")
                    continue
                if self._popup_open:
                    result.error = "a message is still open after joining the waitlist: check the app"
                    return result
                if outcome.status in ("tap_failed", "unconfirmed", "booked_other"):     # a refusal does not stop the person
                    result.error = outcome.detail
                    return result
            result.error = "stopped after too many booking rounds"
        except UserActive:
            result.error = USER_ACTIVE      # what was booked before stays in `result`: it is reported, not lost
        except LoggedOutError as exc:
            self._logged_out = result.error = str(exc)
            self.store.log("logged_out", run_id=self.run_id, person=person.key)
        except Exception as exc:     # one person's failure must not stop the others
            result.error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, ScreenError) and "outside the" in str(exc) and "window" in str(exc):
                result.error = WINDOW_HIDDEN
            self.store.log("person_failed", run_id=self.run_id, person=person.key, error=result.error,
                           trace=traceback.format_exc())
        return result

    # -- what the last full read of the list tells us ----------------------------------
    def _bookkeeping(self, person: Person, result: PersonResult) -> None:
        now = self.now_fn()
        # a booking of the program that the list shows open again was cancelled by hand: it is no longer the program's to
        # cancel (spec R18), also if the user registers the class again by hand
        by_key = {c.key: c for c in result.cards}
        for ident, row in self.store.standing_bookings().items():
            card = by_key.get(row["card_key"])
            if row["person"] == person.key and card is not None and \
                    card.state in (CardState.OPEN, CardState.FULL, CardState.NOT_OPEN):
                self.store.record_booking(self.run_id, person.key, row["card_key"], row["name"], status="cancelled")
        for card in result.cards:
            ident = f"{person.key}|{card.key}"
            if card.state in (CardState.REGISTERED, CardState.WAITLISTED):
                self.store.mark_handled(ident)
                self.store.mark_seen_registered(ident)     # if it shows open later, the user cancelled it: leave it
            elif card.state == CardState.FULL:
                self.store.mark_handled(ident)
                if self.store.bump_attempt(f"full|{ident}") == 1:
                    result.outcomes.append(Outcome(person.key, card.name, card.start, "full",
                                                   "the class is full; the waitlist was not joined"))
            elif card.state == CardState.NOT_OPEN and card.opens_at <= now - timedelta(minutes=2):
                if self.store.bump_attempt(f"open|{ident}") >= self.cfg.max_open_retries:
                    self.store.mark_handled(ident)
                    result.outcomes.append(Outcome(person.key, card.name, card.start, "not_opened",
                                                   "registration did not open at the expected time"))

    def _merge_plan(self, persons: Sequence[Person], results: Sequence[PersonResult]) -> Plan:
        now = self.now_fn()
        old = self.store.load_plan()
        ran = {r.person for r in results if not r.error}
        openings: List[Opening] = []
        if old is not None:
            openings += [o for o in old.openings if o.person not in ran and o.start > now]
        for r in results:
            if not r.error:
                openings += openings_from_cards(r.person, r.cards, now, self.cfg.horizon_days)
        full_run = {p.key for p in persons} == {p.key for p in self.cfg.people} and len(ran) == len(self.cfg.people)
        if full_run:
            created = now
        elif old is not None:
            created = old.created_at
        else:
            created = now - timedelta(hours=self.cfg.replan_after_hours + 1)   # still stale on purpose
        openings.sort(key=lambda o: (o.opens_at, o.person, o.card_key))
        return Plan(created, tuple(openings))

    # -- a whole run ----------------------------------------------------------------------
    def _restore_home(self) -> str:
        """Leave the app on the first person (Alex). Returns a problem text, or an empty string."""
        try:
            self.ensure_person(self.cfg.home_person)
            return ""
        except UserActive:
            return ""                  # the app stays on that profile: every run selects the profile it needs first
        except Exception as exc:
            return f"could not restore the profile: {exc}"

    def run(self, persons: Sequence[Person]) -> RunResult:
        self._started = self.now_fn()
        self._logged_out = ""
        self._popup_open = False
        results: List[PersonResult] = []
        restore_error = ""
        stood_back = False
        try:
            if self.attention is not None:
                self.attention.checkpoint()       # before the club app is brought to the front
            if self.club_app is not None:
                try:
                    self.club_app.activate()       # the first click on a window that is not in front is lost
                except Exception as exc:
                    self.store.log("activate_failed", error=f"{type(exc).__name__}: {exc}")
            for person in persons:
                if self._logged_out:
                    results.append(PersonResult(person.key, error=self._logged_out))
                    continue
                result = self.run_person(person)
                if not result.error and not self.dry_run:      # a dry run changes no decision state
                    try:
                        self._bookkeeping(person, result)
                    except Exception as exc:
                        result.error = f"bookkeeping failed: {type(exc).__name__}: {exc}"
                results.append(result)
                if result.error == USER_ACTIVE:
                    raise UserActive("the user came back")       # the people not reached are marked below
            self._read_all_booked(persons, results)
            for r in results:
                self._reconcile(r)
        except UserActive:
            # the user came back: stop at once. What was booked before stays (it is in the book); the people not finished
            # have no result and are marked, so no failure is counted and no message is sent
            stood_back = True
            done = {r.person for r in results}
            results += [PersonResult(p.key, error=USER_ACTIVE) for p in persons if p.key not in done]
            self.store.log("stood_back", run_id=self.run_id, persons=[r.person for r in results if r.error == USER_ACTIVE])
        finally:
            if not self._logged_out:       # on the login screen there is nothing to restore
                restore_error = self._restore_home()          # also when the run is interrupted
        if restore_error and results and results[-1].error != WINDOW_HIDDEN:
            results[-1].error = (results[-1].error + "; " if results[-1].error else "") + restore_error
        if stood_back and all(r.error == USER_ACTIVE for r in results):
            return RunResult(self.run_id, results, None, [])     # nothing was read to the end: no plan, no report
        plan = self._merge_plan(persons, results)
        if not self.dry_run:
            self.store.save_plan(plan)
        warnings = list(getattr(self.vision, "warnings", []))
        if warnings:
            self.store.log("vision_warnings", run_id=self.run_id, warnings=warnings)
            self.vision.warnings.clear()
        self.store.log("run", run_id=self.run_id, dry_run=self.dry_run, usage=dict(getattr(self.vision, "usage", {})), people=[
            {"person": r.person, "error": r.error, "seen": len(r.cards),
             "outcomes": [{"name": o.name, "start": o.start.isoformat(), "status": o.status, "calendar": o.calendar}
                          for o in r.outcomes]} for r in results])
        return RunResult(self.run_id, results, plan, warnings)


def format_report(cfg: Config, result: RunResult, problems_only: bool = False) -> Optional[Tuple[str, str]]:
    """(title, body), or None when there is nothing to say. The full report (every booking) is for the console and the log.
    With problems_only it is what the PHONE gets: only a class that was NOT booked and needs the user (the user's rule of
    2026-10-15: a message only when a decision or an action is needed, never a confirmation)."""
    names = {p.key: p.first_name for p in cfg.people}
    lines: List[str] = []
    booked = problems = would = 0
    errors: Dict[str, List[str]] = {}          # the same problem for several people is one line
    for r in result.people:
        for o in r.outcomes:
            when = f"{o.start:%a %d/%m %H:%M}"
            who = names.get(o.person, o.person)
            if o.status == "booked":
                booked += 1
                if not problems_only:        # a confirmation (and a calendar problem) is for the console and the log
                    notes = [o.detail] if o.detail else []
                    if o.calendar not in ("ok", "deferred"):
                        problems += 1
                        notes.append(f"calendar problem: {o.calendar}")
                    note = f" ({'; '.join(notes)})" if notes else ""
                    lines.append(f"Booked: {who} - {o.name}, {when}{note}")
            elif o.status == "waitlisted":
                continue           # no question and no message (the user's decision of 2026-10-14): it is in the log
            elif o.status == "would_book":
                would += 1
                if not problems_only:
                    lines.append(f"Would book: {who} - {o.name}, {when}")
            elif o.status == "refused":
                problems += 1
                lines.append(f"Refused: {who} - {o.name}, {when}: {o.detail}")
            else:
                problems += 1
                lines.append(f"Check: {who} - {o.name}, {when}: {o.detail}")
        if r.error and r.error != USER_ACTIVE and not any(o.detail and o.detail in r.error for o in r.outcomes):
            # stepping aside is no problem and no message; an error that a "Check" line above already says is not said twice
            errors.setdefault(r.error, []).append(names.get(r.person, r.person))
    for error, who in errors.items():
        problems += 1
        lines.append(f"Problem ({', '.join(who)}): {error}")
    # what the reader could not understand is in the log (vision_warnings), not on the phone: the user cannot act on it
    if not lines:
        return None
    parts = ([f"{booked} booked"] if booked and not problems_only else []) + \
            ([f"{would} would be booked"] if would and not problems_only else []) + \
            ([f"{problems} to check"] if problems else [])
    if not parts:
        return None
    return "Club booking: " + ", ".join(parts), "\n".join(lines)
