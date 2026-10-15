import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from club_booking.ax import AxDump, AxElement, AxError, parse_dump
from club_booking.ax_reader import AxReader, attendee_count, list_from_dump, screen_kind
from club_booking.models import CardState
from club_booking.vision import DialogRead, ListRead

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=TZ)
FIX = Path(__file__).parent / "fixtures"


def load(name):
    return parse_dump((FIX / name).read_text(encoding="utf-8"))


def dump_of(*texts):
    window = AxElement("AXWindow", "x", "", 0, 0, 1024, 800)
    return AxDump(window, [AxElement("AXStaticText", t, "", 0, 0, 10, 10) for t in texts])


class SelectorRowsTests(unittest.TestCase):
    """The setup wizard reads the profile rows of the person selector, to find their positions by itself."""

    def dump(self, *elements):
        from club_booking.ax import AxDump, AxElement
        return AxDump(AxElement("AXWindow", "", "", 0, 0, 1024, 800),
                      [AxElement(r, d, "", 100.0, y, 600.0, h) for r, d, y, h in elements])

    def test_the_rows_after_the_heading_are_given_with_their_middle_line(self):
        from club_booking.ax_reader import selector_rows
        dump = self.dump(("AXStaticText", "בחירת מועדון\nאפשר לעדכן את המועדון בכל שלב", 60, 30),
                         ("AXButton", "מועדון - דנה כהן", 160, 32), ("AXButton", "מועדון - בן כהן", 198, 32),
                         ("AXButton", "סגור", 700, 30))
        self.assertEqual(selector_rows(dump), [("מועדון - דנה כהן", 176), ("מועדון - בן כהן", 214)])

    def test_a_screen_that_is_not_the_selector_has_no_rows(self):
        from club_booking.ax_reader import selector_rows
        self.assertEqual(selector_rows(self.dump(("AXButton", "מועדון - דנה כהן", 160, 32))), [])


class ScreenKindTests(unittest.TestCase):
    def test_the_real_screens(self):
        self.assertEqual(screen_kind(load("ax_favorites_top.json")), "favorites")
        self.assertEqual(screen_kind(load("ax_login.json")), "login")
        self.assertEqual(screen_kind(load("ax_login_locked.json")), "locked")

    def test_the_other_screens(self):
        self.assertEqual(screen_kind(dump_of("אימות כניסה", "הקוד שקיבלת")), "code")
        self.assertEqual(screen_kind(dump_of("התחדשנו", "גרסה חדשה של האפליקציה זמינה להורדה.\nאנא עדכנו גרסה על מנת להמשיך.")), "update")
        self.assertEqual(screen_kind(dump_of("בחירת מועדון", "אפשר לעדכן את המועדון בכל שלב")), "selector")
        self.assertEqual(screen_kind(dump_of("something else")), "other")

    def test_an_update_popup_wins_over_the_screen_behind_it(self):
        behind = dump_of("כניסה עם טלפון", "גרסה חדשה של האפליקציה זמינה להורדה.")
        self.assertEqual(screen_kind(behind), "update")


class ListFromDumpTests(unittest.TestCase):
    def cards(self, name):
        return list_from_dump(load(name), NOW, TZ)

    def test_the_top_of_the_favorites_list(self):
        cards = self.cards("ax_favorites_top.json")
        by_start = {(c.start.day, c.start.hour, c.start.minute): c for c in cards}
        registered = by_start[(13, 8, 6)]
        self.assertEqual((registered.name, registered.state, registered.button), ("METCON", CardState.REGISTERED, None))
        self.assertEqual(registered.end, datetime(2026, 10, 13, 8, 51, tzinfo=TZ))
        closed = by_start[(13, 8, 50)]
        self.assertEqual(closed.state, CardState.UNKNOWN, "'סגור להרשמה' is not a state the program acts on")
        open_card = by_start[(13, 17, 30)]
        self.assertEqual((open_card.name, open_card.state, open_card.button), ("Metcon", CardState.OPEN, (512, 534)))
        later = by_start[(16, 7, 45)]
        self.assertEqual((later.state, later.button), (CardState.NOT_OPEN, (512, 699)))
        self.assertEqual(later.opens_shown, datetime(2026, 10, 15, 7, 45, tzinfo=TZ))
        self.assertEqual(later.opens_at, datetime(2026, 10, 15, 7, 45, tzinfo=TZ))

    def test_a_card_cut_off_by_the_edge_has_its_text_but_no_button(self):
        cards = self.cards("ax_favorites_top.json")
        cut = [c for c in cards if (c.start.day, c.start.hour, c.start.minute) == (16, 12, 50)][0]
        self.assertEqual(cut.state, CardState.NOT_OPEN)
        self.assertIsNone(cut.button)

    def test_an_open_card_whose_button_is_off_the_screen_has_no_button(self):
        cards = self.cards("ax_favorites_page1.json")
        scrolled_away = [c for c in cards if (c.start.day, c.start.hour, c.start.minute) == (13, 17, 30)][0]
        self.assertEqual(scrolled_away.state, CardState.OPEN)
        self.assertIsNone(scrolled_away.button)

    def test_two_pages_share_cards(self):
        top = {c.key for c in self.cards("ax_favorites_top.json")}
        page = {c.key for c in self.cards("ax_favorites_page1.json")}
        self.assertTrue(top & page, "page scroll keeps an overlap")

    def test_no_cards_on_another_screen(self):
        self.assertEqual(list_from_dump(load("ax_login.json"), NOW, TZ), [])


