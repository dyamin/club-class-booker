import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from club_booking import calendar_audit
from club_booking.calendar_audit import CalendarAudit, CalEvent, create_event, list_events
from club_booking.config import Person
from club_booking.models import CardState, ClassCard
from club_booking.state import Store

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 13, 14, 0, tzinfo=TZ)
ALEX = Person("alex", "Alex", 176, "Alex", "")
SAM = Person("sam", "Sam", 214, "Kids", "Sam - ")
ROBIN = Person("robin", "Robin", 253, "Kids", "Robin - ")
PEOPLE = [ALEX, SAM, ROBIN]


def card(name, day, hour, minute, state=CardState.REGISTERED, minutes=45):
    start = datetime(2026, 10, day, hour, minute, tzinfo=TZ)
    return ClassCard(name, start, start + timedelta(minutes=minutes), state)


def naive(dt):
    return dt.replace(tzinfo=None)


class FakeCalendarOsa:
    """Plays the two AppleScript templates against a list of events, and keeps what it was asked."""

    def __init__(self, events=None, create_alarms=(-60,), create_result="ok", fail_create=False,
                 delete_result="deleted", fail_delete=False, fail_list=False):
        self.events = list(events or [])          # (calendar, CalEvent)
        self.calls = []
        self.create_alarms, self.create_result, self.fail_create = create_alarms, create_result, fail_create
        self.delete_result, self.fail_delete, self.fail_list = delete_result, fail_delete, fail_list
        self.created, self.deleted = [], []
        self.repeating = set()      # listed whatever the window, with the date of their first occurrence
        self.strip_output = False   # run_applescript strips the output: a trailing tab is lost

    def add(self, calendar, title, start, minutes=45, alarms=(-60,), repeating=False):
        self.events.append((calendar, CalEvent(f"u{len(self.events)}", title, naive(start),
                                               naive(start) + timedelta(minutes=minutes), alarms)))
        if repeating:
            self.repeating.add(self.events[-1][1].uid)

    def __call__(self, script, argv):
        self.calls.append((script, list(argv)))
        if script == calendar_audit._LIST:
            if self.fail_list:
                raise RuntimeError("Calendar got an error")
            calendar = argv[0]
            lo = datetime(*map(int, argv[1:6]))
            hi = datetime(*map(int, argv[6:11]))
            lines = []
            for cal, e in self.events:
                if cal == calendar and (lo <= e.start < hi or e.uid in self.repeating):
                    parts = [e.uid, e.title, e.start.year, e.start.month, e.start.day, e.start.hour, e.start.minute,
                             e.end.year, e.end.month, e.end.day, e.end.hour, e.end.minute,
                             ",".join(str(a) for a in e.alarms)]
                    lines.append("\t".join(str(p) for p in parts))
            text = "\n".join(lines)
            return text.strip() if self.strip_output else text
        if script == calendar_audit._CREATE:
            if self.fail_create:
                raise RuntimeError("Calendar got an error")
            calendar, title = argv[0], argv[1]
            start = datetime(*map(int, argv[2:7]))
            minutes = int(argv[7])
            self.created.append((calendar, title, start, minutes))
            if self.create_result == "ok":
                self.add(calendar, title, start.replace(tzinfo=TZ), minutes, self.create_alarms)
                return "ok\t" + self.events[-1][1].uid + "\t" + ",".join(str(a) for a in self.create_alarms)
            return self.create_result
        if script == calendar_audit._DELETE:
            if self.fail_delete:
                raise RuntimeError("Calendar got an error")
            calendar, uid = argv[0], argv[1]
            if self.delete_result != "deleted":
                return self.delete_result
            self.deleted.append((calendar, uid, datetime(*map(int, argv[2:7]))))
            self.events = [(c, e) for c, e in self.events if not (c == calendar and e.uid == uid)]
            return "deleted"
        raise AssertionError("unknown script")

    def lists(self):
        return [c for c in self.calls if c[0] == calendar_audit._LIST]


class AuditCase(unittest.TestCase):
    def audit(self, osa, mode="write", store=None):
        self.store = store or Store(Path(tempfile.mkdtemp()))
        return CalendarAudit(self.store, PEOPLE, osa, mode=mode, now_fn=lambda: NOW)

    def run_audit(self, person, cards, osa=None, mode="write", store=None, horizon=7):
        osa = osa or FakeCalendarOsa()
        self.osa = osa
        return self.audit(osa, mode, store).check(person, cards, horizon)


