import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from club_booking.ax import AxDump, AxElement
from club_booking.login import LoginBlocked, LoginError, LoginGuard, LoginHelper

TZ = ZoneInfo("Asia/Jerusalem")
T0 = datetime(2026, 10, 13, 11, 40, tzinfo=TZ)


def dump_of(*texts):
    window = AxElement("AXWindow", "x", "", 0, 0, 1024, 800)
    return AxDump(window, [AxElement("AXStaticText", t, "", 0, 0, 10, 10) for t in texts])


LOGIN = AxDump(AxElement("AXWindow", "x", "", 0, 0, 1024, 800), [
    AxElement("AXStaticText", "כניסה עם טלפון\nכרטיסייה 1 מתוך 1", "", 8, 186, 1009, 37),
    AxElement("AXButton", "מספר הטלפון שלך", "", 31, 246, 963, 37),
    AxElement("AXButton", "שלחו לי קוד", "", 8, 748, 1009, 37)])
CODE = dump_of("אימות כניסה", "הקוד שקיבלת", "קוד נשלח לטלפון 0500000000")
LOCKED = dump_of("כניסה עם טלפון", "ניסית 3 פעמים, מסך הכניסה ננעל למשך 10 דקות", "סגור")
LOGGED_IN = dump_of("שיעורים מועדפים")


class FakeAx:
    def __init__(self, *dumps):
        self.dumps, self.presses = list(dumps), []

    def dump(self):
        return self.dumps.pop(0) if len(self.dumps) > 1 else self.dumps[0]

    def press(self, role, desc, y, tolerance=25):
        self.presses.append((role, desc, round(y)))


class GuardTests(unittest.TestCase):
    def guard(self):
        return LoginGuard(Path(tempfile.mkdtemp()) / "login.json")

    def test_two_requests_in_ten_minutes_are_allowed_the_third_is_not(self):
        g = self.guard()
        self.assertIsNone(g.check(T0))
        g.record_request(T0)
        self.assertIsNone(g.check(T0 + timedelta(minutes=1)))
        g.record_request(T0 + timedelta(minutes=1))
        wait = g.check(T0 + timedelta(minutes=2))
        self.assertEqual(wait, timedelta(minutes=8), "one request stays free: the user may need it")

    def test_the_window_slides(self):
        g = self.guard()
        g.record_request(T0)
        g.record_request(T0 + timedelta(minutes=1))
        self.assertIsNone(g.check(T0 + timedelta(minutes=10, seconds=1)))

    def test_a_lock_blocks_until_it_ends(self):
        g = self.guard()
        g.record_lock(T0)
        self.assertEqual(g.check(T0 + timedelta(minutes=4)), timedelta(minutes=6))
        self.assertIsNone(g.check(T0 + timedelta(minutes=11)))

    def test_a_damaged_file_counts_as_empty(self):
        g = self.guard()
        g.path.write_text("not json", encoding="utf-8")
        self.assertIsNone(g.check(T0))


