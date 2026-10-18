import json
import os
import contextlib
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from club_booking import calendar_audit
from club_booking import questions as questions_module
from club_booking.cli import RETRY_SOON, App, TickDecision, decide_tick, local_naive, parser
from club_booking.login import LoginBlocked, LoginError
from club_booking.models import CardState
from club_booking.planner import Opening, Plan
from club_booking.osa import AppleScriptError
from club_booking.runner import USER_ACTIVE, Outcome, PersonResult, RunResult
from club_booking.scheduler import TEST_LABEL
from club_booking.screen import ScreenError
from club_booking.secrets import SecretError
from club_booking.state import Store

from tests.fake_app import (FakeApp, FakeAxVision, FakeCalendar, FakeClubApp, FakeVision, TZ, card, make_cfg,
                            sample_favorites)

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)
OPEN, NOT_OPEN = CardState.OPEN, CardState.NOT_OPEN


def opening(person, name, opens_at):
    start = opens_at + timedelta(hours=24)
    return Opening(person, f"{start:%Y-%m-%dT%H:%M}|{name}", name, start, opens_at)


class DecideTickTests(unittest.TestCase):
    def plan(self, *openings, created=NOW):
        return Plan(created, tuple(openings))

    def test_nothing_to_do(self):
        plan = self.plan(opening("sam", "A", NOW + timedelta(hours=3)))
        self.assertEqual(decide_tick(plan, set(), NOW, False, 999, 20), TickDecision("nothing"))

    def test_due_openings_for_several_people_in_order(self):
        a, b = opening("sam", "A", NOW - timedelta(minutes=1)), opening("alex", "B", NOW - timedelta(minutes=5))
        d = decide_tick(self.plan(a, b), set(), NOW, False, 0, 20)
        self.assertEqual((d.action, d.persons), ("run_due", ("sam", "alex")))

    def test_the_grace_period_catches_a_wake_up_a_few_seconds_early(self):
        plan = self.plan(opening("sam", "A", NOW + timedelta(seconds=20)))
        self.assertEqual(decide_tick(plan, set(), NOW, False, 0, 20).action, "run_due")
        late = self.plan(opening("sam", "A", NOW + timedelta(seconds=40)))
        self.assertEqual(decide_tick(late, set(), NOW, False, 0, 20).action, "nothing")

    def test_handled_openings_are_ignored(self):
        o = opening("sam", "A", NOW - timedelta(minutes=1))
        self.assertEqual(decide_tick(self.plan(o), {o.ident}, NOW, False, 0, 20).action, "nothing")

    def test_a_locked_mac_with_something_due(self):
        o = opening("sam", "A", NOW - timedelta(minutes=1))
        d = decide_tick(self.plan(o), set(), NOW, True, 0, 20)
        self.assertEqual((d.action, d.persons), ("locked", ("sam",)))

    def test_a_plan_that_stays_stale_is_rebuilt_even_if_the_user_never_leaves(self):
        very_old = self.plan(created=NOW - timedelta(hours=29))
        self.assertEqual(decide_tick(very_old, set(), NOW, False, 5, 20, force_hours=28).action, "replan")
        old = self.plan(created=NOW - timedelta(hours=21))
        self.assertEqual(decide_tick(old, set(), NOW, False, 5, 20, force_hours=28).action, "wait_idle")
        self.assertEqual(decide_tick(very_old, set(), NOW, True, 5, 20, force_hours=28).action, "nothing",
                         "a locked Mac still waits")

    def test_stale_plan(self):
        old = self.plan(created=NOW - timedelta(hours=21))
        self.assertEqual(decide_tick(old, set(), NOW, False, 700, 20).action, "replan", "away for more than 10 minutes")
        self.assertEqual(decide_tick(old, set(), NOW, False, 500, 20).action, "wait_idle")
        self.assertEqual(decide_tick(old, set(), NOW, False, 5, 20).action, "wait_idle")
        self.assertEqual(decide_tick(old, set(), NOW, True, 700, 20).action, "nothing")
        self.assertEqual(decide_tick(None, set(), NOW, False, 700, 20).action, "replan")


def drop_question(person, ident, label, deadline):
    """A question with a Keep and a Drop option for one booking of the program."""
    return questions_module.Question(questions_module.new_id(), "several", person, f"{label}: keep it?",
                                     [questions_module.Option("Keep", keep=(ident,)), questions_module.Option("Drop")],
                                     [ident], deadline.isoformat())


LOGGED_OUT_TEXT = ("the club app is logged out. Open it and log in with your phone number "
                    "(the code comes by SMS), then run again")


class FakeReporter:
    def __init__(self):
        self.tells, self.macs, self.pruned = [], [], []
        self.tags, self.resolved, self.resolve_error = [], [], None
        self.asked, self.closed, self.answer_map, self.ask_error = [], [], {}, None

    def ask(self, qid, text, options):
        if self.ask_error:
            raise self.ask_error
        self.asked.append((qid, text, list(options)))

    def answers(self, qid):
        return dict(self.answer_map.get(qid, {}))

    def close_question(self, qid):
        self.closed.append(qid)
        return 2

    def tell(self, title, body="", tag=""):
        self.tells.append((title, body))
        if tag:
            self.tags.append((title, tag))
        return []

    def resolve(self, tag):
        if self.resolve_error:
            raise self.resolve_error
        self.resolved.append(tag)
        return 1

    def mac(self, title, text):
        self.macs.append((title, text))

    def phone(self, title, body=""):
        self.tells.append((title, body))

    def prune(self, days=7):
        self.pruned.append(days)
        return 2


class FakeAgents:
    def __init__(self, installed=True):
        self.armed, self.disarmed, self.pruned, self.tests, self.removed = [], 0, 0, [], 0
        self.nows = []      # the time the program gave the scheduler
        self.synced = []    # every list of times the program asked to arm
        self.installed = installed

    def is_installed(self):
        return self.installed

    def sync_openings(self, times, running_label=None, now=None):
        self.synced.append(sorted(times))          # what the program asked to be armed, each time
        self.armed.extend(sorted(times))
        self.nows.append(now)

    def disarm_openings(self, running_label=None, now=None):
        self.disarmed += 1
        self.nows.append(now)

    def prune(self, now, running_label=None):
        self.pruned += 1

    def opening_labels(self):
        return ["com.club-booking.opening.x"] if self.armed else []

    def arm_test(self, when):
        self.tests.append(when)

    def remove_test(self):
        self.removed += 1


class ScreenCM:
    def __init__(self, app, error=None):
        self.app, self.error, self.used = app, error, 0

    def __enter__(self):
        self.used += 1
        if self.error:
            raise self.error
        return self.app

    def __exit__(self, *exc):
        return False


class FakeAudit:
    """Stands in for CalendarAudit: records what the run asked, and answers with the findings it was given."""

    def __init__(self, agents, findings=(), error=None):
        self.agents, self.findings, self.error = agents, list(findings), error
        self.calls, self.armed_when_called, self.order = [], [], []
        self.removal_calls, self.removal_findings, self.removal_error = [], [], None

    def remove_dropped(self, entries):
        self.order.append("remove")
        self.removal_calls.append([(p.key, [c.key for c in cards]) for p, cards in entries])
        if self.removal_error:
            raise self.removal_error
        return self.removal_findings

    def check_all(self, entries, horizon_days):
        self.order.append("add")
        self.calls.append(([(p.key, [c.key for c in cards]) for p, cards in entries], horizon_days))
        self.armed_when_called.append(len(self.agents.armed))
        if self.error:
            raise self.error
        return self.findings


class AppCase(unittest.TestCase):
    def build(self, favs=None, now=NOW, locked=False, idle=999.0, screen_error=None, vision_cls=FakeVision, app_kw=None,
              running_label=None):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp, countdown_seconds=0)
        self.fake = FakeApp(self.cfg, favs if favs is not None else sample_favorites(), **(app_kw or {}))
        self.vision = vision_cls(self.fake)
        self.calendar = FakeCalendar()
        self.store = Store(self.tmp)
        self.reporter, self.agents = FakeReporter(), FakeAgents()
        self.screen = ScreenCM(self.fake, screen_error)
        self.lines = []
        self.state = {"now": now, "locked": locked, "idle": idle}
        self.club_app = FakeClubApp(self.fake)
        self.audit_modes, self.audit = [], FakeAudit(self.agents)
        self.wake_calls, self.wake_result = [], False
        self.awake_depth, self.awake_log = 0, []
        self.sleeps, self.on_sleep = [], None
        self.app = App(self.cfg, self.store, self.reporter, self.agents, lambda: self.screen, lambda: self.vision,
                       lambda s: self.calendar, now_fn=lambda: self.state["now"], sleep=self._sleep,
                       locked_fn=lambda: self.state["locked"], idle_fn=lambda: self.state["idle"],
                       out=self.lines.append, club_app_factory=lambda: self.club_app,
                       audit_factory=self._audit_for, wake_fn=self._wake, running_label=running_label,
                       awake_fn=self._awake)
        return self.app

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))

    @contextlib.contextmanager
    def _awake(self):
        """The display hold: records when it is on, so a test can see what happened while it was held."""
        self.awake_depth += 1
        self.awake_log.append("on")
        try:
            yield
        finally:
            self.awake_depth -= 1
            self.awake_log.append("off")

    def _wake(self):
        """The display wake-up: it works only when a test says so (then the screen is usable)."""
        self.wake_calls.append(1)
        if self.wake_result:
            self.state["locked"] = False
        return self.wake_result

    def _audit_for(self, mode):
        self.audit_modes.append(mode)
        return self.audit


