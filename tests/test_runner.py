import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from club_booking.models import CardState
from club_booking.attention import Attention, GuardedScreen, UserActive
from club_booking.runner import (USER_ACTIVE, LoggedOutError, Outcome, PersonResult, RunResult, RunError, Runner, StuckError, dialog_check,
                                 dialog_matches, format_report, is_age_refusal)
from club_booking.state import Store
from club_booking.vision import DialogRead

from tests.fake_app import (FakeApp, FakeAxVision, FakeCalendar, FakeClubApp, FakeVision, PEOPLE, TZ, card, make_cfg,
                            sample_favorites)

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)
OPEN, FULL, NOT_OPEN = CardState.OPEN, CardState.FULL, CardState.NOT_OPEN
REG, WAIT = CardState.REGISTERED, CardState.WAITLISTED


class RunnerCase(unittest.TestCase):
    def build(self, favs=None, dry_run=False, now=NOW, cfg_kw=None, restart_app=False, fixes=True, vision_cls=FakeVision,
              attention=None, **app_kw):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp, **(cfg_kw or {}))
        self.app = FakeApp(self.cfg, favs if favs is not None else sample_favorites(), **app_kw)
        self.vision = vision_cls(self.app)
        self.calendar = FakeCalendar()
        self.store = Store(self.tmp)
        self.club_app = FakeClubApp(self.app, fixes=fixes)
        self.runner = Runner(self.cfg, self.app, self.vision, self.store, self.calendar,
                             now_fn=lambda: now, sleep=lambda s: None, dry_run=dry_run,
                             club_app=self.club_app if restart_app else None, attention=attention)
        return self.runner


class FullRunTests(RunnerCase):
    def test_books_every_open_favorite_for_all_three_and_builds_the_plan(self):
        runner = self.build()
        result = runner.run(list(self.cfg.people))
        self.assertEqual(self.app.button_taps, [
            ("alex", "2026-10-13T08:50|metcon"),
            ("sam", "2026-10-13T17:15|robotics"),
            ("sam", "2026-10-13T17:30|hiphop"),
        ])
        self.assertEqual(self.calendar.added, self.app.button_taps)
        self.assertEqual(self.store.booked_idents(), {f"{p}|{k}" for p, k in self.app.button_taps})
        self.assertEqual(self.app.profile, 0, "the app is left on Alex")
        statuses = [(o.person, o.status) for r in result.people for o in r.outcomes]
        self.assertIn(("robin", "full"), statuses)
        # the plan: closed classes of all three people, earliest opening first
        self.assertEqual([(o.person, o.name) for o in result.plan.openings],
                         [("alex", "PowerGym"), ("sam", "Robotics"), ("alex", "Metcon"), ("robin", "Judo")])
        self.assertEqual(self.store.load_plan().openings, result.plan.openings)
        self.assertEqual(result.plan.created_at, NOW)
        self.assertEqual(result.people[0].error, "")

    def test_works_when_the_selector_stays_open_after_a_choice(self):
        self.build(sheet_autoclose=False).run(list(self.cfg.people))
        self.assertEqual(len(self.app.button_taps), 3)
        self.assertEqual(self.app.profile, 0)

    def test_scrolls_a_long_list_and_restarts_after_each_booking(self):
        many = [card(f"C{i}", 13, 6 + i % 12, 0, REG) for i in range(9)]
        many.append(card("Late", 13, 20, 0, OPEN))      # the open class is on the last page
        runner = self.build({"alex": many, "sam": [], "robin": []})
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [("alex", "2026-10-13T20:00|late")])
        self.assertGreaterEqual(self.app.swipes, 3)

    def test_the_number_of_booking_rounds_is_a_setting(self):
        four = {"alex": [card(f"O{i}", 13, 6 + i, 0, OPEN) for i in range(4)], "sam": [], "robin": []}
        result = self.build(four).run([self.cfg.person("alex")])
        self.assertEqual((len(self.app.button_taps), result.people[0].error), (4, ""))
        limited = self.build(four, cfg_kw={"max_booking_rounds": 2})
        result = limited.run([self.cfg.person("alex")])
        self.assertEqual(len(self.app.button_taps), 2)
        self.assertIn("too many booking rounds", result.people[0].error)

    def test_a_class_booked_before_is_never_booked_again(self):
        runner = self.build()
        self.store.record_booking("old", "alex", "2026-10-13T08:50|metcon", "METCON")   # the user cancelled it by hand
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [])

    def test_a_class_the_user_unregisters_is_not_booked_again_that_week_but_next_weeks_is(self):
        # the user's rule (2026-10-14): "if you booked a class and I unregister, don't book it again for the same week:
        # I probably cannot go". A class is tracked per date and time, so the same class in the next week is a new one.
        OPEN = CardState.OPEN
        favs = {"alex": [card("Yoga", 14, 7, 0, OPEN)], "sam": [], "robin": []}
        runner = self.build(favs)
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [("alex", "2026-10-14T07:00|yoga")], "booked by the program")
        self.app.cards["alex"][0] = card("Yoga", 14, 7, 0, OPEN)          # the user unregisters: the card is open again
        for _ in range(3):                                                    # every later run, and the tick, leaves it alone
            runner.run([self.cfg.person("alex")])
        self.assertEqual(len(self.app.button_taps), 1)
        self.app.cards["alex"].append(card("Yoga", 19, 7, 0, OPEN))        # the same class, a week later
        runner.run([self.cfg.person("alex")])
        self.assertEqual([k for _, k in self.app.button_taps], ["2026-10-14T07:00|yoga", "2026-10-19T07:00|yoga"])

    def test_a_class_registered_by_hand_and_then_unregistered_is_left_alone_too(self):
        favs = {"alex": [card("Yoga", 14, 7, 0, CardState.REGISTERED)], "sam": [], "robin": []}
        runner = self.build(favs)
        runner.run([self.cfg.person("alex")])                              # a run sees it registered
        self.app.cards["alex"][0] = card("Yoga", 14, 7, 0, CardState.OPEN) # the user unregisters
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [])

    def test_dry_run_taps_no_register_button_and_saves_no_plan(self):
        runner = self.build(dry_run=True)
        result = runner.run(list(self.cfg.people))
        self.assertEqual(self.app.button_taps, [])
        self.assertEqual(self.calendar.added, [])
        self.assertIsNone(self.store.load_plan())
        self.assertIn("would_book", [o.status for r in result.people for o in r.outcomes])


class ProblemTests(RunnerCase):
    def test_one_person_failing_does_not_stop_the_others(self):
        runner = self.build()
        self.vision.fail_for = "sam"
        result = runner.run(list(self.cfg.people))
        by = {r.person: r for r in result.people}
        self.assertIn("VisionError", by["sam"].error)
        self.assertEqual(by["alex"].error, "")
        self.assertEqual([k for p, k in self.app.button_taps], ["2026-10-13T08:50|metcon"])
        self.assertEqual(self.app.profile, 0)
        # a half-done run is not a fresh plan: it must be replaced by the next full run soon
        self.assertLess(result.plan.created_at, NOW - timedelta(hours=self.cfg.replan_after_hours))

    def test_a_confirmation_that_is_not_a_booking_stops_that_person_and_is_reported(self):
        runner = self.build()
        self.vision.dialog_kind = "other"
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(result.people[0].outcomes[0].status, "unconfirmed")
        self.assertNotEqual(result.people[0].error, "")
        self.assertEqual(self.store.booked_idents(), {"alex|2026-10-13T08:50|metcon"},
                         "the attempt stays on record, so the class is never tapped again by itself")
        self.assertIsNotNone(self.app.dialog, "an unknown screen is not tapped blind")

    def test_calendar_failure_keeps_the_booking(self):
        runner = self.build()
        self.calendar.fail_for = "alex"
        result = runner.run([self.cfg.person("alex")])
        outcome = result.people[0].outcomes[0]
        self.assertEqual(outcome.status, "booked")
        self.assertTrue(outcome.calendar.startswith("failed"))
        self.assertEqual(len(self.store.booked_idents()), 1)
        self.assertIsNone(self.app.dialog, "the confirmation dialog was closed")

    def test_a_calendar_failure_saves_a_picture_of_the_window_as_evidence(self):
        runner = self.build()
        self.calendar.fail_for = "alex"
        runner.run([self.cfg.person("alex")])
        saved = [p.name for p in (self.tmp / "evidence").glob("*calendar-failure*")]
        self.assertEqual(len(saved), 1, saved)

    def test_a_class_that_never_opens_is_reported_after_the_retries(self):
        favs = {"alex": [card("Ghost", 13, 8, 0, NOT_OPEN)], "sam": [], "robin": []}   # it opened on 12/10 at 08:00
        runner = self.build(favs)
        statuses = []
        for _ in range(self.cfg.max_open_retries):
            result = self.runner.run([self.cfg.person("alex")])
            statuses += [o.status for o in result.people[0].outcomes]
        self.assertEqual(statuses.count("not_opened"), 1)
        self.assertIn("alex|2026-10-13T08:00|ghost", self.store.handled())