class HelperTests(unittest.TestCase):
    def make(self, ax, front="Runner", shows="0500000000"):
        self.guard = LoginGuard(Path(tempfile.mkdtemp()) / "login.json")
        self.typed, self.clock, self.events, self.slept = [], {"now": T0}, [], []

        def type_into(text, clear):
            self.events.append((text, clear))
            if front != "Runner":          # the real step types nothing when the club app is not in front
                return False
            self.typed.append(text)
            return True

        self.helper = LoginHelper(ax, self.guard, type_into=type_into, sleep=self.slept.append,
                                  now_fn=lambda: self.clock["now"], read_field=lambda: shows)
        return self.helper

    def sends(self, ax):
        return [p for p in ax.presses if p[1] == "שלחו לי קוד"]

    def test_request_code_fills_the_number_and_presses_send_once(self):
        ax = FakeAx(LOGIN, CODE)
        self.assertEqual(self.make(ax).request_code("0500000000"), "code_screen")
        self.assertEqual(self.typed, ["0500000000"])
        self.assertEqual(ax.presses[0], ("AXButton", "מספר הטלפון שלך", 264))
        self.assertEqual(len(self.sends(ax)), 1)
        self.assertEqual(len(self.guard.requests()), 1)

    def test_the_number_is_typed_with_a_clear_and_the_code_without(self):
        ax = FakeAx(LOGIN, CODE)
        self.make(ax).request_code("0500000000")
        self.assertEqual(self.events, [("0500000000", True)], "the field may hold old text: clear it")
        ax = FakeAx(CODE, LOGGED_IN)
        self.make(ax).enter_code("123456")
        self.assertEqual(self.events, [("123456", False)])

    def test_a_field_that_does_not_show_exactly_the_number_stops_before_send(self):
        # a leftover from an earlier try: the new number was appended to old digits and letters
        for shown in ("05000an050000000005000000000", "050000000", "", "0500000000 0500000000"):
            ax = FakeAx(LOGIN, CODE)
            helper = self.make(ax, shows=shown)
            with self.assertRaises(LoginError):
                helper.request_code("0500000000")
            self.assertEqual(self.sends(ax), [], shown)
            self.assertEqual(self.guard.requests(), [], "nothing sent, nothing counted")

    def test_a_field_that_is_still_being_drawn_is_read_again_before_the_number_is_refused(self):
        # 2026-10-15: the fast OCR answered before the last digit was drawn ('050111222'); a read 3 minutes later was right
        ax = FakeAx(LOGIN, CODE)
        helper = self.make(ax)
        reads = iter(["050111222", "0501112223"])
        helper.read_field = lambda: next(reads)
        self.assertEqual(helper.request_code("0501112223"), "code_screen")
        self.assertEqual(len(self.sends(ax)), 1)

    def test_a_field_that_never_shows_the_number_is_refused_after_a_few_reads(self):
        ax = FakeAx(LOGIN, CODE)
        helper = self.make(ax)
        count = []
        helper.read_field = lambda: (count.append(1), "050111222")[1]
        with self.assertRaises(LoginError):
            helper.request_code("0501112223")
        self.assertEqual((len(count), self.sends(ax), self.guard.requests()), (4, [], []))

    def test_a_field_that_cannot_be_read_stops_before_send_with_a_clear_error(self):
        # 2026-10-15: the OCR helper crashed (macOS 27.2 beta). Nothing may be sent, and the error must be a LoginError
        ax = FakeAx(LOGIN, CODE)
        helper = self.make(ax)

        def broken():
            raise RuntimeError("the OCR helper failed")
        helper.read_field = broken
        with self.assertRaises(LoginError) as ctx:
            helper.request_code("0500000000")
        self.assertIn("nothing was sent", str(ctx.exception))
        self.assertEqual((self.sends(ax), self.guard.requests()), ([], []))

    def test_spaces_and_dashes_in_the_field_are_fine(self):
        ax = FakeAx(LOGIN, CODE)
        self.assertEqual(self.make(ax, shows="050-000 0000").request_code("0500000000"), "code_screen")

    def test_a_slow_code_screen_is_waited_for_and_send_is_still_pressed_once(self):
        # the real app needed more than 6 seconds once: the screen still showed the login for a while
        ax = FakeAx(LOGIN, LOGIN, LOGIN, LOGIN, CODE)
        self.assertEqual(self.make(ax).request_code("0500000000"), "code_screen")
        self.assertEqual(len(self.sends(ax)), 1)

    def test_the_wait_for_the_code_screen_has_a_limit(self):
        ax = FakeAx(LOGIN)                      # the screen never changes
        self.assertEqual(self.make(ax).request_code("0500000000"), "unknown")
        waited = sum(s for s in self.slept if s >= 1)
        self.assertGreaterEqual(waited, 20)
        self.assertLessEqual(waited, 30)

    def test_a_lock_popup_that_comes_late_is_still_recorded(self):
        ax = FakeAx(LOGIN, LOGIN, LOGIN, LOCKED)
        with self.assertRaises(LoginBlocked):
            self.make(ax).request_code("0500000000")
        self.assertIsNotNone(self.guard.check(T0 + timedelta(minutes=1)))

    def test_a_slow_login_after_the_code_is_waited_for(self):
        ax = FakeAx(CODE, CODE, CODE, CODE, LOGGED_IN)
        self.assertEqual(self.make(ax).enter_code("123456"), "logged_in")
        self.assertEqual(self.typed, ["123456"], "the code is typed once")

    def test_the_code_screen_that_never_goes_away_is_reported_as_unknown(self):
        ax = FakeAx(CODE)
        self.assertEqual(self.make(ax).enter_code("123456"), "unknown")
        self.assertEqual(self.typed, ["123456"])

    def test_the_send_button_is_never_pressed_twice(self):
        ax = FakeAx(LOGIN)                      # the screen never changes after the press
        self.assertEqual(self.make(ax).request_code("0500000000"), "unknown")
        self.assertEqual(len(self.sends(ax)), 1)

    def test_a_third_request_is_refused_before_anything_is_pressed(self):
        ax = FakeAx(LOGIN)
        helper = self.make(ax)
        self.guard.record_request(T0 - timedelta(minutes=3))
        self.guard.record_request(T0 - timedelta(minutes=2))
        with self.assertRaises(LoginBlocked):
            helper.request_code("0500000000")
        self.assertEqual((ax.presses, self.typed), ([], []))

    def test_a_lock_popup_is_recorded_and_closed(self):
        ax = FakeAx(LOGIN, LOCKED)
        helper = self.make(ax)
        with self.assertRaises(LoginBlocked) as ctx:
            helper.request_code("0500000000")
        self.assertIn("10", str(ctx.exception))
        self.assertIsNotNone(self.guard.check(T0 + timedelta(minutes=1)))
        self.assertIn("סגור", [p[1] for p in ax.presses])

    def test_nothing_is_typed_when_the_app_is_not_in_front(self):
        ax = FakeAx(LOGIN)
        with self.assertRaises(LoginError):
            self.make(ax, front="idea").request_code("0500000000")
        self.assertEqual(self.typed, [])
        self.assertEqual(self.sends(ax), [])
        self.assertEqual(self.guard.requests(), [], "no request counted: nothing was sent")

    def test_the_app_must_show_the_login_screen(self):
        with self.assertRaises(LoginError):
            self.make(FakeAx(LOGGED_IN)).request_code("0500000000")

    def test_enter_code_types_six_digits_on_the_code_screen(self):
        ax = FakeAx(CODE, LOGGED_IN)
        self.assertEqual(self.make(ax).enter_code("123456"), "logged_in")
        self.assertEqual(self.typed, ["123456"])

    def test_a_code_that_is_not_six_digits_is_refused(self):
        helper = self.make(FakeAx(CODE))
        for bad in ("12345", "1234567", "12a456", ""):
            with self.assertRaises(LoginError):
                helper.enter_code(bad)
        self.assertEqual(self.typed, [])

    def test_a_third_code_entry_is_refused(self):
        ax = FakeAx(CODE)
        helper = self.make(ax)
        self.guard.record_entry(T0 - timedelta(minutes=3))
        self.guard.record_entry(T0 - timedelta(minutes=2))
        with self.assertRaises(LoginBlocked):
            helper.enter_code("123456")
        self.assertEqual(self.typed, [])

    def test_enter_code_needs_the_code_screen(self):
        with self.assertRaises(LoginError):
            self.make(FakeAx(LOGIN)).enter_code("123456")
        self.assertEqual(self.typed, [])


if __name__ == "__main__":
    unittest.main()