class MissingEventTests(AuditCase):
    def test_a_registered_class_without_an_event_gets_one_with_the_alert(self):
        found = self.run_audit(ALEX, [card("METCON", 14, 9, 50)])
        self.assertEqual(self.osa.created, [("Alex", "METCON", datetime(2026, 10, 14, 9, 50), 45)])
        self.assertEqual([(f.status, f.name) for f in found], [("added", "METCON")])
        self.assertEqual(self.osa.events[-1][1].alarms, (-60,))

    def test_the_sons_events_get_the_name_in_the_title_and_go_to_the_shared_calendar(self):
        self.run_audit(SAM, [card("כדורסל כיתה א'", 14, 16, 30)])
        self.assertEqual(self.osa.created, [("Kids", "Sam - כדורסל כיתה א'", datetime(2026, 10, 14, 16, 30), 45)],
                         "a name with a geresh goes in as an argument, not as script text")

    def test_the_new_event_is_recorded_so_it_is_never_added_twice(self):
        store = Store(Path(tempfile.mkdtemp()))
        self.run_audit(ALEX, [card("METCON", 14, 9, 50)], store=store)
        self.assertIn("alex|2026-10-14T09:50|metcon", store.calendar_done())
        osa = FakeCalendarOsa()                     # the user deleted the event on purpose
        self.run_audit(ALEX, [card("METCON", 14, 9, 50)], osa=osa, store=store)
        self.assertEqual(osa.created, [])
        self.assertEqual(osa.calls, [], "nothing to check: no Calendar call at all")


class ExistingEventTests(AuditCase):
    def test_an_event_at_the_same_start_and_end_counts_whatever_its_title(self):
        osa = FakeCalendarOsa()
        osa.add("Alex", "Metcon (the app's own title)", datetime(2026, 10, 14, 9, 50, tzinfo=TZ))
        self.assertEqual(self.run_audit(ALEX, [card("METCON", 14, 9, 50)], osa=osa), [])
        self.assertEqual(osa.created, [])

    def test_the_end_time_is_not_compared(self):
        # nobody has seen the end time of an event made by the app's own button: a different end must not make the
        # audit add a second event for a class that already has one
        osa = FakeCalendarOsa()
        osa.add("Alex", "Metcon", datetime(2026, 10, 14, 9, 50, tzinfo=TZ), minutes=60)
        self.assertEqual(self.run_audit(ALEX, [card("METCON", 14, 9, 50)], osa=osa), [])
        self.assertEqual(osa.created, [])

    def test_a_repeating_event_listed_with_its_first_date_counts_when_the_time_of_day_matches(self):
        # Calendar lists a repeating event with the date of its first occurrence (seen on the real Kids calendar)
        osa = FakeCalendarOsa()
        osa.add("Kids", "Sam - Judo", datetime(2026, 10, 7, 16, 30, tzinfo=TZ), repeating=True)
        self.assertEqual(self.run_audit(SAM, [card("Judo", 14, 16, 30)], osa=osa), [])
        self.assertEqual(osa.created, [])

    def test_a_repeating_event_at_another_time_of_day_is_not_the_class(self):
        osa = FakeCalendarOsa()
        osa.add("Kids", "Sam - Judo", datetime(2026, 10, 7, 18, 0, tzinfo=TZ), repeating=True)
        self.run_audit(SAM, [card("Judo", 14, 16, 30)], osa=osa)
        self.assertEqual(len(osa.created), 1)

    def test_an_event_in_another_calendar_is_not_the_class(self):
        osa = FakeCalendarOsa()
        osa.add("Work", "Metcon", datetime(2026, 10, 14, 9, 50, tzinfo=TZ))
        self.run_audit(ALEX, [card("METCON", 14, 9, 50)], osa=osa)
        self.assertEqual(len(osa.created), 1)

    def test_an_event_the_user_made_without_an_alert_is_not_added_twice(self):
        osa = FakeCalendarOsa()
        osa.strip_output = True
        osa.add("Alex", "Metcon", datetime(2026, 10, 14, 9, 50, tzinfo=TZ), alarms=())
        self.assertEqual(self.run_audit(ALEX, [card("METCON", 14, 9, 50)], osa=osa), [])
        self.assertEqual(osa.created, [])

    def test_a_son_with_his_own_event_needs_nothing(self):
        osa = FakeCalendarOsa()
        osa.add("Kids", "Sam - Judo", datetime(2026, 10, 14, 16, 30, tzinfo=TZ))
        self.assertEqual(self.run_audit(SAM, [card("Judo", 14, 16, 30)], osa=osa), [])
        self.assertEqual(osa.created, [])

    def test_the_other_sons_event_at_the_same_minute_does_not_count(self):
        osa = FakeCalendarOsa()
        osa.add("Kids", "Sam - Judo", datetime(2026, 10, 14, 16, 30, tzinfo=TZ))
        found = self.run_audit(ROBIN, [card("Judo", 14, 16, 30)], osa=osa)
        self.assertEqual(osa.created, [("Kids", "Robin - Judo", datetime(2026, 10, 14, 16, 30), 45)])
        self.assertEqual([f.status for f in found], ["added"])

    def test_an_event_without_a_name_in_the_shared_calendar_is_reported_and_nothing_is_added(self):
        # a rename that failed earlier leaves the app's own title: a second event would be a duplicate
        osa = FakeCalendarOsa()
        osa.add("Kids", "Judo", datetime(2026, 10, 14, 16, 30, tzinfo=TZ))
        found = self.run_audit(SAM, [card("Judo", 14, 16, 30)], osa=osa)
        self.assertEqual(osa.created, [])
        self.assertEqual([f.status for f in found], ["unprefixed"])
        self.assertIn("Judo", found[0].detail)