class FakeAx:
    def __init__(self, dumps=None, raises=None):
        self.dumps, self.raises, self.scrolls, self.presses = list(dumps or []), raises, [], []
        self.card_presses = []

    def dump(self):
        if self.raises:
            raise self.raises
        return self.dumps.pop(0) if len(self.dumps) > 1 else self.dumps[0]

    def scroll(self, direction):
        self.scrolls.append(direction)

    def press(self, role, desc, y, tolerance=25):
        self.presses.append((role, desc, y))

    def press_in_card(self, card_desc, button_desc):
        self.card_presses.append((card_desc, button_desc))


class FakeFallback:
    def __init__(self):
        self.warnings, self.usage, self.read_lists, self.read_dialogs = [], {"calls": 0}, [], []

    def read_list(self, png):
        self.read_lists.append(png)
        return ListRead("favorites", [])

    def read_dialog(self, png):
        self.read_dialogs.append(png)
        return DialogRead("booked", "METCON", "13/10", "17:30")


class AxReaderTests(unittest.TestCase):
    def make(self, ax):
        self.fallback = FakeFallback()
        return AxReader(ax, self.fallback, TZ, now_fn=lambda: NOW)

    def test_the_list_comes_from_accessibility_and_the_picture_is_not_used(self):
        reader = self.make(FakeAx([load("ax_favorites_top.json")]))
        got = reader.read_list(b"picture")
        self.assertEqual(got.screen, "favorites")
        self.assertEqual(len(got.cards), 6)
        self.assertEqual(self.fallback.read_lists, [])

    def test_a_screen_that_is_not_a_list_is_named(self):
        got = self.make(FakeAx([load("ax_login.json")])).read_list(b"picture")
        self.assertEqual((got.screen, got.cards), ("login", []))

    def test_when_accessibility_fails_the_picture_reader_takes_over(self):
        reader = self.make(FakeAx(raises=AxError("no permission")))
        got = reader.read_list(b"picture")
        self.assertEqual(self.fallback.read_lists, [b"picture"])
        self.assertEqual(got.screen, "favorites")
        self.assertTrue(any("accessibility" in w for w in reader.warnings))

    def test_the_dialog_is_read_by_the_picture_reader(self):
        reader = self.make(FakeAx([load("ax_login.json")]))
        self.assertEqual(reader.read_dialog(b"picture").kind, "booked")
        self.assertEqual(self.fallback.read_dialogs, [b"picture"])

    def test_a_popup_is_closed_by_pressing_its_close_text(self):
        popup = AxDump(AxElement("AXWindow", "x", "", 0, 0, 1024, 800), [
            AxElement("AXStaticText", "הגיל אינו מתאים לשיעור", "", 400, 700, 200, 20),
            AxElement("AXStaticText", "סגור", "", 495, 736, 33, 32)])
        ax = FakeAx([popup])
        self.make(ax).close_popup()
        self.assertEqual(ax.presses, [("AXStaticText", "סגור", 752)])

    def test_the_real_waitlist_message_is_closed_by_its_ok_button(self):
        # seen on the real app, 2026-10-14 11:23: a bottom sheet "נכנסת להמתנה" with the class and one button "מצויין, תודה"
        sheet = AxDump(AxElement("AXWindow", "x", "", 0, 0, 1024, 800), [
            AxElement("AXStaticText", "נכנסת להמתנה", "", 400, 600, 224, 40),
            AxElement("AXStaticText", "משקולות וכח", "", 450, 650, 120, 20),
            AxElement("AXButton", "מצויין, תודה", "", 462, 740, 100, 32)])
        ax = FakeAx([sheet])
        self.make(ax).close_popup()
        self.assertEqual(ax.presses, [("AXButton", "מצויין, תודה", 756)])
        self.assertEqual(self.make(FakeAx([sheet])).read_popup()[0], "unknown",
                         "a cancel never confirms it: only a success message is closed after a cancel")

    def test_no_popup_to_close_is_an_error(self):
        with self.assertRaises(AxError):
            self.make(FakeAx([load("ax_favorites_top.json")])).close_popup()

    def test_scroll_page_goes_down_one_page(self):
        ax = FakeAx([load("ax_login.json")])
        self.make(ax).scroll_page()
        self.assertEqual(ax.scrolls, ["down"])

    def test_warnings_and_usage_are_the_fallbacks_own_objects(self):
        reader = self.make(FakeAx([load("ax_login.json")]))
        self.assertIs(reader.warnings, self.fallback.warnings)
        self.assertIs(reader.usage, self.fallback.usage)