class QuestionTests(AppCase):
    """After a run the program asks on the iPhone: several classes in a day, overlapping classes (never a waitlist spot)."""

    def setUp(self):
        self.later = NOW + timedelta(days=2)            # a day in the future

    def on(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, questions="on")

    def start(self, hour, minute=0):
        return self.later.replace(hour=hour, minute=minute)

    def key(self, hour, name, minute=0):
        return f"{self.start(hour, minute):%Y-%m-%dT%H:%M}|{name.lower()}"

    def waitlisted(self, person="sam", hour=17, name="Judo"):
        o = Outcome(person, name, self.start(hour), "waitlisted", card_key=self.key(hour, name))
        return RunResult("r", [PersonResult(person, outcomes=[o])], None, [])

    def book(self, person, hour, name):
        self.store.record_booking("r", person, self.key(hour, name), name, status="confirmed")

    def test_a_waitlist_spot_is_never_asked_about(self):
        # the user decided on 2026-10-14: always keep the spot, and no reminder for it
        self.on()
        self.app._ask_questions(self.waitlisted(), dry_run=False)
        self.assertEqual((self.reporter.asked, self.store.open_questions(), self.reporter.tells), ([], [], []))

    def test_nothing_is_asked_when_questions_are_off_or_it_is_a_dry_run(self):
        self.build()
        self.book("alex", 7, "Yoga")
        self.book("alex", 7, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=True)
        self.assertEqual((self.reporter.asked, self.store.open_questions()), ([], []))

    def test_two_classes_on_one_day_for_a_person_who_asks_make_one_question(self):
        self.on()
        self.book("alex", 7, "Yoga")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(self.reporter.asked, [], "one class is nothing to ask about")
        self.book("alex", 18, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        [(qid, text, options)] = self.reporter.asked
        self.assertEqual(len(options), 3)
        self.assertTrue(options[0].startswith("Keep: Yoga") and options[2] == "Keep all")

    def with_son_asking_about_overlaps(self):
        self.on()
        people = tuple(replace(p, asks_overlaps=True) if p.key == "sam" else p for p in self.app.cfg.people)
        self.app.cfg = replace(self.app.cfg, people=people)

    def test_an_overlap_of_a_son_who_asks_about_overlaps_is_a_question(self):
        self.with_son_asking_about_overlaps()
        a = card("Yoga", self.later.day, 7, 0, CardState.OPEN, month=self.later.month)
        b = card("Judo", self.later.day, 7, 15, CardState.OPEN, month=self.later.month)
        self.book("sam", 7, "Yoga")
        result = RunResult("r", [PersonResult("sam", overlaps=[(a, [b], False)],
                                              outcomes=[Outcome("sam", "Yoga", a.start, "booked", card_key=a.key)])], None, [])
        self.app._ask_questions(result, dry_run=False)
        [(qid, text, options)] = self.reporter.asked
        self.assertTrue(options[0].startswith("Keep: Yoga") and options[1].startswith("Switch to: Judo"), options)
        self.assertEqual(self.store.questions()[qid]["person"], "sam")

    def test_a_son_who_asks_about_overlaps_is_not_asked_about_several_classes_in_a_day(self):
        self.with_son_asking_about_overlaps()
        self.book("sam", 7, "Yoga")
        self.book("sam", 18, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(self.reporter.asked, [], "the sons have many classes a day: that is no decision")

    def test_several_classes_in_a_day_are_not_asked_about_when_it_is_switched_off(self):
        # 2026-10-15: "send me only if it needs my decision": two classes on different hours do not conflict
        self.on()
        self.app.cfg = replace(self.app.cfg, questions_several=False)
        self.book("alex", 7, "Yoga")
        self.book("alex", 18, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(self.reporter.asked, [])

    def test_an_open_several_question_is_closed_when_it_is_switched_off(self):
        self.on()
        self.book("alex", 7, "Yoga")
        self.book("alex", 18, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        [(qid, _, _)] = self.reporter.asked
        self.app.cfg = replace(self.app.cfg, questions_several=False)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.store.questions()[qid]["status"], "done")
        self.assertEqual(self.reporter.closed, [qid], "its reminders are deleted from the phone")

    def test_a_third_class_replaces_the_open_question(self):
        self.on()
        self.book("alex", 7, "Yoga")
        self.book("alex", 18, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        first = self.reporter.asked[0][0]
        self.book("alex", 20, "Spin")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(len(self.reporter.asked), 2)
        self.assertEqual(len(self.reporter.asked[1][2]), 4)
        self.assertEqual(self.reporter.closed, [first], "the old question is taken away")
        self.assertEqual([q["status"] for q in self.store.questions().values() if q["id"] == first], ["expired"])

    def test_the_sons_are_not_asked_about_several_classes(self):
        self.on()
        self.book("sam", 7, "Yoga")
        self.book("sam", 18, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(self.reporter.asked, [])

    def test_an_overlap_is_asked_when_the_default_is_booked(self):
        self.on()
        self.book("alex", 7, "Yoga")
        yoga = card("Yoga", self.later.day, 7, 0, CardState.REGISTERED, month=self.later.month)
        judo = card("Judo", self.later.day, 7, 15, CardState.OPEN, month=self.later.month)
        booked = Outcome("alex", "Yoga", yoga.start, "booked", card_key=yoga.key)
        result = RunResult("r", [PersonResult("alex", outcomes=[booked], overlaps=[(yoga, [judo], False)])], None, [])
        self.app._ask_questions(result, dry_run=False)
        self.app._ask_questions(result, dry_run=False)
        [(qid, text, options)] = self.reporter.asked
        self.assertTrue(options[0].startswith("Keep: Yoga") and options[1].startswith("Switch to: Judo"))
        saved = self.store.questions()[qid]
        self.assertEqual((saved["kind"], saved["bookings"]), ("overlap", [f"alex|{yoga.key}"]))

    def test_an_overlap_with_a_class_the_user_registered_is_no_question_and_no_message(self):
        self.on()
        yoga = card("Yoga", self.later.day, 7, 0, CardState.REGISTERED, month=self.later.month)
        judo = card("Judo", self.later.day, 7, 15, CardState.OPEN, month=self.later.month)
        result = RunResult("r", [PersonResult("alex", overlaps=[(yoga, [judo], True)])], None, [])
        self.app._ask_questions(result, dry_run=False)
        self.assertEqual(self.reporter.asked, [], "the program cannot cancel a class it did not book")
        self.assertEqual(self.reporter.tells, [], "and it says nothing on its own: there is nothing to decide")

    def test_an_overlap_with_nothing_left_to_switch_to_is_not_a_question(self):
        self.on()
        self.book("alex", 7, "Yoga")
        yoga = card("Yoga", self.later.day, 7, 0, CardState.REGISTERED, month=self.later.month)
        result = RunResult("r", [PersonResult("alex", overlaps=[(yoga, [], True)])], None, [])
        self.app._ask_questions(result, dry_run=False)
        self.assertEqual((self.reporter.asked, self.store.open_questions()), ([], []))

    def test_an_overlap_whose_default_was_not_booked_is_not_asked_about(self):
        self.on()
        yoga = card("Yoga", self.later.day, 7, 0, CardState.OPEN, month=self.later.month)
        judo = card("Judo", self.later.day, 7, 15, CardState.OPEN, month=self.later.month)
        result = RunResult("r", [PersonResult("alex", overlaps=[(yoga, [judo], False)])], None, [])
        self.app._ask_questions(result, dry_run=False)
        self.assertEqual(self.reporter.asked, [])

    def test_an_answered_overlap_is_not_asked_again_at_the_next_run(self):
        self.on()
        self.book("alex", 7, "Yoga")
        yoga = card("Yoga", self.later.day, 7, 0, CardState.REGISTERED, month=self.later.month)
        judo = card("Judo", self.later.day, 7, 15, CardState.OPEN, month=self.later.month)
        result = RunResult("r", [PersonResult("alex", overlaps=[(yoga, [judo], True)])], None, [])
        self.app._ask_questions(result, dry_run=False)
        qid = self.reporter.asked[0][0]
        self.store.update_question(qid, status="done")
        self.app._ask_questions(result, dry_run=False)
        self.assertEqual(len(self.reporter.asked), 1)

    def test_a_question_that_could_not_be_sent_is_sent_at_the_next_try(self):
        self.on()
        self.reporter.ask_error = AppleScriptError("Reminders is not allowed")
        self.book("alex", 7, "Yoga")
        self.book("alex", 7, "Judo")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        [saved] = self.store.open_questions()
        self.assertFalse(saved["asked"])
        self.reporter.ask_error = None
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(len(self.reporter.asked), 1)
        self.assertTrue(self.store.open_questions()[0]["asked"])

    def test_the_sons_overlapping_classes_are_no_message(self):
        self.on()
        a = card("Yoga", self.later.day, 7, 0, CardState.REGISTERED, month=self.later.month)
        b = card("Judo", self.later.day, 7, 15, CardState.REGISTERED, month=self.later.month)
        result = RunResult("r", [PersonResult("sam", booked=[a, b])], None, [])
        self.app._ask_questions(result, dry_run=False)
        self.assertEqual((self.reporter.asked, self.reporter.tells), ([], []), "both are booked: nothing to decide")

    def test_a_run_with_the_sons_overlapping_classes_sends_no_message_at_all(self):
        OPEN = CardState.OPEN
        favs = {"alex": [], "sam": [card("Yoga", 14, 7, 0, OPEN), card("Judo", 14, 7, 15, OPEN)], "robin": []}
        self.build(favs)
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(self.reporter.tells, [], "two bookings and an overlap: nothing needs the user")

    def test_a_run_joins_the_waitlist_and_says_and_asks_nothing(self):
        # the user decided on 2026-10-14: a waitlist spot is always kept, with no question and no message
        self.build({"alex": [], "sam": [card("Judo", 14, 17, 0, CardState.FULL)], "robin": []}, vision_cls=FakeAxVision)
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(self.fake.waitlist_taps, [("sam", "2026-10-14T17:00|judo")], "the spot was joined")
        self.assertEqual((self.reporter.asked, self.reporter.tells), ([], []))
        rows = [json.loads(line) for line in (self.tmp / "bookings.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["status"], "waitlisted", "it is in the log")


class OverlapRefusalTests(AppCase):
    """2026-10-15: Sam's chess and football (both Fri 13:15). The club refused the second ("לא ניתן להירשם לשני שיעורים
    חופפים"). The user decides which to keep, through a question, and the program carries the answer out by itself."""

    def world(self, now=NOW, **app_kw):
        OPEN = CardState.OPEN
        favs = {"alex": [], "robin": [], "sam": [card("Chess", 14, 13, 15, OPEN), card("Football", 14, 13, 15, OPEN)]}
        self.build(favs, now=now, vision_cls=FakeAxVision, app_kw={"overlap_rule": True, **app_kw})
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)

    def sam(self):
        return {c.name: c.state for c in self.fake.cards["sam"]}

    def test_the_club_books_the_first_class_and_refuses_the_overlapping_one(self):
        self.world()
        self.assertEqual(self.sam(), {"Chess": CardState.REGISTERED, "Football": CardState.OPEN})
        self.assertIn("שיעורים חופפים", self.store.refused()["sam|2026-10-14T13:15|football"]["message"])

    def test_the_refusal_becomes_one_question_with_keep_and_switch(self):
        self.world()
        [(qid, text, options)] = self.reporter.asked
        self.assertEqual([o.split(":")[0] for o in options], ["Keep", "Switch to"])
        self.assertTrue("Chess" in options[0] and "Football" in options[1], options)
        self.assertEqual(self.store.questions()[qid]["person"], "sam")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(len(self.reporter.asked), 1, "asked once")

    def test_a_refusal_for_another_reason_is_no_question(self):
        OPEN = CardState.OPEN
        favs = {"alex": [], "robin": [], "sam": [card("Chess", 14, 13, 15, OPEN), card("Football", 14, 13, 15, OPEN)]}
        self.build(favs, vision_cls=FakeAxVision, app_kw={"refuse": ("Football",)})          # they overlap, but the reason is the age
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(self.reporter.asked, [])

    def answer(self, number):
        [(self.qid, _, _)] = self.reporter.asked
        self.reporter.answer_map[self.qid] = {n: n == number for n in (1, 2)}

    def test_switch_cancels_chess_then_books_football_and_says_nothing(self):
        self.world()
        self.answer(2)
        tells = len(self.reporter.tells)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.sam(), {"Chess": CardState.OPEN, "Football": CardState.REGISTERED})
        self.assertEqual(self.fake.cancel_presses, [("sam", "2026-10-14T13:15|chess", "ביטול")])
        events = self.fake.events
        self.assertLess(events.index("cancel"), len(events) - 1 - events[::-1].index("book"), "cancel first: the club allows only one")
        self.assertNotIn("sam|2026-10-14T13:15|football", self.store.refused(), "the old refusal is over")
        self.assertEqual(self.reporter.tells[tells:], [], "the user's own answer is no news")
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")

    def test_after_a_switch_the_calendar_is_brought_up_to_date_by_the_program(self):
        # 2026-10-15 16:10: the switch worked, but the chess event stayed and the football event was missing: the program
        # does a pass over the calendar right after it carries out an answer (and the class it cancelled itself is
        # not waited for: no second reading 30 minutes later)
        self.world()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        self.answer(2)
        self.app._process_answers(self.app.now_fn())
        self.assertGreaterEqual(len(self.audit.calls), 1, "the audit ran after the answer")
        chess = "sam|2026-10-14T13:15|chess"
        first = datetime.fromisoformat(self.store.calendar_dropped()[chess])
        self.assertGreaterEqual((self.app.now_fn() - first).total_seconds(), 30 * 60, "already past the waiting time")

    def test_no_calendar_pass_when_the_calendar_check_is_off(self):
        self.world()
        self.answer(2)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.audit.calls, [])

    def test_keep_changes_nothing(self):
        self.world()
        self.answer(1)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.sam(), {"Chess": CardState.REGISTERED, "Football": CardState.OPEN})
        self.assertEqual(self.fake.cancel_presses, [])

    def test_a_chosen_class_that_is_full_cancels_nothing_and_tells_the_user(self):
        self.world()
        self.fake.cards["sam"][1] = card("Football", 14, 13, 15, CardState.FULL)
        self.answer(2)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.sam()["Chess"], CardState.REGISTERED)
        self.assertEqual(self.fake.cancel_presses, [], "never give up a class for one that cannot be booked")
        self.assertTrue(any("nothing was changed" in body for _, body in self.reporter.tells))

    def test_a_chosen_class_that_cannot_be_booked_after_the_cancel_is_reported_loudly(self):
        self.world()
        self.answer(2)
        self.fake.before_button_tap = lambda key, c: (_ for _ in ()).throw(RuntimeError("the screen froze"))
        self.app._process_answers(self.app.now_fn())
        bodies = " ".join(b for _, b in self.reporter.tells)
        self.assertIn("by hand", bodies)


class AnswerWakeTests(AppCase):
    """The reminders are two-way, but the program wakes only for openings. While a decision is open it also wakes a few times to
    read the answer and carry it out, so no Claude session is needed (the user's goal, 2026-10-15)."""

    world, sam, answer = OverlapRefusalTests.world, OverlapRefusalTests.sam, OverlapRefusalTests.answer

    def armed(self):
        return self.agents.synced[-1]

    def test_while_a_question_is_open_the_program_wakes_10_minutes_later_and_at_fixed_hours(self):
        self.world(now=datetime(2026, 10, 13, 15, 0, tzinfo=TZ))
        asked_at = self.state["now"]
        times = [t for t in self.armed()]
        self.assertIn(local_naive(asked_at + timedelta(minutes=10)), times)
        for hour in ((13, 20), (14, 8), (14, 10), (14, 12)):                # the evening, then 8, 10, 12 on the class day
            self.assertIn(datetime(2026, 10, *hour[:1], hour[1]), times, hour)
        self.assertNotIn(datetime(2026, 10, 14, 20), times, "not after the class has started")

    def test_no_wake_up_is_armed_for_a_question_that_is_done(self):
        self.world(now=datetime(2026, 10, 13, 15, 0, tzinfo=TZ))
        self.answer(1)
        self.app._process_answers(self.app.now_fn())
        syncs, disarmed = len(self.agents.synced), self.agents.disarmed
        self.app.rearm()
        self.assertEqual((len(self.agents.synced), self.agents.disarmed), (syncs, disarmed + 1),
                         "nothing is armed any more: no question is open")

    def test_an_answer_near_the_class_is_carried_out_even_when_the_user_is_at_the_mac(self):
        self.world(now=datetime(2026, 10, 14, 11, 0, tzinfo=TZ))
        self.answer(2)
        self.state["idle"] = 5.0
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.sam()["Football"], CardState.REGISTERED)

    def test_an_answer_is_read_even_when_the_plan_is_stale_and_the_user_is_at_the_mac(self):
        # 2026-10-15 16:03: the first answer wake-up logged "wait_idle" (a stale plan, the user at the Mac) and read nothing
        from unittest import mock
        from club_booking.cli import TickDecision
        self.world(now=datetime(2026, 10, 13, 15, 0, tzinfo=TZ))
        self.answer(2)
        self.state["idle"] = 5.0
        with mock.patch("club_booking.cli.decide_tick", return_value=TickDecision("wait_idle", (), ())):
            self.app.cmd_tick(False, False)
        qid = next(iter(self.store.questions()))
        self.assertEqual(self.store.questions()[qid]["status"], "decided", "the answer was read")
        self.assertEqual(self.sam()["Chess"], CardState.REGISTERED, "not applied yet: the user is at the Mac")

    def test_an_answer_long_before_the_class_waits_until_the_user_is_away(self):
        self.world(now=datetime(2026, 10, 13, 15, 0, tzinfo=TZ))
        self.answer(2)
        self.state["idle"] = 5.0
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.sam()["Chess"], CardState.REGISTERED, "not yet")
        self.state["idle"] = 9999.0
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.sam()["Football"], CardState.REGISTERED)


