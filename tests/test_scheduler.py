import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from club_booking.scheduler import (OPENING_PREFIX, TICK_LABEL, LaunchAgents, label_time, opening_label,
                                    wake_command, wake_commands)


class Recorder:
    def __init__(self, fail_bootstrap=False):
        self.calls = []
        self.fail_bootstrap = fail_bootstrap

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        bad = self.fail_bootstrap and cmd[1] == "bootstrap"
        return SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr="boom" if bad else "")


T1 = datetime(2026, 10, 13, 8, 50)
T2 = datetime(2026, 10, 13, 17, 15)
T3 = datetime(2026, 10, 14, 16, 30)
NOW = datetime(2026, 10, 13, 7, 0)


class LaunchAgentTests(unittest.TestCase):
    def make(self, **kw):
        self.dir = Path(tempfile.mkdtemp())
        self.rec = Recorder(**kw)
        return LaunchAgents(Path("/repo/.venv/bin/python"), Path("/repo"), Path("/log/launchd.log"),
                            uid=501, runner=self.rec, directory=self.dir)

    def boots(self, what):
        return [c for c in self.rec.calls if c[1] == what]

    # ---- the login agent: it runs once at login, never on a timer ----------------------------------
    def test_the_login_agent_runs_once_at_login_and_never_on_a_timer(self):
        agents = self.make()
        agents.install_tick()
        data = plistlib.loads((self.dir / f"{TICK_LABEL}.plist").read_bytes())
        self.assertEqual(data["ProgramArguments"], ["/repo/.venv/bin/python", "-m", "club_booking", "tick"])
        self.assertTrue(data["RunAtLoad"])
        self.assertNotIn("StartInterval", data, "the program wakes at the openings, not before")
        self.assertNotIn("StartCalendarInterval", data)
        self.assertEqual(data["WorkingDirectory"], "/repo")
        self.assertEqual(self.rec.calls[-1], ["launchctl", "bootstrap", "gui/501", str(self.dir / f"{TICK_LABEL}.plist")])

    def test_the_scheduler_counts_as_installed_when_the_login_agent_is(self):
        agents = self.make()
        self.assertFalse(agents.is_installed())
        agents.sync_openings([T1], now=NOW)
        self.assertFalse(agents.is_installed(), "an opening agent alone is not an installation")
        agents.install_tick()
        self.assertTrue(agents.is_installed())
        agents.uninstall()
        self.assertFalse(agents.is_installed())

    # ---- one persistent agent for every planned opening -------------------------------------------------
    def test_an_opening_agent_writes_the_calendar_time(self):
        agents = self.make()
        [label] = agents.sync_openings([T1], now=NOW)
        self.assertEqual(label, "com.club-booking.opening.202610130850")
        data = plistlib.loads((self.dir / f"{label}.plist").read_bytes())
        self.assertEqual(data["StartCalendarInterval"], {"Month": 10, "Day": 13, "Hour": 8, "Minute": 50})
        self.assertNotIn("StartInterval", data)
        self.assertNotIn("RunAtLoad", data)

    def test_every_planned_opening_has_its_own_agent_and_one_per_minute(self):
        agents = self.make()
        labels = agents.sync_openings([T3, T1, T2, T1], now=NOW)               # T1 twice: two people, the same minute
        self.assertEqual(labels, [opening_label(T1), opening_label(T2), opening_label(T3)])
        self.assertEqual(agents.opening_labels(), sorted(labels))

    def test_syncing_again_changes_only_the_difference(self):
        agents = self.make()
        agents.sync_openings([T1, T2, T3], now=NOW)
        self.rec.calls.clear()
        agents.sync_openings([T1, T3, datetime(2026, 10, 15, 9, 0)], now=NOW)    # T2 is gone, a new time is added
        touched = {c[2].split("/")[-1] if c[1] == "bootout" else Path(c[3]).stem for c in self.rec.calls}
        self.assertEqual(touched, {opening_label(T2), opening_label(datetime(2026, 10, 15, 9, 0))},
                         "T1 and T3 are not touched: only the removed and the added agent")
        self.assertIn(["launchctl", "bootout", f"gui/501/{opening_label(T2)}"], self.rec.calls)
        self.assertEqual(len(self.boots("bootstrap")), 1)
        self.assertIn(opening_label(datetime(2026, 10, 15, 9, 0)), agents.opening_labels())
        self.assertNotIn(opening_label(T2), agents.opening_labels())

    def test_syncing_the_same_times_twice_does_nothing(self):
        agents = self.make()
        agents.sync_openings([T1, T2], now=NOW)
        self.rec.calls.clear()
        agents.sync_openings([T2, T1], now=NOW)
        self.assertEqual(self.rec.calls, [])

    def test_an_agent_whose_time_has_come_is_never_booted_out_by_another_job(self):
        agents = self.make()
        fired = agents.sync_openings([T1], now=NOW)[0]
        self.rec.calls.clear()
        # a job that runs at 08:52 syncs a list without T1: the 08:50 job may still be booking
        agents.sync_openings([T2], now=datetime(2026, 10, 13, 8, 52))
        self.assertIn(fired, agents.opening_labels())
        self.assertNotIn(["launchctl", "bootout", f"gui/501/{fired}"], self.rec.calls)

    def test_the_running_job_is_kept_even_when_its_time_is_not_in_the_list(self):
        agents = self.make()
        running = agents.sync_openings([T2], now=NOW)[0]
        agents.sync_openings([T3], running_label=running, now=NOW)               # not yet fired, but it is the caller
        self.assertIn(running, agents.opening_labels())

    def test_disarm_keeps_agents_whose_time_has_come_but_uninstall_removes_all(self):
        agents = self.make()
        fired = agents.sync_openings([T1], now=NOW)[0]
        agents.sync_openings([T1, T2], now=datetime(2026, 10, 13, 8, 52))
        agents.disarm_openings(now=datetime(2026, 10, 13, 8, 53))
        self.assertEqual(agents.opening_labels(), [fired])
        agents.uninstall()
        self.assertEqual(agents.opening_labels(), [])

    def test_prune_keeps_an_agent_that_fired_less_than_an_hour_ago(self):
        # an opening agent waits up to 20 minutes for the unlock and then runs for up to 20 minutes
        agents = self.make()
        agents.sync_openings([T1], now=NOW)
        agents.prune(datetime(2026, 10, 13, 9, 40))
        self.assertEqual(len(agents.opening_labels()), 1)
        agents.prune(datetime(2026, 10, 13, 10, 5))
        self.assertEqual(agents.opening_labels(), [])

    def test_a_time_that_has_passed_is_not_armed(self):
        agents = self.make()
        agents.sync_openings([datetime(2026, 10, 13, 6, 0), T1], now=NOW)
        self.assertEqual(agents.opening_labels(), [opening_label(T1)])

    # ---- the test agent ----------------------------------------------------------------------------------
    def test_test_agent_runs_a_dry_run_with_a_notification(self):
        agents = self.make()
        agents.arm_test(datetime(2026, 10, 13, 9, 0))
        data = plistlib.loads((self.dir / "com.club-booking.wake-test.plist").read_bytes())
        self.assertEqual(data["ProgramArguments"][-2:], ["--dry-run", "--notify"])
        agents.remove_test()
        self.assertFalse((self.dir / "com.club-booking.wake-test.plist").exists())

    def test_a_finished_test_agent_is_pruned_by_a_later_job_but_not_by_itself(self):
        agents = self.make()
        agents.arm_test(datetime(2026, 10, 13, 9, 0))
        agents.prune(datetime(2026, 10, 13, 8, 55))
        self.assertTrue((self.dir / "com.club-booking.wake-test.plist").exists(), "not yet time")
        agents.prune(datetime(2026, 10, 13, 9, 5), running_label="com.club-booking.wake-test")
        self.assertTrue((self.dir / "com.club-booking.wake-test.plist").exists(), "it is the running job")
        agents.prune(datetime(2026, 10, 13, 9, 5), running_label="com.club-booking.tick")
        self.assertFalse((self.dir / "com.club-booking.wake-test.plist").exists())

    def test_bootstrap_failure_is_an_error(self):
        agents = self.make(fail_bootstrap=True)
        with self.assertRaises(RuntimeError):
            agents.install_tick()

    def test_a_failed_bootstrap_leaves_no_plist_behind(self):
        agents = self.make(fail_bootstrap=True)
        with self.assertRaises(RuntimeError):
            agents.sync_openings([T1], now=NOW)
        self.assertEqual(agents.opening_labels(), [], "a plist left behind would count as armed for ever")
        with self.assertRaises(RuntimeError):
            agents.install_tick()
        self.assertFalse(agents.is_installed())

    def test_uninstall_removes_everything(self):
        agents = self.make()
        agents.install_tick()
        agents.sync_openings([T1, T2], now=NOW)
        agents.uninstall()
        self.assertEqual(list(self.dir.glob("*.plist")), [])

    def test_labels(self):
        when = datetime(2026, 1, 2, 3, 4)
        self.assertEqual(label_time(opening_label(when)), when)
        self.assertIsNone(label_time("com.other.thing"))
        self.assertIsNone(label_time(OPENING_PREFIX + "garbage"))

    # ---- waking the Mac from sleep (needs root: the user runs it) ------------------------------------------
    def test_wake_command_is_two_minutes_early(self):
        self.assertEqual(wake_command(T1), 'sudo pmset schedule wake "10/13/26 08:48:00"')

    def test_one_command_wakes_the_mac_for_every_opening(self):
        text = wake_commands([T3, T1, T1, T2])
        lines = text.splitlines()
        self.assertEqual(lines, ['sudo pmset schedule wake "10/13/26 08:48:00"',
                                 'sudo pmset schedule wake "10/13/26 17:13:00"',
                                 'sudo pmset schedule wake "10/14/26 16:28:00"'])


if __name__ == "__main__":
    unittest.main()
