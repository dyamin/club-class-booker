import unittest
from types import SimpleNamespace

from club_booking.club_app import ClubApp


class Runner:
    """Plays subprocess.run. The app counts as running for the first `running_polls` calls of pgrep."""

    def __init__(self, running_polls=0):
        self.calls, self.polls_left = [], running_polls

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0].endswith("pgrep"):
            self.polls_left -= 1
            return SimpleNamespace(returncode=0 if self.polls_left >= 0 else 1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def kinds(runner):
    return [c[0].rsplit("/", 1)[-1] for c in runner.calls]


class ClubAppTests(unittest.TestCase):
    def app(self, runner):
        return ClubApp("Runner 2", "Runner", runner=runner, sleep=lambda s: None, quit_wait=5)

    def test_restart_quits_waits_for_the_end_and_opens_again(self):
        runner = Runner(running_polls=2)
        self.app(runner).restart()
        self.assertEqual(kinds(runner)[0], "osascript")
        self.assertIn("Runner 2", runner.calls[0], "the name is an argument, not pasted into the script")
        self.assertEqual(runner.calls[kinds(runner).index("open")], ["/usr/bin/open", "-a", "Runner 2"])
        self.assertNotIn("pkill", kinds(runner))

    def test_can_control_is_empty_when_the_app_answers(self):
        self.assertEqual(self.app(Runner()).can_control(), "")

    def test_can_control_says_why_when_automation_is_not_allowed(self):
        def refused(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="Not authorized to send Apple events to Runner 2. (-1743)\n")
        self.assertIn("Not authorized", self.app(refused).can_control())

    def test_can_control_names_a_timeout(self):
        import subprocess

        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)
        self.assertIn("did not answer", self.app(hangs).can_control())

    def test_an_app_that_does_not_quit_is_ended_after_the_wait(self):
        runner = Runner(running_polls=999)
        self.app(runner).restart()
        self.assertLess(kinds(runner).index("pkill"), kinds(runner).index("open"))
        self.assertEqual(runner.calls[kinds(runner).index("pkill")][-2:], ["-x", "Runner"])

    def test_activate_brings_the_app_to_the_front(self):
        runner = Runner(running_polls=99)
        self.app(runner).activate()
        front = [c for c in runner.calls if any("to activate" in part for part in c)]
        self.assertEqual(len(front), 1)
        self.assertIn("Runner 2", front[0])
        self.assertTrue({"open", "pkill"}.isdisjoint(kinds(runner)), "the app is only asked, nothing is opened or ended")

    def test_activate_also_sets_the_process_frontmost_because_activate_alone_failed(self):
        # 2026-10-15 (macOS 27.2 beta): `tell application ... to activate` returned without an error and the app stayed
        # behind Terminal, so the runner's taps went to another window ("could not switch the app to Alex", then a
        # restart that can end the session). `set frontmost of process ... to true` brought the app to the front.
        runner = Runner(running_polls=99)
        self.app(runner).activate()
        front = [c for c in runner.calls if any("to activate" in part for part in c)][0]
        self.assertIn("set frontmost of process", " ".join(front))
        self.assertEqual(front[-2:], ["Runner 2", "Runner"], "the app name and the process are arguments")

    # 2026-10-15 14:37: the app was running with no window (closed); the run failed with "Coordinates are outside the
    # 'club' window (500x500)". `open -a` brings the window back. 2026-10-22 13:35 to 14:29: the same, and every try
    # failed for an hour: `tell application ... to activate` HANGS while the app has no window, so the script timed out
    # (30 seconds) before it reached its `open -a` step. The window is now looked at and reopened FIRST, by its own calls.
    def windowless(self, counts, hang_until_open=True, count_error=False, sleeps=None):
        state = {"opened": 0, "counts": list(counts), "last": None}

        def run(cmd, **kw):
            self.calls.append(list(cmd))
            script = " ".join(cmd)
            if cmd[0].endswith("open"):
                state["opened"] += 1
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[0].endswith("osascript") and "return count of windows" in script:
                if count_error:
                    return SimpleNamespace(returncode=1, stdout="", stderr="no answer")
                n = state["counts"].pop(0) if len(state["counts"]) > 1 else state["counts"][0]
                state["last"] = n
                return SimpleNamespace(returncode=0, stdout=f"{n}\n", stderr="")
            if (cmd[0].endswith("osascript") and "to activate" in script and hang_until_open and not state["opened"]
                    and state["last"] == 0):               # only an app with no window hangs
                import subprocess
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 30))      # what the real app did
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        self.calls, self.state = [], state
        return ClubApp("Runner 2", "Runner", runner=run, sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                       quit_wait=5)

    def opens(self):
        return [c for c in self.calls if c[0].endswith("open")]

    def test_a_running_app_with_no_window_is_opened_again_before_it_is_activated(self):
        app = self.windowless([0, 1])
        app.activate()                                # the old order raised TimeoutExpired here
        self.assertEqual(self.opens(), [["/usr/bin/open", "-a", "Runner 2"]])
        order = [("open" if c[0].endswith("open") else "activate" if "to activate" in " ".join(c) else "count")
                 for c in self.calls]
        self.assertLess(order.index("open"), order.index("activate"))

    def test_an_app_with_a_window_is_not_opened_again(self):
        app = self.windowless([1])
        app.activate()
        self.assertEqual(self.opens(), [])

    def test_it_waits_for_the_window_to_appear(self):
        sleeps = []
        app = self.windowless([0, 0, 0, 1], sleeps=sleeps)
        app.activate()
        self.assertEqual(len(sleeps), 2, "one short wait for each look after the opening that still saw no window")
        self.assertEqual(len(self.opens()), 1, "it is opened once, not at every look")

    def test_a_window_that_never_comes_does_not_stop_the_activation_or_open_the_app_again(self):
        app = self.windowless([0], hang_until_open=False)
        app.activate()
        self.assertEqual(len(self.opens()), 1)
        self.assertTrue(any("to activate" in " ".join(c) for c in self.calls), "the front step still runs")

    def test_when_the_windows_cannot_be_counted_nothing_is_opened(self):
        app = self.windowless([0], hang_until_open=False, count_error=True)
        app.activate()
        self.assertEqual(self.opens(), [])

    # 2026-10-22: the window the program reopened was 2560x1320, not 1024x800. Every tap position was measured in a
    # 1024x800 window, so the person selector could not be closed ("the person selector did not close") and the run
    # failed for everyone. The program now sets the size itself.
    def sized(self, size_text="2560x1320", windows=1, size_error=False):
        self.calls = []
        self.size_text = size_text

        def run(cmd, **kw):
            self.calls.append(list(cmd))
            script = " ".join(cmd)
            if not cmd[0].endswith("osascript"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "return count of windows" in script:
                return SimpleNamespace(returncode=0, stdout=f"{windows}\n", stderr="")
            if "set size of window 1" in script:
                self.size_text = "1024x800"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "size of window 1" in script:
                if size_error:
                    return SimpleNamespace(returncode=1, stdout="", stderr="no answer")
                return SimpleNamespace(returncode=0, stdout=self.size_text + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return ClubApp("Runner 2", "Runner", runner=run, sleep=lambda s: None, quit_wait=5)

    def size_sets(self):
        return [c for c in self.calls if "set size of window 1" in " ".join(c)]

    def test_a_window_of_the_wrong_size_is_set_to_1024_by_800(self):
        app = self.sized("2560x1320")
        app.activate()
        self.assertEqual(len(self.size_sets()), 1)
        self.assertIn("{1024, 800}", " ".join(self.size_sets()[0]))

    def test_a_window_of_the_right_size_is_left_alone(self):
        app = self.sized("1024x800")
        app.activate()
        self.assertEqual(self.size_sets(), [])

    def test_a_pixel_of_difference_is_no_reason_to_resize(self):
        app = self.sized("1024x799")
        app.activate()
        self.assertEqual(self.size_sets(), [])

    def test_a_size_that_cannot_be_read_is_not_set_on_a_guess(self):
        app = self.sized(size_error=True)
        app.activate()
        self.assertEqual(self.size_sets(), [])

    def test_an_app_without_a_window_has_no_size_to_set(self):
        app = self.sized(windows=0)
        app.activate()
        self.assertEqual(self.size_sets(), [])

    def test_fit_says_whether_it_set_the_size(self):
        app = self.sized("2560x1320")
        self.assertTrue(app.fit())
        self.assertFalse(app.fit(), "now the window has its size: nothing to set")

    def test_the_windows_of_the_app_are_described_for_the_log(self):
        def run(cmd, **kw):
            script = " ".join(cmd)
            if "repeat with w in windows" in script:
                return SimpleNamespace(returncode=0, stdout="1024x800 515x800 \n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        app = ClubApp("Runner 2", "Runner", runner=run, sleep=lambda s: None)
        self.assertEqual(app.describe_windows(), "2 windows: 1024x800 515x800")

    def test_a_description_that_cannot_be_had_says_so_and_never_raises(self):
        import subprocess

        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 15)
        app = ClubApp("Runner 2", "Runner", runner=run, sleep=lambda s: None)
        self.assertEqual(app.describe_windows(), "unknown")
        quiet = ClubApp("Runner 2", "Runner", runner=lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
                        sleep=lambda s: None)
        self.assertEqual(quiet.describe_windows(), "no window")

    def test_the_size_is_set_after_the_restart_too(self):
        app = self.sized("2560x1320")
        app.restart()
        self.assertEqual(len(self.size_sets()), 1)
        opened = [i for i, c in enumerate(self.calls) if c[0].endswith("open")]
        sets = [i for i, c in enumerate(self.calls) if "set size of window 1" in " ".join(c)]
        self.assertLess(opened[0], sets[0], "the window of the new process is the one that is set")

    # 2026-10-29: the program quits the club app when a booking session is over (the user's request). The session of the
    # account survives a quit (tried: the app came back on its home screen, logged in), but the next session must
    # start the app again.
    def test_quit_asks_the_app_to_quit_and_waits_for_the_end(self):
        runner = Runner(running_polls=3)
        self.assertTrue(self.app(runner).quit())
        scripts = [" ".join(c) for c in runner.calls if c[0].endswith("osascript")]
        self.assertTrue(any("to quit" in x for x in scripts))
        self.assertNotIn("pkill", kinds(runner))

    def test_an_app_that_is_not_running_is_already_quit(self):
        runner = Runner()                                  # not running
        self.assertTrue(self.app(runner).quit())
        self.assertNotIn("osascript", kinds(runner))

    def test_an_app_that_does_not_quit_is_left_alone_unless_it_is_forced(self):
        runner = Runner(running_polls=999)
        self.assertFalse(self.app(runner).quit())
        self.assertNotIn("pkill", kinds(runner), "the end of a session never kills the app")
        self.assertIn("pkill", kinds(self.forced()))

    def forced(self):
        runner = Runner(running_polls=999)
        self.app(runner).quit(force=True)
        return runner

    def started(self, windows_after=1):
        """The app is not running until `open` is called; its window shows up after that."""
        self.calls, state = [], {"open": 0}

        def run(cmd, **kw):
            self.calls.append(list(cmd))
            script = " ".join(cmd)
            if cmd[0].endswith("pgrep"):
                return SimpleNamespace(returncode=0 if state["open"] else 1, stdout="", stderr="")
            if cmd[0].endswith("open"):
                state["open"] += 1
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[0].endswith("osascript") and "return count of windows" in script:
                if not state["open"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="no such process")
                return SimpleNamespace(returncode=0, stdout=f"{windows_after}\n", stderr="")
            if cmd[0].endswith("osascript") and "size of window 1" in script and "set size" not in script:
                return SimpleNamespace(returncode=0, stdout="1024x800\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return ClubApp("Runner 2", "Runner", runner=run, sleep=lambda s: None, quit_wait=5)

    def test_activate_starts_an_app_that_is_not_running_and_says_so(self):
        app = self.started()
        self.assertTrue(app.activate(), "the app (and so its window) is new: the caller must wait and reconnect")
        self.assertEqual(len(self.opens()), 1)
        front = [i for i, c in enumerate(self.calls) if any("to activate" in part for part in c)]
        opened = [i for i, c in enumerate(self.calls) if c[0].endswith("open")]
        self.assertLess(opened[0], front[0], "it is started before it is brought to the front")

    def test_activate_says_so_when_it_had_to_reopen_the_window(self):
        self.assertTrue(self.windowless([0, 1]).activate())

    def test_activate_says_nothing_happened_when_the_app_was_ready(self):
        self.assertFalse(self.windowless([1]).activate())

    def test_can_control_asks_for_the_same_two_permissions_as_activate(self):
        runner = Runner()
        self.app(runner).can_control()
        script = " ".join(runner.calls[0])
        self.assertIn("set frontmost of process", script, "System Events must be allowed for the job too")


if __name__ == "__main__":
    unittest.main()