class AnswerTests(AppCase):
    """The user answers on the iPhone by completing one reminder; the next tick acts on it (spec R17, R18)."""

    def two_classes(self, **app_kw):
        OPEN = CardState.OPEN
        favs = {"alex": [card("Yoga", 14, 7, 0, OPEN), card("Judo", 14, 18, 0, OPEN),
                           card("Spin", 14, 20, 0, CardState.NOT_OPEN)], "sam": [], "robin": []}
        self.build(favs, vision_cls=FakeAxVision, app_kw=app_kw)
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        [(self.qid, _, self.options)] = self.reporter.asked
        return self.qid

    def overlap(self):
        OPEN = CardState.OPEN
        favs = {"alex": [card("Yoga", 14, 7, 0, OPEN), card("Judo", 14, 7, 15, OPEN)], "sam": [], "robin": []}
        self.build(favs, vision_cls=FakeAxVision)
        self.vision.crowd = {"2026-10-14T07:00|yoga": 9, "2026-10-14T07:15|judo": 2}
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        [(self.qid, _, self.options)] = self.reporter.asked
        return self.qid

    def states(self):
        return {c.name: c.state for c in self.fake.cards["alex"] if c.name != "Spin"}      # Spin only gives the plan an opening

    def answer(self, number, of=3):
        self.reporter.answer_map[self.qid] = {n: n == number for n in range(1, of + 1)}

    def test_keeping_one_of_two_classes_cancels_the_other_and_closes_the_question(self):
        self.two_classes()
        self.assertEqual(self.states(), {"Yoga": CardState.REGISTERED, "Judo": CardState.REGISTERED})
        self.answer(1)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.states(), {"Yoga": CardState.REGISTERED, "Judo": CardState.OPEN})
        self.assertEqual(self.fake.cancel_presses, [("alex", "2026-10-14T18:00|judo", "ביטול")])
        saved = self.store.questions()[self.qid]
        self.assertEqual((saved["status"], saved["answer"]), ("done", 1))
        self.assertEqual(self.reporter.closed, [self.qid])
        self.assertEqual([t for t, _ in self.reporter.tells if "answer" in t], [], "the user's own answer is no news")
        self.assertEqual(self.fake.profile, 0, "the app is back on Alex")

    def test_an_answer_that_changes_nothing_sends_no_message(self):
        self.two_classes()
        tells = len(self.reporter.tells)
        self.answer(3)                                                            # Keep all
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(len(self.reporter.tells), tells)
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")

    def test_a_cancel_the_user_asked_for_is_no_message(self):
        # 2026-10-15: "send me only if it needs my decision": the user just answered, a confirmation is a redundant reminder
        self.two_classes()
        self.answer(1)
        tells = len(self.reporter.tells)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.reporter.tells[tells:], [])
        self.assertEqual(len(self.fake.cancel_presses), 1, "it was done")

    def test_keep_all_cancels_nothing(self):
        self.two_classes()
        self.answer(3)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [])
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")
        self.assertEqual(self.reporter.closed, [self.qid])

    def test_no_answer_no_action(self):
        self.two_classes()
        used = self.screen.used
        self.app._process_answers(self.app.now_fn())
        self.assertEqual((self.fake.cancel_presses, self.screen.used), ([], used), "the screen is not even touched")
        self.assertEqual(self.store.questions()[self.qid]["status"], "open")

    def test_two_options_completed_is_no_answer(self):
        self.two_classes()
        self.reporter.answer_map[self.qid] = {1: True, 2: True, 3: False}
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [])

    def test_the_answer_waits_while_the_mac_is_locked_or_in_use(self):
        self.two_classes()
        self.answer(1)
        self.state["locked"] = True
        self.app._process_answers(self.app.now_fn())
        self.state["locked"], self.state["idle"] = False, 5.0
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [])
        self.assertEqual(self.store.questions()[self.qid]["status"], "decided", "the answer is remembered")
        self.state["idle"] = 999.0
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(len(self.fake.cancel_presses), 1)
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")

    def test_a_question_is_taken_away_when_the_class_has_started_and_the_booking_stays(self):
        self.two_classes()
        self.state["now"] = datetime(2026, 10, 14, 7, 5, tzinfo=TZ)           # the first class has started
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.store.questions()[self.qid]["status"], "expired")
        self.assertEqual((self.reporter.closed, self.fake.cancel_presses), ([self.qid], []))

    def test_switching_cancels_the_default_first_and_then_books_the_other_class(self):
        # the club refuses two overlapping registrations, so the held class must go before the other one is booked
        self.overlap()
        self.assertEqual(self.states(), {"Yoga": CardState.REGISTERED, "Judo": CardState.OPEN}, "the more crowded one")
        self.answer(2, of=2)                                                  # Switch to: Judo
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.states(), {"Yoga": CardState.OPEN, "Judo": CardState.REGISTERED})
        self.assertEqual(self.fake.button_taps[-1], ("alex", "2026-10-14T07:15|judo"))
        self.assertEqual(self.fake.cancel_presses, [("alex", "2026-10-14T07:00|yoga", "ביטול")])
        events = self.fake.events
        self.assertLess(len(events) - 1 - events[::-1].index("cancel"), len(events) - 1 - events[::-1].index("book"),
                        "the cancel comes before the last booking")

    def test_when_the_other_class_cannot_be_booked_the_default_is_kept(self):
        self.overlap()
        self.fake.cards["alex"][1] = card("Judo", 14, 7, 15, CardState.NOT_OPEN)       # it is not open any more
        self.answer(2, of=2)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [], "never cancel the class that is booked for one that is not")
        self.assertEqual(self.states()["Yoga"], CardState.REGISTERED)
        self.assertTrue(any("kept" in body.lower() for _, body in self.reporter.tells))

    def test_an_old_waitlist_question_is_closed_and_its_remove_answer_cancels_nothing(self):
        # the user decided on 2026-10-14: the spot is always kept. The question of that morning stood open on the phone.
        self.build({"alex": [], "sam": [card("Judo", 14, 17, 0, CardState.FULL)], "robin": []}, vision_cls=FakeAxVision)
        self.app.cfg = replace(self.app.cfg, questions="on")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(self.fake.cards["sam"][0].state, CardState.WAITLISTED)
        old = {"id": "old1", "kind": "waitlist", "person": "sam", "text": "Judo is full.", "asked": True,
               "status": "open", "deadline": self.later().isoformat(), "answer": None, "tries": 0, "result": "",
               "bookings": ["sam|2026-10-14T17:00|judo"],
               "options": [{"label": "Keep the waitlist spot", "keep": ["sam|2026-10-14T17:00|judo"], "book": []},
                           {"label": "Remove the waitlist spot", "keep": [], "book": []}]}
        self.store.save_question(old)
        self.qid = "old1"
        self.answer(2, of=2)                                                  # the old "Remove the waitlist spot"
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [], "the spot is kept")
        self.assertEqual(self.fake.cards["sam"][0].state, CardState.WAITLISTED)
        self.assertEqual(self.store.open_questions(), [])
        self.assertIn("old1", self.reporter.closed, "its reminders are deleted from the phone")

    def test_a_damaged_question_cannot_make_the_program_cancel_something_it_did_not_book(self):
        self.build({"alex": [card("Yoga", 14, 7, 0, CardState.REGISTERED)], "sam": [], "robin": []}, vision_cls=FakeAxVision)
        self.app.cfg = replace(self.app.cfg, questions="on")
        q = drop_question("alex", "alex|2026-10-14T07:00|yoga", "Yoga", self.later())
        self.store.save_question({**q.to_dict(), "asked": True})
        self.qid = q.id
        self.answer(2, of=2)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [], "registered by hand: not the program's to cancel")
        self.assertEqual(self.fake.cards["alex"][0].state, CardState.REGISTERED)
        self.assertTrue(any("left alone" in body for _, body in self.reporter.tells))

    def later(self):
        return datetime(2026, 10, 14, 7, 0, tzinfo=TZ)

    def test_a_cancel_that_does_nothing_is_told_and_the_question_is_still_closed(self):
        self.two_classes(cancel_ignored=True)
        self.answer(1)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(len(self.fake.cancel_presses), 1)
        saved = self.store.questions()[self.qid]
        self.assertEqual(saved["status"], "done")
        self.assertIn("problem", saved["result"])
        self.assertTrue(any("problem" in body for _, body in self.reporter.tells))

    def test_an_answer_waits_when_the_user_comes_back_and_is_applied_later(self):
        self.two_classes()
        self.answer(1)                                                             # keep Yoga: Judo is to be cancelled
        looks = [0]
        self.app.idle_fn = lambda: (looks.__setitem__(0, looks[0] + 1) or (3000.0 if looks[0] <= 1 else 0.5))
        tells = len(self.reporter.tells)
        self.app._process_answers(self.app.now_fn())                               # away for the check, back at the first click
        self.assertEqual(self.fake.cancel_presses, [], "nothing was pressed")
        saved = self.store.questions()[self.qid]
        self.assertEqual((saved["status"], saved.get("tries", 0)), ("decided", 0), "the answer waits, no try is used")
        self.assertEqual(len(self.reporter.tells), tells, "no message")
        self.app.idle_fn = lambda: 3000.0                                          # the user leaves
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(len(self.fake.cancel_presses), 1)
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")

    # ---- the fix pass after the review of plan 2 ---------------------------------------------------------
    def test_a_kept_class_that_is_gone_means_nothing_is_cancelled(self):
        # Q: keep Yoga or Judo. The user answers "Keep Yoga", but Yoga was cancelled by hand before the answer was applied
        self.two_classes()
        self.answer(1)
        self.fake.cards["alex"][0] = card("Yoga", 14, 7, 0, CardState.OPEN)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [], "never cancel the other class when the kept one is not booked")
        self.assertEqual(self.states()["Judo"], CardState.REGISTERED)
        self.assertTrue(any("not booked any more" in body for _, body in self.reporter.tells))
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")

    def test_a_step_that_failed_before_anything_was_pressed_is_tried_again_and_then_told(self):
        self.two_classes()
        self.answer(1)
        with mock.patch("club_booking.cli.Runner.cancel", return_value="retry: StuckError: the booked tab did not open"):
            self.app._process_answers(self.app.now_fn())
            self.assertEqual(self.store.questions()[self.qid]["status"], "decided", "the answer is not lost")
            self.app._process_answers(self.app.now_fn())
            self.assertEqual(self.store.questions()[self.qid]["status"], "decided")
            self.app._process_answers(self.app.now_fn())
        saved = self.store.questions()[self.qid]
        self.assertEqual(saved["status"], "done")
        self.assertIn("retry", saved["result"])
        self.assertTrue(any("Judo" in body and "retry" in body for _, body in self.reporter.tells), "the user is told after 3 tries")

    def test_an_answer_that_could_not_be_applied_before_the_class_is_told_to_the_phone(self):
        self.two_classes()
        self.answer(1)
        self.state["locked"] = True
        self.app._process_answers(self.app.now_fn())                              # decided, but the Mac is locked
        self.state["now"] = datetime(2026, 10, 14, 7, 5, tzinfo=TZ)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.store.questions()[self.qid]["status"], "expired")
        self.assertTrue(any("not applied" in title for title, _ in self.reporter.tells))

    def test_a_third_class_does_not_open_a_second_question_while_the_first_answer_waits(self):
        self.two_classes()
        self.answer(1)
        self.state["locked"] = True
        self.app._process_answers(self.app.now_fn())                              # Q1 is decided and waits
        self.store.record_booking("r", "alex", "2026-10-14T20:00|spin", "Spin", status="confirmed")
        self.app._ask_questions(RunResult("r", [], None, []), dry_run=False)
        self.assertEqual(len(self.reporter.asked), 1, "Q2 would offer the classes Q1 is about to cancel")

    def test_the_answer_waits_while_a_class_is_about_to_open(self):
        self.two_classes()
        self.answer(1)
        soonest = min(o.opens_at for o in self.store.load_plan().openings if o.opens_at > self.state["now"])
        self.state["now"] = soonest - timedelta(minutes=5)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [])
        self.assertEqual(self.store.questions()[self.qid]["status"], "decided")

    def test_a_switch_that_could_not_start_is_done_at_the_next_tick(self):
        from club_booking.runner import LoggedOutError
        self.overlap()
        self.answer(2, of=2)
        with mock.patch("club_booking.cli.Runner.cancel", side_effect=LoggedOutError("logged out")):
            self.app._process_answers(self.app.now_fn())                          # the app is logged out before the cancel
        self.assertEqual(self.states(), {"Yoga": CardState.REGISTERED, "Judo": CardState.OPEN}, "nothing was changed")
        self.assertEqual(self.store.questions()[self.qid]["status"], "decided")
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.states(), {"Yoga": CardState.OPEN, "Judo": CardState.REGISTERED}, "switched now")
        self.assertEqual(self.store.questions()[self.qid]["status"], "done")

    def test_what_applying_an_answer_finds_is_asked_about_too(self):
        self.overlap()
        self.answer(2, of=2)                                   # a switch books a class: its result is looked at
        asked = []
        real = self.app._ask_questions
        self.app._ask_questions = lambda result, dry_run: (asked.append(result), real(result, dry_run))[1]
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(len(asked), 1)
        self.assertEqual([r.person for r in asked[0].people], ["alex"])

    def test_one_damaged_question_does_not_block_the_others(self):
        self.two_classes()
        broken = drop_question("alex", "alex|x", "X", self.later())
        data = {**broken.to_dict(), "asked": True, "status": "decided", "answer": 99}      # an answer no option has
        self.store.save_question(data)
        self.answer(1)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(len(self.fake.cancel_presses), 1, "the good question was applied")

    def test_the_answers_command_acts_even_when_the_user_is_at_the_keyboard(self):
        self.two_classes()
        self.answer(1)
        self.state["idle"] = 3.0
        self.app.cmd_answers()
        self.assertEqual(len(self.fake.cancel_presses), 1)

    def test_nothing_happens_when_questions_are_off(self):
        self.two_classes()
        self.app.cfg = replace(self.app.cfg, questions="off")
        self.answer(1)
        self.app._process_answers(self.app.now_fn())
        self.assertEqual(self.fake.cancel_presses, [])

    def test_a_tick_with_nothing_due_reads_the_answers(self):
        self.two_classes()
        self.answer(1)
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.fake.cancel_presses), 1)

    def test_the_answers_command_does_the_same_by_hand(self):
        self.two_classes()
        self.answer(1)
        self.assertEqual(self.app.cmd_answers(), 0)
        self.assertEqual(len(self.fake.cancel_presses), 1)


