"""The program logs the club app in by itself (the user decided on 2026-10-15). The code is requested at most once an
hour automatically, and only when the program can read the answer."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from club_booking.autologin import AutoLogin
from club_booking.login import LoginBlocked, LoginError, LoginGuard
from club_booking.sms import SmsError

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 15, 12, 50, tzinfo=TZ)


class FakeHelper:
    def __init__(self, home):
        self.guard = LoginGuard(home / "login.json")
        self.requests, self.entries = [], []
        self.request_result, self.request_error = "code_screen", None
        self.enter_result, self.enter_error = "logged_in", None
        self.order = None

    def request_code(self, phone):
        self.order.append("request")
        self.requests.append(phone)
        if self.request_error:
            raise self.request_error
        return self.request_result

    def enter_code(self, code):
        self.order.append("enter")
        self.entries.append(code)
        if self.enter_error:
            raise self.enter_error
        return self.enter_result


class FakeInbox:
    sender = "hashmonaim"
    last_age = None            # seconds since the newest message of any kind (None: not known / no messages)

    def __init__(self):
        self.snapshot, self.snapshot_error = 100, None
        self.script = []              # one answer per poll: a list of (rowid, code), or an exception
        self.polls, self.order = [], None

    def newest_rowid(self):
        self.order.append("snapshot")
        if self.snapshot_error:
            raise self.snapshot_error
        return self.snapshot

    def last_message_age(self, now):
        return self.last_age

    def fresh_codes(self, after, since):
        self.polls.append((after, since))
        answer = self.script.pop(0) if self.script else []
        if isinstance(answer, Exception):
            raise answer
        return answer


class AutoCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.order, self.sleeps, self.logs = [], [], []
        self.helper, self.inbox = FakeHelper(self.home), FakeInbox()
        self.helper.order = self.inbox.order = self.order
        self.state = {"now": NOW, "kind": "login", "phone": "0501112233"}

    def make(self, **kw):
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.state["now"] += timedelta(seconds=seconds)

        def phone():
            if self.state["phone"] is None:
                raise RuntimeError("no phone in the Keychain")
            return self.state["phone"]
        return AutoLogin(self.helper, self.inbox, LoginGuard(self.home / "h.json", 2, timedelta(minutes=45)),
                         LoginGuard(self.home / "d.json", 4, timedelta(hours=24)), self.home / "autologin.json",
                         phone=phone, log=lambda event, **f: self.logs.append((event, f)), sleep=sleep,
                         now_fn=lambda: self.state["now"], screen=lambda: self.state["kind"], **kw)

    def saved(self):
        return json.loads((self.home / "autologin.json").read_text())


class HappyPathTests(AutoCase):
    def test_it_requests_once_reads_the_new_code_and_types_it_once(self):
        self.inbox.script = [[], [(101, "123456")]]
        result = self.make().run()
        self.assertEqual((result.status, self.helper.requests, self.helper.entries),
                         ("logged_in", ["0501112233"], ["123456"]))
        self.assertEqual(self.order, ["snapshot", "request", "enter"], "the snapshot comes BEFORE the request")

    def test_the_search_is_for_messages_after_the_snapshot_and_after_the_request_time(self):
        self.inbox.script = [[(101, "123456")]]
        self.make().run()
        self.assertEqual(self.inbox.polls[0], (100, NOW))

    def test_the_request_is_written_down_before_it_is_made(self):
        seen = {}
        original = self.helper.request_code

        def spy(phone):
            seen["state"] = self.saved()
            seen["hour"] = LoginGuard(self.home / "h.json", 2, timedelta(minutes=45)).requests()
            return original(phone)
        self.helper.request_code = spy
        self.inbox.script = [[(101, "123456")]]
        self.make().run()
        self.assertEqual(seen["state"]["request"], {"at": NOW.isoformat(), "snapshot": 100})
        self.assertEqual(len(seen["hour"]), 1)

    def test_the_rowid_that_was_used_is_kept_and_never_used_again(self):
        self.inbox.script = [[(101, "123456")]]
        self.make().run()
        self.assertEqual(self.saved()["used_rowid"], 101)
        self.inbox.polls.clear()
        self.state["kind"], self.state["now"] = "login", NOW + timedelta(hours=2)
        self.inbox.script = [[(105, "222222")]]
        self.make().run()
        self.assertEqual(self.inbox.polls[0][0], 101, "after the newest of the snapshot (100) and the used rowid (101)")

    def test_the_code_and_the_message_are_never_logged(self):
        self.inbox.script = [[(101, "123456")]]
        self.make().run()
        self.assertTrue(self.logs)
        self.assertNotIn("123456", json.dumps(self.logs, default=str))
        self.assertNotIn("0501112233", json.dumps(self.logs, default=str))

    def test_the_reason_of_a_failure_is_logged_without_digits(self):
        # 2026-10-17 16:54: a login_error was logged with no reason, and nobody could tell what had gone wrong
        self.helper.request_error = LoginError("the field shows 0501112233 and 123456")
        self.assertEqual(self.make().run().status, "login_error")
        logged = json.dumps(self.logs, default=str)
        self.assertIn("the field shows", logged)
        for digits in ("0501112233", "123456", "050", "1112233"):
            self.assertNotIn(digits, logged)

    def test_a_good_login_logs_no_detail(self):
        self.inbox.script = [[(101, "123456")]]
        self.make().run()
        self.assertNotIn("detail", json.dumps(self.logs, default=str))


class WhenItDoesNothingTests(AutoCase):
    def test_a_screen_that_is_not_the_login_screen_is_left_alone(self):
        for kind in ("locked", "update", "favorites", "other"):
            self.state["kind"] = kind
            result = self.make().run()
            self.assertEqual(result.status, "not_login_screen", kind)
        self.assertEqual((self.helper.requests, self.order), ([], []))

    def test_no_request_is_made_when_the_messages_cannot_be_read(self):
        self.inbox.snapshot_error = SmsError("cannot read the Messages database (Full Disk Access)")
        result = self.make().run()
        self.assertEqual(result.status, "no_access")
        self.assertIn("Full Disk Access", result.detail)
        self.assertEqual(self.helper.requests, [], "a code that nobody can read must not be asked for")

    def test_no_request_is_made_without_a_phone_number(self):
        self.state["phone"] = None
        result = self.make().run()
        self.assertEqual((result.status, self.helper.requests), ("no_phone", []))

    def test_the_apps_own_lock_counter_is_asked_first_and_costs_no_budget(self):
        for _ in range(2):
            self.helper.guard.record_request(NOW - timedelta(minutes=1))       # two manual requests just now
        result = self.make().run()
        self.assertEqual((result.status, self.helper.requests), ("blocked", []))
        self.assertEqual(LoginGuard(self.home / "h.json", 2, timedelta(minutes=45)).requests(), [])


class BudgetTests(AutoCase):
    def test_a_second_request_within_45_minutes_is_refused(self):
        self.inbox.script = [[(101, "123456")]]
        self.make().run()
        self.state["now"] = NOW + timedelta(minutes=30)
        result = self.make().run()
        self.assertEqual((result.status, len(self.helper.requests)), ("budget", 1))
        self.assertIn("minutes", result.detail)

    def test_after_45_minutes_it_may_ask_again_but_never_a_fourth_time_in_a_day(self):
        for i in range(4):
            self.state["now"] = NOW + timedelta(minutes=50 * i)
            self.inbox.script = [[(101 + i, f"11111{i}")]]
            self.make().run()
        self.assertEqual(len(self.helper.requests), 3, "three a day, at most")

    def test_a_request_that_failed_still_uses_up_the_budget(self):
        self.helper.request_error = LoginError("the field shows something else")
        self.assertEqual(self.make().run().status, "login_error")
        self.state["now"] = NOW + timedelta(minutes=10)
        self.assertEqual(self.make().run().status, "budget")


class NoSmsTests(AutoCase):
    def test_when_no_sms_comes_it_waits_then_stops_and_never_asks_twice(self):
        result = self.make(wait=120.0, poll=4.0).run()
        self.assertEqual((result.status, len(self.helper.requests), self.helper.entries), ("no_sms", 1, []))
        self.assertGreaterEqual(sum(self.sleeps), 120)
        self.assertLessEqual(sum(self.sleeps), 130)
        self.assertIn("Text Message Forwarding", result.detail)

    def test_a_mac_that_received_nothing_for_days_says_the_pairing_is_broken(self):
        # 2026-10-15: no SMS had reached this Mac for 59 days; "no SMS arrived" alone sent the user searching
        self.inbox.last_age = 59 * 86400
        result = self.make(wait=8.0, poll=4.0).run()
        self.assertEqual(result.status, "no_sms")
        self.assertIn("59 days", result.detail)
        self.assertIn("paired", result.detail)

    def test_a_mac_that_receives_messages_does_not_blame_the_pairing(self):
        self.inbox.last_age = 600.0
        result = self.make(wait=8.0, poll=4.0).run()
        self.assertNotIn("paired", result.detail)

    def test_a_database_that_fails_while_waiting_is_no_access_and_types_nothing(self):
        self.inbox.script = [[], SmsError("database is locked")]
        result = self.make().run()
        self.assertEqual((result.status, self.helper.entries), ("no_access", []))

    def test_a_code_that_the_app_does_not_accept_is_not_tried_again(self):
        self.helper.enter_result = "unknown"
        self.inbox.script = [[(101, "123456")]]
        result = self.make().run()
        self.assertEqual((result.status, self.helper.entries), ("bad_code", ["123456"]))

    def test_the_lock_of_the_app_after_the_request_is_reported(self):
        self.helper.request_error = LoginBlocked("the app locked the login for 10 minutes")
        result = self.make().run()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.helper.entries, [])


class CodeScreenTests(AutoCase):
    def test_a_code_screen_after_our_own_request_waits_for_that_code_and_asks_for_no_new_one(self):
        self.inbox.script = [[]] * 40            # the first attempt: no SMS in time
        self.make(wait=8.0, poll=4.0).run()
        self.assertEqual(len(self.helper.requests), 1)
        self.state["kind"], self.state["now"] = "code", NOW + timedelta(minutes=3)
        self.inbox.script = [[(102, "654321")]]
        result = self.make().run()
        self.assertEqual((result.status, self.helper.entries, len(self.helper.requests)), ("logged_in", ["654321"], 1))
        self.assertEqual(self.inbox.polls[-1], (100, NOW), "the snapshot and the time of the first request")

    def test_a_code_screen_with_no_recent_request_of_ours_gets_nothing_typed(self):
        self.state["kind"] = "code"
        result = self.make().run()
        self.assertEqual((result.status, self.helper.entries, self.helper.requests, self.inbox.polls),
                         ("stale_code_screen", [], [], []))

    def test_a_code_screen_whose_request_is_older_than_five_minutes_is_stale(self):
        self.inbox.script = [[]] * 40
        self.make(wait=4.0, poll=4.0).run()
        self.state["kind"], self.state["now"] = "code", NOW + timedelta(minutes=6)
        self.assertEqual(self.make().run().status, "stale_code_screen")
        self.assertEqual(self.helper.entries, [])


if __name__ == "__main__":
    unittest.main()