class ScopeTests(AuditCase):
    def test_only_registered_classes_in_the_future_inside_the_horizon_are_checked(self):
        cards = [card("Open", 14, 9, 0, CardState.OPEN),
                 card("Waitlist", 14, 9, 0, CardState.WAITLISTED),
                 card("Closed", 14, 9, 0, CardState.NOT_OPEN),
                 card("Full", 14, 9, 0, CardState.FULL),
                 card("Past", 13, 9, 0),                      # started before NOW
                 card("TooFar", 21, 9, 0)]                    # 8 days away, horizon 7
        self.assertEqual(self.run_audit(ALEX, cards), [])
        self.assertEqual(self.osa.calls, [], "nothing qualifies: Calendar is not touched")

    def test_one_list_query_per_person_not_one_per_card(self):
        cards = [card("A", 14, 9, 0), card("B", 15, 10, 0), card("C", 16, 11, 0)]
        osa = FakeCalendarOsa()
        for c in cards:
            osa.add("Alex", c.name, c.start)
        self.run_audit(ALEX, cards, osa=osa)
        self.assertEqual(len(osa.lists()), 1, "three classes, all there: one look at the calendar")

    def test_adding_events_needs_no_second_pass_over_the_calendar(self):
        cards = [card("A", 14, 9, 0), card("B", 15, 10, 0), card("C", 16, 11, 0)]
        self.run_audit(ALEX, cards)
        self.assertEqual(len(self.osa.created), 3)
        self.assertEqual(len(self.osa.lists()), 1, "the alert is read back inside the create script: no second pass")

    def test_the_two_sons_share_one_look_at_the_shared_calendar(self):
        # one pass over a calendar costs about 20 seconds: Sam and Robin are looked up together
        osa = FakeCalendarOsa()
        osa.add("Kids", "Sam - Judo", datetime(2026, 10, 14, 16, 30, tzinfo=TZ))
        osa.add("Kids", "Robin - Judo", datetime(2026, 10, 14, 16, 30, tzinfo=TZ))
        found = self.audit(osa).check_all([(ALEX, []), (SAM, [card("Judo", 14, 16, 30)]),
                                           (ROBIN, [card("Judo", 14, 16, 30)])], 7)
        self.assertEqual(found, [])
        self.assertEqual(len(osa.lists()), 1)

    def test_each_calendar_is_looked_at_once_and_each_class_is_added_to_its_own(self):
        osa = FakeCalendarOsa()
        found = self.audit(osa).check_all(
            [(ALEX, [card("METCON", 14, 9, 50)]), (SAM, [card("Judo", 14, 16, 30)]),
             (ROBIN, [card("Spinning", 15, 17, 0)])], 7)
        self.assertEqual(sorted((f.person, f.status) for f in found),
                         [("alex", "added"), ("robin", "added"), ("sam", "added")])
        self.assertEqual(sorted((c, t) for c, t, _, _ in osa.created),
                         [("Alex", "METCON"), ("Kids", "Robin - Spinning"), ("Kids", "Sam - Judo")])
        scans = [c[1][0] for c in osa.lists() if c[1][1:6] != c[1][6:11]]
        self.assertEqual(sorted(set(scans)), ["Alex", "Kids"])

    def test_report_mode_only_says_what_it_would_add(self):
        store = Store(Path(tempfile.mkdtemp()))
        found = self.run_audit(ALEX, [card("METCON", 14, 9, 50)], mode="report", store=store)
        self.assertEqual([f.status for f in found], ["would_add"])
        self.assertEqual(self.osa.created, [])
        self.assertEqual(store.calendar_done(), set(), "a report changes nothing")

    def test_an_unknown_mode_adds_nothing(self):
        self.assertEqual(self.run_audit(ALEX, [card("METCON", 14, 9, 50)], mode="off"), [])
        self.assertEqual(self.osa.calls, [])