class UserFirstTests(AppCase):
    """The user comes first: the program takes the mouse only for a class that is opening (2026-10-14: "I cannot work
    on this computer"). Standing back is no failure, no message, and no reason to give up."""

    def away_then_back(self, away_looks=1):
        """An idle clock: the user is away for the first look(s), then touches the mouse."""
        looks = [0]

        def idle():
            looks[0] += 1
            return 3000.0 if looks[0] <= away_looks else 0.5
        return idle

    def attempts(self):
        return self.store._read_json("attempts.json", {})

    def stale_plan(self):
        self.build(idle=3000.0)
        self.app.cmd_tick(False, False)                                           # plans (the user was away)
        plan = self.store.load_plan()
        self.old = NOW - timedelta(hours=100)
        self.store.save_plan(Plan(self.old, plan.openings))                       # a refresh is due
        self.fake.taps.clear()
        del self.fake.button_taps[:]                                              # what the first tick booked
        self.replans = self.attempts().get(f"replan|{NOW:%Y%m%d}", 0)

    def test_a_refresh_stands_back_when_the_user_comes_back_and_costs_nothing(self):
        self.stale_plan()
        self.app.idle_fn = self.away_then_back()          # away when the tick decides, back before the first click
        before = len(self.reporter.tells)
        self.app.cmd_tick(False, False)
        self.assertEqual(self.fake.taps, [], "not one click")
        self.assertEqual(len(self.reporter.tells), before, "no message")
        self.assertEqual([k for k in self.attempts() if k.startswith("fail|")], [], "no failure")
        self.assertEqual(self.attempts().get(f"replan|{NOW:%Y%m%d}", 0), self.replans, "the refresh of the day is not used up")
        self.assertEqual(self.store.load_plan().created_at, self.old, "the plan is as it was")
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("stood_back", events)

    def test_a_refresh_stands_back_in_the_middle_too(self):
        self.stale_plan()
        taps = lambda: len(self.fake.taps)                                         # noqa: E731
        self.app.idle_fn = lambda: 3000.0 if taps() < 4 else 0.5                    # the mouse moves after the 4th click
        self.app.now_fn = self._moving_clock()
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.fake.taps), 4, "it stopped at once")
        self.assertEqual(self.fake.button_taps, [], "a refresh books nothing")
        self.assertEqual([k for k in self.attempts() if k.startswith("fail|")], [])
        self.assertEqual(self.store.load_plan().created_at, self.old)

    def _moving_clock(self):
        ticks = [0]

        def now():
            ticks[0] += 1
            return NOW + timedelta(seconds=3 * ticks[0])
        return now

    def test_a_refresh_that_is_a_week_overdue_is_urgent_because_without_it_classes_are_missed(self):
        # nothing else wakes the program when the plan has run out: it waits for a pause in the typing, then goes on
        self.build(idle=3000.0)
        self.app.cmd_tick(False, False)                                           # plans
        plan = self.store.load_plan()
        self.store.save_plan(Plan(NOW - timedelta(hours=self.cfg.replan_force_hours + 5), plan.openings))
        self.fake.taps.clear()
        self.sleeps.clear()
        waited = lambda: sum(1 for s in self.sleeps if s == 1.0)                    # noqa: E731
        self.app.idle_fn = lambda: 0.5 if waited() < 2 else 3000.0                  # the user types, then stops
        self.app.cmd_tick(False, False)
        self.assertGreaterEqual(waited(), 2, "it waited for a pause")
        self.assertTrue(self.fake.taps, "and then it refreshed the plan")
        self.assertGreater(self.store.load_plan().created_at, NOW - timedelta(hours=1), "the plan is new")

    def test_an_opening_waits_for_a_pause_in_the_typing_and_then_books(self):
        self.build(idle=3000.0)
        self.app.cmd_tick(False, False)                                           # plans
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) opens
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=CardState.OPEN)
        self.sleeps.clear()
        waited = lambda: sum(1 for s in self.sleeps if s == 1.0)                    # noqa: E731
        self.app.idle_fn = lambda: 0.5 if waited() < 3 else 3000.0                  # the user types, then stops
        self.app.cmd_tick(False, False)
        self.assertGreaterEqual(waited(), 3, "it waited for the user")
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps, "and then it booked")

    def test_a_run_the_user_started_is_not_guarded(self):
        self.build(idle=0.1)                                                       # the user is at the keyboard, watching
        self.app.cmd_run([], False, countdown=False)
        self.assertTrue(self.fake.button_taps, "nobody stands back for a run the user asked for")
        self.assertNotIn(1.0, self.sleeps, "and nobody waits for a pause in their typing")

    def test_answers_the_user_asked_for_by_hand_are_not_guarded_but_those_of_a_wake_are(self):
        seen = []
        self.build()
        with mock.patch.object(self.app, "_apply_decisions", side_effect=lambda d, guard=False, urgent=False: seen.append(guard)):
            self.app.store.save_question({"id": "x", "status": "decided", "asked": True, "deadline": "2099-01-01T00:00:00+00:00",
                                          "person": "alex", "kind": "several", "bookings": [], "text": "?", "options": [],
                                          "answer": 1})
            self.app.cfg = replace(self.app.cfg, questions="on")
            self.app._process_answers(NOW, require_away=False)
            self.app._process_answers(NOW, require_away=True)
        self.assertEqual(seen, [False, True])

    def test_a_run_that_stood_back_is_tried_again_soon_and_not_like_a_failure(self):
        self.build(idle=3000.0)
        self.app.cmd_tick(False, False)                                           # plans
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) is due
        stood = RunResult("r", [PersonResult("alex", error=USER_ACTIVE)], None, [])
        with mock.patch("club_booking.cli.Runner.run", return_value=stood):
            self.app.do_run([self.cfg.person("alex")], False, countdown=False)
        soon = local_naive(self.state["now"] + RETRY_SOON)
        self.assertIn(soon, self.agents.synced[-1], "one minute, as for a class that should be open but was not read")

    def test_a_failure_counter_ignores_standing_back(self):
        self.build()
        due = (opening("alex", "PowerGym", NOW - timedelta(minutes=1)),)
        decision = TickDecision("run_due", ("alex",), due)
        for _ in range(5):
            self.app._give_up_on_failing(decision, RunResult("r", [PersonResult("alex", error=USER_ACTIVE)], None, []))
        self.assertEqual([k for k in self.attempts() if k.startswith("fail|")], [])
        self.assertEqual(self.store.handled(), set(), "the opening was not given up")


class PlanRefreshTests(AppCase):
    """No timer wakes the program to refresh its plan: it does it right after an opening run, when the user is away."""

    def stale_and_due(self, idle):
        self.build(idle=3000.0)
        self.app.cmd_tick(False, False)                                           # plans (the user was away)
        self.state["idle"] = idle
        plan = self.store.load_plan()
        self.store.save_plan(Plan(NOW - timedelta(hours=100), plan.openings))     # an old plan
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) opens
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=CardState.OPEN)
        self.screen.used = 0

    def test_a_stale_plan_is_refreshed_right_after_an_opening_run_when_the_user_is_away(self):
        self.stale_and_due(idle=3000.0)
        self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, 2, "the opening run, then the refresh of the whole plan")

    def test_it_is_not_refreshed_while_the_user_is_at_the_computer(self):
        self.stale_and_due(idle=20.0)
        self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, 1, "only the booking: the refresh waits for a time the user is away")

    def test_the_defaults_are_gentle(self):
        cfg = make_cfg(self.tmp if hasattr(self, "tmp") else Path(tempfile.mkdtemp()))
        self.assertEqual((cfg.min_idle_seconds, cfg.replan_after_hours, cfg.replan_force_hours), (600.0, 48, 168))


class OpeningWaitTests(AppCase):
    """2026-10-14: the booking waited for the next 5-minute tick after the unlock. The agent that is started at the
    opening time now waits for the unlock itself (up to 20 minutes) and books at once."""

    LABEL = "com.club-booking.opening.202610130600"

    def due_locked(self, label=LABEL):
        self.build(running_label=label)
        self.app.cmd_tick(False, False)                                           # plans (the test sleep counts calls)
        self.sleeps.clear()
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) opens
        self.state["locked"] = True
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=CardState.OPEN)

    def unlock_after(self, n):
        def on_sleep(count):
            if count >= n:
                self.state["locked"] = False
        self.on_sleep = on_sleep

    def locked_notes(self):
        return [t for t, _ in self.reporter.tells if "locked" in t]

    def test_the_opening_agent_waits_for_the_unlock_and_books_at_once(self):
        self.due_locked()
        self.unlock_after(3)
        self.app.cmd_tick(False, False)
        self.assertEqual([x for x in self.sleeps if x == 15], [15, 15, 15], "a look every 15 seconds, then the run")
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)
        self.assertEqual(len(self.locked_notes()), 1, "the reminder once, not again")

    def test_it_stops_waiting_after_twenty_minutes(self):
        self.due_locked()
        self.app.cmd_tick(False, False)
        self.assertEqual(sum(self.sleeps), 20 * 60)
        self.assertNotIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)
        self.assertEqual(len(self.locked_notes()), 1)

    def test_a_locked_mac_is_told_once_a_day_not_once_per_class(self):
        # 2026-10-17: five openings found the Mac locked, and the phone got five reminders for one cause
        from club_booking.planner import Opening
        self.due_locked()
        self.app.cmd_tick(False, False)
        plan = self.store.load_plan()
        extra = Opening("sam", "2026-10-14T08:00|other", "Other", datetime(2026, 10, 14, 8, 0, tzinfo=TZ),
                        datetime(2026, 10, 13, 8, 0, tzinfo=TZ))
        self.store.save_plan(replace(plan, openings=plan.openings + (extra,)))
        self.state["now"] = datetime(2026, 10, 13, 8, 0, 10, tzinfo=TZ)                  # a later opening, still locked
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.locked_notes()), 1)
        self.assertEqual(self.store.bump_attempt("locked|sam|2026-10-14T08:00|other"), 2,
                         "the second opening was looked at and counted: only its message was held back")

    def test_the_opening_agent_also_waits_for_the_user_to_log_the_app_in(self):
        from tests.test_login import LOGGED_IN, LOGIN
        self.due_locked()
        self.state["locked"] = False                                              # unlocked, but the app is logged out
        screens = {"kind": LOGIN}

        class Ax:
            def dump(inner):
                return screens["kind"]
        self.app.ax_factory = lambda: Ax()

        def on_sleep(count):
            if count >= 3:
                screens["kind"] = LOGGED_IN                                       # the user logs in
        self.on_sleep = on_sleep
        self.app.cmd_tick(False, False)
        self.assertEqual([x for x in self.sleeps if x == 15], [15, 15, 15], "a look every 15 seconds")
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)
        self.assertEqual(len([t for t, _ in self.reporter.tells if "logged out" in t]), 1, "told once")

    def test_the_slow_tick_does_not_wait(self):
        self.due_locked(label="com.club-booking.tick")
        self.app.cmd_tick(False, False)
        self.assertEqual(self.sleeps, [])

    def test_it_stops_waiting_when_the_class_was_handled_meanwhile(self):
        # the user registered by hand: another run marked the opening handled
        self.due_locked()
        ident = next(o.ident for o in self.store.load_plan().openings if o.person == "alex" and o.name == "PowerGym")

        def on_sleep(count):
            if count == 2:
                self.store.mark_handled(ident)
        self.on_sleep = on_sleep
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.sleeps), 2)
        self.assertNotIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)

    def test_a_pause_file_stops_the_waiting(self):
        self.due_locked()

        def on_sleep(count):
            if count == 2:
                (self.tmp / "PAUSE").write_text("")
        self.on_sleep = on_sleep
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.sleeps), 2)
        self.assertNotIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)

    def test_a_dry_run_never_waits(self):
        self.due_locked()
        self.app.cmd_tick(True, False)
        self.assertEqual(self.sleeps, [])


class DisplayHoldTests(AppCase):
    """2026-10-17/18: the display went dark about 30 seconds after the program woke it, in the middle of a run, and the
    openings of the Sabbath were lost ("Failed to capture screenshot"). While the program uses the screen it holds the
    display on (caffeinate -d)."""

    def spy_screen(self):
        seen, screen = [], self.screen

        class Spy:
            def __enter__(inner):
                seen.append(self.awake_depth)
                return screen.__enter__()

            def __exit__(inner, *exc):
                return screen.__exit__(*exc)
        self.app.screen_factory = lambda: Spy()
        return seen

    def test_a_run_holds_the_display_on_and_lets_go_at_the_end(self):
        self.build()
        seen = self.spy_screen()
        self.app.do_run([self.cfg.person("alex")], dry_run=False, countdown=False, guard=False, phone=False)
        self.assertEqual(seen, [1], "the screen was used while the display was held")
        self.assertEqual((self.awake_depth, self.awake_log), (0, ["on", "off"]))

    def test_a_run_that_fails_still_lets_go(self):
        self.build(screen_error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.app.do_run([self.cfg.person("alex")], dry_run=False, countdown=False)
        self.assertEqual((self.awake_depth, self.awake_log), (0, ["on", "off"]))

    def test_the_answers_step_holds_the_display_on(self):
        self.build()
        seen = self.spy_screen()
        self.app._apply_decisions([])
        self.assertEqual(seen, [1])
        self.assertEqual(self.awake_depth, 0)

    def test_the_look_at_the_login_screen_holds_the_display_on(self):
        from tests.test_login import LOGGED_IN
        self.build()
        seen = []

        class Ax:
            def dump(inner):
                seen.append(self.awake_depth)
                return LOGGED_IN
        self.app.ax_factory = lambda: Ax()
        self.app._logged_out_now(TickDecision("run_due", ("alex",), ()), False)
        self.assertEqual(seen, [1])


class DailyTests(AppCase):
    def test_a_warning_that_comes_from_two_places_is_sent_once_a_day(self):
        self.build()
        self.app._tell_daily("login", "Club booking: the club app is logged out", "First wording.")
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Another wording, another place.")
        self.assertEqual(len(self.reporter.tells), 1)
        self.state["now"] = NOW + timedelta(days=1)
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Tomorrow.")
        self.assertEqual(len(self.reporter.tells), 2)

    def test_the_logged_out_message_is_sent_once_a_day_even_when_the_look_repeats(self):
        from tests.test_login import LOGIN
        self.build()
        self.app.cmd_tick(False, False)

        class Ax:
            def dump(inner):
                return LOGIN
        self.app.ax_factory = lambda: Ax()
        before = len(self.reporter.tells)
        for _ in range(3):
            self.app._logged_out_now(TickDecision("run_due", ("alex",), ()))
        logged_out = [t for t, _ in self.reporter.tells[before:] if "logged out" in t]
        self.assertEqual(len(logged_out), 1)

    def test_old_reports_are_deleted_once_a_day(self):
        self.build()
        for _ in range(3):
            self.app.cmd_tick(False, False)
        self.assertEqual(self.reporter.pruned, [2], "reports older than two days, once")
        self.app._prune_old_reports(NOW + timedelta(hours=3))
        self.assertEqual(self.reporter.pruned, [2], "the same day: not again")
        self.app._prune_old_reports(NOW + timedelta(days=1))
        self.assertEqual(self.reporter.pruned, [2, 2], "the next day: again")


class SolvedMessageTests(AppCase):
    """A message about a problem goes away when the problem is gone (2026-10-14: a "Mac is locked" and a "Problem" message
    stayed on the phone for hours after the class was booked, and the user asked why there were so many reminders)."""

    def person_run(self, *keys, dry_run=False):
        return self.app.do_run([self.cfg.person(k) for k in keys], dry_run, countdown=False)

    def failing_sam(self):
        self.build()
        self.vision.fail_for = "sam"
        self.person_run("sam")
        (mid,) = self.store.messages()
        return mid

    def test_a_report_with_a_problem_is_tagged_and_remembered(self):
        mid = self.failing_sam()
        self.assertEqual([t for _, t in self.reporter.tags], [mid])
        self.assertEqual((self.store.messages()[mid]["kind"], self.store.messages()[mid]["persons"]),
                         ("problem", ["sam"]))

    def test_the_message_is_taken_back_when_the_person_runs_well(self):
        mid = self.failing_sam()
        self.vision.fail_for = None
        self.person_run("sam")
        self.assertEqual(self.reporter.resolved, [mid])
        self.assertEqual(self.store.messages(), {})

    def test_the_message_stays_while_the_person_still_fails_or_another_person_runs(self):
        self.failing_sam()
        self.person_run("sam")
        self.person_run("alex")
        self.assertEqual(self.reporter.resolved, [])
        self.assertEqual(len(self.store.messages()), 1)

    def test_a_message_about_two_people_waits_for_both(self):
        self.build()
        self.app._tell_once("Club booking: 0 booked, 2 to check", "Problem (Alex, Sam): x", kind="problem",
                            persons=["alex", "sam"])
        (mid,) = self.store.messages()
        self.app._clear_solved(RunResult("r", [PersonResult("alex")], None, []))
        self.assertEqual(self.reporter.resolved, [])
        self.app._clear_solved(RunResult("r", [PersonResult("alex"), PersonResult("sam")], None, []))
        self.assertEqual(self.reporter.resolved, [mid])

    def test_the_same_problem_is_told_again_after_it_was_solved(self):
        self.failing_sam()
        self.vision.fail_for = None
        self.person_run("sam")
        told = len(self.reporter.tells)
        self.vision.fail_for = "sam"
        self.person_run("sam")
        self.assertEqual(len(self.reporter.tells), told + 1, "not hidden as 'already told today'")

    def test_the_locked_message_is_taken_back_when_the_class_is_booked(self):
        self.build()
        self.app.cmd_tick(False, False)                                           # plans
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) opens
        self.state["locked"] = True
        self.app.cmd_tick(False, False)
        (mid,) = self.store.messages()
        self.assertEqual(self.store.messages()[mid]["kind"], "locked")
        self.assertEqual(self.store.messages()[mid]["persons"], ["alex"])
        self.state["locked"] = False
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=CardState.OPEN)
        self.app.cmd_tick(False, False)
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)
        self.assertEqual(self.reporter.resolved, [mid])
        self.assertEqual(self.store.messages(), {})

    def test_the_login_message_is_taken_back_after_a_good_run(self):
        self.build()
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Log in.", kind="login")
        (mid,) = self.store.messages()
        self.assertEqual(self.store.messages()[mid]["kind"], "login")
        self.person_run("alex")
        self.assertEqual(self.reporter.resolved, [mid])

    def test_a_dry_run_takes_nothing_back(self):
        self.build()
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Log in.", kind="login")
        self.person_run("alex", dry_run=True)
        self.assertEqual(self.reporter.resolved, [])
        self.assertEqual(len(self.store.messages()), 1)

    def test_a_run_that_stood_back_takes_nothing_back(self):
        self.build()
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Log in.", kind="login")
        self.app._clear_solved(RunResult("r", [PersonResult("alex", error=USER_ACTIVE)], None, []))
        self.assertEqual(self.reporter.resolved, [])

    def test_a_phone_that_does_not_answer_keeps_the_message_for_the_next_run(self):
        self.build()
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Log in.", kind="login")
        self.reporter.resolve_error = AppleScriptError("Reminders is not allowed")
        self.person_run("alex")                                   # the run goes on
        self.assertEqual(len(self.store.messages()), 1)
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("message_not_taken_back", events)
        self.reporter.resolve_error = None
        self.person_run("alex")
        self.assertEqual(self.store.messages(), {})

    def test_a_message_the_phone_did_not_get_is_not_remembered(self):
        self.build()
        self.reporter.tell = lambda title, body="", tag="": ["phone: Reminders is not allowed"]
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Log in.", kind="login")
        self.assertEqual(self.store.messages(), {})

    def test_a_report_that_only_says_what_was_booked_is_not_remembered(self):
        self.build()
        self.person_run("alex", "sam", "robin")
        self.assertTrue(self.reporter.tells, "the booking report was sent")
        self.assertEqual((self.reporter.tags, self.store.messages()), ([], {}))


