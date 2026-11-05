"""2026-11-05: the club app asked for an update, and nobody could log it in. The program now opens the app's page in the
App Store and presses "Update" once, when the page offers it. The page of this app was read on the real App Store: one
button in the details list, "Open" when the app is current (the layout of the fakes below)."""
import unittest

from club_booking.app_update import AppStoreUpdater
from club_booking.ax import AxDump, AxElement, AxError

URL = "macappstore://apps.apple.com/app/id1445866275"


def page(label=None):
    window = AxElement("AXWindow", "App Store", "", 0, 0, 1000, 500)
    elements = [AxElement("AXButton", "Updates", "", 0, 372, 200, 38),
                AxElement("AXStaticText", "club app", "", 405, 62, 200, 30)]
    if label:
        elements.append(AxElement("AXButton", label, "", 404, 150, 80, 30))
    return AxDump(window, elements)


class FakeStore:
    def __init__(self, *states, press_error=None):
        self.states, self.presses, self.press_error = list(states), [], press_error

    def dump(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        return state

    def press(self, role, desc, y, tolerance=25):
        self.presses.append((role, desc, y))
        if self.press_error:
            raise self.press_error


class UpdaterCase(unittest.TestCase):
    def make(self, ax, open_url=None, **kw):
        self.opened, self.sleeps = [], []
        kw.setdefault("page_wait", 10.0)
        kw.setdefault("update_wait", 60.0)
        kw.setdefault("poll", 2.0)
        return AppStoreUpdater(ax, open_url=open_url or self.opened.append, store_id=1445866275,
                               sleep=self.sleeps.append, **kw)


class StatusTests(UpdaterCase):
    def test_an_app_that_is_current_is_left_alone(self):
        ax = FakeStore(page("Open"))
        result = self.make(ax).run()
        self.assertEqual((result.status, ax.presses, self.opened), ("up_to_date", [], [URL]))

    def test_an_update_is_pressed_once_and_waited_for_until_the_button_says_open(self):
        ax = FakeStore(page("Update"), page("Cancel"), page("Cancel"), page("Open"))
        result = self.make(ax).run()
        self.assertEqual(result.status, "updated")
        self.assertEqual(ax.presses, [("AXButton", "Update", 165)], "one press, at the middle of the button")

    def test_a_page_with_no_button_is_no_page_after_the_wait(self):
        ax = FakeStore(page())
        result = self.make(ax).run()
        self.assertEqual((result.status, ax.presses), ("no_page", []))
        self.assertGreaterEqual(sum(self.sleeps), 10)

    def test_a_button_that_is_not_update_or_open_is_not_pressed(self):
        ax = FakeStore(page("Get"))
        result = self.make(ax).run()
        self.assertEqual((result.status, ax.presses), ("no_button", []))
        self.assertIn("Get", result.detail)

    def test_an_update_that_never_ends_times_out_after_one_press(self):
        ax = FakeStore(page("Update"), page("Cancel"))
        result = self.make(ax).run()
        self.assertEqual((result.status, len(ax.presses)), ("timeout", 1))
        self.assertGreaterEqual(sum(self.sleeps), 60)

    def test_an_app_store_window_that_is_not_ready_yet_is_waited_for(self):
        ax = FakeStore(AxError("no window"), AxError("no window"), page("Update"), page("Open"))
        self.assertEqual(self.make(ax).run().status, "updated")

    def test_a_press_that_fails_is_a_failure_and_is_never_repeated(self):
        ax = FakeStore(page("Update"), press_error=AxError("the press failed"))
        result = self.make(ax).run()
        self.assertEqual((result.status, len(ax.presses)), ("failed", 1))
        self.assertIn("press failed", result.detail)

    def test_a_page_that_cannot_be_opened_is_a_failure_not_a_crash(self):
        def broken(url):
            raise OSError("no such command")
        result = self.make(FakeStore(page("Open")), open_url=broken).run()
        self.assertEqual(result.status, "failed")
        self.assertIn("OSError", result.detail)

    def test_the_app_store_is_closed_again_whatever_happens(self):
        for ax in (FakeStore(page("Open")), FakeStore(page("Update"), page("Open")), FakeStore(page()), FakeStore(page("Get"))):
            closed = []
            result = self.make(ax, close=lambda: closed.append(1)).run()
            self.assertEqual(closed, [1], result.status)

    def test_a_close_that_fails_changes_nothing(self):
        def broken():
            raise OSError("cannot quit")
        self.assertEqual(self.make(FakeStore(page("Open")), close=broken).run().status, "up_to_date")

    def test_the_sidebar_button_called_updates_is_never_taken_for_the_update_button(self):
        ax = FakeStore(page())              # the page has the sidebar item "Updates" but no button of the app
        self.assertEqual(self.make(ax).run().status, "no_page")
        self.assertEqual(ax.presses, [])


if __name__ == "__main__":
    unittest.main()