class ScreenCheckTests(RunnerCase):
    def test_the_list_must_be_the_favorites_list(self):
        runner = self.build(favorites_tab_broken=True)
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [], "the Schedule list holds classes that are not favorites")
        self.assertIn("Favorites", result.people[0].error)

    def test_a_lost_tap_on_the_favorites_tab_is_tapped_again(self):
        # the first click on a window that is not in front only brings it to the front
        runner = self.build(favorites_tab_lost=1)
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(result.people[0].error, "")
        self.assertEqual(len(self.app.button_taps), 1)

    def test_a_selector_screenshot_is_never_sent_to_the_reader(self):
        runner = self.build()
        self.app.sheet_open = True          # the names of the children are on the screen
        with self.assertRaises(RunError):
            runner._scan(self.cfg.person("alex"), set())
        self.assertEqual(self.vision.list_reads, 0)

    def test_a_selector_that_does_not_close_at_once_is_closed_again(self):
        runner = self.build(sheet_close_failures=1)
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(result.people[0].error, "")
        self.assertEqual(len(self.app.button_taps), 1)
        self.assertNotIn("selector", self.vision.read_kinds)

    def test_a_selector_that_never_closes_stops_the_person_before_any_reading(self):
        runner = self.build(sheet_close_failures=99)
        result = runner.run([self.cfg.person("alex")])
        self.assertIn("selector", result.people[0].error.lower())
        self.assertEqual(self.vision.list_reads, 0)
        self.assertEqual(self.app.button_taps, [])

    def test_an_empty_list_is_not_trusted_when_classes_are_planned(self):
        self.build().run(list(self.cfg.people))
        self.assertIn("sam", {o.person for o in self.store.load_plan().openings})
        self.app.cards["sam"] = []
        again = Runner(self.cfg, self.app, self.vision, self.store, self.calendar,
                       now_fn=lambda: NOW + timedelta(minutes=30), sleep=lambda s: None)
        result = again.run([self.cfg.person("sam")])
        self.assertIn("empty", result.people[0].error)
        self.assertIn("sam", {o.person for o in self.store.load_plan().openings}, "the planned wake-ups stay")

    def test_an_empty_list_is_fine_when_nothing_is_planned(self):
        runner = self.build({"alex": [], "sam": [], "robin": []})
        result = runner.run(list(self.cfg.people))
        self.assertEqual([r.error for r in result.people], ["", "", ""])

    def test_a_list_that_skips_cards_between_two_screens_is_an_error(self):
        many = [card(f"C{i}", 13, 6 + i, 0, REG) for i in range(9)]
        runner = self.build({"alex": many, "sam": [], "robin": []}, scroll_overlap=0)
        result = runner.run([self.cfg.person("alex")])
        self.assertIn("no class in common", result.people[0].error)


    def test_a_list_that_jumps_once_is_read_again_from_the_top(self):
        # seen on 2026-10-14: one scan failed with "no class in common" (the list moved: a touch of the mouse, or a jump)
        # and the person's whole run failed. The guard stays, but a one-off gets a second reading.
        many = [card(f"C{i}", 13, 6 + i, 0, REG) for i in range(9)]
        runner = self.build({"alex": many, "sam": [], "robin": []})
        self.vision.jump_after_reads = {2}                  # the list jumps by itself after the second screen was read
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(result.people[0].error, "")
        self.assertEqual(len(result.people[0].cards), 9)
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("scan_gap_retry", events)

    def test_a_list_that_keeps_jumping_still_fails_after_the_second_reading(self):
        many = [card(f"C{i}", 13, 6 + i, 0, REG) for i in range(9)]
        runner = self.build({"alex": many, "sam": [], "robin": []}, scroll_overlap=0)
        result = runner.run([self.cfg.person("alex")])
        self.assertIn("no class in common", result.people[0].error)

    def test_a_gap_between_two_screens_leaves_evidence_for_the_next_look(self):
        # 2026-10-14: the cause was not in the log. Now the two screens' classes and a picture of the window are kept.
        many = [card(f"C{i}", 13, 6 + i, 0, REG) for i in range(9)]
        runner = self.build({"alex": many, "sam": [], "robin": []}, scroll_overlap=0)
        runner.run([self.cfg.person("alex")])
        gaps = [json.loads(line) for line in (self.tmp / "runs.jsonl").read_text().splitlines() if '"scan_gap"' in line]
        self.assertTrue(gaps)
        self.assertTrue(gaps[0]["before"] and gaps[0]["now"])
        self.assertFalse(set(gaps[0]["before"]) & set(gaps[0]["now"]))
        self.assertTrue(list((self.tmp / "evidence").glob("*alex-scan-gap*.png")))


class StuckAppTests(RunnerCase):
    def test_a_stuck_app_is_restarted_once_and_the_run_goes_on(self):
        runner = self.build(restart_app=True, mode="stuck")
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.club_app.restarts, 1)
        self.assertEqual(self.app.reconnects, 1, "the screen tool looks for the new window")
        self.assertEqual(result.people[0].error, "")
        self.assertEqual(len(self.app.button_taps), 1)

    def test_an_app_that_stays_stuck_is_restarted_once_and_then_reported(self):
        runner = self.build(restart_app=True, fixes=False, mode="stuck")
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.club_app.restarts, 1, "no restart loop")
        self.assertIn("could not switch", result.people[0].error)
        self.assertEqual(self.app.button_taps, [])

    def test_a_login_screen_is_reported_and_never_restarted(self):
        runner = self.build(restart_app=True, mode="login")
        result = runner.run(list(self.cfg.people))
        self.assertEqual(self.club_app.restarts, 0, "a restart cannot log in: the code comes by SMS")
        for r in result.people:
            self.assertIn("logged out", r.error)
        self.assertEqual(self.vision.list_reads, 1, "one look; the sons are not tried")
        self.assertLessEqual(self.app.taps.count(self.cfg.coords.selector_open), 4, "nothing more after the first look")
        title, body = format_report(self.cfg, result)
        self.assertEqual(body.count("logged out"), 1, "one line for the three people")
        self.assertIn("Alex, Sam, Robin", body)

    def test_a_session_that_dies_as_the_selector_closes_gets_no_blind_tap_on_the_login_screen(self):
        # the Classes tab (640, 775) lies inside "send me a code" (y 748-785) on the login screen: a tap there asks
        # for an SMS, and three of them lock the login for ten minutes. One look comes before the first tap.
        runner = self.build()
        close = runner._close_selector

        def close_then_die():
            close()
            self.app.mode = "login"            # the session ends right after the selector was checked and closed
        runner._close_selector = close_then_die
        result = runner.run_person(self.cfg.person("sam"))
        self.assertIn("logged out", result.error)
        self.assertNotIn(self.cfg.coords.tab_classes, self.app.taps)

    def test_a_class_beyond_the_horizon_at_the_top_does_not_hide_the_open_ones_below(self):
        # the favorites list is not in date order (field notes): a far class on the first screen must not end the scan
        NOT_OPEN, OPEN = CardState.NOT_OPEN, CardState.OPEN
        favs = {"alex": [card("Far", 25, 8, 0, NOT_OPEN), card("A", 14, 9, 0, NOT_OPEN), card("B", 15, 9, 0, NOT_OPEN),
                           card("C", 16, 9, 0, NOT_OPEN), card("D", 17, 9, 0, NOT_OPEN), card("Later", 18, 9, 0, OPEN)],
                "sam": [], "robin": []}
        result = self.build(favs).run_person(self.cfg.person("alex"))
        self.assertEqual([(o.name, o.status) for o in result.outcomes], [("Later", "booked")])
        self.assertEqual(result.error, "")

    def test_an_update_popup_stops_the_run_with_a_clear_message_and_no_restart(self):
        runner = self.build(restart_app=True, mode="update")
        result = runner.run(list(self.cfg.people))
        self.assertEqual(self.club_app.restarts, 0)
        for r in result.people:
            self.assertIn("needs an update", r.error)
        self.assertEqual(self.vision.list_reads, 1)

    def test_the_code_screen_counts_as_logged_out(self):
        runner = self.build(restart_app=True, mode="code")
        result = runner.run([self.cfg.person("alex")])
        self.assertIn("logged out", result.people[0].error)
        self.assertEqual(self.club_app.restarts, 0)

    def test_the_list_is_scrolled_by_the_reader_when_it_can(self):
        many = [card(f"C{i}", 13, 6 + i % 12, 0, REG) for i in range(9)]
        many.append(card("Late", 13, 20, 0, OPEN))
        runner = self.build({"alex": many, "sam": [], "robin": []}, vision_cls=FakeAxVision)
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [("alex", "2026-10-13T20:00|late")])
        self.assertEqual(self.app.swipes, 0, "no swipe: a swipe flings the real list")
        self.assertGreaterEqual(self.vision.page_scrolls, 3)

    def test_the_club_app_is_brought_to_the_front_before_a_run(self):
        runner = self.build(restart_app=True)
        runner.run([self.cfg.person("alex")])
        self.assertGreaterEqual(self.club_app.activations, 1)

    def test_no_restart_when_the_run_has_no_club_app(self):
        runner = self.build(mode="stuck")
        result = runner.run([self.cfg.person("alex")])
        self.assertIn("could not switch", result.people[0].error)
        self.assertEqual(self.club_app.restarts, 0)
        self.assertEqual(self.vision.list_reads, 0)