class TellOnceTests(AppCase):
    def answers(self, *answers):
        queue = list(answers)

        def tell(title, body=""):
            self.reporter.tells.append((title, body))
            return queue.pop(0) if queue else []
        self.reporter.tell = tell

    def test_a_message_the_phone_did_not_get_is_sent_again_and_then_only_once(self):
        self.build()
        self.answers(["phone: Reminders is not allowed"], [])
        results = [self.app._tell_once("Club booking: 1 booked", "Booked: x") for _ in range(3)]
        self.assertEqual(results, [["phone: Reminders is not allowed"], [], []])
        self.assertEqual(len(self.reporter.tells), 2, "tried again after the failure, then said once")

    def test_a_failure_of_the_mac_notice_alone_does_not_repeat_the_message(self):
        self.build()
        self.answers(["mac: notification failed"])
        self.app._tell_once("Club booking: 1 booked", "Booked: x")
        self.app._tell_once("Club booking: 1 booked", "Booked: x")
        self.assertEqual(len(self.reporter.tells), 1)


class TickTests(AppCase):
    def test_first_tick_plans_books_and_arms_the_earliest_opening_of_any_person(self):
        self.build()
        self.assertEqual(self.app.cmd_tick(False, False), 0)
        self.assertEqual(len(self.fake.button_taps), 3)
        # three bookings and no confirmation; the one message is a class that was NOT booked (full, waitlist not joined)
        self.assertEqual([t for t, _ in self.reporter.tells], ["Club booking: 1 to check"])
        self.assertIn("the class is full", self.reporter.tells[0][1])
        self.assertNotIn("Booked", self.reporter.tells[0][1])
        earliest = min(o.opens_at for o in self.store.load_plan().openings)
        self.assertEqual(self.agents.synced[-1][0], local_naive(earliest))
        planned = {local_naive(o.opens_at) for o in self.store.load_plan().openings if o.opens_at > NOW}
        self.assertTrue(planned <= set(self.agents.synced[-1]), "an agent for EVERY planned opening, installed in advance")
        self.assertGreaterEqual(len(planned), 3)
        self.assertEqual(self.reporter.macs, [], "a refresh gives no warning: it stands back when the user comes back")

    def test_the_banner_is_shown_only_when_the_user_is_at_the_mac(self):
        # 2026-10-15: "too many reminder messages": a warning for nobody is noise (the attention guard still protects)
        self.build(idle=999.0)
        self.app._countdown()
        self.assertEqual((self.reporter.macs, self.sleeps), ([], []))
        self.state["idle"] = 5.0
        self.app._countdown()
        self.assertEqual(len(self.reporter.macs), 1)
        self.assertEqual(self.sleeps, [self.cfg.countdown_seconds])

    def test_a_tick_when_a_class_has_just_opened_books_only_for_that_person(self):
        self.build()
        self.app.cmd_tick(False, False)
        taps_before, tells_before = len(self.fake.button_taps), len(self.reporter.tells)
        # PowerGym (Alex) opens 13/10 06:00: in the app it is now open
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)
        cards = self.fake.cards["alex"]
        for i, c in enumerate(cards):
            if c.name == "PowerGym":
                cards[i] = replace(c, state=OPEN)
        self.assertEqual(self.app.cmd_tick(False, False), 0)
        self.assertEqual(self.fake.button_taps[taps_before:], [("alex", "2026-10-14T06:00|powergym")])
        self.assertEqual(len(self.reporter.tells), tells_before, "a booking is no phone message (the user's rule of 2026-10-15)")
        self.assertEqual(self.fake.profile, 0)

    def test_nothing_due_does_not_touch_the_screen_and_rearms(self):
        self.build()
        self.app.cmd_tick(False, False)
        used = self.screen.used
        self.state["now"] = NOW + timedelta(hours=1)
        self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, used)
        self.assertGreaterEqual(len(self.agents.synced), 2)

    def test_locked_mac_sends_one_notice_per_opening_and_books_nothing(self):
        self.build()
        self.app.cmd_tick(False, False)
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)
        self.state["locked"] = True
        before = len(self.reporter.tells)
        used = self.screen.used
        self.app.cmd_tick(False, False)
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.reporter.tells), before + 1)
        self.assertIn("locked", self.reporter.tells[-1][0])
        self.assertIn("Alex", self.reporter.tells[-1][1])
        self.assertEqual(self.screen.used, used)
        # after the user unlocks, the next tick books it
        self.state["locked"] = False
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=OPEN)
        self.app.cmd_tick(False, False)
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)

    def due_but_unavailable(self):
        self.build()
        self.app.cmd_tick(False, False)                                           # plans
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) opens
        self.state["locked"] = True
        return len(self.fake.button_taps)

    def test_a_display_that_is_only_asleep_is_woken_when_a_class_is_due_and_the_run_goes_on(self):
        taps = self.due_but_unavailable()
        self.wake_result = True
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=CardState.OPEN)
        tells = len(self.reporter.tells)
        self.app.cmd_tick(False, False)
        self.assertEqual(self.wake_calls, [1])
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)
        self.assertGreater(len(self.fake.button_taps), taps)
        self.assertTrue(all("locked" not in t for t, _ in self.reporter.tells[tells:]))

    def test_a_screen_that_cannot_be_woken_gets_the_locked_message(self):
        self.due_but_unavailable()
        self.app.cmd_tick(False, False)
        self.assertEqual(self.wake_calls, [1])
        self.assertIn("locked", self.reporter.tells[-1][0])

    def test_the_display_is_never_woken_when_nothing_is_due(self):
        self.build()
        self.app.cmd_tick(False, False)
        self.state["locked"] = True
        for _ in range(3):
            self.app.cmd_tick(False, False)
        self.assertEqual(self.wake_calls, [], "the display may sleep: nothing needs it")

    def test_the_locked_message_is_short(self):
        self.build()
        self.app.cmd_tick(False, False)
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)
        self.state["locked"] = True
        self.app.cmd_tick(False, False)
        body = self.reporter.tells[-1][1]
        self.assertLessEqual(len(body.splitlines()), 4)
        self.assertLessEqual(len(body), 200)
        self.assertIn("Alex - PowerGym, Wed 14/10 06:00", body)

    def test_the_locked_notice_also_says_when_the_screen_is_off(self):
        self.build()
        self.app.cmd_tick(False, False)
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)
        self.state["locked"] = True
        self.app.cmd_tick(False, False)
        self.assertIn("locked or its screen is off", self.reporter.tells[-1][1])

    def test_the_locked_notice_names_the_class_so_the_user_can_register_by_hand(self):
        # the user may be away from the Mac: the phone reminder must be enough to act on
        self.build()
        self.app.cmd_tick(False, False)
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)
        self.state["locked"] = True
        self.app.cmd_tick(False, False)
        body = self.reporter.tells[-1][1]
        self.assertIn("Alex - PowerGym, Wed 14/10 06:00", body)
        self.assertIn("register", body.lower())
        self.assertIn("as soon as you unlock", body, "the automatic way is still told")

    def test_a_stale_plan_waits_for_the_user_to_be_away(self):
        self.build(idle=5.0)
        self.assertEqual(self.app.cmd_tick(False, False), 0)
        self.assertEqual(self.screen.used, 0)
        self.state["idle"] = 600.0
        self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, 1)

    def test_a_very_old_plan_is_rebuilt_while_the_user_is_at_the_mac(self):
        self.build(idle=5.0)
        self.store.save_plan(Plan(NOW - timedelta(hours=self.cfg.replan_force_hours + 1), ()))
        self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, 1)

    def test_a_busy_lock_is_not_an_error(self):
        self.build()
        with Store(self.tmp).run_lock():
            self.assertEqual(self.app.cmd_tick(False, False), 0)
        self.assertEqual(self.screen.used, 0)

    def test_the_screen_tool_failing_is_reported_once(self):
        self.build(screen_error=ScreenError("the app window is not ready"))
        self.assertEqual(self.app.cmd_tick(False, False), 1)
        self.assertEqual(self.app.cmd_tick(False, False), 1)
        problems = [t for t in self.reporter.tells if t[0] == "Club booking problem"]
        self.assertEqual(len(problems), 1)

    def test_a_person_with_a_problem_is_in_the_report(self):
        self.build()
        self.vision.fail_for = "sam"
        self.app.cmd_tick(False, False)
        self.assertIn("Problem (Sam)", self.reporter.tells[0][1])

    def test_paused(self):
        self.build()
        (self.tmp / "PAUSE").write_text("")
        self.assertEqual(self.app.cmd_tick(False, False), 0)
        self.assertEqual(self.screen.used, 0)

    def due_for_sam(self):
        self.store.save_plan(Plan(NOW, (opening("sam", "Robotics", NOW - timedelta(minutes=1)),)))
        self.vision.fail_for = "sam"

    def test_a_window_that_is_not_visible_is_not_a_failure_of_the_day(self):
        # like a logged-out app: nothing is wrong with the program, the user must open the window
        from club_booking.runner import WINDOW_HIDDEN
        self.build()
        self.due_for_sam()
        from club_booking.cli import TickDecision
        due = tuple(o for o in self.store.load_plan().openings if o.person == "sam")
        failed = RunResult("r", [PersonResult("sam", error=WINDOW_HIDDEN)], None, [])
        for _ in range(6):
            self.app._give_up_on_failing(TickDecision("run_due", ("sam",), due), failed)
        self.assertEqual([t for t, _ in self.reporter.tells if "giving up" in t.lower()], [])

    def test_a_person_that_keeps_failing_is_cut_off_and_reported_once(self):
        self.build()
        self.due_for_sam()
        for _ in range(6):
            self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, self.cfg.max_person_failures, "no more runs after the cap")
        giving_up = [t for t in self.reporter.tells if "giving up" in t[0].lower()]
        self.assertEqual(len(giving_up), 1)
        self.assertIn("Sam", giving_up[0][1])
        self.assertEqual(len([t for t in self.reporter.tells if "Problem (Sam)" in t[1]]), 1,
                         "the same problem is told once a day")
        self.assertEqual(self.store.handled(), {o.ident for o in self.store.load_plan().openings})

    def test_a_failing_person_is_retried_in_five_minutes_not_in_one(self):
        # there is no timer any more: a retry is an agent armed for later, and it is the only way a failure is retried
        self.build()
        self.due_for_sam()
        self.app.cmd_tick(False, False)
        self.assertNotIn(local_naive(NOW + timedelta(minutes=1)), self.agents.armed)

    def test_a_retry_is_armed_only_a_few_times_for_one_opening(self):
        self.build({"alex": [card("Ghost", 13, 8, 0, NOT_OPEN)], "sam": [], "robin": []})
        self.app.cmd_run([], False, False)                                         # Ghost should be open but is not
        for _ in range(12):
            self.app.rearm()
        retries = [t for t in self.agents.armed if t == local_naive(NOW + timedelta(minutes=1))]
        self.assertEqual(len(retries), 6, "bounded: a class that never opens does not keep an agent chain alive")

    def test_an_unexpected_exception_is_caught_logged_and_told_once_a_day(self):
        self.build()

        def broken():
            raise RuntimeError("boom")

        self.app.vision_factory = broken
        self.assertEqual(self.app.cmd_tick(False, False), 1)
        self.assertEqual(self.app.cmd_tick(False, False), 1)
        problems = [t for t in self.reporter.tells if t[0] == "Club booking problem"]
        self.assertEqual(len(problems), 1)
        self.assertIn("RuntimeError: boom", problems[0][1])
        self.assertIn("run_failed", (self.tmp / "runs.jsonl").read_text())

    def test_a_failure_to_arm_the_scheduler_does_not_crash_the_tick(self):
        self.build()
        self.app.cmd_tick(False, False)
        self.state["now"] = NOW + timedelta(hours=1)

        def broken(*args, **kwargs):
            raise RuntimeError("launchctl bootstrap failed")

        self.agents.sync_openings = broken
        self.assertEqual(self.app.cmd_tick(False, False), 1)

    def test_a_whole_run_that_fails_is_counted_against_the_people_it_was_for(self):
        self.build(idle=5.0, screen_error=ScreenError("the app window is not ready"))        # the user is at the Mac
        self.due_for_sam()
        for _ in range(6):
            self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, self.cfg.max_person_failures)
        self.assertEqual(len(self.reporter.macs), self.cfg.max_person_failures, "the 'hands off the mouse' notices")
        self.assertNotIn(local_naive(NOW + timedelta(minutes=1)), self.agents.armed)
        self.assertEqual(len([t for t in self.reporter.tells if "giving up" in t[0].lower()]), 1)
        self.assertEqual(len([t for t in self.reporter.tells if t[0] == "Club booking problem"]), 1)

    def test_a_replan_is_tried_at_most_twice_a_day(self):
        self.build(screen_error=ScreenError("the app window is not ready"))
        for _ in range(5):
            self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, self.cfg.max_replans_per_day)
        self.state["now"] = NOW + timedelta(days=1)
        self.app.cmd_tick(False, False)
        self.assertEqual(self.screen.used, self.cfg.max_replans_per_day + 1, "a new day, a new budget")

    def test_wake_up_test_shows_a_banner_and_is_no_reminder_on_the_phone(self):
        self.build()
        with mock.patch("club_booking.cli.check_mirroir_permissions", return_value=[]), \
             mock.patch("club_booking.cli.get_api_key", return_value="test-key"), \
             mock.patch("club_booking.cli.calendar_names", return_value=["Alex", "Kids"]):
            self.assertEqual(self.app.cmd_tick(True, True), 0)
        self.assertEqual(self.screen.used, 0)
        title, body = self.reporter.macs[-1]
        self.assertEqual(title, "Club booking: wake-up test")
        self.assertIn("All permissions are fine", body)
        self.assertEqual(self.reporter.tells, [], "the user's rule of 2026-10-15: no reminder for a wake-up")
        self.assertIn('"event": "wake_test"', (self.tmp / "runs.jsonl").read_text())
        self.assertEqual(self.agents.removed, 1)
        self.assertIn('"event": "tick"', (self.tmp / "runs.jsonl").read_text())

    def test_wake_up_test_lists_the_problems(self):
        self.build()
        with mock.patch("club_booking.cli.check_mirroir_permissions", return_value=["Accessibility is not allowed for this program"]), \
             mock.patch("club_booking.cli.get_api_key", return_value="test-key"), \
             mock.patch("club_booking.cli.calendar_names", return_value=["Alex"]):
            self.app.cmd_tick(True, True)
        body = self.reporter.macs[-1][1]
        self.assertIn("Accessibility is not allowed", body)
        self.assertIn("calendar 'Kids' not found", body)


