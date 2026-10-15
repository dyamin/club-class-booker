import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from club_booking.models import CardState, ClassCard
from club_booking.planner import (Opening, Plan, due, next_opening, openings_from_cards,
                                  persons_in, plan_is_stale)
from club_booking.state import LockBusy, Store

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)


def card(name, day, hh, mm, state):
    start = datetime(2026, 10, day, hh, mm, tzinfo=TZ)
    return ClassCard(name, start, start + timedelta(minutes=45), state)


class PlannerTests(unittest.TestCase):
    def test_only_closed_cards_within_the_horizon_become_openings(self):
        cards = [
            card("A", 13, 8, 50, CardState.OPEN),          # already open: not an opening
            card("B", 14, 6, 45, CardState.NOT_OPEN),
            card("C", 13, 18, 30, CardState.NOT_OPEN),
            card("D", 30, 6, 0, CardState.NOT_OPEN),       # beyond the horizon
            card("E", 12, 9, 0, CardState.NOT_OPEN),       # already started
        ]
        found = openings_from_cards("sam", cards, NOW, horizon_days=7)
        self.assertEqual([o.name for o in found], ["C", "B"])  # sorted by opening time
        self.assertEqual(found[0].opens_at, datetime(2026, 10, 12, 18, 30, tzinfo=TZ))

    def test_due_and_next_opening_use_all_people(self):
        a = openings_from_cards("alex", [card("A", 13, 17, 0, CardState.NOT_OPEN)], NOW, 7)
        b = openings_from_cards("robin", [card("B", 13, 19, 0, CardState.NOT_OPEN)], NOW, 7)
        c = openings_from_cards("sam", [card("C", 14, 9, 0, CardState.NOT_OPEN)], NOW, 7)
        everything = a + b + c
        self.assertEqual(due(everything, NOW, set()), a)          # A opened at 17:00 today
        nxt = next_opening(everything, NOW, set(x.ident for x in a))
        self.assertEqual((nxt.person, nxt.name), ("robin", "B"))   # the earliest for any of the three
        self.assertEqual(persons_in(everything), ["alex", "robin", "sam"])
        self.assertIsNone(next_opening(everything, NOW + timedelta(days=5), set()))

    def test_openings_of_classes_that_already_started_are_not_due(self):
        started = Opening("sam", "k", "Old", NOW - timedelta(hours=1), NOW - timedelta(hours=25))
        self.assertEqual(due([started], NOW, set()), [])

    def test_handled_openings_are_skipped(self):
        a = openings_from_cards("alex", [card("A", 13, 17, 0, CardState.NOT_OPEN)], NOW, 7)
        self.assertEqual(due(a, NOW, {a[0].ident}), [])

    def test_staleness(self):
        plan = Plan(NOW, ())
        self.assertTrue(plan_is_stale(None, NOW, 20))
        self.assertFalse(plan_is_stale(plan, NOW + timedelta(hours=19), 20))
        self.assertTrue(plan_is_stale(plan, NOW + timedelta(hours=21), 20))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()))

    def test_plan_round_trip(self):
        openings = tuple(openings_from_cards("sam", [card("C", 14, 9, 0, CardState.NOT_OPEN)], NOW, 7))
        self.store.save_plan(Plan(NOW, openings))
        loaded = self.store.load_plan()
        self.assertEqual(loaded.created_at, NOW)
        self.assertEqual(loaded.openings, openings)

    def test_no_plan_yet(self):
        self.assertIsNone(self.store.load_plan())

    def test_handled_and_attempts(self):
        self.assertEqual(self.store.handled(), set())
        self.store.mark_handled("sam|x")
        self.assertEqual(self.store.handled(), {"sam|x"})
        self.assertEqual(self.store.bump_attempt("sam|x"), 1)
        self.assertEqual(self.store.bump_attempt("sam|x"), 2)

    def test_the_classes_the_calendar_audit_handled_can_be_read_and_forgotten(self):
        self.store.mark_calendar_done("sam|2026-10-15T16:45|judo", "2026-10-15T16:45", NOW)
        self.assertEqual(self.store.calendar_done_starts(), {"sam|2026-10-15T16:45|judo": "2026-10-15T16:45"})
        self.assertEqual(self.store.calendar_done(), {"sam|2026-10-15T16:45|judo"})
        self.store.unmark_calendar_done("sam|2026-10-15T16:45|judo")
        self.store.unmark_calendar_done("sam|never-there")
        self.assertEqual(self.store.calendar_done(), set())

    def test_the_first_time_a_class_was_missing_is_kept(self):
        self.store.mark_calendar_dropped("sam|x", NOW)
        self.store.mark_calendar_dropped("sam|x", NOW + timedelta(hours=1))
        self.assertEqual(self.store.calendar_dropped(), {"sam|x": NOW.isoformat()})
        self.store.clear_calendar_dropped("sam|x")
        self.store.clear_calendar_dropped("sam|y")
        self.assertEqual(self.store.calendar_dropped(), {})

    def test_an_attempt_counter_can_be_cleared(self):
        self.store.bump_attempt("calendar|x")
        self.store.clear_attempt("calendar|x")
        self.store.clear_attempt("calendar|never-there")
        self.assertEqual(self.store.attempt_count("calendar|x"), 0)

    def test_messages_the_program_may_take_back(self):
        self.store.remember_message("m1", "problem", ["alex"], "report|20261012|m1", NOW)
        self.assertEqual(self.store.messages()["m1"]["persons"], ["alex"])
        self.assertEqual(self.store.messages()["m1"]["ident"], "report|20261012|m1")
        self.store.forget_message("m1")
        self.store.forget_message("never-there")
        self.assertEqual(self.store.messages(), {})

    def test_old_messages_are_forgotten_when_a_new_one_is_remembered(self):
        # the reminders themselves are pruned after two days
        self.store.remember_message("old", "problem", ["alex"], "", NOW - timedelta(days=4))
        self.store.remember_message("new", "locked", ["sam"], "", NOW)
        self.assertEqual(set(self.store.messages()), {"new"})

    def test_bookings_and_log(self):
        self.store.record_booking("run1", "alex", "2026-10-13T08:50|metcon", "METCON")
        self.assertEqual(self.store.booked_idents(), {"alex|2026-10-13T08:50|metcon"})
        self.store.log("booked", person="alex")
        lines = (self.store.home / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"event": "booked"', lines[0])

    def test_evidence_and_pause(self):
        path = self.store.save_evidence("dialog: METCON/1", b"png-bytes")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"png-bytes")
        self.assertFalse(self.store.paused())
        (self.store.home / "PAUSE").write_text("")
        self.assertTrue(self.store.paused())

    def test_a_corrupt_state_file_is_set_aside_and_treated_as_empty(self):
        (self.store.home / "plan.json").write_text("{not json", encoding="utf-8")
        (self.store.home / "handled.json").write_text("[", encoding="utf-8")
        self.assertIsNone(self.store.load_plan())
        self.assertEqual(self.store.handled(), set())
        self.assertTrue(list(self.store.home.glob("plan.json.bad-*")))
        self.assertIn("state_file_unreadable", (self.store.home / "runs.jsonl").read_text(encoding="utf-8"))

    def test_a_bad_line_in_the_bookings_file_is_skipped(self):
        self.store.record_booking("r", "alex", "k1", "A")
        with (self.store.home / "bookings.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        self.store.record_booking("r", "alex", "k2", "B")
        self.assertEqual(self.store.booked_idents(), {"alex|k1", "alex|k2"})

    def test_attempts_can_be_read(self):
        self.assertEqual(self.store.attempt_count("x"), 0)
        self.store.bump_attempt("x")
        self.assertEqual(self.store.attempt_count("x"), 1)

    def test_classes_seen_registered_are_remembered(self):
        self.assertEqual(self.store.seen_registered(), set())
        self.store.mark_seen_registered("sam|k")
        self.assertEqual(self.store.seen_registered(), {"sam|k"})

    def test_an_attempt_before_the_tap_counts_as_booked(self):
        self.store.record_booking("r", "alex", "k1", "A", status="attempted")
        self.assertEqual(self.store.booked_idents(), {"alex|k1"})
        self.assertEqual(self.store.bookings()[0]["status"], "attempted")

    def test_two_writers_never_share_a_temp_file(self):
        seen, real = [], os.replace

        def spy(src, dst):
            seen.append(str(src))
            real(src, dst)

        with mock.patch("club_booking.state.os.replace", side_effect=spy):
            self.store._write_json("a.json", 1)
            self.store._write_json("a.json", 2)
        self.assertEqual(len(set(seen)), 2)
        self.assertEqual(list(self.store.home.glob("*.tmp")), [])

    def test_counters_and_marks_survive_many_writers_at_once(self):
        def work(n):
            for i in range(40):
                self.store.bump_attempt("k")
                self.store.mark_handled(f"p|{n}-{i}")
                self.store.mark_seen_registered(f"p|{n}-{i}")

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.attempt_count("k"), 160)
        self.assertEqual(len(self.store.handled()), 160)
        self.assertEqual(len(self.store.seen_registered()), 160)

    def test_old_evidence_is_removed_and_new_evidence_is_kept(self):
        old, new = self.store.save_evidence("a", b"x"), self.store.save_evidence("b", b"y")
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        self.assertEqual(self.store.prune_evidence(30), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_a_big_log_is_rotated(self):
        self.store.max_log_bytes = 300
        for i in range(30):
            self.store.log("event", number=i)
        self.assertTrue((self.store.home / "runs.jsonl.1").exists())
        self.assertIn('"number": 29', (self.store.home / "runs.jsonl").read_text())
        self.assertLess((self.store.home / "runs.jsonl").stat().st_size, 1000)

    def test_refused_classes_are_remembered_with_the_reason(self):
        self.assertEqual(self.store.refused(), {})
        self.store.mark_refused("robin|2026-10-15T17:30|judo", "הגיל אינו מתאים")
        got = self.store.refused()
        self.assertEqual(list(got), ["robin|2026-10-15T17:30|judo"])
        self.assertEqual(got["robin|2026-10-15T17:30|judo"]["message"], "הגיל אינו מתאים")
        self.assertIn("ts", got["robin|2026-10-15T17:30|judo"])

    def test_run_lock_is_exclusive(self):
        with self.store.run_lock():
            with self.assertRaises(LockBusy):
                with Store(self.store.home).run_lock():
                    pass
        with self.store.run_lock():   # free again afterwards
            pass


if __name__ == "__main__":
    unittest.main()
