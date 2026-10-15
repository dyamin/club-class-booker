import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from club_booking import questions, reports
from club_booking.questions import Decision, Question, chosen_option, decide
from club_booking.reports import Reporter
from club_booking.state import Store

TZ = ZoneInfo("Asia/Jerusalem")
DEADLINE = datetime(2026, 10, 14, 9, 50, tzinfo=TZ)


class FakeRemindersOsa:
    """Plays the three question scripts against a list of reminders: dicts with name, body, completed."""

    def __init__(self):
        self.reminders, self.calls = [], []

    def __call__(self, script, argv):
        self.calls.append((script, list(argv)))
        if script == reports._ASK:
            for number, name in enumerate(argv[3:], start=1):
                self.reminders.append({"name": name, "completed": False,
                                       "body": f"[club-booking {argv[1]} #{number}] {argv[2]}"})
            return "ok"
        tag = f"[club-booking {argv[1]} #" if len(argv) > 1 else ""
        if script == reports._ANSWERS:
            return "\n".join(f"{str(r['completed']).lower()}\t{r['body']}" for r in self.reminders if tag in r["body"])
        if script == reports._CLOSE:
            before = len(self.reminders)
            self.reminders = [r for r in self.reminders if tag not in r["body"]]
            return str(before - len(self.reminders))
        raise AssertionError("unknown script")

    def complete(self, qid, number):
        for r in self.reminders:
            if f"[club-booking {qid} #{number}]" in r["body"]:
                r["completed"] = True