class LoggedOutTickTests(AppCase):
    """2026-10-14: the app was logged out. The tick started a run every 5 minutes, each with the mouse warning, and each
    counted as a failure: after three the due class was dropped for the day, although nothing had been tried."""

    def setUp(self):
        from tests.test_login import LOGGED_IN, LOGIN
        self.screens = {"login": LOGIN, "favorites": LOGGED_IN}
        self.kind = "login"

    def due(self):
        self.build()
        self.app.cmd_tick(False, False)                                           # plans
        self.state["now"] = datetime(2026, 10, 13, 6, 0, 10, tzinfo=TZ)           # PowerGym (Alex) opens
        for i, c in enumerate(self.fake.cards["alex"]):
            if c.name == "PowerGym":
                self.fake.cards["alex"][i] = replace(c, state=CardState.OPEN)
        looks = []
        test = self

        class Ax:
            def dump(inner):
                looks.append(1)
                return test.screens[test.kind]
        self.app.ax_factory = lambda: Ax()
        return looks

    def test_a_logged_out_app_is_not_run_against_and_the_phone_is_told_once(self):
        looks = self.due()
        used, taps, tells = self.screen.used, len(self.fake.button_taps), len(self.reporter.tells)
        for _ in range(4):
            self.app.cmd_tick(False, False)
        self.assertEqual((self.screen.used, len(self.fake.button_taps)), (used, taps), "no run, no mouse, no warning")
        self.assertEqual(len(self.reporter.tells) - tells, 1, "the same message once")
        title, body = self.reporter.tells[-1]
        self.assertEqual(title, "Club booking: the club app is logged out")
        self.assertIn("Alex - PowerGym", body)
        self.assertIn("right after", body.lower())
        self.assertTrue(looks)

    def test_nothing_is_counted_as_a_failure_while_the_app_is_logged_out(self):
        self.due()
        for _ in range(5):
            self.app.cmd_tick(False, False)
        self.assertNotIn("fail|alex|20261013", self.store._read_json("attempts.json", {}))

    def test_the_class_is_booked_as_soon_as_the_user_has_logged_in(self):
        self.due()
        for _ in range(4):
            self.app.cmd_tick(False, False)
        self.kind = "favorites"                                                    # the user logs in
        self.app.cmd_tick(False, False)
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps)

    def test_a_run_that_finds_the_app_logged_out_halfway_is_not_a_failure_of_the_day(self):
        from club_booking.runner import LOGGED_OUT, UPDATE_NEEDED
        self.due()
        o = next(x for x in self.store.load_plan().openings if x.person == "alex")
        decision = TickDecision("run_due", ("alex",), (o,))
        for error in (LOGGED_OUT, UPDATE_NEEDED, LOGGED_OUT, LOGGED_OUT, LOGGED_OUT):
            self.app._give_up_on_failing(decision, RunResult("r", [PersonResult("alex", error=error)], None, []))
        self.assertNotIn(o.ident, self.store.handled(), "the class is not dropped for a problem the user can fix")

    def test_other_failures_still_end_in_giving_up(self):
        self.due()
        o = next(x for x in self.store.load_plan().openings if x.person == "alex")
        decision = TickDecision("run_due", ("alex",), (o,))
        for _ in range(3):
            self.app._give_up_on_failing(decision, RunResult("r", [PersonResult("alex", error="ScreenError: boom")], None, []))
        self.assertIn(o.ident, self.store.handled())


class AutoLoginTickTests(AppCase):
    """2026-10-15: the user decided that the program logs in by itself ("it should be independent"). It tries only on the
    login screen, only at an opening that waits (a run_due), only when `auto_login` is on, and tells the phone if it fails."""

    def setUp(self):
        LoggedOutTickTests.setUp(self)
        from club_booking.autologin import AutoResult
        self.AutoResult = AutoResult
        self.attempts, self.outcome = [], AutoResult("logged_in")

    due = LoggedOutTickTests.due

    def turn_on(self):
        looks = self.due()
        self.app.cfg = replace(self.app.cfg, auto_login=True)

        class Auto:
            def run(inner):
                test = self
                test.attempts.append(test.state["now"])
                if test.outcome.ok:
                    test.kind = "favorites"                     # the app is logged in now
                return test.outcome
        self.app.auto_login_factory = lambda attention: Auto()
        return looks

    def test_the_display_is_held_on_during_the_login(self):
        # 2026-10-17/18: the display went dark in the middle of the automatic login
        self.turn_on()
        seen = []

        class Auto:
            def run(inner):
                seen.append(self.awake_depth)
                self.kind = "favorites"
                return self.AutoResult("logged_in")
        self.app.auto_login_factory = lambda attention: Auto()
        self.app.cmd_tick(False, False)
        self.assertEqual(seen, [1])

    def test_it_is_off_by_default_and_the_old_message_is_sent(self):
        self.due()
        built = []
        self.app.auto_login_factory = lambda attention: built.append(attention)      # an exception would be caught: count
        self.app.cmd_tick(False, False)
        self.assertEqual(built, [], "the program must not try to log in")
        self.assertEqual(self.reporter.tells[-1][0], "Club booking: the club app is logged out")
        self.assertNotIn("Automatic login", self.reporter.tells[-1][1])

    def test_a_login_that_works_lets_the_class_be_booked_at_once_and_sends_no_message(self):
        self.turn_on()
        tells = len(self.reporter.tells)
        self.app.cmd_tick(False, False)
        self.assertEqual(len(self.attempts), 1)
        self.assertIn(("alex", "2026-10-14T06:00|powergym"), self.fake.button_taps, "booked in the same wake-up")
        self.assertFalse([t for t, _ in self.reporter.tells[tells:] if "logged out" in t])

    def test_the_banner_is_shown_before_the_program_takes_the_keyboard(self):
        self.turn_on()
        self.state["idle"] = 5.0                       # the user is at the Mac: the banner warns him
        self.app.cmd_tick(False, False)
        self.assertTrue(any("keep your hands off" in text for _, text in self.reporter.macs))

    def test_a_login_that_fails_tells_the_phone_why_once_and_books_nothing(self):
        self.outcome = self.AutoResult("no_sms", "no SMS from hashmonaim arrived in 120 seconds: is Text Message Forwarding on for this Mac?")
        self.turn_on()
        taps = len(self.fake.button_taps)
        for _ in range(3):
            self.app.cmd_tick(False, False)
        title, body = self.reporter.tells[-1]
        self.assertEqual(title, "Club booking: the club app is logged out")
        self.assertIn("Automatic login", body)
        self.assertIn("Text Message Forwarding", body)
        self.assertEqual(len([t for t, _ in self.reporter.tells if "logged out" in t]), 1)
        self.assertEqual(len(self.fake.button_taps), taps)

    def test_it_never_tries_for_a_plan_refresh_nor_for_an_update_nor_when_paused_nor_in_a_dry_run(self):
        self.turn_on()
        from club_booking.cli import TickDecision
        refresh = TickDecision("replan", (), ())
        self.assertFalse(self.app._may_log_in_by_itself("login", refresh, False), "no class is waiting")
        run_due = TickDecision("run_due", ("alex",), ())
        self.assertTrue(self.app._may_log_in_by_itself("login", run_due, False))
        self.assertTrue(self.app._may_log_in_by_itself("code", run_due, False))
        self.assertFalse(self.app._may_log_in_by_itself("update", run_due, False))
        self.assertFalse(self.app._may_log_in_by_itself("locked", run_due, False), "the app's 10-minute lock")
        self.assertFalse(self.app._may_log_in_by_itself("login", run_due, True))
        (self.tmp / "PAUSE").write_text("")
        self.assertFalse(self.app._may_log_in_by_itself("login", run_due, False))

    def test_a_crash_inside_the_login_is_logged_and_the_phone_gets_the_usual_message(self):
        self.turn_on()

        class Broken:
            def run(inner):
                raise RuntimeError("the screen tool failed")
        self.app.auto_login_factory = lambda attention: Broken()
        self.app.cmd_tick(False, False)
        self.assertEqual(self.reporter.tells[-1][0], "Club booking: the club app is logged out")
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("auto_login_failed", events)

    def test_the_attempt_is_made_under_the_attention_guard_of_an_urgent_run(self):
        self.turn_on()
        seen, test = [], self

        class Auto:
            def run(inner):
                return test.outcome

        def factory(attention):
            seen.append(attention.patient)
            return Auto()
        self.app.auto_login_factory = factory
        self.app.cmd_tick(False, False)
        self.assertEqual(seen, [True], "it waits for a pause in the typing, like an opening")

    def test_the_login_message_after_a_failed_attempt_is_taken_back_when_a_run_works(self):
        self.outcome = self.AutoResult("no_sms", "no SMS arrived")
        self.turn_on()
        self.app.cmd_tick(False, False)
        self.assertEqual([m["kind"] for m in self.store.messages().values()], ["login"])
        self.kind = "favorites"
        self.app.cmd_tick(False, False)
        self.assertEqual(self.store.messages(), {})


class FakeInbox:
    def __init__(self, count=2, age=3600.0, error=None, last=60.0):
        self.count, self.age, self.error, self.last = count, age, error, last

    def last_message_age(self, now):
        return self.last

    def probe(self, now):
        if self.error:
            raise self.error
        return self.count, self.age


