"""A small pretend club app for the runner tests.

FakeApp plays the screen tool (screenshot, tap, swipe) and keeps the state of the app.
FakeVision plays the reader: it tells what a screenshot shows, from the state at that moment.
"""
import io
import zlib
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from club_booking.config import Config, Person
from club_booking.models import CardState, ClassCard
from club_booking.screen import ScreenError
from club_booking.vision import DialogRead, ListRead

TZ = ZoneInfo("Asia/Jerusalem")
BLUE = (76, 159, 213)
LINK = (62, 150, 210)
CARD_TOP, CARD_STEP, BUTTON_DY = 250, 138, 60

PEOPLE = (
    Person("alex", "Alex", 176, "Alex", "", asks=True),
    Person("sam", "Sam", 214, "Kids", "Sam - "),
    Person("robin", "Robin", 253, "Kids", "Robin - "),
)


def make_cfg(tmp, **over):
    return Config(home=tmp, repo=tmp, **{"people": PEOPLE, "settle_seconds": 0.0, "app_restart_wait_seconds": 0.0,
                                                "app_start_wait_seconds": 0.0, **over})


def card(name, day, hh, mm, state, month=10):
    start = datetime(2026, month, day, hh, mm, tzinfo=TZ)
    return ClassCard(name, start, start + timedelta(minutes=45), state)