class ReporterQuestionTests(unittest.TestCase):
    def reporter(self):
        self.osa = FakeRemindersOsa()
        return Reporter("Club bookings", self.osa)

    def test_each_option_is_one_reminder_with_the_question_in_its_body(self):
        rep = self.reporter()
        rep.ask("q1", "Which class to keep?", ["Keep: Yoga", "Keep: Judo"])
        self.assertEqual([r["name"] for r in self.osa.reminders], ["Keep: Yoga", "Keep: Judo"])
        self.assertTrue(all("Which class to keep?" in r["body"] for r in self.osa.reminders))
        self.assertEqual(self.osa.calls[0][1][:2], ["Club bookings", "q1"], "the list name and the question id are arguments")

    def test_a_question_needs_two_options(self):
        with self.assertRaises(ValueError):
            self.reporter().ask("q1", "?", ["only one"])

    def test_answers_say_which_option_was_completed(self):
        rep = self.reporter()
        rep.ask("q1", "?", ["A", "B", "C"])
        self.assertEqual(rep.answers("q1"), {1: False, 2: False, 3: False})
        self.osa.complete("q1", 2)
        self.assertEqual(rep.answers("q1"), {1: False, 2: True, 3: False})

    def test_another_question_is_not_mixed_in(self):
        rep = self.reporter()
        rep.ask("q1", "?", ["A", "B"])
        rep.ask("q2", "?", ["C", "D"])
        self.osa.complete("q2", 1)
        self.assertEqual(rep.answers("q1"), {1: False, 2: False})

    def test_closing_a_question_removes_only_its_reminders(self):
        rep = self.reporter()
        rep.ask("q1", "?", ["A", "B"])
        rep.ask("q2", "?", ["C", "D"])
        self.assertEqual(rep.close_question("q1"), 2)
        self.assertEqual([r["name"] for r in self.osa.reminders], ["C", "D"])

    def test_a_question_that_is_not_there_has_no_answers(self):
        self.assertEqual(self.reporter().answers("nope"), {})

    def test_the_body_is_turned_into_text_before_a_paragraph_is_taken(self):
        # on the real Reminders, `paragraph 1 of (body of r)` fails with error -1700 (the body is a reference, not text)
        self.assertNotIn("paragraph 1 of (body of r)", reports._ANSWERS)
        self.assertIn("(body of r) as text", reports._ANSWERS)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("osacompile"), "needs macOS osacompile")
    def test_the_applescript_templates_compile(self):
        for name in ("_ASK", "_ANSWERS", "_CLOSE", "_RESOLVE"):
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "t.applescript"
                source.write_text(getattr(reports, name), encoding="utf-8")
                done = subprocess.run(["osacompile", "-o", str(Path(tmp) / "t.scpt"), str(source)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, f"{name}: {done.stderr}")


class DecideTests(unittest.TestCase):
    def several(self):
        return questions.several_question("alex", "Alex", [("alex|a", "Yoga, Wed 07:00"), ("alex|b", "Judo, Wed 18:00")], DEADLINE)

    def test_several_classes_offer_each_one_and_keep_all(self):
        q = self.several()
        self.assertEqual([o.label for o in q.options], ["Keep: Yoga, Wed 07:00", "Keep: Judo, Wed 18:00", "Keep all"])
        self.assertEqual(q.bookings, ["alex|a", "alex|b"])
        self.assertIn("Alex", q.text)

    def test_keeping_one_class_cancels_the_others(self):
        self.assertEqual(decide(self.several(), 1), Decision(cancel=("alex|b",), book=()))
        self.assertEqual(decide(self.several(), 2), Decision(cancel=("alex|a",), book=()))

    def test_keep_all_cancels_nothing(self):
        self.assertEqual(decide(self.several(), 3), Decision(cancel=(), book=()))

    def overlap(self):
        return questions.overlap_question("alex", "Alex", ("alex|a", "Yoga, Wed 07:00"),
                                          [("alex|b", "Judo, Wed 07:15")], DEADLINE)

    def test_an_overlap_keeps_the_default_or_switches_to_the_other(self):
        q = self.overlap()
        self.assertEqual([o.label for o in q.options], ["Keep: Yoga, Wed 07:00", "Switch to: Judo, Wed 07:15"])
        self.assertEqual(q.bookings, ["alex|a"], "only the default was booked")
        self.assertEqual(decide(q, 1), Decision(cancel=(), book=()))
        self.assertEqual(decide(q, 2), Decision(cancel=("alex|a",), book=("alex|b",)))

    def test_there_is_no_waitlist_question(self):
        # the user decided on 2026-10-14: a waitlist spot is always kept, nothing is asked
        self.assertFalse(hasattr(questions, "waitlist_question"))

    def test_nothing_outside_the_questions_own_bookings_can_be_cancelled(self):
        q = self.several()
        q.options[0] = questions.Option("Keep: Yoga, Wed 07:00", keep=())     # even a damaged option
        self.assertEqual(set(decide(q, 1).cancel), {"alex|a", "alex|b"}, "only its own bookings, never others")

    def test_an_answer_is_valid_only_when_exactly_one_option_is_completed(self):
        self.assertEqual(chosen_option({1: False, 2: True, 3: False}), 2)
        self.assertIsNone(chosen_option({1: False, 2: False}))
        self.assertIsNone(chosen_option({1: True, 2: True}), "two taps: ask nothing of the program")
        self.assertIsNone(chosen_option({}))

    def test_a_wrong_option_number_is_an_error(self):
        with self.assertRaises(ValueError):
            decide(self.several(), 9)


def drop_question(person, ident, label):
    """A question with a Keep and a Drop option for one booking of the program."""
    return Question(questions.new_id(), "several", person, f"{label}: keep it?",
                    [questions.Option("Keep", keep=(ident,)), questions.Option("Drop")], [ident], DEADLINE.isoformat())


class QuestionStoreTests(unittest.TestCase):
    def test_a_question_survives_a_round_trip_through_the_store(self):
        store = Store(Path(tempfile.mkdtemp()))
        q = drop_question("sam", "sam|w", "Judo, Wed 17:00")
        store.save_question(q.to_dict())
        again = Question.from_dict(store.questions()[q.id])
        self.assertEqual(again, q)

    def test_updating_a_question_changes_only_its_fields(self):
        store = Store(Path(tempfile.mkdtemp()))
        q = drop_question("sam", "sam|w", "Judo, Wed 17:00")
        store.save_question(q.to_dict())
        store.update_question(q.id, status="decided", answer=2)
        got = store.questions()[q.id]
        self.assertEqual((got["status"], got["answer"], got["person"]), ("decided", 2, "sam"))

    def test_only_unfinished_questions_are_open(self):
        store = Store(Path(tempfile.mkdtemp()))
        a = drop_question("sam", "sam|w", "Judo")
        b = drop_question("robin", "robin|w", "Judo")
        for q in (a, b):
            store.save_question(q.to_dict())
        store.update_question(b.id, status="done")
        self.assertEqual([q["id"] for q in store.open_questions()], [a.id])


if __name__ == "__main__":
    unittest.main()