class AutoLoginChecksTests(AppCase):
    """The checks of the automatic login: doctor (from Terminal) and the wake-up test (from launchd, where Terminal's
    permissions do not count). Neither reads a message: only a count and an age."""

    def setUp(self):
        self.patches = [mock.patch("club_booking.cli.get_api_key", return_value="k"),
                        mock.patch("club_booking.cli.calendar_names", return_value=["Alex", "Kids"]),
                        mock.patch("club_booking.cli.check_mirroir_permissions", return_value=[]),
                        mock.patch("club_booking.cli.check_mirroir_config", return_value=[])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def doctor(self, inbox, phone_ok=True, **cfg):
        self.build()
        (self.tmp / "tools" / "bin").mkdir(parents=True, exist_ok=True)
        self.app.cfg = replace(self.app.cfg, auto_login=True, **cfg)
        self.app.inbox_factory = lambda: inbox
        self.fake.window_region = lambda: (0, 0, 1024, 800)

        def key(service):
            if service == self.app.cfg.phone_keychain_service and not phone_ok:
                raise SecretError("no phone")
            return "k"
        with mock.patch("club_booking.cli.get_api_key", side_effect=key):
            self.app.cmd_doctor()
        return "\n".join(self.lines)

    def test_doctor_says_nothing_about_the_messages_when_the_automatic_login_is_off(self):
        self.build()
        (self.tmp / "tools" / "bin").mkdir(parents=True, exist_ok=True)
        self.fake.window_region = lambda: (0, 0, 1024, 800)
        self.app.inbox_factory = lambda: self.fail("must not be read")
        self.app.cmd_doctor()
        self.assertNotIn("Messages", "\n".join(self.lines))

    def test_doctor_checks_the_phone_number_and_the_messages_and_shows_no_text(self):
        out = self.doctor(FakeInbox(count=3, age=7200.0))
        self.assertIn("OK    phone number found in the Keychain", out)
        self.assertIn("OK    the Messages database can be read; 3 messages from Hashmonaim, the newest 2 hours old", out)

    def test_doctor_says_when_there_is_no_message_from_the_sender_yet(self):
        out = self.doctor(FakeInbox(count=0, age=None))
        self.assertIn("no message from Hashmonaim yet", out)

    def test_doctor_says_when_the_mac_has_received_nothing_for_days(self):
        out = self.doctor(FakeInbox(count=3, age=7200.0, last=59 * 86400.0))
        self.assertIn("FAIL  the Mac has received no message for 59 days", out)
        self.assertIn("Text Message Forwarding", out)

    def test_doctor_is_content_when_messages_arrive(self):
        out = self.doctor(FakeInbox(count=3, age=7200.0, last=3600.0))
        self.assertIn("OK    the Mac receives messages (the newest is 60 minutes old)", out)

    def test_doctor_says_how_to_fix_a_database_that_cannot_be_read(self):
        from club_booking.sms import SmsError
        out = self.doctor(FakeInbox(error=SmsError("cannot read the Messages database (unable to open database file). Give Full Disk Access ...")))
        self.assertIn("FAIL  cannot read the Messages database", out)
        self.assertIn("Python.app", out, "the exact program to allow is named")

    def test_doctor_says_how_to_store_the_phone_number(self):
        out = self.doctor(FakeInbox(), phone_ok=False)
        self.assertIn("FAIL  no phone number in the Keychain", out)
        self.assertIn("security add-generic-password", out)

    def wake(self, inbox, events_problem=""):
        self.build()
        self.app.cfg = replace(self.app.cfg, auto_login=True)
        self.app.inbox_factory = lambda: inbox
        self.app.ax_factory = lambda: None
        with mock.patch("club_booking.cli.system.system_events_problem", return_value=events_problem):
            return self.app._wake_test_problems()

    def test_the_wake_up_test_checks_the_messages_and_system_events_from_the_job(self):
        from club_booking.sms import SmsError
        self.assertEqual(self.wake(FakeInbox()), [])
        problems = self.wake(FakeInbox(error=SmsError("cannot read the Messages database")))
        self.assertTrue(any(p.startswith("Messages: cannot read") for p in problems))
        problems = self.wake(FakeInbox(), events_problem="Not authorized to send Apple events to System Events. (-1743)")
        self.assertTrue(any(p.startswith("System Events:") and "-1743" in p for p in problems))

    def test_the_wake_up_test_skips_both_when_the_automatic_login_is_off(self):
        self.build()
        self.app.ax_factory = lambda: None
        self.app.inbox_factory = lambda: self.fail("must not be read")
        with mock.patch("club_booking.cli.system.system_events_problem", side_effect=AssertionError("not asked")):
            self.assertEqual(self.app._wake_test_problems(), [])


class LoginAutoCommandTests(AppCase):
    """`login auto`: the user starts the whole automatic login by hand, also before switching it on in the config."""

    def test_it_runs_the_login_once_and_says_the_result_without_secrets(self):
        from club_booking.autologin import AutoResult
        self.build()
        seen = []

        class Auto:
            def run(inner):
                return AutoResult("logged_in")
        self.app.auto_login_factory = lambda attention: (seen.append(attention.patient), Auto())[1]
        self.assertEqual(self.app.cmd_login_auto(), 0)
        self.assertIn("Logged in.", self.lines[-1])
        self.assertEqual(seen, [True])

    def test_the_display_is_held_on_during_the_login(self):
        from club_booking.autologin import AutoResult
        self.build()
        seen = []

        class Auto:
            def run(inner):
                seen.append(self.awake_depth)
                return AutoResult("logged_in")
        self.app.auto_login_factory = lambda attention: Auto()
        self.app.cmd_login_auto()
        self.assertEqual((seen, self.awake_depth), ([1], 0))

    def test_a_failure_says_why_and_returns_one(self):
        from club_booking.autologin import AutoResult
        self.build()

        class Auto:
            def run(inner):
                return AutoResult("no_sms", "no SMS from hashmonaim arrived in 120 seconds")
        self.app.auto_login_factory = lambda attention: Auto()
        self.assertEqual(self.app.cmd_login_auto(), 1)
        self.assertIn("no SMS from hashmonaim", "\n".join(self.lines))

    def test_it_refuses_while_paused_and_when_the_mac_is_locked(self):
        self.build(locked=True)
        self.app.auto_login_factory = lambda attention: self.fail("must not start")
        self.assertEqual(self.app.cmd_login_auto(), 1)
        self.assertIn("locked", "\n".join(self.lines).lower())
        self.state["locked"] = False
        (self.tmp / "PAUSE").write_text("")
        self.lines.clear()
        self.assertEqual(self.app.cmd_login_auto(), 1)
        self.assertIn("Paused", "\n".join(self.lines))


class WakeTestAxTests(AppCase):
    def run_test(self, ax, key="test-key", control_problem=""):
        self.build()
        self.app.ax_factory = lambda: ax
        self.club_app.control_problem = control_problem
        keychain = mock.patch("club_booking.cli.get_api_key", return_value=key) if key else \
            mock.patch("club_booking.cli.get_api_key", side_effect=SecretError("no key in the Keychain"))
        with mock.patch("club_booking.cli.check_mirroir_permissions", return_value=[]), \
             mock.patch("club_booking.cli.calendar_names", return_value=["Alex", "Kids"]), keychain:
            self.app.cmd_tick(True, True)
        return self.reporter.macs[-1][1]

    def ax_ok(self):
        from club_booking.ax import AxDump, AxElement

        class Ax:
            def dump(self):
                return AxDump(AxElement("AXWindow", "x", "", 0, 0, 1024, 800), [])
        return Ax()

    def test_the_wake_up_test_says_when_the_keychain_cannot_be_read(self):
        # under launchd a failing Keychain read would stop every due run before the first tap
        body = self.run_test(self.ax_ok(), key=None)
        self.assertIn("Keychain: no key in the Keychain", body)
        self.assertNotIn("All permissions are fine", body)

    def test_the_wake_up_test_says_when_the_club_app_cannot_be_brought_to_the_front(self):
        body = self.run_test(self.ax_ok(), control_problem="Not authorized to send Apple events to Runner 2. (-1743)")
        self.assertIn("club app: Not authorized", body)
        self.assertNotIn("All permissions are fine", body)

    def test_the_wake_up_test_is_fine_when_everything_works(self):
        self.assertIn("All permissions are fine", self.run_test(self.ax_ok()))

    def test_the_wake_up_test_says_when_accessibility_is_not_allowed_for_the_job(self):
        from club_booking.ax import AxError

        class Ax:
            def dump(self):
                raise AxError("the app Runner has no window (is accessibility allowed for this program?)")

        body = self.run_test(Ax())
        self.assertIn("Accessibility", body)
        self.assertNotIn("All permissions are fine", body)

    def test_the_wake_up_test_is_fine_when_accessibility_works(self):
        from club_booking.ax import AxDump, AxElement

        class Ax:
            def dump(self):
                return AxDump(AxElement("AXWindow", "x", "", 0, 0, 1024, 800), [])

        self.assertIn("All permissions are fine", self.run_test(Ax()))


class CommandTests(AppCase):
    def test_run_refuses_when_locked_or_paused(self):
        self.build(locked=True)
        self.assertEqual(self.app.cmd_run([], False, False), 1)
        self.state["locked"] = False
        (self.tmp / "PAUSE").write_text("")
        self.assertEqual(self.app.cmd_run([], False, False), 0)
        self.assertEqual(self.screen.used, 0)

    def test_run_for_one_person_and_dry_run(self):
        self.build()
        self.assertEqual(self.app.cmd_run(["sam"], False, False), 0)
        self.assertEqual({p for p, k in self.fake.button_taps}, {"sam"})
        self.build()
        self.assertEqual(self.app.cmd_run([], True, False), 0)
        self.assertEqual(self.fake.button_taps, [])
        self.assertEqual(self.reporter.tells, [], "a dry run does not message the phone")
        self.assertTrue(any("Would book" in l for l in self.lines))
        text = "\n".join(self.lines)
        self.assertIn("Read for Alex: 4 classes", text)
        self.assertIn("not_open   Wed 14/10 06:00  PowerGym, opens Tue 13/10 06:00", text)

    def test_a_run_removes_old_evidence_pictures(self):
        self.build()
        old = self.store.save_evidence("old", b"x")
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        self.app.cmd_run([], False, False)
        self.assertFalse(old.exists())

    def test_a_logged_out_app_is_told_to_the_phone_once(self):
        self.build()
        self.fake.mode = "login"
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(len(self.reporter.tells), 1)
        self.assertIn("logged out", self.reporter.tells[0][1])
        self.assertEqual(self.club_app.restarts, 0)

    def test_a_run_the_user_started_by_hand_sends_no_phone_message(self):
        # 2026-10-15: he reads the result on the console; a reminder on top of it is a redundant reminder
        self.build()
        self.fake.mode = "login"
        self.app.cmd_run([], False, False)
        self.assertEqual(self.reporter.tells, [])
        self.assertTrue(any("logged out" in line for line in self.lines))

    def test_a_run_that_finds_the_app_logged_out_sends_the_login_message_and_no_problem_report(self):
        # 2026-10-15: the phone had "0 booked, 1 to check / Problem (Alex): the club app is logged out" AND "the club app
        # is logged out": two reminders for one cause
        self.build()
        self.fake.mode = "login"
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual([t for t, _ in self.reporter.tells], ["Club booking: the club app is logged out"])
        self.assertIn("Alex, Sam, Robin", self.reporter.tells[0][1])
        self.assertEqual([m["kind"] for m in self.store.messages().values()], ["login"], "taken back after a good run")

    def test_the_look_before_a_run_and_the_run_itself_make_one_login_message_a_day(self):
        self.build()
        self.app._tell_daily("login", "Club booking: the club app is logged out", "Seen before the run.", kind="login")
        self.fake.mode = "login"
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(len(self.reporter.tells), 1)

    def test_an_update_message_is_its_own_kind_and_a_real_problem_still_has_its_report(self):
        from club_booking.runner import LOGGED_OUT, UPDATE_NEEDED
        self.build()
        result = RunResult("r", [PersonResult("alex", error=UPDATE_NEEDED),
                                 PersonResult("sam", error="RunError: the list jumped"),
                                 PersonResult("robin")], None, [])
        self.app._report_run(result, dry_run=False)
        titles = [t for t, _ in self.reporter.tells]
        self.assertIn("Club booking: the club app needs an update", titles)
        problem = [b for t, b in self.reporter.tells if t == "Club booking: 1 to check"]
        self.assertEqual(len(problem), 1)
        self.assertIn("Problem (Sam)", problem[0])
        self.assertNotIn("Alex", problem[0], "the update message already says it")
        self.assertEqual(sorted(m["kind"] for m in self.store.messages().values()), ["problem", "update"])

    def test_a_dry_run_sends_no_login_message(self):
        self.build()
        self.app._report_run(RunResult("r", [PersonResult("alex", error=LOGGED_OUT_TEXT)], None, []), dry_run=True)
        self.assertEqual(self.reporter.tells, [])

    def test_a_stuck_app_is_restarted_by_a_run(self):
        self.build()
        self.fake.mode = "stuck"
        self.assertEqual(self.app.cmd_run(["alex"], False, False), 0)
        self.assertEqual(self.club_app.restarts, 1)
        self.assertEqual(len(self.fake.button_taps), 1)

    def test_the_same_report_is_sent_once_a_day(self):
        self.build()
        self.vision.fail_for = "sam"
        sam = [self.cfg.person("sam")]
        self.app.do_run(sam, False, countdown=False)
        self.app.do_run(sam, False, countdown=False)
        self.assertEqual(len(self.reporter.tells), 1)
        self.state["now"] = NOW + timedelta(days=1)
        self.app.do_run(sam, False, countdown=False)
        self.assertEqual(len(self.reporter.tells), 2)

    def test_run_survives_an_unexpected_exception(self):
        self.build()

        def broken():
            raise RuntimeError("boom")

        self.app.vision_factory = broken
        self.assertEqual(self.app.cmd_run([], False, False), 1)
        self.assertIn("RuntimeError", "\n".join(self.lines))

    def test_rearm_tells_the_scheduler_what_time_it_is(self):
        self.build()
        self.app.cmd_run([], False, False)
        self.assertTrue(self.agents.nows)
        self.assertEqual(self.agents.nows[-1], local_naive(NOW))

    def test_a_run_arms_nothing_before_the_scheduler_is_installed(self):
        self.build()
        self.agents.installed = False
        self.app.cmd_run([], False, False)
        self.assertEqual((self.agents.armed, self.agents.disarmed, self.agents.pruned), ([], 0, 0))

    def test_rearm_with_nothing_left_disarms(self):
        self.build({"alex": [], "sam": [], "robin": []})
        self.app.cmd_run([], False, False)
        self.assertEqual(self.agents.disarmed, 1)

    def test_rearm_retries_in_a_minute_when_something_that_should_be_open_is_not(self):
        self.build({"alex": [card("Ghost", 13, 8, 0, NOT_OPEN)], "sam": [], "robin": []})
        self.app.cmd_run([], False, False)   # Ghost opened on 12/10 08:00, but the app still shows it closed
        self.assertEqual(self.agents.armed[-1], local_naive(NOW + timedelta(minutes=1)))

    def test_status_wake_command_arm_test_and_prune(self):
        self.build()
        self.app.cmd_run([], False, False)
        self.lines.clear()
        self.assertEqual(self.app.cmd_status(), 0)
        text = "\n".join(self.lines)
        self.assertIn("Next opening: Tue 13/10 06:00 (Alex - PowerGym", text)
        self.lines.clear()
        self.assertEqual(self.app.cmd_wake_command(), 0)
        printed = "\n".join(self.lines)
        plan = self.store.load_plan()
        times = {local_naive(o.opens_at) for o in plan.openings if o.opens_at > NOW and o.ident not in self.store.handled()}
        self.assertGreaterEqual(len(times), 3)
        self.assertEqual(printed.count("pmset schedule wake"), len(times), "one command for every planned opening")
        self.assertIn("administrator", printed)
        self.assertIn("Armed wake-ups:", text)
        self.assertLess(len(text), 700, "a list of many agents is a count, not a wall of labels")
        self.app.cmd_arm_test(3)
        self.assertEqual(self.agents.tests[-1], local_naive(NOW + timedelta(minutes=3)))
        self.assertEqual(self.app.cmd_prune_reports(7), 0)
        self.assertEqual(self.reporter.pruned, [7])

    def test_doctor_reports_each_check(self):
        self.build()
        (self.tmp / "tools" / "bin").mkdir(parents=True)
        (self.tmp / "tools" / "bin" / "mirroir-mcp").write_text("x")
        (self.tmp / "tools" / "bin" / "ocr").write_text("x")
        self.fake.window_region = lambda: (-1377, 101, 1024, 800)
        with mock.patch("club_booking.cli.get_api_key", return_value="k"), \
             mock.patch("club_booking.cli.calendar_names", return_value=["Alex", "Kids"]), \
             mock.patch("club_booking.cli.check_mirroir_permissions", return_value=[]), \
             mock.patch("club_booking.cli.check_mirroir_config", return_value=[]):
            self.assertEqual(self.app.cmd_doctor(), 0)
        self.assertTrue(any(l.startswith("OK") and "API key" in l for l in self.lines))
        with mock.patch("club_booking.cli.get_api_key", side_effect=SecretError("no key")), \
             mock.patch("club_booking.cli.calendar_names", return_value=["Alex"]), \
             mock.patch("club_booking.cli.check_mirroir_permissions", return_value=["Screen Recording is not allowed for this program"]), \
             mock.patch("club_booking.cli.check_mirroir_config", return_value=["bad"]):
            self.lines.clear()
            self.assertEqual(self.app.cmd_doctor(), 1)
        self.assertTrue(any(l.startswith("FAIL") for l in self.lines))


class FakeLogin:
    def __init__(self, raises=None, result="code_screen"):
        self.raises, self.result, self.calls = raises, result, []

    def request_code(self, phone):
        self.calls.append(("request", phone))
        if self.raises:
            raise self.raises
        return self.result

    def enter_code(self, code):
        self.calls.append(("code", code))
        if self.raises:
            raise self.raises
        return "logged_in"


class LoginCommandTests(AppCase):
    def build_with(self, login):
        self.build()
        self.app.login_factory = lambda: login
        return self.app

    def test_request_prints_what_to_do_next(self):
        login = FakeLogin()
        app = self.build_with(login)
        self.assertEqual(app.cmd_login_request("0500000000"), 0)
        self.assertEqual(login.calls, [("request", "0500000000")])
        self.assertTrue(any("code" in line.lower() for line in self.lines))

    def test_a_blocked_login_is_told_and_returns_1(self):
        app = self.build_with(FakeLogin(raises=LoginBlocked("too many code requests: wait 8 minutes")))
        self.assertEqual(app.cmd_login_request("0500000000"), 1)
        self.assertIn("wait 8 minutes", "\n".join(self.lines))

    def test_the_code_is_typed_and_the_result_told(self):
        login = FakeLogin()
        app = self.build_with(login)
        self.assertEqual(app.cmd_login_code("123456"), 0)
        self.assertEqual(login.calls, [("code", "123456")])
        self.assertEqual(self.build_with(FakeLogin(raises=LoginError("the code must be exactly 6 digits"))).cmd_login_code("1"), 1)

    def test_no_phone_number_means_no_login_step(self):
        app = self.build_with(FakeLogin())
        self.assertEqual(app.cmd_login_request(""), 1)


class DoctorAxTests(AppCase):
    def run_doctor(self, ax):
        self.build()
        self.app.ax_factory = lambda: ax
        (self.tmp / "tools" / "bin").mkdir(parents=True)
        (self.tmp / "tools" / "bin" / "mirroir-mcp").write_text("x")
        (self.tmp / "tools" / "bin" / "ocr").write_text("x")
        self.fake.window_region = lambda: (0, 0, 1024, 800)
        with mock.patch("club_booking.cli.get_api_key", return_value="k"), \
             mock.patch("club_booking.cli.calendar_names", return_value=["Alex", "Kids"]), \
             mock.patch("club_booking.cli.check_mirroir_permissions", return_value=[]), \
             mock.patch("club_booking.cli.check_mirroir_config", return_value=[]):
            return self.app.cmd_doctor()

    def test_doctor_shows_that_accessibility_works(self):
        from club_booking.ax import AxDump, AxElement

        class Ax:
            def dump(self):
                return AxDump(AxElement("AXWindow", "x", "", 0, 0, 1024, 800),
                              [AxElement("AXStaticText", "שיעורים מועדפים", "", 0, 0, 1, 1)])

        self.assertEqual(self.run_doctor(Ax()), 0)
        self.assertTrue(any(l.startswith("OK") and "accessibility" in l and "favorites" in l for l in self.lines))

    def test_doctor_fails_when_accessibility_does_not_work(self):
        from club_booking.ax import AxError

        class Ax:
            def dump(self):
                raise AxError("the app Runner is not running")

        self.assertEqual(self.run_doctor(Ax()), 1)
        self.assertTrue(any(l.startswith("FAIL") and "accessibility" in l for l in self.lines))


class ParserTests(unittest.TestCase):
    def test_login_commands(self):
        args = parser().parse_args(["login", "request", "--phone", "0500000000"])
        self.assertEqual((args.command, args.step, args.phone), ("login", "request", "0500000000"))
        args = parser().parse_args(["login", "code", "123456"])
        self.assertEqual((args.command, args.step, args.code), ("login", "code", "123456"))

    def test_parser(self):
        args = parser().parse_args(["run", "--person", "sam", "--person", "robin", "--dry-run"])
        self.assertEqual((args.command, args.person, args.dry_run), ("run", ["sam", "robin"], True))
        self.assertEqual(parser().parse_args(["arm-test"]).in_minutes, 2)


class CalendarAuditWiringTests(AppCase):
    def audited(self, mode, dry_run=False):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit=mode)
        self.app.do_run(list(self.app.cfg.people), dry_run, countdown=False)

    def finding(self, status="added", person="sam", name="Robotics"):
        return calendar_audit.Finding(person, name, datetime(2026, 10, 14, 17, 15, tzinfo=TZ), status)

    def test_the_audit_does_not_run_unless_it_is_switched_on(self):
        self.audited("off")
        self.assertEqual(self.audit_modes, [])

    def test_a_run_audits_what_it_read_and_tells_the_phone_after_the_booking_report(self):
        self.build()
        self.audit.findings = [self.finding()]
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(self.audit_modes, ["write"])
        entries, horizon = self.audit.calls[0]
        self.assertEqual([k for k, _ in entries], ["alex", "sam", "robin"])
        self.assertEqual(horizon, self.app.cfg.horizon_days)
        self.assertEqual([t for t, _ in self.reporter.tells], ["Club booking: 1 to check"],
                         "a booking and an added event are no phone messages; only the full class that was not booked is")
        self.assertNotIn("Booked", self.reporter.tells[0][1])
        events = [json.loads(line) for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "calendar_audit_added" for e in events), "it is in the log")

    def test_a_calendar_problem_is_in_the_log_and_not_on_the_phone(self):
        # the user's rule of 2026-10-15: a message only for a decision or an action; the calendar is neither
        self.build()
        self.audit.findings = [self.finding("failed")]
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual([t for t, _ in self.reporter.tells if "calendar" in t.lower()], [])
        events = [json.loads(line) for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        report = [e for e in events if e["event"] == "calendar_audit_report"]
        self.assertEqual(len(report), 1)
        self.assertIn("1 to check", report[0]["title"])

    def test_the_next_wake_up_is_armed_before_the_slow_calendar_pass(self):
        self.audited("write")
        self.assertGreaterEqual(self.audit.armed_when_called[0], 1)

    def test_a_dry_run_with_the_audit_off_does_not_touch_calendar(self):
        self.audited("off", dry_run=True)
        self.assertEqual(self.audit_modes, [])

    def test_the_audit_waits_when_an_opening_is_about_to_happen(self):
        # it holds the run lock for a minute or more: an opening agent that fires meanwhile would find the lock busy
        self.build()
        result = self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        soonest = min(o.opens_at for o in self.store.load_plan().openings if o.opens_at > self.state["now"])
        self.state["now"] = soonest - timedelta(minutes=5)
        self.audit.calls.clear()
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(self.audit.calls, [])
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("calendar_audit_deferred", events)
        self.state["now"] = soonest - timedelta(minutes=30)
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(len(self.audit.calls), 1, "with half an hour to spare the audit runs")

    def test_a_dry_run_reports_even_when_an_opening_is_overdue(self):
        # a dry run keeps the saved plan, so an overdue opening stays "due"; the read-only report must not wait for it
        self.build()
        result = self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        soonest = min(o.opens_at for o in self.store.load_plan().openings if o.opens_at > self.state["now"])
        self.state["now"] = soonest + timedelta(minutes=1)          # that opening is now overdue and unhandled
        self.audit.calls.clear()
        self.app._audit_calendars(result, dry_run=True)
        self.assertEqual(len(self.audit.calls), 1)
        self.assertEqual(self.audit_modes[-1], "report")
        self.audit.calls.clear()
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(self.audit.calls, [], "a real run still waits for the booking")

    def test_a_dry_run_only_reports_and_tells_the_phone_nothing(self):
        self.build()
        self.audit.findings = [self.finding("would_add")]
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        self.app.do_run(list(self.app.cfg.people), True, countdown=False)
        self.assertEqual(self.audit_modes, ["report"], "a dry run changes nothing, also not the calendar")
        self.assertEqual(self.reporter.tells, [])
        self.assertTrue(any("would add Sam - Robotics" in line for line in self.lines))

    def test_nothing_found_is_silent(self):
        self.audited("write")
        self.assertEqual([t for t, _ in self.reporter.tells if "calendar" in t.lower()], [])

    def test_a_person_whose_read_failed_is_not_audited(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        registered = card("Judo", 14, 16, 30, CardState.REGISTERED)
        result = RunResult("r", [PersonResult("alex", cards=[registered]),
                                 PersonResult("sam", cards=[registered], error="boom"),
                                 PersonResult("robin", cards=[])], None, [])
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual([k for k, _ in self.audit.calls[0][0]], ["alex"])

    def test_a_class_booked_by_hand_that_is_not_a_favorite_is_audited_too(self):
        # the booked tab lists every class the person is registered for; favorites list only some of them
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        favorite = card("Judo", 14, 16, 30, CardState.REGISTERED)
        outside = card("Yoga", 15, 7, 0, CardState.REGISTERED)
        result = RunResult("r", [PersonResult("alex", cards=[favorite], booked=[favorite, outside]),
                                 PersonResult("sam", cards=[], booked=[outside])], None, [])
        self.app._audit_calendars(result, dry_run=False)
        entries, _ = self.audit.calls[0]
        self.assertEqual(entries, [("alex", [favorite.key, outside.key]), ("sam", [outside.key])],
                         "each class once, and a person with only booked-tab classes is audited")

    def test_only_people_whose_booked_tab_was_read_to_its_end_are_checked_for_unregistered_classes(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        outside = card("Yoga", 15, 7, 0, CardState.REGISTERED)
        result = RunResult("r", [PersonResult("alex", cards=[], booked=[outside], booked_read=True, booked_complete=True),
                                 PersonResult("sam", cards=[], booked=[outside], booked_read=True),
                                 PersonResult("robin", cards=[], booked=[], booked_read=True, booked_complete=True,
                                              error="boom")], None, [])
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(self.audit.removal_calls, [[("alex", [outside.key])]],
                         "Sam's list was cut short, and Robin's run failed: neither is judged")

    def test_a_person_with_nothing_on_a_complete_booked_tab_is_still_checked(self):
        # an empty list is a true answer: the user may have unregistered from everything
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        result = RunResult("r", [PersonResult("sam", cards=[], booked=[], booked_read=True, booked_complete=True)],
                           None, [])
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(self.audit.removal_calls, [[("sam", [])]])
        self.assertEqual(self.audit.calls, [], "nothing to add for him")

    def test_the_removal_runs_before_the_adding(self):
        # 2026-10-15: two classes at the same minute (chess cancelled, football booked): the old event must go first, or
        # it counts as the new class's event and the new class never gets one
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        result = RunResult("r", [PersonResult("sam", cards=[card("Judo", 14, 16, 30, CardState.REGISTERED)], booked=[],
                                              booked_read=True, booked_complete=True)], None, [])
        self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(self.audit.order, ["remove", "add"])

    def test_the_removal_runs_after_the_adding_and_its_findings_are_reported(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        self.audit.removal_findings = [self.finding("removed"), self.finding("check", name="calendar")]
        self.audit.removal_findings[1] = calendar_audit.Finding("sam", "calendar", NOW, "check", "5 classes look unregistered")
        result = RunResult("r", [PersonResult("sam", cards=[], booked=[], booked_read=True, booked_complete=True)],
                           None, [])
        self.app._audit_calendars(result, dry_run=False)
        events = [json.loads(line) for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        removed = [e for e in events if e["event"] == "calendar_audit_removed"]
        self.assertEqual(len(removed), 1, "what was deleted is in the log")
        self.assertEqual(self.reporter.tells, [])
        report = [e for e in events if e["event"] == "calendar_audit_report"]
        self.assertTrue(any("5 classes look unregistered" in e["body"] for e in report), "and the check is in the log")

    def test_a_dry_run_asks_the_removal_pass_to_report_only(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        result = RunResult("r", [PersonResult("sam", cards=[], booked=[], booked_read=True, booked_complete=True)],
                           None, [])
        self.app._audit_calendars(result, dry_run=True)
        self.assertEqual(self.audit_modes, ["report"])

    def test_a_failing_removal_pass_never_breaks_the_run_and_the_adding_is_kept(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        self.audit.removal_error = RuntimeError("Calendar is not running")
        self.audit.findings = [self.finding()]
        result = RunResult("r", [PersonResult("sam", cards=[card("Judo", 14, 16, 30, CardState.REGISTERED)],
                                              booked_read=True, booked_complete=True)], None, [])
        self.app._audit_calendars(result, dry_run=False)
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("calendar_audit_added", events)
        self.assertIn("calendar_audit_failed", events)

    def test_a_failing_audit_never_breaks_the_run(self):
        self.build()
        self.audit.error = RuntimeError("Calendar is not running")
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        result = self.app.do_run(list(self.app.cfg.people), False, countdown=False)
        self.assertEqual(len(result.people), 3)
        self.assertEqual([t for t, _ in self.reporter.tells if "calendar" in t.lower()], [], "the log, not the phone")
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("calendar_audit_failed", events)

    def test_the_audit_waits_for_no_one_when_another_run_is_in_progress(self):
        self.build()
        self.app.cfg = replace(self.app.cfg, calendar_audit="write")
        result = RunResult("r", [PersonResult("alex", cards=[card("Judo", 14, 16, 30, CardState.REGISTERED)])], None, [])
        with Store(self.tmp).run_lock():
            self.app._audit_calendars(result, dry_run=False)
        self.assertEqual(self.audit.calls, [], "the other run does its own audit")


if __name__ == "__main__":
    unittest.main()