class RefusalTests(RunnerCase):
    FAVS = {"alex": [], "sam": [], "robin": [card("Judo", 15, 17, 30, OPEN), card("Capoeira", 15, 17, 20, OPEN)]}

    def test_a_refused_class_is_recorded_closed_and_the_next_class_is_tried(self):
        runner = self.build(self.FAVS, vision_cls=FakeAxVision, refuse=("Judo",))
        result = runner.run([self.cfg.person("robin")])
        by_name = {o.name: o for o in result.people[0].outcomes}
        self.assertEqual(by_name["Judo"].status, "refused")
        self.assertIn("הגיל", by_name["Judo"].detail)
        self.assertEqual(by_name["Capoeira"].status, "booked")
        self.assertEqual(result.people[0].error, "", "one refusal does not stop the person")
        self.assertIn("robin|2026-10-15T17:30|judo", self.store.refused())
        self.assertEqual(self.vision.popups_closed, 1)

    def age_ok(self, favs=None, **kw):
        """Robin goes to a class that the club refuses for his age (the user's decision of 2026-10-19)."""
        people = tuple(replace(p, age_refusal_ok=True) if p.key == "robin" else p for p in PEOPLE)
        return self.build(favs or self.FAVS, vision_cls=FakeAxVision, cfg_kw={"people": people}, **kw)

    def test_an_age_refusal_of_a_person_who_goes_anyway_is_no_problem(self):
        # 2026-10-19: the club refuses Robin ("entry from age 4") but lets him in; the class belongs in his calendar
        runner = self.age_ok(refuse=("Judo",))
        result = runner.run([self.cfg.person("robin")])
        by_name = {o.name: o for o in result.people[0].outcomes}
        self.assertEqual(by_name["Judo"].status, "age_refused")
        self.assertEqual(by_name["Capoeira"].status, "booked")
        self.assertEqual(result.people[0].error, "")
        self.assertIn("robin|2026-10-15T17:30|judo", self.store.refused(), "never tried again")
        self.assertEqual(self.vision.popups_closed, 1)
        self.assertIsNone(format_report(self.cfg, result, problems_only=True), "nothing for the phone")

    def test_the_console_says_that_the_class_was_not_registered_but_it_is_no_problem(self):
        runner = self.age_ok(refuse=("Judo",))
        result = runner.run([self.cfg.person("robin")])
        title, body = format_report(self.cfg, result)
        self.assertNotIn("to check", title)
        self.assertIn("Not registered: Robin - Judo", body)
        self.assertIn("goes anyway", body)

    def test_an_age_refusal_of_a_person_without_the_flag_is_still_a_refusal(self):
        runner = self.build(self.FAVS, vision_cls=FakeAxVision, refuse=("Judo",))
        result = runner.run([self.cfg.person("robin")])
        self.assertEqual({o.name: o.status for o in result.people[0].outcomes}["Judo"], "refused")
        self.assertIn("to check", format_report(self.cfg, result, problems_only=True)[0])

    def test_what_is_an_age_refusal(self):
        for text in ("כניסה מותרת מגיל 4", "הגיל אינו מתאים לשיעור"):        # the real text (2026-10-18), the fake one
            self.assertTrue(is_age_refusal(text), text)
        for text in ("לא ניתן להירשם לשני שיעורים חופפים", "", "השיעור מלא"):
            self.assertFalse(is_age_refusal(text), text)

    def test_a_refused_class_that_shows_full_later_is_not_reported_as_full(self):
        # 2026-10-19: the club had refused Robin's judo for his age; at 16:35 the class showed full and the phone got
        # "the class is full; the waitlist was not joined", a message that nobody can act on
        favs = {"alex": [], "sam": [], "robin": [card("Judo", 15, 17, 30, FULL), card("Yoga", 15, 18, 30, FULL)]}
        runner = self.build(favs, vision_cls=FakeAxVision)
        self.store.mark_refused("robin|2026-10-15T17:30|judo", "כניסה מותרת מגיל 4")
        result = runner.run([self.cfg.person("robin")])
        self.assertEqual({o.name for o in result.people[0].outcomes if o.status == "full"}, {"Yoga"},
                         "a full class that the program has no reason to skip is still reported")
        self.assertIn("robin|2026-10-15T17:30|judo", self.store.handled(), "and it counts as handled")

    def test_a_refused_class_is_never_tried_again(self):
        runner = self.build(self.FAVS, vision_cls=FakeAxVision, refuse=("Judo",))
        runner.run([self.cfg.person("robin")])
        runner.run([self.cfg.person("robin")])
        self.assertEqual(len(self.app.refused_taps), 1)

    def test_the_report_says_what_was_refused_and_why(self):
        runner = self.build(self.FAVS, vision_cls=FakeAxVision, refuse=("Judo",))
        title, body = format_report(self.cfg, runner.run([self.cfg.person("robin")]))
        self.assertIn("Refused: Robin - Judo", body)
        self.assertIn("הגיל", body)
        self.assertIn("to check", title)

    def test_when_the_scroll_by_the_reader_fails_the_list_is_swiped(self):
        many = [card(f"C{i}", 13, 6 + i % 12, 0, REG) for i in range(9)]
        many.append(card("Late", 13, 20, 0, OPEN))
        runner = self.build({"alex": many, "sam": [], "robin": []}, vision_cls=FakeAxVision)
        self.vision.scroll_fails = True
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [("alex", "2026-10-13T20:00|late")])
        self.assertGreaterEqual(self.app.swipes, 3)


class TapSafetyTests(RunnerCase):
    FAVS = {"alex": [card("A", 13, 7, 0, OPEN), card("B", 13, 8, 0, REG), card("C", 13, 9, 0, REG),
                       card("D", 13, 10, 0, NOT_OPEN), card("E", 13, 11, 0, REG)], "sam": [], "robin": []}

    def test_a_list_that_moves_before_the_tap_is_read_again(self):
        runner = self.build(self.FAVS)
        self.vision.jump_after_reads = {1}       # the list moves after the reader looked, before the tap
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [("alex", "2026-10-13T07:00|a")])
        self.assertEqual(result.people[0].error, "")

    def test_a_list_that_keeps_moving_stops_the_person_without_a_tap(self):
        runner = self.build(self.FAVS)
        self.vision.jump_after_reads = set(range(1, 40))
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [])
        self.assertIn("moved", result.people[0].error)

    def test_a_button_outside_the_register_column_is_not_tapped(self):
        runner = self.build(button_x=300)
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [])
        self.assertEqual(result.people[0].outcomes[0].status, "tap_failed")
        self.assertIn("column", result.people[0].outcomes[0].detail)

    def test_the_attempt_is_on_record_before_the_tap(self):
        runner = self.build()
        seen = []
        self.app.before_button_tap = lambda key, card_: seen.append(
            [(b["person"], b["card_key"], b["status"]) for b in self.store.bookings()])
        runner.run([self.cfg.person("alex")])
        self.assertEqual(seen, [[("alex", "2026-10-13T08:50|metcon", "attempted")]])
        self.assertEqual([b["status"] for b in self.store.bookings()], ["attempted", "confirmed"])

    def test_a_club_app_window_that_is_not_visible_is_one_clear_error_not_a_screen_tool_text(self):
        # 2026-10-15 14:39: the app was closed / on another desktop; the screen tool answered "Coordinates (520, 68) are
        # outside the 'club' window (500x500)" and the person failed with two long lines
        from club_booking.runner import WINDOW_HIDDEN
        from club_booking.screen import ScreenError
        runner = self.build()

        def refuse(x, y):
            raise ScreenError("tool tap reported an error: Coordinates (520, 68) are outside the 'club' window (500x500). "
                              "x must be 0-500, y must be 0-500.")
        self.app.tap = refuse
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(result.people[0].error, WINDOW_HIDDEN, "and no 'could not restore the profile' behind it")
        self.assertIn("open it", WINDOW_HIDDEN)

    def test_an_unconfirmed_booking_that_the_booked_tab_shows_is_a_booking_and_no_problem(self):
        # 2026-10-15 14:03: the screen showed the list after the tap; the class was in fact booked. The program checks the
        # booked tab itself and bothers the user only when the class is really not booked.
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
        self.vision.dialog_kind = "list"
        person = self.cfg.person("alex")
        result = runner.run_person(person)
        self.assertEqual(result.outcomes[0].status, "unconfirmed")
        self.app.dialog = None                       # the real screen showed the list: nothing was left open
        runner._read_all_booked([person], [result])  # the booked tab is read although the person has an error
        runner._reconcile(result)
        self.assertEqual(([o.status for o in result.outcomes], result.error), (["booked"], ""))
        self.assertEqual([b["status"] for b in self.store.bookings()], ["attempted", "confirmed"])
        wrapped = RunResult("r", [result], None, [])
        self.assertIsNone(format_report(self.cfg, wrapped, problems_only=True), "no message")

    def test_run_reconciles_every_person_after_the_booked_tabs_are_read(self):
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
        seen = []
        runner._reconcile = lambda r: seen.append(r.person)
        runner.run(list(self.cfg.people))
        self.assertEqual(seen, ["alex", "sam", "robin"])

    def test_an_unconfirmed_booking_that_the_booked_tab_does_not_show_stays_a_problem(self):
        runner = self.build()
        start = datetime(2026, 10, 14, 7, 0, tzinfo=TZ)
        outcome = Outcome("alex", "Yoga", start, "unconfirmed", "after the tap the screen showed 'list'", card_key="k1")
        result = PersonResult("alex", outcomes=[outcome], error=outcome.detail, booked=[], booked_read=True,
                              booked_complete=True)
        runner._reconcile(result)
        self.assertEqual((outcome.status, result.error), ("unconfirmed", outcome.detail), "not on the booked tab: a real problem")
        on_the_tab = card("Yoga", 14, 7, 0, REG)
        on_the_tab = replace(on_the_tab, name="Yoga")
        unread = PersonResult("alex", outcomes=[outcome], error=outcome.detail, booked=[on_the_tab], booked_read=False)
        outcome.card_key = on_the_tab.key
        runner._reconcile(unread)
        self.assertEqual(unread.error, outcome.detail, "an unread booked tab proves nothing")

    def test_the_same_failure_is_one_line_not_two(self):
        runner = self.build()
        self.vision.dialog_kind = "other"
        result = runner.run([self.cfg.person("alex")])
        title, body = format_report(self.cfg, result, problems_only=True)
        self.assertEqual(title, "Club booking: 1 to check")
        self.assertEqual(len(body.splitlines()), 1, body)
        self.assertTrue(body.startswith("Check: "))

    def test_an_error_after_the_tap_still_counts_as_booked(self):
        runner = self.build()
        self.vision.read_dialog_error = RuntimeError("boom")
        result = runner.run([self.cfg.person("alex")])            # must not raise
        self.assertEqual(result.people[0].outcomes[0].status, "unconfirmed")
        self.assertIn("RuntimeError", result.people[0].error)
        self.assertEqual(self.store.booked_idents(), {"alex|2026-10-13T08:50|metcon"})
        self.app.cards["alex"][1] = replace(self.app.cards["alex"][1], state=OPEN)   # as if nothing happened
        self.app.dialog = None
        self.vision.read_dialog_error = None
        runner.run([self.cfg.person("alex")])
        self.assertEqual(len(self.app.button_taps), 1, "never tapped again by itself")

    def test_an_unexpected_error_stops_only_that_person_and_the_app_goes_home(self):
        runner = self.build()
        self.vision.fail_for = "sam"
        self.vision.fail_with = RuntimeError("boom")
        result = runner.run(list(self.cfg.people))
        by = {r.person: r for r in result.people}
        self.assertIn("RuntimeError", by["sam"].error)
        self.assertEqual((by["alex"].error, by["robin"].error), ("", ""))
        self.assertEqual(self.app.profile, 0)

    def test_a_keyboard_interrupt_still_returns_the_app_to_alex(self):
        runner = self.build()
        self.vision.fail_for = "sam"
        self.vision.fail_with = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            runner.run(list(self.cfg.people))
        self.assertEqual(self.app.profile, 0)


