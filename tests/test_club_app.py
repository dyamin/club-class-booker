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
        self.assertEqual(runner.calls[-1], ["/usr/bin/open", "-a", "Runner 2"])
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
        runner = Runner()
        self.app(runner).activate()
        self.assertEqual(kinds(runner), ["osascript"])
        self.assertIn("Runner 2", runner.calls[0])
        self.assertTrue(any("activate" in part for part in runner.calls[0]))

    def test_activate_also_sets_the_process_frontmost_because_activate_alone_failed(self):
        # 2026-10-15 (macOS 27.2 beta): `tell application ... to activate` returned without an error and the app stayed
        # behind Terminal, so the runner's taps went to another window ("could not switch the app to Alex", then a
        # restart that can end the session). `set frontmost of process ... to true` brought the app to the front.
        runner = Runner()
        self.app(runner).activate()
        script = " ".join(runner.calls[0])
        self.assertIn("set frontmost of process", script)
        self.assertEqual(runner.calls[0][-2:], ["Runner 2", "Runner"], "the app name and the process are arguments")

    def test_activate_opens_the_app_again_when_it_has_no_window(self):
        # 2026-10-15 14:37: the app was running with no window (closed); the run failed with "Coordinates are outside the
        # 'club' window (500x500)". `open -a` brings the window back.
        runner = Runner()
        self.app(runner).activate()
        script = " ".join(runner.calls[0])
        self.assertIn("count of windows of process", script)
        self.assertIn("/usr/bin/open -a", script)

    def test_can_control_asks_for_the_same_two_permissions_as_activate(self):
        runner = Runner()
        self.app(runner).can_control()
        script = " ".join(runner.calls[0])
        self.assertIn("set frontmost of process", script, "System Events must be allowed for the job too")


if __name__ == "__main__":
    unittest.main()