class FailureTests(AuditCase):
    def test_a_failing_calendar_is_a_finding_not_a_crash(self):
        found = self.run_audit(ALEX, [card("A", 14, 9, 0), card("B", 15, 9, 0)],
                               osa=FakeCalendarOsa(fail_create=True))
        self.assertEqual([f.status for f in found], ["failed", "failed"])
        self.assertEqual(self.store.calendar_done(), set(), "a failed class is tried again later")

    def test_a_failing_list_query_is_one_finding_and_creates_nothing(self):
        class Broken(FakeCalendarOsa):
            def __call__(self, script, argv):
                raise RuntimeError("Calendar is not running")
        found = self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=Broken())
        self.assertEqual([f.status for f in found], ["failed"])

    def test_a_calendar_that_cannot_be_read_is_left_alone_after_three_tries_in_a_day(self):
        class Broken(FakeCalendarOsa):
            def __call__(self, script, argv):
                self.calls.append((script, argv))
                raise RuntimeError("Calendar is not running")
        store = Store(Path(tempfile.mkdtemp()))
        for _ in range(3):
            self.assertEqual([f.status for f in self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=Broken(),
                                                              store=store)], ["failed"])
        osa = Broken()
        self.assertEqual(self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=osa, store=store), [])
        self.assertEqual(osa.calls, [], "a broken Calendar costs up to 90 seconds a run: stop trying for today")

    def test_an_event_without_the_alert_is_reported_after_it_is_added(self):
        found = self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=FakeCalendarOsa(create_alarms=()))
        self.assertEqual([f.status for f in found], ["failed"])
        self.assertIn("alert", found[0].detail)

    def test_a_refusal_by_calendar_is_a_finding(self):
        found = self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=FakeCalendarOsa(create_result="calendar: 0"))
        self.assertEqual([f.status for f in found], ["failed"])

    def test_after_three_failures_the_class_is_left_alone(self):
        store = Store(Path(tempfile.mkdtemp()))
        for _ in range(3):
            found = self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=FakeCalendarOsa(fail_create=True), store=store)
            self.assertEqual([f.status for f in found], ["failed"])
        osa = FakeCalendarOsa(fail_create=True)
        self.assertEqual(self.run_audit(ALEX, [card("A", 14, 9, 0)], osa=osa, store=store), [])
        self.assertEqual(osa.created, [])