class ConfirmationTests(RunnerCase):
    def test_a_confirmation_with_another_title_at_the_same_time_counts_as_booked_and_is_noted(self):
        runner = self.build()
        self.vision.dialog_title = "Something else"
        result = runner.run([self.cfg.person("alex")])
        outcome = result.people[0].outcomes[0]
        self.assertEqual(outcome.status, "booked")
        self.assertIn("Something else", outcome.detail)
        self.assertEqual(result.people[0].error, "")
        self.assertEqual(len(self.calendar.added), 1)
        self.assertIsNone(self.app.dialog)

    def test_a_different_class_booked_is_recorded_reported_and_closed(self):
        runner = self.build()
        self.vision.dialog_start = "09:50"
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(result.people[0].outcomes[0].status, "booked_other")
        self.assertNotEqual(result.people[0].error, "")
        rows = self.store.bookings()
        self.assertEqual([r["status"] for r in rows], ["attempted", "unexpected"])
        self.assertIn("09:50", rows[1]["card_key"])
        self.assertIsNone(self.app.dialog, "a booking confirmation is closed")
        self.assertEqual(self.calendar.added, [], "no calendar event for a class that was not meant")

    def test_the_dialog_button_is_not_tapped_blind_when_the_calendar_step_changed_the_screen(self):
        runner = self.build()
        self.calendar.app, self.calendar.closes_dialog = self.app, True
        runner.run([self.cfg.person("alex")])
        self.assertNotIn(self.cfg.coords.dialog_ok, self.app.taps)

    def test_a_class_seen_registered_is_never_booked_again_even_if_it_shows_open_later(self):
        runner = self.build({"alex": [card("METCON", 13, 8, 50, REG)], "sam": [], "robin": []})
        runner.run([self.cfg.person("alex")])
        self.app.cards["alex"][0] = replace(self.app.cards["alex"][0], state=OPEN)   # cancelled by hand
        runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [])

    def test_a_dry_run_changes_no_decision_state(self):
        favs = {"alex": [card("Ghost", 13, 8, 0, NOT_OPEN), card("Done", 13, 9, 0, REG),
                           card("Crowd", 13, 10, 0, FULL)], "sam": [], "robin": []}
        runner = self.build(favs, dry_run=True)
        for _ in range(self.cfg.max_open_retries + 1):
            runner.run([self.cfg.person("alex")])
        self.assertEqual(self.store.handled(), set())
        self.assertEqual(self.store.seen_registered(), set())
        self.assertFalse((self.tmp / "attempts.json").exists())

    def test_what_the_reader_could_not_understand_stays_in_the_log_not_on_the_phone(self):
        runner = self.build()
        self.vision.warnings.append("skipped a card: {'title': 'X', 'date': '31/02'}")
        result = runner.run([self.cfg.person("alex")])
        report = format_report(self.cfg, result)
        self.assertIsNotNone(report, "Alex's booking is still reported")
        self.assertNotIn("31/02", report[1])
        self.assertNotIn("Note", report[1])
        self.assertIn("31/02", (self.tmp / "runs.jsonl").read_text(), "it is in the log")
        quiet = self.build({"alex": [card("X", 14, 9, 0, NOT_OPEN)], "sam": [], "robin": []})
        self.vision.warnings.append("skipped a card")
        self.assertIsNone(format_report(self.cfg, quiet.run(list(self.cfg.people))), "a reading problem alone is no message")


class SafetyTests(RunnerCase):
    def test_the_program_only_taps_allowed_places(self):
        runner = self.build()
        runner.run(list(self.cfg.people))
        c = self.cfg.coords
        fixed = {c.selector_open, c.selector_close, c.tab_classes, c.subtab_schedule, c.subtab_favorites, c.dialog_ok}
        rows = {(c.selector_row_x, p.selector_row_y) for p in self.cfg.people}
        others = [t for t in self.app.taps if t not in fixed and t not in rows]
        # everything else must be a tap on the register button of a card that was open: the fake app records those
        self.assertEqual(len(others), len(self.app.button_taps))
        for x, y in others:
            self.assertLessEqual(abs(x - 512), 2)

    def test_a_run_that_takes_too_long_is_stopped(self):
        runner = self.build()
        clock = {"t": NOW}

        def now():
            clock["t"] += timedelta(seconds=90)
            return clock["t"]

        runner.now_fn = now
        runner.cfg = replace(self.cfg, max_run_seconds=200)
        result = runner.run(list(self.cfg.people))
        self.assertTrue(any("longer than 200 seconds" in r.error for r in result.people))
        self.assertEqual(self.app.profile, 0, "the app is still returned to Alex")


class MisreadTests(RunnerCase):
    def test_the_token_use_of_the_run_is_logged(self):
        runner = self.build()
        self.vision.usage = {"calls": 3, "input_tokens": 4500, "output_tokens": 1200}
        runner.run([self.cfg.person("alex")])
        log = (self.tmp / "runs.jsonl").read_text(encoding="utf-8")
        self.assertIn('"input_tokens": 4500', log)

    def test_cards_the_reader_could_not_understand_are_logged(self):
        runner = self.build()
        self.vision.warnings.append("skipped a card: {'title': 'X', 'date': '31/02'}")
        runner.run([self.cfg.person("alex")])
        log = (self.tmp / "runs.jsonl").read_text(encoding="utf-8")
        self.assertIn("vision_warnings", log)
        self.assertIn("31/02", log)
        self.assertEqual(self.vision.warnings, [], "the warnings are cleared after they are logged")


class PartialRunTests(RunnerCase):
    def test_a_partial_run_keeps_the_other_peoples_openings(self):
        runner = self.build()
        runner.run(list(self.cfg.people))
        first = self.store.load_plan()
        again = Runner(self.cfg, self.app, self.vision, self.store, self.calendar,
                       now_fn=lambda: NOW + timedelta(minutes=30), sleep=lambda s: None)
        second = again.run([self.cfg.person("sam")])
        persons = {o.person for o in second.plan.openings}
        self.assertEqual(persons, {"alex", "sam", "robin"})
        self.assertEqual(second.plan.created_at, first.created_at, "a partial run does not refresh the plan")


