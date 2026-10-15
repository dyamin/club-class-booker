import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from club_booking.calendar_app import (CalendarError, CalendarService, EventInfo, calendar_names, find_events,
                                       name_pattern, rename_event)
from club_booking import reports
from club_booking.config import Coords, Person
from club_booking.models import CardState, ClassCard
from club_booking.ocr import TextHit
from club_booking.osa import AppleScriptError, ensure_running, osa_for, run_applescript
from club_booking.reports import Reporter
from club_booking.system import (display_asleep, idle_seconds, screen_locked, screen_unavailable, type_into,
                                 wake_display, wake_if_only_asleep)

TZ = ZoneInfo("Asia/Jerusalem")
START = datetime(2026, 10, 13, 8, 50, tzinfo=TZ)
CARD = ClassCard("METCON", START, datetime(2026, 10, 13, 9, 35, tzinfo=TZ), CardState.OPEN, (512, 557))
ALEX = Person("alex", "Alex", 176, "Alex", "")
SAM = Person("sam", "Sam", 214, "Kids", "Sam - ")


class OsaTests(unittest.TestCase):
    def test_arguments_are_not_pasted_into_the_script(self):
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"] = cmd
            seen["text"] = Path(cmd[1]).read_text(encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        out = run_applescript("on run argv\nreturn item 1 of argv\nend run", ['He said "hi"; quit'], runner)
        self.assertEqual(out, "ok")
        self.assertEqual(seen["cmd"][0], "/usr/bin/osascript")
        self.assertEqual(seen["cmd"][2], 'He said "hi"; quit')
        self.assertNotIn("He said", seen["text"])

    def test_failure(self):
        runner = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="bad script")
        with self.assertRaises(AppleScriptError):
            run_applescript("x", [], runner)