class DroppedClassTests(AuditCase):
    """The user unregistered a class: the program deletes the calendar event it had made (the user's decision of
    2026-10-14). Deleting is hard to undo, so the rules are strict: only a class the program handled, only in the
    future, only after two readings 30 minutes apart, only an event that is exactly this class's, never a series."""

    def world(self, person=SAM, name="Robotics", title=None, day=15, hour=16, minute=45, mode="write", event=True,
              handled=True, now=NOW):
        self.now = now
        self.osa = FakeCalendarOsa()
        self.store = Store(Path(tempfile.mkdtemp()))
        self.card = card(name, day, hour, minute)
        self.ident = f"{person.key}|{self.card.key}"
        if handled:
            self.store.mark_calendar_done(self.ident, self.card.start.strftime("%Y-%m-%dT%H:%M"), NOW)
        if event:
            self.osa.add(person.calendar, title or f"{person.title_prefix}{name}", self.card.start)
        self.audit_obj = CalendarAudit(self.store, PEOPLE, self.osa, mode=mode, now_fn=lambda: self.now)

    def look(self, person=SAM, cards=()):
        """One audit pass: the booked tab of this person was read to its end and shows these cards."""
        return self.audit_obj.remove_dropped([(person, list(cards))])

    def later(self, minutes):
        self.now = self.now + timedelta(minutes=minutes)

    def confirmed(self, person=SAM):
        self.assertEqual(self.look(person), [])
        self.later(31)
        return self.look(person)

    def test_the_first_time_a_class_is_missing_it_is_only_noted(self):
        self.world()
        self.assertEqual(self.look(), [])
        self.assertIn(self.ident, self.store.calendar_dropped())
        self.assertEqual((self.osa.calls, self.osa.deleted), ([], []), "no Calendar call at all")

    def test_the_second_reading_half_an_hour_later_deletes_the_event(self):
        self.world()
        uid = self.osa.events[0][1].uid
        found = self.confirmed()
        self.assertEqual([(f.status, f.person, f.name) for f in found], [("removed", "sam", "Robotics")])
        self.assertEqual(self.osa.deleted, [("Kids", uid, datetime(2026, 10, 15, 16, 45))])
        self.assertEqual(self.osa.events, [])
        self.assertNotIn(self.ident, self.store.calendar_done(), "so a new registration gets its event again")
        self.assertNotIn(self.ident, self.store.calendar_dropped())

    def test_a_second_reading_too_soon_waits_and_keeps_the_first_time(self):
        self.world()
        self.look()
        self.later(10)
        self.assertEqual(self.look(), [])
        self.assertEqual(self.osa.deleted, [])
        self.assertEqual(self.store.calendar_dropped()[self.ident], NOW.isoformat())

    def test_a_class_that_is_on_the_list_again_clears_the_note(self):
        self.world()
        self.look()
        self.later(31)
        self.look(cards=[self.card])
        self.assertNotIn(self.ident, self.store.calendar_dropped())
        self.later(31)
        self.assertEqual(self.look(), [], "the first reading again, not the second")
        self.assertEqual(self.osa.deleted, [])

    def test_a_class_that_is_only_open_on_the_favorites_list_is_a_dropped_class(self):
        # 2026-10-15: after the program cancelled chess for football, chess was still a favorite (an OPEN card), so the old
        # rule "it is on a card" never let its event go
        self.world()
        still_a_favorite = card("Robotics", 15, 16, 45, CardState.OPEN)
        self.look(cards=[still_a_favorite])
        self.later(31)
        found = self.look(cards=[still_a_favorite])
        self.assertEqual([f.status for f in found], ["removed"])

    def test_a_waitlisted_class_is_not_a_dropped_class(self):
        self.world()
        waiting = card("Robotics", 15, 16, 45, CardState.WAITLISTED)
        self.look(cards=[waiting])
        self.later(31)
        self.assertEqual(self.look(cards=[waiting]), [])
        self.assertEqual(self.osa.deleted, [])

    def test_a_class_the_program_never_handled_is_left_alone(self):
        self.world(handled=False)
        self.assertEqual(self.confirmed(), [])
        self.assertEqual((self.osa.calls, self.osa.deleted), ([], []))

    def test_a_class_that_has_started_is_left_alone(self):
        self.world(day=13, hour=13, minute=0)          # the audit runs at 14:00 on the 13th
        self.assertEqual(self.confirmed(), [])
        self.assertEqual((self.osa.calls, self.osa.deleted), ([], []))

    def test_the_other_sons_event_at_the_same_time_is_not_deleted(self):
        self.world(event=False)
        self.osa.add("Kids", "Robin - Robotics", self.card.start)
        self.confirmed()
        self.assertEqual(self.osa.deleted, [])
        self.assertEqual(len(self.osa.events), 1)

    def test_an_event_with_another_title_at_that_time_is_not_deleted(self):
        self.world(person=ALEX, title="Dentist")
        self.confirmed(ALEX)
        self.assertEqual(self.osa.deleted, [])
        self.assertNotIn(self.ident, self.store.calendar_done(), "no event of the class is left: nothing to watch")

    def test_the_title_is_matched_the_way_the_class_key_is(self):
        self.world(title="Sam - ROBOTICS!")
        self.assertEqual([f.status for f in self.confirmed()], ["removed"])

    def test_an_event_in_another_calendar_is_not_deleted(self):
        self.world(event=False)
        self.osa.add("Alex", "Sam - Robotics", self.card.start)
        self.confirmed()
        self.assertEqual(self.osa.deleted, [])

    def test_two_matching_events_are_not_deleted_and_reported(self):
        self.world()
        self.osa.add("Kids", "Sam - Robotics", self.card.start)
        found = self.confirmed()
        self.assertEqual(self.osa.deleted, [])
        self.assertEqual([f.status for f in found], ["check"])
        self.assertIn("2 events", found[0].detail)

    def test_a_repeating_event_is_never_deleted(self):
        self.world()
        self.osa.delete_result = "repeating"
        found = self.confirmed()
        self.assertEqual([f.status for f in found], ["failed"])
        self.assertIn("repeating", found[0].detail)
        self.assertEqual(len(self.osa.events), 1)
        self.assertIn(self.ident, self.store.calendar_done())

    def test_the_delete_is_counted_before_it_is_tried_and_stops_after_three(self):
        self.world()
        self.osa.fail_delete = True
        self.confirmed()
        self.assertEqual(self.store.attempt_count(f"calendar-delete|{self.ident}"), 1)
        for _ in range(4):
            self.later(31)
            self.look()
        deletes = [c for c in self.osa.calls if c[0] == calendar_audit._DELETE]
        self.assertEqual(len(deletes), 3)

    def test_a_calendar_that_cannot_be_read_deletes_nothing(self):
        self.world()
        self.osa.fail_list = True
        found = self.confirmed()
        self.assertEqual([f.status for f in found], ["failed"])
        self.assertEqual(self.osa.deleted, [])
        self.assertIn(self.ident, self.store.calendar_dropped(), "it is tried again at the next run")

    def test_more_than_three_dropped_classes_at_once_look_like_a_misreading(self):
        self.world(event=False)
        for hour in (9, 10, 11, 12):
            c = card(f"Class {hour}", 15, hour, 0)
            self.store.mark_calendar_done(f"sam|{c.key}", c.start.strftime("%Y-%m-%dT%H:%M"), NOW)
            self.osa.add("Kids", f"Sam - Class {hour}", c.start)
        found = self.confirmed()
        self.assertEqual(self.osa.deleted, [], "a list that lost most of its classes is a reading problem")
        self.assertEqual([f.status for f in found], ["check"])
        self.assertIn("5 classes", found[0].detail)   # the four and Robotics

    def test_report_mode_says_what_it_would_delete_and_writes_nothing(self):
        self.world(mode="report")
        found = self.look()
        self.assertEqual([(f.status, f.name) for f in found], [("would_remove", "Robotics")])
        self.assertEqual(self.osa.deleted, [])
        self.assertEqual(self.store.calendar_dropped(), {})
        self.assertIn(self.ident, self.store.calendar_done())

    def test_mode_off_does_nothing(self):
        self.world(mode="off")
        self.assertEqual(self.confirmed(), [])
        self.assertEqual(self.osa.calls, [])

    def test_the_two_sons_share_one_look_at_the_shared_calendar(self):
        self.world()
        other = card("Judo", 15, 17, 30)
        self.store.mark_calendar_done(f"robin|{other.key}", other.start.strftime("%Y-%m-%dT%H:%M"), NOW)
        self.osa.add("Kids", "Robin - Judo", other.start)
        self.audit_obj.remove_dropped([(SAM, []), (ROBIN, [])])
        self.later(31)
        found = self.audit_obj.remove_dropped([(SAM, []), (ROBIN, [])])
        self.assertEqual(sorted(f.person for f in found), ["robin", "sam"])
        self.assertEqual(len(self.osa.lists()), 1)
        self.assertEqual(len(self.osa.deleted), 2)

    def test_a_person_who_was_not_read_is_not_touched(self):
        self.world()
        self.audit_obj.remove_dropped([(ROBIN, [])])
        self.later(31)
        self.audit_obj.remove_dropped([(ROBIN, [])])
        self.assertEqual((self.osa.calls, self.osa.deleted), ([], []), "only people in the list are looked at")

    def test_a_class_that_is_registered_again_gets_its_event_again(self):
        self.world()
        self.confirmed()
        self.assertEqual(self.osa.events, [])
        found = self.audit_obj.check(SAM, [self.card], 7)
        self.assertEqual([f.status for f in found], ["added"])
        self.assertEqual([c[1] for c in self.osa.created], ["Sam - Robotics"])