class FakeApp:
    """Options: scroll_overlap = cards shared by two screens after a swipe (0 hides cards between screens);
    sheet_close_failures = how many taps on the selector's X are ignored; favorites_tab_broken = a tap on the
    Favorites sub-tab does nothing, so the app stays on the Schedule list; button_x = where the register
    button is drawn; before_button_tap = a function called just before a register tap books a class;
    favorites_tab_lost = how many taps on the Favorites sub-tab are lost (the first click of an inactive window);
    mode = ok | stuck (a spinner, taps do nothing) | login (the phone login screen)."""

    def __init__(self, cfg: Config, favorites: Dict[str, List[ClassCard]], page_size: int = 4,
                 sheet_autoclose: bool = True, button_x: int = 512, scroll_overlap: int = 1,
                 sheet_close_failures: int = 0, favorites_tab_broken: bool = False, favorites_tab_lost: int = 0,
                 mode: str = "ok", refuse=(), booked_extra=None, booked_tab_lost: int = 0,
                 cancel_ignored: bool = False, overlap_rule: bool = False) -> None:
        self.cfg = cfg
        self.overlap_rule = overlap_rule   # the club refuses a class that overlaps one the person is registered for
        self.refusal_message = "הגיל אינו מתאים לשיעור"
        self.refuse = set(refuse)        # class names the club refuses to register
        self.dialog_refused = False
        self.dialog_waitlisted = False
        self.dialog_cancelled = False
        self.waitlist_taps = []
        self.cancel_presses = []           # (person key, card key, button text) of every accessibility press on a card
        self.cancel_ignored = cancel_ignored   # the press does nothing (the class stays booked)
        self.mode = mode
        self.favorites_tab_lost = favorites_tab_lost
        self.booked_extra = {k: list(v) for k, v in (booked_extra or {}).items()}   # booked, but not a favorite
        self.booked_tab_lost = booked_tab_lost         # taps on the booked sub-tab that are lost
        self.reconnects = 0
        self.cards = {k: list(v) for k, v in favorites.items()}
        self.page_size = page_size
        self.sheet_autoclose = sheet_autoclose
        self.button_x = button_x
        self.scroll_overlap = scroll_overlap
        self.sheet_close_failures = sheet_close_failures
        self.favorites_tab_broken = favorites_tab_broken
        self.before_button_tap = None
        self.profile = 0
        self.sheet_open = False
        self.subtab = "favorites"
        self.page = 0
        self.dialog = None
        self.counter = 0
        self.snapshots = {}
        self.taps = []
        self.swipes = 0
        self.booked_swipes = 0          # swipes made while the booked tab was open
        self.events = []                # order of the things that matter: 'book', 'booked_tab'
        self.button_taps = []   # (person key, card key)
        self.refused_taps = []

    def person_key(self):
        return self.cfg.people[self.profile].key

    @property
    def step(self):
        return self.page_size - self.scroll_overlap

    def listed(self):
        """What the current sub-tab lists: the favorites, or (booked tab) every class the profile is registered for."""
        key = self.person_key()
        if self.subtab == "booked":
            mine = [c for c in self.cards[key] if c.state in (CardState.REGISTERED, CardState.WAITLISTED)]
            return mine + self.booked_extra.get(key, [])
        return self.cards[key]

    def visible(self):
        start = self.page * self.step
        return self.listed()[start:start + self.page_size]

    def jump(self):
        """The list moves by itself (the app does this sometimes)."""
        more = (self.page + 1) * self.step < len(self.cards[self.person_key()]) - self.scroll_overlap
        self.page = self.page + 1 if more else 0

    # -- the screen tool -------------------------------------------------------------
    def reconnect(self):
        self.reconnects += 1

    def region_png(self):
        """The window region with pop-up menus: here the same picture as a screenshot."""
        return self.screenshot()

    window_error = None      # text: while set, every tap fails like the screen tool does when the window has the wrong size

    def tap(self, x, y):
        self.taps.append((x, y))
        if self.window_error:
            raise ScreenError(self.window_error)
        if self.mode != "ok":
            return "tapped"
        c = self.cfg.coords
        rows = [p.selector_row_y for p in self.cfg.people]
        if self.dialog is not None:
            if (x, y) == c.dialog_ok:
                self.dialog = None
            return "tapped"
        if self.sheet_open:
            if (x, y) == c.selector_close:
                if self.sheet_close_failures > 0:
                    self.sheet_close_failures -= 1
                else:
                    self.sheet_open = False
            elif x == c.selector_row_x and y in rows:
                self.profile = rows.index(y)
                self.page = 0
                if self.sheet_autoclose:
                    self.sheet_open = False
            return "tapped"
        if (x, y) == c.selector_open:
            self.sheet_open = True
        elif (x, y) == c.subtab_schedule:
            self.subtab = "schedule"
            self.page = 0
        elif (x, y) == c.subtab_booked:
            self.events.append("booked_tab")
            if self.booked_tab_lost > 0:
                self.booked_tab_lost -= 1
            else:
                self.subtab = "booked"
                self.page = 0
        elif (x, y) == c.subtab_favorites:
            if self.favorites_tab_lost > 0:
                self.favorites_tab_lost -= 1
            elif not self.favorites_tab_broken:
                self.subtab = "favorites"
                self.page = 0
        else:
            for i, card_ in enumerate(self.visible()):
                cy = CARD_TOP + CARD_STEP * i + BUTTON_DY
                if abs(x - self.button_x) <= 12 and abs(y - cy) <= 12 and card_.state == CardState.OPEN \
                        and card_.name in self.refuse:
                    self.dialog, self.dialog_refused = card_, True          # a popup says no; nothing is booked
                    self.refusal_message = "הגיל אינו מתאים לשיעור"
                    self.refused_taps.append((self.person_key(), card_.key))
                elif abs(x - self.button_x) <= 12 and abs(y - cy) <= 12 and card_.state == CardState.OPEN \
                        and self.overlap_rule and self._overlaps_a_registered_class(card_):
                    self.dialog, self.dialog_refused = card_, True          # "two overlapping classes": nothing is booked
                    self.refusal_message = "לא ניתן להירשם לשני שיעורים חופפים"
                    self.refused_taps.append((self.person_key(), card_.key))
                elif abs(x - self.button_x) <= 12 and abs(y - cy) <= 12 and card_.state == CardState.FULL:
                    key = self.person_key()                     # "הרשמה להמתנה": joins the waitlist
                    cards = self.cards[key]
                    cards[cards.index(card_)] = replace(card_, state=CardState.WAITLISTED)
                    self.dialog, self.dialog_waitlisted = card_, True
                    self.waitlist_taps.append((key, card_.key))
                elif abs(x - self.button_x) <= 12 and abs(y - cy) <= 12 and card_.state == CardState.OPEN:
                    key = self.person_key()
                    if self.before_button_tap is not None:
                        self.before_button_tap(key, card_)
                    cards = self.cards[key]
                    cards[cards.index(card_)] = replace(card_, state=CardState.REGISTERED)
                    self.dialog = card_
                    self.events.append("book")
                    self.button_taps.append((key, card_.key))
        return "tapped"

    def _overlaps_a_registered_class(self, card_):
        key = self.person_key()
        held = [c for c in [*self.cards[key], *self.booked_extra.get(key, [])] if c.state == CardState.REGISTERED]
        return any(c.start < card_.end and card_.start < c.end for c in held)

    def press_card_action(self, card_key, desc):
        """What an accessibility press on 'ביטול' or 'הסרה מהמתנה' inside one card of the booked tab does."""
        from club_booking.ax import AxError
        if self.subtab != "booked":
            raise AxError(f"no button {desc!r} on this screen")
        for i, card_ in enumerate(self.visible()):
            wants = {CardState.REGISTERED: "ביטול", CardState.WAITLISTED: "הסרה מהמתנה"}.get(card_.state)
            if card_.key == card_key and wants == desc:
                key = self.person_key()
                self.cancel_presses.append((key, card_.key, desc))
                self.events.append("cancel")
                if not self.cancel_ignored:
                    cards = self.cards[key]
                    back = CardState.OPEN if card_.state == CardState.REGISTERED else CardState.FULL
                    if card_ in cards:
                        cards[cards.index(card_)] = replace(card_, state=back)
                    else:
                        self.booked_extra[key].remove(card_)
                self.dialog, self.dialog_cancelled = card_, True
                return
        raise AxError(f"no button {desc!r} in the card {card_key}")

    def swipe(self, *args, **kwargs):
        self.swipes += 1
        if self.subtab == "booked":
            self.booked_swipes += 1
        self.scroll_page()
        return "swiped"

    def scroll_page(self):
        if (self.page + 1) * self.step < len(self.listed()) - self.scroll_overlap:
            self.page += 1

    def screenshot(self) -> bytes:
        self.counter += 1
        img = Image.new("RGB", (1024, 800), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 3, 3], fill=(self.counter % 256, (self.counter // 256) % 256, 7))
        if self.mode != "ok":
            draw.ellipse([490, 150, 530, 190], outline=(100, 170, 220), width=4)       # a spinner
            snap = (self.mode,)
        elif self.sheet_open:
            for i, p in enumerate(self.cfg.people):
                draw.rectangle([8, p.selector_row_y - 15, 1016, p.selector_row_y + 15],
                               fill=BLUE if i == self.profile else (255, 255, 255))
            snap = ("selector",)
        elif self.dialog is not None:
            draw.rectangle([450, 740, 700, 775], fill=(30, 30, 30))          # the dialog's buttons
            snap = ("dialog", self.dialog)
        else:
            shown = self.visible()
            draw.rectangle([0, 770, 1024, 800], fill=(230, 230, 230))       # the bottom tab bar
            ux, uy = {"favorites": self.cfg.coords.favorites_underline, "booked": self.cfg.coords.booked_underline,
                      "schedule": self.cfg.coords.schedule_underline}[self.subtab]
            draw.rectangle([ux - 40, uy - 1, ux + 40, uy + 1], fill=(90, 164, 215))      # the selected sub-tab
            for i, card_ in enumerate(shown):
                cy = CARD_TOP + CARD_STEP * i + BUTTON_DY
                width = 40 + zlib.crc32(card_.key.encode("utf-8")) % 400
                draw.rectangle([100, cy - 50, 100 + width, cy - 40], fill=(40, 40, 40))   # the title: differs per card
                if card_.state in (CardState.OPEN, CardState.FULL, CardState.NOT_OPEN):
                    bx = self.button_x
                    draw.rectangle([bx - 20, cy - 6, bx + 19, cy + 6], fill=LINK)
            snap = ("list", list(shown), self.subtab)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
        self.snapshots[png] = snap
        return png


class FakeVision:
    """fail_for = person key for which every list read fails (with fail_with, a VisionError by default);
    jump_after_reads = read numbers after which the app list moves by itself;
    dialog_kind / dialog_title / dialog_start = what the reader says about a confirmation dialog."""

    def __init__(self, app: FakeApp, jitter=(9, -7), fail_for=None, dialog_kind=None) -> None:
        self.app = app
        self.jitter = jitter
        self.fail_for = fail_for
        self.fail_with = None
        self.dialog_kind = dialog_kind
        self.dialog_title = None
        self.dialog_start = None
        self.read_dialog_error = None
        self.jump_after_reads = set()
        self.warnings = []
        self.list_reads = 0
        self.read_kinds = []       # the kind of every screenshot that was sent to the reader

    def read_list(self, png):
        from club_booking.vision import VisionError
        self.list_reads += 1
        snap = self.app.snapshots[png]
        self.read_kinds.append(snap[0])
        if self.fail_for == self.app.person_key():
            raise self.fail_with or VisionError("fake API failure")
        if snap[0] == "selector":
            result = ListRead("selector", [])
        elif snap[0] == "stuck":
            result = ListRead("other", [])
        elif snap[0] in ("login", "update", "code", "locked"):
            result = ListRead(snap[0], [])
        elif snap[0] == "dialog":
            result = ListRead("other", [])
        else:
            _kind, cards, subtab = snap
            out = []
            for i, c in enumerate(cards):
                button = None
                if c.state in (CardState.OPEN, CardState.FULL, CardState.NOT_OPEN) or \
                        (subtab == "booked" and c.state in (CardState.REGISTERED, CardState.WAITLISTED)):
                    button = (self.app.button_x + self.jitter[0], CARD_TOP + CARD_STEP * i + BUTTON_DY + self.jitter[1])
                out.append(replace(c, button=button))
            result = ListRead(subtab, out)
        if self.list_reads in self.jump_after_reads:
            self.app.jump()
        return result

    def read_dialog(self, png):
        if self.read_dialog_error is not None:
            raise self.read_dialog_error
        snap = self.app.snapshots[png]
        if snap[0] == "dialog" and self.app.dialog_waitlisted:
            c = snap[1]
            return DialogRead("waitlisted", c.name, f"{c.start:%d/%m}", f"{c.start:%H:%M}")
        if snap[0] == "dialog" and self.app.dialog_refused:
            c = snap[1]
            return DialogRead("refused", c.name, f"{c.start:%d/%m}", f"{c.start:%H:%M}", self.app.refusal_message)
        if snap[0] == "dialog":
            c = snap[1]
            return DialogRead(self.dialog_kind or "booked", self.dialog_title or c.name, f"{c.start:%d/%m}",
                              self.dialog_start or f"{c.start:%H:%M}")
        return DialogRead("list", "", "", "")


class FakeAxVision(FakeVision):
    """A reader that can scroll the list by page, like the accessibility reader."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_scrolls = 0
        self.popups_closed = 0
        self.scroll_fails = False
        self.scroll_error = None   # an exception to raise instead (for example AxError("no scroll area"))
        self.crowd = {}            # card key -> how many people registered (what a class page shows)
        self.opened, self.details_open, self.pages_fail, self.backs = [], None, False, 0
        self.popup_override = None     # set to (kind, text) to play a message the program has never seen

    def open_card(self, card):
        if self.pages_fail:
            raise RuntimeError("the class page did not open")
        if card.key not in [c.key for c in self.app.visible()]:       # like the real list: only what is on the screen
            raise RuntimeError("the class card is not on the screen")
        self.opened.append(card.key)
        self.details_open = card

    def read_attendees(self):
        return self.crowd.get(self.details_open.key) if self.details_open else None

    def back(self):
        self.backs += 1
        self.details_open = None

    def scroll_page(self):
        self.page_scrolls += 1
        if self.scroll_error is not None:
            raise self.scroll_error
        if self.scroll_fails:
            raise RuntimeError("accessibility is not allowed")
        self.app.scroll_page()

    def close_popup(self):
        self.popups_closed += 1
        self.app.dialog, self.app.dialog_refused, self.app.dialog_waitlisted = None, False, False
        self.app.dialog_cancelled = False

    def press_card_action(self, card, desc):
        self.app.press_card_action(card.key, desc)

    def read_popup(self):
        """What a message on the screen says: ("success", text), ("unknown", text) or ("none", "")."""
        if self.popup_override is not None:
            return self.popup_override
        if self.app.dialog_cancelled:
            return ("success", "ההרשמה בוטלה בהצלחה!")
        return ("none", "")


class FakeClubApp:
    """Plays the club app's launcher. fixes: a restart cures a stuck app (it never cures the login screen)."""

    def __init__(self, app, fixes=True, control_problem="", launch_on_activate=False):
        self.app, self.fixes, self.restarts, self.activations = app, fixes, 0, 0
        self.control_problem = control_problem
        self.launch_on_activate = launch_on_activate     # activate() had to start the app or reopen its window
        self.quits, self.quit_result, self.quit_error, self.quit_log = 0, True, None, []
        self.fits, self.order = 0, []
        self.fit_cures, self.restart_cures = True, True    # what cures a window of the wrong size

    def can_control(self):
        return self.control_problem

    def activate(self):
        self.activations += 1
        self.order.append("activate")
        return self.launch_on_activate

    def fit(self):
        self.fits += 1
        self.order.append("fit")
        if self.fit_cures:
            self.app.window_error = None
        return True

    def describe_windows(self):
        return "2 windows: 1024x800 515x800"

    def quit(self, force=False):
        if self.quit_error:
            raise self.quit_error
        self.quits += 1
        self.quit_log.append(len(self.app.button_taps))      # how many bookings were made before the quit
        return self.quit_result

    def restart(self):
        self.restarts += 1
        self.order.append("restart")
        if self.restart_cures:
            self.app.window_error = None
        if self.fixes and self.app.mode == "stuck":
            self.app.mode = "ok"
            self.app.sheet_open, self.app.dialog, self.app.subtab, self.app.page = False, None, "favorites", 0


class FakeCalendar:
    """closes_dialog: the calendar step leaves the app without its confirmation dialog (needs app)."""

    def __init__(self, fail_for=None, app=None, closes_dialog=False):
        self.added = []
        self.fail_for = fail_for
        self.app = app
        self.closes_dialog = closes_dialog

    def add(self, person, card_):
        from club_booking.calendar_app import CalendarError
        if self.fail_for == person.key:
            raise CalendarError("fake calendar failure")
        self.added.append((person.key, card_.key))
        if self.closes_dialog and self.app is not None:
            self.app.dialog = None


def sample_favorites():
    OPEN, FULL, NOT_OPEN, WAIT = CardState.OPEN, CardState.FULL, CardState.NOT_OPEN, CardState.WAITLISTED
    return {
        "alex": [card("METCON", 13, 8, 6, WAIT), card("METCON", 13, 8, 50, OPEN),
                   card("PowerGym", 14, 6, 0, NOT_OPEN), card("Metcon", 15, 17, 30, NOT_OPEN)],
        "sam": [card("Robotics", 13, 17, 15, OPEN), card("HipHop", 13, 17, 30, OPEN),
                 card("Robotics", 14, 17, 15, NOT_OPEN)],
        "robin": [card("Spinning", 13, 17, 0, FULL), card("Judo", 16, 10, 0, NOT_OPEN)],
    }