class BookedTabAndDetailsTests(unittest.TestCase):
    """Real dumps of 2026-10-13: the booked sub-tab (every class the profile is registered for) and a class page."""

    def make(self, ax):
        return AxReader(ax, FakeFallback(), TZ, now_fn=lambda: NOW)

    def test_the_booked_tab_and_the_details_page_have_their_own_names(self):
        self.assertEqual(screen_kind(load("ax_booked.json")), "booked")
        self.assertEqual(screen_kind(load("ax_details.json")), "details")
        self.assertEqual(screen_kind(load("ax_favorites_top.json")), "favorites")

    def test_a_booked_card_is_registered_and_its_main_button_is_cancel(self):
        cards = list_from_dump(load("ax_booked.json"), NOW, TZ)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card.name, card.state), ("Functional Training", CardState.REGISTERED))
        self.assertEqual((card.start, card.end), (datetime(2026, 10, 14, 9, 50, tzinfo=TZ), datetime(2026, 10, 14, 10, 30, tzinfo=TZ)))
        self.assertEqual(card.button, (46, 239), "the center of the 'ביטול' button (x 15, y 221, 62 x 37)")

    def test_the_attendees_of_a_class_are_counted_from_the_details_page(self):
        self.assertEqual(attendee_count(load("ax_details.json")), 5)

    def test_a_page_with_no_names_counts_zero(self):
        self.assertEqual(attendee_count(dump_of("תיאור השיעור", "משך השיעור: 40 דקות", "נרשמת")), 0)

    def test_the_reader_gives_the_booked_cards_the_attendees_and_goes_back(self):
        ax = FakeAx([load("ax_booked.json"), load("ax_details.json"), load("ax_details.json")])
        reader = self.make(ax)
        listing = reader.read_list(b"x")
        self.assertEqual((listing.screen, len(listing.cards)), ("booked", 1))
        self.assertEqual(reader.read_attendees(), 5)
        reader.back()
        self.assertEqual(ax.presses[-1][:2], ("AXButton", "חזרה לעמוד קודם"))

    def test_a_card_on_the_screen_is_opened_by_its_own_text(self):
        dump = load("ax_favorites_top.json")
        cards = list_from_dump(dump, NOW, TZ)
        ax = FakeAx([dump])
        wanted = next(c for c in cards if c.name == "Metcon")                  # 13/10 17:30
        self.make(ax).open_card(wanted)
        role, desc, y = ax.presses[0]
        self.assertEqual(role, "AXButton")
        self.assertIn("שעה: 17:30", desc)
        self.assertIn("Metcon", desc)

    def test_a_card_that_is_not_on_the_screen_cannot_be_opened(self):
        from dataclasses import replace
        cards = list_from_dump(load("ax_favorites_top.json"), NOW, TZ)
        elsewhere = replace(cards[0], start=datetime(2026, 11, 1, 6, 0, tzinfo=TZ))
        with self.assertRaises(AxError):
            self.make(FakeAx([load("ax_favorites_top.json")])).open_card(elsewhere)

    def test_a_card_button_is_pressed_inside_that_card_only(self):
        dump = load("ax_booked.json")
        card = list_from_dump(dump, NOW, TZ)[0]
        ax = FakeAx([dump])
        self.make(ax).press_card_action(card, "ביטול")
        [(card_desc, button_desc)] = ax.card_presses
        self.assertEqual(button_desc, "ביטול")
        self.assertIn("Functional Training", card_desc)
        self.assertIn("שעה: 09:50", card_desc)
        self.assertEqual(ax.presses, [], "no press by position")

    def test_a_card_that_is_not_on_the_screen_is_not_pressed(self):
        from dataclasses import replace
        dump = load("ax_booked.json")
        gone = replace(list_from_dump(dump, NOW, TZ)[0], start=datetime(2026, 11, 1, 6, 0, tzinfo=TZ))
        ax = FakeAx([dump])
        with self.assertRaises(AxError):
            self.make(ax).press_card_action(gone, "ביטול")
        self.assertEqual(ax.card_presses, [])

    def test_a_class_that_is_on_the_screen_twice_is_not_pressed(self):
        dump = load("ax_booked.json")
        card = list_from_dump(dump, NOW, TZ)[0]
        twice = AxDump(dump.window, list(dump.elements) + [e for e in dump.elements if "שעה:" in e.desc])
        ax = FakeAx([twice])
        with self.assertRaises(AxError):
            self.make(ax).press_card_action(card, "ביטול")
        self.assertEqual(ax.card_presses, [])

    def test_a_message_on_the_screen_is_read_before_anything_is_pressed(self):
        done = dump_of("ההרשמה בוטלה בהצלחה!", "סגור")
        self.assertEqual(self.make(FakeAx([done])).read_popup()[0], "success")
        fee = dump_of("ביטול בשעה זו כרוך בחיוב. להמשיך?", "אישור", "ביטול")
        self.assertEqual(self.make(FakeAx([fee])).read_popup()[0], "unknown")
        self.assertEqual(self.make(FakeAx([load("ax_booked.json")])).read_popup(), ("none", ""))

    def test_attendees_are_unknown_when_accessibility_fails(self):
        self.assertIsNone(self.make(FakeAx(raises=AxError("no permission"))).read_attendees())


if __name__ == "__main__":
    unittest.main()
