import unittest
from datetime import datetime, timedelta

from club_booking.attention import Attention, GuardedScreen, UserActive

T0 = datetime(2026, 10, 14, 12, 0, 0)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return T0 + timedelta(seconds=self.t)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def attention(clock, idle, patient, **kw):
    """idle is a function of the clock: how long ago the user last touched the keyboard or the mouse."""
    return Attention(lambda: idle(clock.t), clock.now, clock.sleep, patient=patient, **kw)


class AttentionTests(unittest.TestCase):
    def test_a_user_who_is_away_is_never_in_the_way(self):
        clock = Clock()
        a = attention(clock, lambda t: 9999.0, patient=False)
        a.checkpoint()
        a.note()
        clock.t += 30
        a.checkpoint()
        self.assertEqual(clock.slept, [])

    def test_a_user_at_the_keyboard_at_the_start_makes_a_non_urgent_run_stand_back(self):
        clock = Clock()
        with self.assertRaises(UserActive):
            attention(clock, lambda t: 0.5, patient=False).checkpoint()

    def test_an_urgent_run_waits_for_a_pause_in_the_users_typing(self):
        clock = Clock()
        typing_until = 6.0                                                         # the user stops typing at t = 6
        a = attention(clock, lambda t: 0.5 if t < typing_until else t - typing_until, patient=True, quiet=2.0)
        a.checkpoint()
        self.assertGreaterEqual(clock.t, 8.0, "it went on two quiet seconds after the last key")
        self.assertLess(clock.t, 12.0)

    def test_an_urgent_run_does_not_wait_for_ever(self):
        clock = Clock()
        a = attention(clock, lambda t: 0.1, patient=True, max_wait=20.0)           # the user never stops
        a.checkpoint()
        self.assertLessEqual(clock.t, 21.0, "a class is opening: after a while the booking goes on")

    def test_the_programs_own_clicks_are_not_the_user(self):
        # a click made by the program resets the idle time too: idle is then about the time since that click
        clock = Clock()
        last_click = {"t": 0.0}
        a = attention(clock, lambda t: t - last_click["t"], patient=False)
        for _ in range(4):
            clock.t += 3.0
            a.checkpoint()                                                         # no exception
            a.note()
            last_click["t"] = clock.t

    def test_a_touch_after_the_programs_last_action_is_the_user(self):
        clock = Clock()
        a = attention(clock, lambda t: 9999.0, patient=False)
        a.checkpoint()
        a.note()                                                                   # the program acted at t = 0
        clock.t += 10.0
        touched = Attention(lambda: 1.0, clock.now, clock.sleep, patient=False)    # the user moved the mouse 1 s ago
        touched._last = a._last
        with self.assertRaises(UserActive):
            touched.checkpoint()


    def test_an_urgent_run_that_waited_in_vain_does_not_wait_again(self):
        # a class is opening and the user types without a break: wait once, then book. Not a minute and a half per tap
        clock = Clock()
        a = attention(clock, lambda t: 0.1, patient=True, max_wait=20.0)
        a.checkpoint()
        a.note()
        waited = clock.t
        for _ in range(5):
            clock.t += 3.0
            a.checkpoint()
            a.note()
        self.assertEqual(clock.t, waited + 15.0, "no more waiting after the first time")

    def test_the_user_is_not_in_the_way_while_a_booking_is_being_finished(self):
        # between the register tap and the tap that closes its confirmation the program must go on: a dialog left open
        # would be worse than two more clicks
        clock = Clock()
        a = attention(clock, lambda t: 0.1, patient=False)
        with a.hold():
            a.checkpoint()                                                         # no exception
            with a.hold():                                                         # holds nest
                a.checkpoint()
            a.checkpoint()
        with self.assertRaises(UserActive):
            a.checkpoint()

    def test_user_active_is_not_swallowed_by_a_step_that_goes_on_after_a_failure(self):
        # the runner has many `except Exception` that say "this step failed, go on": stepping aside must pass through them
        try:
            try:
                raise UserActive("the user is using the Mac")
            except Exception:
                self.fail("UserActive was caught as an ordinary Exception")
        except UserActive:
            pass


class FakeScreen:
    def __init__(self):
        self.log = []

    def tap(self, x, y):
        self.log.append(("tap", x, y))
        return "tapped"

    def swipe(self, *a, **k):
        self.log.append(("swipe",))

    def screenshot(self):
        self.log.append(("shot",))
        return b"png"


class GuardedScreenTests(unittest.TestCase):
    def test_taps_and_swipes_wait_for_the_checkpoint_and_pictures_do_not(self):
        calls = []

        class A:
            def checkpoint(self):
                calls.append("check")

            def note(self):
                calls.append("note")

        screen = FakeScreen()
        guarded = GuardedScreen(screen, A())
        guarded.screenshot()
        self.assertEqual(calls, [], "looking never disturbs the user")
        self.assertEqual(guarded.tap(1, 2), "tapped")
        guarded.swipe(1, 2, 3, 4)
        self.assertEqual(calls, ["check", "note", "check", "note"])
        self.assertEqual(screen.log, [("shot",), ("tap", 1, 2), ("swipe",)])

    def test_a_user_in_the_way_stops_the_tap_before_it_is_made(self):
        class A:
            def checkpoint(self):
                raise UserActive("the user is using the Mac")

            def note(self):
                raise AssertionError("no action was made")

        screen = FakeScreen()
        with self.assertRaises(UserActive):
            GuardedScreen(screen, A()).tap(1, 2)
        self.assertEqual(screen.log, [])

    def test_everything_else_passes_through(self):
        screen = FakeScreen()
        screen.reconnect = lambda: "reconnected"
        self.assertEqual(GuardedScreen(screen, object()).reconnect(), "reconnected")


if __name__ == "__main__":
    unittest.main()