class ScriptTests(unittest.TestCase):
    def test_list_events_reads_the_numbers_the_script_prints(self):
        osa = FakeCalendarOsa()
        osa.add("Alex", "METCON", datetime(2026, 10, 14, 9, 50, tzinfo=TZ), alarms=(-60, -15))
        events = list_events("Alex", datetime(2026, 10, 14, 0, 0), datetime(2026, 10, 15, 0, 0), osa)
        self.assertEqual([(e.title, e.start, e.end, e.alarms) for e in events],
                         [("METCON", datetime(2026, 10, 14, 9, 50), datetime(2026, 10, 14, 10, 35), (-60, -15))])

    def test_a_damaged_line_is_skipped(self):
        events = list_events("Alex", datetime(2026, 10, 14), datetime(2026, 10, 15),
                             lambda script, argv: "garbage\nu1\tA\t2026\t10\t14\t9\t0\t2026\t10\t14\t9\t45\t-60")
        self.assertEqual([e.uid for e in events], ["u1"])

    def test_an_event_without_an_alert_on_the_last_line_is_still_read(self):
        # run_applescript strips the output, so the empty alarm field of the last line loses its tab
        events = list_events("Alex", datetime(2026, 10, 14), datetime(2026, 10, 15),
                             lambda script, argv: "u1\tMetcon\t2026\t10\t14\t9\t50\t2026\t10\t14\t10\t35")
        self.assertEqual([(e.uid, e.alarms) for e in events], [("u1", ())])

    def test_create_event_gives_back_the_uid_and_refuses_anything_but_ok(self):
        osa = FakeCalendarOsa()
        uid, alarms = create_event("Alex", "METCON", datetime(2026, 10, 14, 9, 50),
                                   datetime(2026, 10, 14, 10, 35), osa)
        self.assertEqual((uid[:1], alarms), ("u", (-60,)))
        with self.assertRaises(calendar_audit.CalendarError):
            create_event("Alex", "METCON", datetime(2026, 10, 14, 9, 50), datetime(2026, 10, 14, 10, 35),
                         FakeCalendarOsa(create_result="calendar: 2"))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("osacompile"), "needs macOS osacompile")
    def test_the_applescript_templates_compile(self):
        for name in ("_LIST", "_CREATE", "_DELETE"):
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "t.applescript"
                source.write_text(getattr(calendar_audit, name), encoding="utf-8")
                done = subprocess.run(["osacompile", "-o", str(Path(tmp) / "t.scpt"), str(source)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, f"{name}: {done.stderr}")


class DeleteScriptTests(unittest.TestCase):
    def test_delete_event_sends_the_uid_and_the_start_and_wants_the_word_deleted(self):
        osa = FakeCalendarOsa()
        osa.add("Alex", "METCON", datetime(2026, 10, 14, 9, 50, tzinfo=TZ))
        uid = osa.events[0][1].uid
        calendar_audit.delete_event("Alex", uid, datetime(2026, 10, 14, 9, 50, tzinfo=TZ), osa)
        self.assertEqual(osa.deleted, [("Alex", uid, datetime(2026, 10, 14, 9, 50))])
        self.assertEqual(osa.events, [])

    def test_anything_but_deleted_is_an_error(self):
        for answer in ("repeating", "events: 0", "calendar: 2", ""):
            with self.assertRaises(calendar_audit.CalendarError, msg=answer):
                calendar_audit.delete_event("Alex", "u1", datetime(2026, 10, 14, 9, 50),
                                            FakeCalendarOsa(delete_result=answer))

    def test_the_script_refuses_a_repeating_event_and_runs_its_filter_once(self):
        script = calendar_audit._DELETE
        self.assertIn("recurrence", script, "a repeating event is a series: deleting it would delete every week")
        self.assertIn('then return "repeating"', script, "the script gives up on a series instead of deleting it")
        self.assertIn("set evs to (every event of c whose", script)
        self.assertNotIn("repeat with e in (every event", script)
        self.assertIn("count of evs) is not 1", script, "exactly one event, or nothing is deleted")


class FormatTests(unittest.TestCase):
    NAMES = {"alex": "Alex", "sam": "Sam"}

    def finding(self, status, person="sam", name="Judo", detail=""):
        return calendar_audit.Finding(person, name, datetime(2026, 10, 14, 16, 30, tzinfo=TZ), status, detail)

    def test_nothing_to_say_is_no_message(self):
        self.assertIsNone(calendar_audit.format_findings([], self.NAMES, "write"))

    def test_added_events_are_not_reported_in_write_mode(self):
        # a calendar event that was added is what the user expected: no message for it
        self.assertIsNone(calendar_audit.format_findings(
            [self.finding("added"), self.finding("added", "alex", "METCON")], self.NAMES, "write"))

    def test_a_report_only_run_says_what_it_would_add(self):
        title, body = calendar_audit.format_findings([self.finding("would_add")], self.NAMES, "report")
        self.assertEqual(title, "Club booking: calendar check (report only), 1 event missing")
        self.assertEqual(body, "Calendar: would add Sam - Judo, Wed 14/10 16:30")

    def test_only_the_problems_are_reported(self):
        title, body = calendar_audit.format_findings(
            [self.finding("added"), self.finding("failed", detail="Calendar got an error"),
             self.finding("unprefixed", detail="the calendar has 'Judo' at that time without a name")],
            self.NAMES, "write")
        self.assertEqual(title, "Club booking: calendar check, 2 to check")
        self.assertEqual(len(body.splitlines()), 2)
        self.assertIn("Check: Sam - Judo, Wed 14/10 16:30: calendar problem: Calendar got an error", body)
        self.assertIn("Check: Sam - Judo, Wed 14/10 16:30: the calendar has 'Judo' at that time without a name", body)
        self.assertNotIn("added", body)

    def test_a_removed_event_is_no_message_and_a_class_to_remove_is_said_in_a_report(self):
        self.assertIsNone(calendar_audit.format_findings([self.finding("removed")], self.NAMES, "write"))
        title, body = calendar_audit.format_findings([self.finding("would_remove")], self.NAMES, "report")
        self.assertEqual(title, "Club booking: calendar check (report only), 1 event to delete")
        self.assertEqual(body, "Calendar: would delete the event of Sam - Judo, Wed 14/10 16:30")

    def test_a_check_finding_says_its_detail_as_it_is(self):
        _, body = calendar_audit.format_findings(
            [self.finding("check", name="calendar", detail="4 classes look unregistered at once: nothing deleted")],
            self.NAMES, "write")
        self.assertEqual(body, "Check: Sam: 4 classes look unregistered at once: nothing deleted")

    def test_only_problems_still_make_a_title(self):
        title, _ = calendar_audit.format_findings([self.finding("failed", detail="x")], self.NAMES, "write")
        self.assertEqual(title, "Club booking: calendar check, 1 to check")


class ScriptSpeedTests(unittest.TestCase):
    """On the real Mac a filter over one calendar ("every event whose start date ...") takes about 20 seconds
    (1100+ events, measured on 2026-10-13). Written as `repeat with e in (every event ... whose ...)`, the filter runs
    again at every property read: one event cost minutes and the call timed out. The filter must run once, into a
    variable. The unit tests cannot run AppleScript, so this guards the text; the real timing is in the lessons."""

    def test_no_script_runs_the_event_filter_inside_its_loop(self):
        from club_booking import calendar_app
        for name, script in (("calendar_app._FIND", calendar_app._FIND), ("calendar_app._RENAME", calendar_app._RENAME),
                             ("calendar_audit._LIST", calendar_audit._LIST), ("calendar_audit._DELETE", calendar_audit._DELETE)):
            self.assertNotIn("repeat with e in (every event", script, name)
        for name, script in (("calendar_app._FIND", calendar_app._FIND), ("calendar_audit._LIST", calendar_audit._LIST)):
            self.assertIn("set evs to (every event of c whose", script, name)
            self.assertIn("repeat with e in evs", script, name)


if __name__ == "__main__":
    unittest.main()