class ReportTests(RunnerCase):
    def test_report_text_and_silence(self):
        runner = self.build()
        result = runner.run(list(self.cfg.people))
        title, body = format_report(self.cfg, result)
        self.assertEqual(title, "Club booking: 3 booked, 1 to check")   # the full class needs a look
        self.assertIn("Booked: Sam - Robotics, Tue 13/10 17:15", body)
        self.assertIn("Check: Robin - Spinning", body)
        quiet = self.build({"alex": [card("X", 14, 9, 0, NOT_OPEN)], "sam": [], "robin": []})
        self.assertIsNone(format_report(self.cfg, quiet.run(list(self.cfg.people))))

    def test_with_the_audit_on_a_booking_does_not_open_the_calendar_editor(self):
        # the editor costs about a minute per class and delays the next booking: the audit adds the events afterwards
        runner = self.build(cfg_kw={"calendar_audit": "write"})
        result = runner.run(list(self.cfg.people))
        booked = [o for r in result.people for o in r.outcomes if o.status == "booked"]
        self.assertEqual(len(booked), 3)
        self.assertEqual({o.calendar for o in booked}, {"deferred"})
        self.assertEqual(self.calendar.added, [])
        self.assertEqual(len(self.app.button_taps), 3)
        title, body = format_report(self.cfg, result)
        self.assertNotIn("calendar problem", body)
        self.assertTrue(title.startswith("Club booking: 3 booked"))

    def test_with_the_audit_off_the_editor_is_still_used(self):
        runner = self.build()
        runner.run(list(self.cfg.people))
        self.assertEqual(len(self.calendar.added), 3)

    def test_with_the_audit_on_the_booked_tab_is_read_after_the_favorites(self):
        # the booked tab lists every class the person is registered for, also one that is not a favorite
        extra = {"alex": [card("Yoga", 14, 7, 0, CardState.REGISTERED)]}
        runner = self.build(cfg_kw={"calendar_audit": "report"}, booked_extra=extra)
        result = runner.run(list(self.cfg.people))
        alex = next(r for r in result.people if r.person == "alex")
        self.assertEqual({c.name for c in alex.booked}, {"METCON", "Yoga"}, "the waitlisted METCON and the extra class")
        self.assertEqual(result.people[0].error, "")
        self.assertEqual(self.app.profile, 0, "the app is back on Alex")

    def test_the_booked_tab_is_not_read_when_nothing_needs_it(self):
        runner = self.build()
        result = runner.run(list(self.cfg.people))
        self.assertNotIn(self.cfg.coords.subtab_booked, self.app.taps)
        self.assertTrue(all(r.booked == [] for r in result.people))

    def test_a_lost_tap_on_the_booked_tab_is_made_again(self):
        runner = self.build(cfg_kw={"calendar_audit": "report"}, booked_tab_lost=1)
        result = runner.run(list(self.cfg.people))
        self.assertTrue(any(r.booked for r in result.people))
        self.assertGreaterEqual(self.app.taps.count(self.cfg.coords.subtab_booked), 2)

    def test_a_booked_tab_that_will_not_open_does_not_fail_the_person(self):
        runner = self.build(cfg_kw={"calendar_audit": "report"}, booked_tab_lost=99)
        result = runner.run(list(self.cfg.people))
        self.assertTrue(all(r.error == "" for r in result.people))
        self.assertTrue(all(r.booked == [] for r in result.people))
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("booked_read_failed", events)

    def full_class(self):
        FULL = CardState.FULL
        return {"alex": [], "sam": [card("Judo", 14, 17, 0, FULL)], "robin": []}

    def test_with_questions_on_a_full_class_is_joined_on_the_waitlist_once(self):
        runner = self.build(self.full_class(), cfg_kw={"questions": "on"}, fixes=True)
        self.app = runner.screen
        result = runner.run(list(self.cfg.people))
        sam = next(r for r in result.people if r.person == "sam")
        self.assertEqual([(o.name, o.status, o.card_key) for o in sam.outcomes],
                         [("Judo", "waitlisted", "2026-10-14T17:00|judo")])
        self.assertEqual(self.app.waitlist_taps, [("sam", "2026-10-14T17:00|judo")])
        self.assertEqual(self.calendar.added, [], "a waitlist spot is not a booking: no calendar event")
        rows = [json.loads(line) for line in (self.tmp / "bookings.jsonl").read_text().splitlines()]
        self.assertEqual([r["status"] for r in rows], ["attempted", "waitlisted"])
        runner.run(list(self.cfg.people))
        self.assertEqual(len(self.app.waitlist_taps), 1, "never tapped twice")

    def test_with_questions_off_a_full_class_is_only_reported(self):
        runner = self.build(self.full_class())
        runner.run(list(self.cfg.people))
        self.assertEqual(self.app.waitlist_taps, [])

    def test_a_waitlist_spot_is_not_reported(self):
        # the user decided on 2026-10-14: a waitlist spot is always kept, with no question and no message
        runner = self.build(self.full_class(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        self.assertIsNone(format_report(self.cfg, runner.run(list(self.cfg.people))))

    def test_a_waitlist_spot_adds_no_line_and_no_count_to_a_report_that_is_sent_anyway(self):
        runner = self.build()
        result = runner.run(list(self.cfg.people))
        before = format_report(self.cfg, result)
        self.assertIn("3 booked", before[0], "there is a report to send anyway")
        result.people[1].outcomes.append(Outcome("sam", "Judo", datetime(2026, 10, 14, 17, 0, tzinfo=TZ), "waitlisted",
                                                 "joined the waitlist", card_key="2026-10-14T17:00|judo"))
        self.assertEqual(format_report(self.cfg, result), before)

    def test_a_waitlist_confirmation_counts_for_a_full_card_only(self):
        full, open_ = card("Judo", 14, 17, 0, CardState.FULL), card("Judo", 14, 17, 0, CardState.OPEN)
        waitlisted = DialogRead("waitlisted", "Judo", "14/10", "17:00")
        self.assertEqual(dialog_check(waitlisted, full), "match")
        self.assertEqual(dialog_check(waitlisted, open_), "not_booked", "an open class must show a booking, not a waitlist")
        self.assertEqual(dialog_check(DialogRead("list", "", "", ""), full), "not_booked")

    def overlapping(self, who="alex"):
        OPEN = CardState.OPEN
        favs = {"alex": [], "sam": [], "robin": []}
        favs[who] = [card("Yoga", 14, 7, 0, OPEN), card("Judo", 14, 7, 15, OPEN), card("Spin", 14, 18, 0, OPEN)]
        return favs

    def test_overlapping_classes_of_a_person_who_asks_book_only_the_more_crowded(self):
        runner = self.build(self.overlapping(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        yoga, judo = "2026-10-14T07:00|yoga", "2026-10-14T07:15|judo"
        self.vision.crowd = {yoga: 3, judo: 8}
        result = runner.run_person(self.cfg.person("alex"))
        self.assertEqual(sorted(k for p, k in self.app.button_taps), ["2026-10-14T07:15|judo", "2026-10-14T18:00|spin"])
        [(default, others, was_booked)] = result.overlaps
        self.assertEqual((default.key, [o.key for o in others], was_booked), (judo, [yoga], False))
        self.assertIn(judo, self.vision.opened, "the class pages were opened to count")
        self.assertEqual(result.error, "")

    def test_an_unknown_crowd_falls_back_to_the_earlier_class(self):
        runner = self.build(self.overlapping(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        self.vision.pages_fail = True
        result = runner.run_person(self.cfg.person("alex"))
        self.assertEqual(sorted(k for p, k in self.app.button_taps), ["2026-10-14T07:00|yoga", "2026-10-14T18:00|spin"])
        self.assertEqual(result.overlaps[0][0].key, "2026-10-14T07:00|yoga")

    def test_a_reader_without_class_pages_falls_back_too(self):
        runner = self.build(self.overlapping(), cfg_kw={"questions": "on"})          # the picture reader cannot count
        runner.run_person(self.cfg.person("alex"))
        self.assertEqual(sorted(k for p, k in self.app.button_taps), ["2026-10-14T07:00|yoga", "2026-10-14T18:00|spin"])

    def test_a_class_that_overlaps_one_already_booked_is_left_for_the_question(self):
        runner = self.build(self.overlapping(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        self.vision.crowd = {"2026-10-14T07:00|yoga": 1, "2026-10-14T07:15|judo": 9}
        runner.run_person(self.cfg.person("alex"))
        taps = len(self.app.button_taps)
        result = runner.run_person(self.cfg.person("alex"))       # the next tick: Judo is booked, Yoga is open again
        self.assertEqual(len(self.app.button_taps), taps, "Yoga overlaps the booked Judo: it is not booked behind the user's back")
        self.assertEqual(result.overlaps[0][2], True, "the default was booked earlier")

    def test_the_sons_overlapping_classes_are_both_tried_unless_they_ask_about_overlaps(self):
        runner = self.build(self.overlapping("sam"), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        result = runner.run_person(self.cfg.person("sam"))
        self.assertEqual(len(self.app.button_taps), 3)
        self.assertEqual(result.overlaps, [])
        self.assertEqual(self.vision.opened, [], "no crowd counting for a person who does not ask")

    def test_a_son_who_asks_about_overlaps_books_only_the_more_crowded_like_alex(self):
        # 2026-10-15: Sam's chess and football (both Fri 13:15): the club refused the second ("two overlapping classes")
        from dataclasses import replace
        runner = self.build(self.overlapping("sam"), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        yoga, judo = "2026-10-14T07:00|yoga", "2026-10-14T07:15|judo"
        self.vision.crowd = {yoga: 3, judo: 8}
        runner.cfg = replace(runner.cfg, people=tuple(replace(x, asks_overlaps=True) if x.key == "sam" else x
                                                      for x in runner.cfg.people))
        result = runner.run_person(runner.cfg.person("sam"))
        self.assertEqual(sorted(k for p, k in self.app.button_taps), [judo, "2026-10-14T18:00|spin"])
        [(default, others, was_booked)] = result.overlaps
        self.assertEqual((default.key, [o.key for o in others], was_booked), (judo, [yoga], False))

    def test_with_questions_off_nothing_changes(self):
        runner = self.build(self.overlapping(), vision_cls=FakeAxVision)
        result = runner.run_person(self.cfg.person("alex"))
        self.assertEqual(len(self.app.button_taps), 3)
        self.assertEqual((result.overlaps, self.vision.opened), ([], []))

    def ident(self, who, hour, name):
        return f"{who}|2026-10-14T{hour:02d}:00|{name.lower()}"

    def booked(self, who="alex", state=CardState.OPEN, **app_kw):
        favs = {"alex": [], "sam": [], "robin": []}
        favs[who] = [card("Yoga", 14, 7, 0, state)]
        runner = self.build(favs, vision_cls=FakeAxVision, cfg_kw={"questions": "on"}, **app_kw)
        runner.run_person(self.cfg.person(who))
        return runner, self.ident(who, 7, "Yoga")

    def statuses(self):
        return [json.loads(line)["status"] for line in (self.tmp / "bookings.jsonl").read_text().splitlines()]

    def test_a_booking_the_program_made_can_be_cancelled(self):
        runner, ident = self.booked()
        self.assertIn(ident, self.store.standing_bookings())
        self.assertEqual(runner.cancel(self.cfg.person("alex"), ident), "cancelled")
        self.assertEqual(self.app.cancel_presses, [("alex", "2026-10-14T07:00|yoga", "ביטול")])
        self.assertEqual(self.app.cards["alex"][0].state, CardState.OPEN)
        self.assertEqual(self.statuses(), ["attempted", "confirmed", "cancel_attempted", "cancelled"])
        self.assertNotIn(ident, self.store.standing_bookings())
        self.assertEqual(self.app.profile, 0)

    def test_a_waitlist_spot_the_program_joined_can_be_left(self):
        runner, ident = self.booked("sam", CardState.FULL)
        self.assertEqual(self.app.cards["sam"][0].state, CardState.WAITLISTED)
        self.assertEqual(runner.cancel(self.cfg.person("sam"), ident), "cancelled")
        self.assertEqual(self.app.cancel_presses, [("sam", "2026-10-14T07:00|yoga", "הסרה מהמתנה")])
        self.assertEqual(self.app.cards["sam"][0].state, CardState.FULL)

    def test_a_class_the_program_did_not_book_is_never_touched(self):
        # registered by hand: the program has no booking of it, so it has no right to cancel it (R18)
        favs = {"alex": [card("Yoga", 14, 7, 0, CardState.REGISTERED)], "sam": [], "robin": []}
        runner = self.build(favs, vision_cls=FakeAxVision, cfg_kw={"questions": "on"})
        taps = len(self.app.taps)
        answer = runner.cancel(self.cfg.person("alex"), self.ident("alex", 7, "Yoga"))
        self.assertTrue(answer.startswith("refused"), answer)
        self.assertEqual((self.app.cancel_presses, len(self.app.taps)), ([], taps), "not a single tap or press")
        self.assertEqual(self.app.cards["alex"][0].state, CardState.REGISTERED)

    def test_a_booking_of_another_person_is_refused(self):
        runner, ident = self.booked("sam")
        answer = runner.cancel(self.cfg.person("alex"), ident)
        self.assertTrue(answer.startswith("refused"), answer)
        self.assertEqual(self.app.cancel_presses, [])

    def test_a_class_that_is_no_longer_booked_is_marked_gone_without_a_press(self):
        runner, ident = self.booked()
        self.app.cards["alex"][0] = card("Yoga", 14, 7, 0, CardState.OPEN)          # cancelled by hand meanwhile
        self.assertEqual(runner.cancel(self.cfg.person("alex"), ident), "already_gone")
        self.assertEqual(self.app.cancel_presses, [])
        self.assertNotIn(ident, self.store.standing_bookings())

    def test_a_press_that_changes_nothing_is_a_problem_and_is_not_repeated(self):
        runner, ident = self.booked(cancel_ignored=True)
        answer = runner.cancel(self.cfg.person("alex"), ident)
        self.assertTrue(answer.startswith("problem"), answer)
        self.assertEqual(len(self.app.cancel_presses), 1)
        self.assertTrue(runner.cancel(self.cfg.person("alex"), ident).startswith("refused"), "never a second press")
        self.assertEqual(len(self.app.cancel_presses), 1)
        self.assertEqual(self.statuses()[-1], "cancel_attempted")

    def test_the_press_names_the_card_not_a_position(self):
        runner, ident = self.booked()
        presses = []
        original = self.vision.press_card_action
        self.vision.press_card_action = lambda card, desc: (presses.append((card.key, desc)), original(card, desc))[1]
        self.assertEqual(runner.cancel(self.cfg.person("alex"), ident), "cancelled")
        self.assertEqual(presses, [("2026-10-14T07:00|yoga", "ביטול")])

    def test_an_unknown_message_after_the_press_is_not_confirmed_blind(self):
        # a fee or an "are you sure" message: the only cancel ever seen said "בוטלה בהצלחה" with a close button
        runner, ident = self.booked()
        self.vision.popup_override = ("unknown", "ביטול בשעה זו כרוך בחיוב. להמשיך?")
        answer = runner.cancel(self.cfg.person("alex"), ident)
        self.assertTrue(answer.startswith("problem"), answer)
        self.assertIn("unexpected message", answer)
        self.assertEqual(self.vision.popups_closed, 0, "nothing was pressed on a message nobody read")
        self.assertEqual(self.statuses()[-1], "cancel_attempted")

    def test_a_failure_before_anything_was_pressed_is_a_retry_and_writes_nothing(self):
        runner, ident = self.booked()
        before = self.statuses()
        runner.go_to_booked = lambda: (_ for _ in ()).throw(StuckError("the booked tab did not open"))
        answer = runner.cancel(self.cfg.person("alex"), ident)
        self.assertTrue(answer.startswith("retry"), answer)
        self.assertEqual((self.statuses(), self.app.cancel_presses), (before, []))
        self.assertIn(ident, self.store.standing_bookings(), "still the program's: the answer can be tried again")

    def test_a_failure_after_the_press_says_that_the_press_was_made(self):
        runner, ident = self.booked()
        real, calls = runner.go_to_booked, []

        def go():
            calls.append(1)
            if len(calls) == 2:                  # the second look, after the press
                raise StuckError("the booked tab did not open")
            real()
        runner.go_to_booked = go
        answer = runner.cancel(self.cfg.person("alex"), ident)
        self.assertTrue(answer.startswith("problem: after the press"), answer)
        self.assertEqual(len(self.app.cancel_presses), 1)

    def test_after_the_press_nothing_is_tapped_at_the_buttons_position(self):
        runner, ident = self.booked()
        taps_before = list(self.app.taps)
        runner.cancel(self.cfg.person("alex"), ident)
        spot = (self.app.button_x + self.vision.jitter[0], 250 + 60 + self.vision.jitter[1])    # where the reader saw the button
        self.assertNotIn(spot, self.app.taps[len(taps_before):], "the cancel is pressed by text, never tapped by position")

    def test_a_dry_run_never_cancels(self):
        runner, ident = self.booked()
        dry = Runner(self.cfg, self.app, self.vision, self.store, self.calendar, now_fn=lambda: NOW,
                     sleep=lambda s: None, dry_run=True)
        self.assertTrue(dry.cancel(self.cfg.person("alex"), ident).startswith("refused"))
        self.assertEqual(self.app.cancel_presses, [])
        self.assertIn(ident, self.store.standing_bookings())

    def test_a_waitlist_spot_that_became_a_booking_is_not_cancelled_by_the_waitlist_answer(self):
        # the answer was about a waitlist spot; the club moved the person up: it is a booking now, and a different thing
        runner, ident = self.booked("sam", CardState.FULL)
        self.app.cards["sam"][0] = card("Yoga", 14, 7, 0, CardState.REGISTERED)
        answer = runner.cancel(self.cfg.person("sam"), ident)
        self.assertTrue(answer.startswith("problem"), answer)
        self.assertIn("registered", answer)
        self.assertEqual(self.app.cancel_presses, [])

    def test_without_accessibility_nothing_is_cancelled_and_nothing_is_written(self):
        favs = {"alex": [card("Yoga", 14, 7, 0, CardState.OPEN)], "sam": [], "robin": []}
        runner = self.build(favs, cfg_kw={"questions": "on"})                # the picture reader cannot press by text
        runner.run_person(self.cfg.person("alex"))
        before = self.statuses()
        answer = runner.cancel(self.cfg.person("alex"), self.ident("alex", 7, "Yoga"))
        self.assertTrue(answer.startswith("problem"), answer)
        self.assertEqual((self.app.cancel_presses, self.statuses()), ([], before))

    # ---- the fix pass after the review of plan 2 -----------------------------------------------------
    def test_the_booked_tab_is_read_after_every_person_has_been_booked(self):
        # reading it takes about half a minute per person: it must not sit between two people's bookings
        runner = self.build(cfg_kw={"calendar_audit": "report"})
        runner.run(list(self.cfg.people))
        last_book = max(i for i, e in enumerate(self.app.events) if e == "book")
        first_booked_tab = min(i for i, e in enumerate(self.app.events) if e == "booked_tab")
        self.assertLess(last_book, first_booked_tab)

    def test_a_booked_tab_read_to_its_end_is_marked_complete(self):
        # the calendar audit deletes an event only for a class that is missing from a list that was read to its end
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
        result = runner.run(list(self.cfg.people))
        self.assertTrue(all(r.booked_read and r.booked_complete for r in result.people))

    def test_a_booked_tab_that_could_not_be_scrolled_is_not_complete(self):
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
        self.vision.scroll_fails = True
        result = runner.run(list(self.cfg.people))
        self.assertTrue(all(r.booked_read and not r.booked_complete for r in result.people),
                        "the first screen was read, but the rest of the list was never seen")

    def test_a_booked_list_that_has_nothing_to_scroll_is_all_there_and_complete(self):
        # 2026-10-15 16:20: on a short list the accessibility helper answers "no scroll area": everything is on the screen.
        # Treated as "incomplete" it kept the event of a cancelled class for ever.
        from club_booking.ax import AxError
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
        self.vision.scroll_error = AxError("no scroll area")
        result = runner.run(list(self.cfg.people))
        self.assertTrue(all(r.booked_read and r.booked_complete for r in result.people))

    def test_any_other_scroll_error_still_means_the_list_was_not_read_to_its_end(self):
        from club_booking.ax import AxError
        for error in (AxError("the scroll failed"), AxError("Accessibility is not allowed"), RuntimeError("boom")):
            runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
            self.vision.scroll_error = error
            result = runner.run(list(self.cfg.people))
            self.assertTrue(all(not r.booked_complete for r in result.people), repr(error))

    def test_a_booked_tab_that_will_not_open_is_not_complete(self):
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision, booked_tab_lost=99)
        result = runner.run(list(self.cfg.people))
        self.assertTrue(all(not r.booked_complete for r in result.people))

    def test_a_reading_that_stops_at_one_class_is_not_complete(self):
        runner = self.build(cfg_kw={"calendar_audit": "write"}, vision_cls=FakeAxVision)
        person = self.cfg.person("alex")
        runner.ensure_person(person)
        runner.go_to_booked()
        runner.read_booked(stop_key=next(iter({c.key for c in runner.read_booked()})))
        self.assertFalse(runner.booked_complete)
        runner.read_booked(stop_key="2026-01-01T00:00|a class that is not there")
        self.assertFalse(runner.booked_complete, "a reading for one class says nothing about the whole list")
        runner.read_booked()
        self.assertTrue(runner.booked_complete)

    def test_the_booked_tab_is_never_swiped(self):
        # a swipe that starts on a card opens its class page (lessons row 4); without a scroll area there is nothing to scroll
        runner = self.build(cfg_kw={"calendar_audit": "report"}, vision_cls=FakeAxVision)
        self.vision.scroll_fails = True
        result = runner.run(list(self.cfg.people))
        self.assertEqual(self.app.booked_swipes, 0)
        self.assertTrue(any(r.booked for r in result.people), "the first screen was read")

    def test_a_class_page_left_open_is_closed_before_any_tap(self):
        runner = self.build(vision_cls=FakeAxVision)
        kinds = iter(["details", "favorites"])
        runner._screen_kind = lambda: next(kinds)
        runner._stop_if_blocked()
        self.assertEqual(self.vision.backs, 1)

    def test_a_failing_overlap_plan_does_not_stop_the_bookings(self):
        runner = self.build(self.overlapping(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)

        def broken(person, skip):
            raise StuckError("the favorites tab did not open")
        runner._plan_overlaps = broken
        result = runner.run_person(self.cfg.person("alex"))
        self.assertEqual(result.error, "")
        self.assertEqual(len(self.app.button_taps), 3, "booked as without the rule")
        events = [json.loads(line)["event"] for line in (self.tmp / "runs.jsonl").read_text().splitlines()]
        self.assertIn("overlap_plan_failed", events)

    def test_a_logged_out_app_still_stops_the_overlap_plan(self):
        runner = self.build(self.overlapping(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)

        def out(person, skip):
            raise LoggedOutError("logged out")
        runner._plan_overlaps = out
        self.assertIn("logged out", runner.run_person(self.cfg.person("alex")).error)
        self.assertEqual(self.app.button_taps, [])

    def full_then_open(self):
        return {"alex": [], "sam": [card("Judo", 14, 17, 0, CardState.FULL), card("Yoga", 14, 7, 0, CardState.OPEN)],
                "robin": []}

    def test_a_waitlist_tap_that_cannot_be_made_does_not_block_the_other_classes(self):
        runner = self.build(self.full_then_open(), cfg_kw={"questions": "on"})
        real = runner._book

        def book(person, card_, png):
            if card_.state == CardState.FULL:
                return Outcome(person.key, card_.name, card_.start, "tap_failed", "no button where it should be")
            return real(person, card_, png)
        runner._book = book
        result = runner.run_person(self.cfg.person("sam"))
        self.assertEqual([(o.name, o.status) for o in result.outcomes],
                         [("Judo", "tap_failed"), ("Yoga", "booked")])
        self.assertEqual(result.error, "", "the person's run goes on")

    def test_a_waitlist_that_cannot_be_tapped_is_given_up_after_three_runs(self):
        runner = self.build(self.full_then_open(), cfg_kw={"questions": "on"})
        calls = []
        real = runner._book

        def book(person, card_, png):
            if card_.state == CardState.FULL:
                calls.append(1)
                return Outcome(person.key, card_.name, card_.start, "tap_failed", "no button")
            return real(person, card_, png)
        runner._book = book
        for _ in range(5):
            runner.run_person(self.cfg.person("sam"))
        self.assertEqual(len(calls), 3)

    def test_a_popup_left_open_after_joining_stops_the_run_without_a_restart(self):
        # a restart of the app can log it out: a message that stays open is reported, never "cured" by a restart
        runner = self.build(self.full_class(), cfg_kw={"questions": "on"}, vision_cls=FakeAxVision, restart_app=True)
        self.vision.close_popup = lambda: None                          # the popup cannot be closed
        result = runner.run(list(self.cfg.people))
        self.assertEqual(self.club_app.restarts, 0)
        sam = next(r for r in result.people if r.person == "sam")
        self.assertEqual([o.status for o in sam.outcomes], ["waitlisted"])
        self.assertIn("message is still open", sam.error)

    def test_a_waitlist_confirmation_that_names_no_class_still_counts(self):
        full = card("Judo", 14, 17, 0, CardState.FULL)
        self.assertEqual(dialog_check(DialogRead("waitlisted", "", "", ""), full), "match")
        self.assertEqual(dialog_check(DialogRead("waitlisted", "Yoga", "15/10", "09:00"), full), "other_class",
                         "a confirmation that names another class is still another class")
        self.assertEqual(dialog_check(DialogRead("booked", "", "", ""), full), "other_class",
                         "only a waitlist confirmation may be nameless")

    def test_the_crowd_of_a_class_below_the_first_screen_is_counted_too(self):
        # the list is virtual: only the classes on the screen can be opened. The count goes to the class first.
        NOT_OPEN, OPEN = CardState.NOT_OPEN, CardState.OPEN
        fillers = [card(f"F{i}", 15 + i, 9, 0, NOT_OPEN) for i in range(6)]
        favs = {"alex": [card("Yoga", 14, 7, 0, OPEN)] + fillers + [card("Judo", 14, 7, 15, OPEN)], "sam": [], "robin": []}
        runner = self.build(favs, cfg_kw={"questions": "on"}, vision_cls=FakeAxVision, page_size=3)
        self.vision.crowd = {"2026-10-14T07:00|yoga": 2, "2026-10-14T07:15|judo": 9}
        result = runner.run_person(self.cfg.person("alex"))
        self.assertEqual(sorted(self.vision.opened), ["2026-10-14T07:00|yoga", "2026-10-14T07:15|judo"])
        self.assertEqual(result.overlaps[0][0].key, "2026-10-14T07:15|judo", "the more crowded one, although it is far down")
        self.assertEqual(self.app.button_taps, [("alex", "2026-10-14T07:15|judo")])

    def test_a_class_that_overlaps_one_the_user_registered_by_hand_is_not_booked(self):
        OPEN = CardState.OPEN
        favs = {"alex": [card("Yoga", 14, 7, 0, CardState.REGISTERED), card("Judo", 14, 7, 15, OPEN)],
                "sam": [], "robin": []}
        runner = self.build(favs, cfg_kw={"questions": "on"}, vision_cls=FakeAxVision)
        result = runner.run_person(self.cfg.person("alex"))
        self.assertEqual(self.app.button_taps, [], "the user holds Yoga: Judo is not booked behind his back")
        [(default, others, was_booked)] = result.overlaps
        self.assertEqual((default.name, [o.name for o in others], was_booked), ("Yoga", ["Judo"], True))

    def test_a_booking_the_user_cancelled_by_hand_is_no_longer_the_programs_to_cancel(self):
        # R18: the program may cancel only what it booked. If the user cancels it and registers again by hand, it is his
        runner, ident = self.booked()
        self.assertIn(ident, self.store.standing_bookings())
        self.app.cards["alex"][0] = card("Yoga", 14, 7, 0, CardState.OPEN)          # cancelled by hand
        runner.run(list(self.cfg.people))                                              # a run sees it open again
        self.assertNotIn(ident, self.store.standing_bookings())
        self.app.cards["alex"][0] = card("Yoga", 14, 7, 0, CardState.REGISTERED)    # registered again by hand
        self.assertTrue(runner.cancel(self.cfg.person("alex"), ident).startswith("refused"))
        self.assertEqual(self.app.cancel_presses, [])

    def test_a_booking_that_still_shows_registered_stays_the_programs(self):
        runner, ident = self.booked()
        runner.run(list(self.cfg.people))
        self.assertIn(ident, self.store.standing_bookings())

    def test_dialog_matches(self):
        c = card("METCON", 13, 8, 50, OPEN)
        self.assertTrue(dialog_matches(DialogRead("booked", "metcon", "13/10", "08:50"), c))
        self.assertTrue(dialog_matches(DialogRead("booked", "METCON", "13/10", "8:50"), c))
        self.assertFalse(dialog_matches(DialogRead("booked", "METCON", "13/10", "09:50"), c))
        self.assertFalse(dialog_matches(DialogRead("booked", "Yoga", "13/10", "08:50"), c))
        self.assertFalse(dialog_matches(DialogRead("list", "", "", ""), c))
        self.assertFalse(dialog_matches(DialogRead("booked", "METCON", "bad", "08:50"), c))
        g = card("ג'ימבורי", 13, 8, 50, OPEN)       # an apostrophe in the app, a geresh in the reader's text
        self.assertTrue(dialog_matches(DialogRead("booked", "ג׳ימבורי", "13/10", "08:50"), g))

    def test_dialog_check_tells_the_four_cases_apart(self):
        c = card("METCON", 13, 8, 50, OPEN)
        self.assertEqual(dialog_check(DialogRead("booked", "METCON", "13/10", "08:50"), c), "match")
        self.assertEqual(dialog_check(DialogRead("booked", "Other name", "13/10", "08:50"), c), "title_differs")
        self.assertEqual(dialog_check(DialogRead("booked", "METCON", "13/10", "09:50"), c), "other_class")
        self.assertEqual(dialog_check(DialogRead("booked", "METCON", "bad", "08:50"), c), "other_class")
        self.assertEqual(dialog_check(DialogRead("waitlisted", "METCON", "13/10", "08:50"), c), "not_booked")
        self.assertEqual(dialog_check(DialogRead("list", "", "", ""), c), "not_booked")


class UserFirstTests(RunnerCase):
    """The user comes first: a run that is not for a class that is opening stands back the moment they touch the
    mouse or the keyboard (2026-10-14: "I cannot work on this computer")."""

    def attention(self, patient, idle):
        """An Attention whose clock moves 4 seconds at every look, so the time since the program's last action grows."""
        from datetime import datetime as dt, timedelta as td
        ticks = [0]

        def now():
            ticks[0] += 1
            return dt(2026, 10, 14, 12, 0, 0) + td(seconds=4 * ticks[0])
        self.waits = []
        return Attention(idle, now, self.waits.append, patient=patient)

    def test_a_non_urgent_run_stands_back_the_moment_the_user_touches_the_mouse(self):
        taps = lambda: len(self.app.taps)                                                    # noqa: E731
        idle = lambda: 9999.0 if taps() < 3 else 0.5          # the user moves the mouse after the third click      # noqa: E731
        runner = self.build(attention=self.attention(False, idle), restart_app=True)
        result = runner.run(list(self.cfg.people))
        self.assertEqual({r.error for r in result.people}, {USER_ACTIVE})
        self.assertEqual(len(self.app.taps), 3, "not one more click once the user was there")
        self.assertEqual(self.club_app.restarts, 0)
        self.assertIsNone(format_report(self.cfg, result), "no message for stepping aside")
        self.assertIsNone(self.store.load_plan(), "nothing half-read is saved as the plan")

    def test_a_non_urgent_run_does_not_even_start_when_the_user_is_busy(self):
        runner = self.build(attention=self.attention(False, lambda: 0.5), restart_app=True)
        result = runner.run(list(self.cfg.people))
        self.assertEqual({r.error for r in result.people}, {USER_ACTIVE})
        self.assertEqual((self.app.taps, self.club_app.activations), ([], 0), "not even the app is brought to the front")

    def test_nothing_is_written_when_the_user_comes_back_just_before_the_register_tap(self):
        # the booking is written ahead of the tap: a class written but never tapped would count as booked for ever
        def idle():
            return 0.5 if list((self.tmp / "evidence").glob("*-before-*")) else 9999.0     # after the picture before the tap
        runner = self.build(attention=self.attention(False, idle))
        result = runner.run([self.cfg.person("alex")])
        self.assertEqual(self.app.button_taps, [])
        self.assertEqual(self.store.booked_idents(), set(), "no 'attempted' without a tap")
        self.assertEqual(result.people[0].error, USER_ACTIVE)

    def test_a_booking_that_was_begun_is_finished_and_reported_when_the_user_comes_back(self):
        # the user touches the mouse right after the register tap: the confirmation is closed and the class is reported;
        # only then does the run stand back
        idle = lambda: 9999.0 if not self.app.button_taps else 0.5                  # noqa: E731
        runner = self.build(attention=self.attention(False, idle))
        result = runner.run(list(self.cfg.people))
        self.assertEqual(len(self.app.button_taps), 1, "one booking was begun, no other")
        self.assertIsNone(self.app.dialog, "its confirmation was closed")
        by = {r.person: r for r in result.people}
        self.assertEqual([o.status for o in by["alex"].outcomes], ["booked"])
        self.assertEqual((by["alex"].error, by["sam"].error, by["robin"].error), (USER_ACTIVE,) * 3)
        title, body = format_report(self.cfg, result)
        self.assertIn("Booked: Alex", body)
        self.assertNotIn("Problem", body)
        self.assertEqual(self.store.booked_idents(), {"alex|2026-10-13T08:50|metcon"})

    def test_an_urgent_run_waits_for_a_pause_and_still_books(self):
        busy = {"n": 0}

        def idle():
            busy["n"] += 1
            return 0.5 if busy["n"] <= 6 else 9999.0              # the user types for a while, then stops
        runner = self.build(attention=self.attention(True, idle), restart_app=True)
        result = runner.run(list(self.cfg.people))
        self.assertTrue(self.waits, "it waited")
        self.assertEqual({r.error for r in result.people}, {""})
        self.assertEqual(len(self.app.button_taps), 3, "all three classes were booked")


    def a_booked_class(self):
        favs = {"alex": [card("Yoga", 14, 7, 0, CardState.OPEN)], "sam": [], "robin": []}
        runner = self.build(favs, vision_cls=FakeAxVision, cfg_kw={"questions": "on"})
        runner.run_person(self.cfg.person("alex"))
        return runner, "alex|2026-10-14T07:00|yoga"

    def guard(self, runner, idle):
        att = self.attention(False, idle)
        runner.attention, runner.screen = att, GuardedScreen(self.app, att)
        return att

    def statuses(self):
        return [json.loads(line)["status"] for line in (self.tmp / "bookings.jsonl").read_text().splitlines()]

    def test_a_cancel_is_not_begun_while_the_user_is_busy(self):
        runner, ident = self.a_booked_class()
        self.guard(runner, lambda: 0.5)
        with self.assertRaises(UserActive):
            runner.cancel(self.cfg.person("alex"), ident)
        self.assertEqual(self.app.cancel_presses, [])
        self.assertNotIn("cancel_attempted", self.statuses(), "nothing was written: the answer is applied later")

    def test_a_cancel_that_was_pressed_is_finished_whatever_the_user_does(self):
        runner, ident = self.a_booked_class()
        self.guard(runner, lambda: 9999.0 if not self.app.cancel_presses else 0.5)     # the user comes back after the press
        self.assertEqual(runner.cancel(self.cfg.person("alex"), ident), "cancelled")
        self.assertEqual(self.statuses()[-2:], ["cancel_attempted", "cancelled"])

    def test_a_non_urgent_run_does_not_quit_the_app_under_the_users_hands(self):
        att = self.attention(False, lambda: 9999.0)
        runner = self.build(attention=att, restart_app=True)
        att.note()                                           # the program acted a moment ago
        att.idle_fn = lambda: 0.5                            # and the user touched the mouse since
        with self.assertRaises(UserActive):
            runner._restart_app(StuckError("the app is stuck"))
        self.assertEqual(self.club_app.restarts, 0)


if __name__ == "__main__":
    unittest.main()