class EnsureRunningTests(unittest.TestCase):
    def runner(self, running_after):
        """pgrep says 'running' once `running_after` polls have passed (None = never)."""
        calls = {"list": [], "polls": 0}

        def run(cmd, **kw):
            calls["list"].append(list(cmd))
            if cmd[0].endswith("pgrep"):
                calls["polls"] += 1
                up = running_after is not None and calls["polls"] > running_after
                return SimpleNamespace(returncode=0 if up else 1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        run.calls = calls
        return run

    def test_an_app_that_runs_is_left_alone(self):
        run = self.runner(running_after=0)
        ensure_running("Calendar", "Calendar", runner=run, sleep=lambda s: None)
        self.assertFalse(any(c[0].endswith("open") for c in run.calls["list"]))

    def test_an_app_that_does_not_run_is_started_hidden_and_waited_for(self):
        # a launchd job cannot start Calendar with an Apple event ("Application isn't running", -600)
        run = self.runner(running_after=3)
        ensure_running("Calendar", "Calendar", runner=run, sleep=lambda s: None)
        self.assertIn(["/usr/bin/open", "-g", "-a", "Calendar"], run.calls["list"])

    def test_an_app_that_never_starts_is_an_error(self):
        with self.assertRaises(AppleScriptError):
            ensure_running("Calendar", "Calendar", runner=self.runner(None), sleep=lambda s: None, wait=3)

    def test_osa_for_starts_the_app_before_the_script(self):
        order = []
        osa = osa_for("Calendar", ensure=lambda n, p: order.append(("ensure", n, p)),
                      runner=lambda script, args=(): order.append(("run", script, list(args))) or "ok")
        self.assertEqual(osa("on run argv\nend run", ["x"]), "ok")
        self.assertEqual(order, [("ensure", "Calendar", "Calendar"), ("run", "on run argv\nend run", ["x"])])


class FindRenameTests(unittest.TestCase):
    def test_find_events_parses_lines(self):
        calls = []

        def osa(script, args):
            calls.append(args)
            return "UID1\tMETCON\tTuesday\tTuesday\t-60,\nUID2\tOther\ta\tb\t\n"

        events = find_events("Alex", START, osa)
        self.assertEqual(events, [EventInfo("UID1", "METCON", "Tuesday", "Tuesday", (-60,)),
                                  EventInfo("UID2", "Other", "a", "b", ())])
        self.assertEqual(calls[0], ["Alex", "2026", "10", "13", "8", "50"])

    def test_rename_failure(self):
        with self.assertRaises(CalendarError):
            rename_event("Kids", "UID1", "Sam - METCON", lambda s, a: "not found")

    def test_name_pattern(self):
        self.assertEqual(name_pattern("Alex"), "Alex")
        self.assertEqual(name_pattern("Kids"), "Kids")


class FakeScreen:
    def __init__(self):
        self.taps = []
        self.shots = 0

    def tap(self, x, y):
        self.taps.append((x, y))

    def region_png(self):
        self.shots += 1
        return f"png{self.shots}".encode()


def hits_for(script):
    """ocr_fn that returns the hits scripted for each region screenshot, in order."""
    queue = list(script)
    return lambda png: queue.pop(0) if len(queue) > 1 else queue[0]


EDITOR = [TextHit("New Event", 510, 64), TextHit("Calendar", 88, 585), TextHit("Alert", 75, 637)]
MENU_CAL = EDITOR + [TextHit("Alex", 990, 631), TextHit("Famil", 990, 660), TextHit("Kids", 985, 696)]
EDITOR_AFTER_CAL = [TextHit("New Event", 510, 64), TextHit("Calendar", 88, 585), TextHit("• Alex", 960, 583),
                    TextHit("Alert", 75, 687)]
MENU_ALERT = EDITOR_AFTER_CAL + [TextHit("None", 975, 418), TextHit("1 hour be", 993, 561), TextHit("2 hours t", 993, 586)]
FINAL = [TextHit("New Event", 510, 64), TextHit("Calendar", 88, 585), TextHit("• Alex", 960, 583),
         TextHit("Alert", 75, 687), TextHit("1 hour before", 940, 687)]
CLOSED = [TextHit("Classes", 40, 40)]          # the editor is gone after Save


class CalendarServiceTests(unittest.TestCase):
    def service(self, screen, ocr_script, osa):
        return CalendarService(screen, Coords(), hits_for(ocr_script), osa, sleep=lambda s: None)

    def test_happy_path_for_alex(self):
        screen = FakeScreen()
        states = iter([[], [EventInfo("U1", "METCON", "s", "e", (-60,))]])
        osa_calls = []

        def osa(script, args):
            osa_calls.append(args)
            events = next(states)
            return "".join(f"{e.uid}\t{e.title}\t{e.start}\t{e.end}\t-60,\n" for e in events)

        svc = self.service(screen, [EDITOR, EDITOR, MENU_CAL, EDITOR_AFTER_CAL, MENU_ALERT, FINAL, CLOSED], osa)
        event = svc.add(ALEX, CARD)
        self.assertEqual(event.uid, "U1")
        taps = screen.taps
        self.assertEqual(taps[0], Coords().dialog_save_calendar)          # the app's button
        self.assertIn((969, 585), taps)                                   # open the Calendar menu
        self.assertIn((990, 631), taps)                                   # choose Alex
        self.assertIn((969, 687), taps)                                   # open the Alert menu
        self.assertIn((993, 561), taps)                                   # choose 1 hour before
        self.assertEqual(taps[-1], Coords().editor_save)

    def test_son_event_is_renamed(self):
        screen = FakeScreen()
        states = iter(["", "U9\tJudo\ts\te\t-60,\n"])
        osa_calls = []

        def osa(script, args):
            osa_calls.append(args)
            if "ok" in script and len(args) == 3:
                return "ok"
            return next(states)

        menu_cal = EDITOR + [TextHit("Kids", 985, 696)]
        after = [TextHit("New Event", 510, 64), TextHit("Calendar", 88, 585), TextHit("• Kids", 960, 583), TextHit("Alert", 75, 687)]
        final = after + [TextHit("1 hour before", 940, 687)]
        svc = self.service(screen, [EDITOR, EDITOR, menu_cal, after, after + [TextHit("1 hour be", 993, 561)], final, CLOSED], osa)
        svc.add(SAM, CARD)
        self.assertIn(["Kids", "U9", "Sam - Judo"], osa_calls)

    def test_menu_item_missing_cancels_the_editor(self):
        screen = FakeScreen()
        osa = lambda script, args: ""
        svc = self.service(screen, [EDITOR, EDITOR, EDITOR], osa)   # menu never shows the item
        with self.assertRaises(CalendarError):
            svc.add(ALEX, CARD)
        self.assertEqual(screen.taps[-1], Coords().editor_cancel)

    def test_editor_does_not_open(self):
        screen = FakeScreen()
        svc = self.service(screen, [[TextHit("something else", 10, 10)]], lambda s, a: "")
        with self.assertRaises(CalendarError) as ctx:
            svc.add(ALEX, CARD)
        self.assertIn("did not open", str(ctx.exception))

    def test_an_editor_that_never_opened_is_not_cancelled_blind(self):
        screen = FakeScreen()
        svc = self.service(screen, [[TextHit("something else", 10, 10)]], lambda s, a: "")
        with self.assertRaises(CalendarError):
            svc.add(ALEX, CARD)
        self.assertEqual(screen.taps, [Coords().dialog_save_calendar], "no tap at the editor's Cancel position")

    def test_a_cancel_that_does_not_close_the_editor_is_reported(self):
        screen = FakeScreen()
        svc = self.service(screen, [EDITOR], lambda s, a: "")       # the menu has no item and nothing closes
        with self.assertRaises(CalendarError) as ctx:
            svc.add(ALEX, CARD)
        self.assertIn("still open", str(ctx.exception))

    def test_an_editor_that_does_not_close_after_save_is_an_error(self):
        screen = FakeScreen()
        svc = self.service(screen, [EDITOR, EDITOR, MENU_CAL, EDITOR_AFTER_CAL, MENU_ALERT, FINAL], lambda s, a: "")
        with self.assertRaises(CalendarError) as ctx:
            svc.add(ALEX, CARD)
        self.assertIn("did not close", str(ctx.exception))
        self.assertIn(Coords().editor_cancel, screen.taps)

    def test_no_new_event_or_two_new_events_is_an_error(self):
        for lines in ("", "A\tx\ts\te\t-60,\nB\tx\ts\te\t-60,\n"):
            screen = FakeScreen()
            states = iter(["", lines])
            svc = self.service(screen, [EDITOR, EDITOR, MENU_CAL, EDITOR_AFTER_CAL, MENU_ALERT, FINAL, CLOSED],
                               lambda s, a: next(states, lines))
            with self.assertRaises(CalendarError):
                svc.add(ALEX, CARD)

    def test_missing_alarm_is_an_error(self):
        screen = FakeScreen()
        states = iter(["", "U1\tMETCON\ts\te\t\n"])
        svc = self.service(screen, [EDITOR, EDITOR, MENU_CAL, EDITOR_AFTER_CAL, MENU_ALERT, FINAL, CLOSED],
                           lambda s, a: next(states))
        with self.assertRaises(CalendarError) as ctx:
            svc.add(ALEX, CARD)
        self.assertIn("no alert", str(ctx.exception))


class ReporterTests(unittest.TestCase):
    def test_phone_mac_and_prune_pass_arguments(self):
        calls = []

        def osa(script, args):
            calls.append((script, list(args)))
            return "3"

        rep = Reporter("Club bookings", osa)
        rep.phone("Booked", "Sam - Judo")
        rep.mac("Booked", "text")
        self.assertEqual(rep.prune(7), 3)
        self.assertEqual(calls[0][1], ["Club bookings", "Booked", "Sam - Judo"])
        self.assertEqual(calls[1][1], ["Booked", "text"])
        self.assertEqual(calls[2][1], ["Club bookings", "7"])
        self.assertIn('exists list (item 1 of argv)', calls[0][0])   # only the one list is touched

    def test_the_phone_gets_the_message_and_the_mac_only_when_the_phone_could_not(self):
        # one message, one place: the Reminders list reaches the Mac as well, so a banner on top of it is a second copy
        calls = []

        def osa(script, args):
            calls.append("mac" if "display notification" in script else "phone")
            return "ok"

        self.assertEqual(Reporter("L", osa).tell("t", "b"), [])
        self.assertEqual(calls, ["phone"])

    def test_a_failing_phone_channel_falls_back_to_the_mac_and_is_reported(self):
        calls = []

        def osa(script, args):
            if "display notification" in script:
                calls.append("mac")
                return "ok"
            raise AppleScriptError("no permission")

        problems = Reporter("L", osa).tell("t", "b")
        self.assertEqual(calls, ["mac"], "the user still hears about it")
        self.assertEqual(len(problems), 1)
        self.assertIn("phone", problems[0])


class TakeBackTests(unittest.TestCase):
    def test_a_tagged_message_ends_with_its_tag_and_the_tag_finds_it_again(self):
        calls = []

        def osa(script, args):
            calls.append((script, list(args)))
            return "1"

        rep = Reporter("Club bookings", osa)
        self.assertEqual(rep.tell("Problem", "Alex: x", tag="abc123"), [])
        self.assertEqual(calls[0][1], ["Club bookings", "Problem", "Alex: x\n[club-booking m:abc123]"])
        self.assertEqual(rep.resolve("abc123"), 1)
        self.assertEqual(calls[1][1], ["Club bookings", "abc123"])
        self.assertIn("exists list (item 1 of argv)", calls[1][0], "only the one list is touched")
        self.assertIn("[club-booking m:", calls[1][0])

    def test_a_message_without_a_tag_is_sent_as_it_is(self):
        calls = []
        Reporter("L", lambda script, args: calls.append(list(args)) or "ok").tell("t", "b")
        self.assertEqual(calls, [["L", "t", "b"]])

    def test_the_tag_does_not_match_a_longer_one(self):
        # "abc12" must not take back the message "abc123"
        self.assertIn('"[club-booking m:" & (item 2 of argv) & "]"', reports._RESOLVE)


class SystemEventsTests(unittest.TestCase):
    def test_a_job_that_may_control_system_events_has_no_problem(self):
        from club_booking.system import system_events_problem
        self.assertEqual(system_events_problem(lambda *a, **k: SimpleNamespace(returncode=0, stdout="Finder", stderr="")), "")

    def test_a_refusal_is_given_back_as_text(self):
        from club_booking.system import system_events_problem
        text = system_events_problem(lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Not authorized (-1743)\n"))
        self.assertIn("-1743", text)

    def test_a_missing_tool_is_a_problem_not_a_crash(self):
        from club_booking.system import system_events_problem

        def broken(*a, **k):
            raise OSError("no osascript")
        self.assertIn("no osascript", system_events_problem(broken))


class SystemTests(unittest.TestCase):
    def test_locked_and_unlocked(self):
        locked = lambda *a, **k: SimpleNamespace(stdout='  | "IOConsoleUsers" = ({"CGSSessionScreenIsLocked"=Yes,"kCGSSessionOnConsoleKey"=Yes})')
        unlocked = lambda *a, **k: SimpleNamespace(stdout='  | "IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes})')
        self.assertTrue(screen_locked(locked))
        self.assertFalse(screen_locked(unlocked))

    def test_a_locked_or_sleeping_screen_is_unavailable(self):
        # seen on 2026-10-13 18:58: the displays were asleep, the screenshot failed, and the run counted as a failure
        self.assertTrue(screen_unavailable(lambda: True, lambda: False))
        self.assertTrue(screen_unavailable(lambda: False, lambda: True))
        self.assertFalse(screen_unavailable(lambda: False, lambda: False))

    def woken(self, locked, asleep, wakes=True):
        """locked and asleep are lists of answers, one per question; after a wake-up the display is on if wakes."""
        woke, state = [], {"asleep": list(asleep), "locked": list(locked)}

        def is_locked():
            return state["locked"].pop(0) if len(state["locked"]) > 1 else state["locked"][0]

        def is_asleep():
            return state["asleep"].pop(0) if len(state["asleep"]) > 1 else state["asleep"][0]

        def wake():
            woke.append(1)
            if wakes:
                state["asleep"] = [False]

        return wake_if_only_asleep(is_locked, is_asleep, wake, lambda s: None), woke

    def test_a_locked_screen_is_not_woken_it_needs_a_password(self):
        self.assertEqual(self.woken([True], [True]), (False, []))

    def test_a_display_that_is_on_needs_no_wake_up(self):
        self.assertEqual(self.woken([False], [False]), (True, []))

    def test_a_sleeping_display_is_woken_and_then_usable_when_it_asks_for_no_password(self):
        self.assertEqual(self.woken([False], [True]), (True, [1]))

    def test_a_display_that_will_not_wake_stays_unusable(self):
        self.assertEqual(self.woken([False], [True], wakes=False), (False, [1]))

    def test_a_display_that_wakes_to_the_login_screen_stays_unusable(self):
        self.assertEqual(self.woken([False, True], [True]), (False, [1]))

    def test_the_wake_up_declares_user_activity_without_waiting_for_it(self):
        started = []
        wake_display(lambda cmd, **kw: started.append(cmd))
        self.assertEqual(started, [["/usr/bin/caffeinate", "-u", "-t", "5"]])

    def test_a_display_probe_that_fails_means_not_asleep(self):
        # a broken probe must not crash the tick or hold the screen back for ever
        from unittest import mock
        with mock.patch("ctypes.CDLL", side_effect=OSError("no such framework")):
            self.assertFalse(display_asleep())

    def test_the_display_state_can_be_read_on_this_mac(self):
        self.assertIsInstance(display_asleep(), bool)

    def test_idle_seconds(self):
        runner = lambda *a, **k: SimpleNamespace(stdout='    | |   "HIDIdleTime" = 5000000000\n')
        self.assertEqual(idle_seconds(runner), 5.0)
        # when the idle time cannot be read, the user counts as away: a replan must not wait for ever
        self.assertEqual(idle_seconds(lambda *a, **k: SimpleNamespace(stdout="")), float("inf"))


class TypeIntoTests(unittest.TestCase):
    def test_one_script_activates_checks_the_front_app_and_types(self):
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertTrue(type_into("Runner 2", "Runner", '12"3; quit', True, runner))
        self.assertEqual(seen["cmd"][0], "/usr/bin/osascript")
        self.assertEqual(seen["cmd"][-4:], ["Runner 2", "Runner", '12"3; quit', "1"], "all values are arguments")
        script = " ".join(seen["cmd"][1:-4])
        self.assertNotIn("12", script)
        self.assertIn("activate", script)
        self.assertIn("frontmost", script)
        self.assertIn('keystroke "a" using command down', script)
        self.assertIn("key code 51", script)

    def test_the_app_is_put_in_front_by_system_events_too_because_activate_alone_failed(self):
        # 2026-10-15 (macOS 27.2 beta): `tell application ... to activate` returned without error and the app stayed behind
        # Terminal; `set frontmost of process ... to true` and `open -a` worked. The typing step relied on activate only,
        # so "login request" stopped with "the club app did not come to the front". The check still follows.
        seen = {}
        runner = lambda cmd, **kw: (seen.setdefault("cmd", cmd), SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
        type_into("Runner 2", "Runner", "1", False, runner)
        script = [part for part in seen["cmd"][1:-4] if part != "-e"]
        front = next(i for i, line in enumerate(script) if "set frontmost of process (item 2 of argv) to true" in line)
        check = next(i for i, line in enumerate(script) if "is not (item 2 of argv) then error" in line)
        keys = next(i for i, line in enumerate(script) if line.strip().startswith("keystroke (item 3"))
        self.assertLess(front, check)
        self.assertLess(check, keys, "the check is still the last thing before the typing")

    def test_without_clear_the_flag_is_zero(self):
        seen = {}
        runner = lambda cmd, **kw: (seen.setdefault("cmd", cmd), SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
        type_into("Runner 2", "Runner", "123456", False, runner)
        self.assertEqual(seen["cmd"][-1], "0")

    def test_a_script_that_fails_means_nothing_was_typed(self):
        failing = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="not in front")
        self.assertFalse(type_into("Runner 2", "Runner", "1", False, failing))

        def broken(cmd, **kw):
            raise OSError("no osascript")

        self.assertFalse(type_into("Runner 2", "Runner", "1", False, broken))


class CalendarNamesTests(unittest.TestCase):
    def test_calendar_names(self):
        self.assertEqual(calendar_names(lambda s, a: "Home\nAlex\n\nKids\n"), ["Home", "Alex", "Kids"])


if __name__ == "__main__":
    unittest.main()
