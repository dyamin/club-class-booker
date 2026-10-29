import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from club_booking.config import ConfigError, load_config
from club_booking.models import CardState, ClassCard, opening_time

TZ = ZoneInfo("Asia/Jerusalem")


class OpeningTimeTests(unittest.TestCase):
    def test_opens_exactly_24_hours_before(self):
        start = datetime(2026, 10, 14, 6, 45, tzinfo=TZ)
        self.assertEqual(opening_time(start), datetime(2026, 10, 13, 6, 45, tzinfo=TZ))

    def test_absolute_24_hours_across_the_clock_change(self):
        # Clocks go back on 2026-10-25. 24 absolute hours before 12:00 on the 25th is 13:00 on the 24th.
        start = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)
        self.assertEqual(opening_time(start), datetime(2026, 10, 24, 13, 0, tzinfo=TZ))

    def test_a_shown_opening_time_overrides_the_computed_one(self):
        start = datetime(2026, 10, 25, 12, 0, tzinfo=TZ)
        shown = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
        card = ClassCard("Late", start, start, CardState.NOT_OPEN, None, shown)
        self.assertEqual(card.opens_at, shown)
        self.assertEqual(ClassCard("Late", start, start, CardState.NOT_OPEN).opens_at, datetime(2026, 10, 24, 13, 0, tzinfo=TZ))

    def test_the_key_ignores_case_punctuation_and_marks(self):
        start = datetime(2026, 10, 13, 8, 50, tzinfo=TZ)
        def key(name):
            return ClassCard(name, start, start, CardState.OPEN).key
        self.assertEqual(key("ג'ודו"), key("ג׳ודו"))          # an ASCII apostrophe and a geresh
        self.assertEqual(key("METCON"), key(" Metcon "))
        self.assertEqual(key("Power-Gym"), key("power gym"))
        self.assertNotEqual(key("Yoga"), key("Yoga 2"))

    def test_card_key_and_opens_at(self):
        card = ClassCard("METCON", datetime(2026, 10, 13, 8, 50, tzinfo=TZ),
                         datetime(2026, 10, 13, 9, 35, tzinfo=TZ), CardState.OPEN, (512, 557))
        self.assertEqual(card.key, "2026-10-13T08:50|metcon")
        self.assertEqual(card.opens_at, datetime(2026, 10, 12, 8, 50, tzinfo=TZ))


class ConfigTests(unittest.TestCase):
    def write(self, data):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p, Path(d)

    PEOPLE = [
        {"key": "alex", "first_name": "Alex", "selector_row_y": 176, "calendar": "Alex"},
        {"key": "sam", "first_name": "Sam", "selector_row_y": 214, "calendar": "Kids", "title_prefix": "Sam - "},
    ]

    def test_loads_people_and_defaults(self):
        p, repo = self.write({"people": self.PEOPLE})
        cfg = load_config(p, repo)
        self.assertEqual([x.key for x in cfg.people], ["alex", "sam"])
        self.assertEqual(cfg.home_person.key, "alex")
        self.assertEqual(cfg.person("sam").title_prefix, "Sam - ")
        self.assertEqual(cfg.coords.selector_open, (520, 68))
        self.assertEqual(cfg.model, "claude-opus-5-5")
        self.assertEqual((cfg.max_person_failures, cfg.max_replans_per_day), (3, 2))
        self.assertEqual((cfg.max_booking_rounds, cfg.keep_evidence_days, cfg.replan_force_hours), (12, 30, 168))
        self.assertEqual((cfg.club_app_name, cfg.club_app_process, cfg.max_app_restarts), ("Runner 2", "Runner", 1))
        self.assertEqual((cfg.coords.favorites_underline, cfg.coords.schedule_underline), ((512, 126), (860, 126)))
        self.assertEqual((cfg.reader, cfg.ax_binary, cfg.phone_keychain_service), ("ax", Path("tools/bin/ax"), "club-booking-phone"))

    def test_the_automatic_login_is_off_unless_asked_and_only_true_or_false_count(self):
        p, repo = self.write({"people": self.PEOPLE})
        cfg = load_config(p, repo)
        self.assertEqual((cfg.auto_login, cfg.sms_sender, cfg.sms_wait_seconds), (False, "Hashmonaim", 120))
        self.assertEqual(cfg.messages_db, Path.home() / "Library" / "Messages" / "chat.db")
        p, repo = self.write({"people": self.PEOPLE, "auto_login": True, "sms_sender": "Other", "sms_wait_seconds": 60,
                              "messages_db": "/tmp/x/chat.db"})
        cfg = load_config(p, repo)
        self.assertEqual((cfg.auto_login, cfg.sms_sender, cfg.sms_wait_seconds, cfg.messages_db),
                         (True, "Other", 60, Path("/tmp/x/chat.db")))
        for bad in ("false", "yes", 1, None):
            p, repo = self.write({"people": self.PEOPLE, "auto_login": bad})
            with self.assertRaises(ConfigError, msg=repr(bad)):
                load_config(p, repo)

    def test_quitting_the_club_app_after_a_session_is_off_unless_asked_and_only_true_or_false_count(self):
        p, repo = self.write({"people": self.PEOPLE})
        cfg = load_config(p, repo)
        self.assertEqual((cfg.quit_app_after_run, cfg.app_start_wait_seconds), (False, 8.0))
        p, repo = self.write({"people": self.PEOPLE, "quit_app_after_run": True, "app_start_wait_seconds": 3})
        cfg = load_config(p, repo)
        self.assertEqual((cfg.quit_app_after_run, cfg.app_start_wait_seconds), (True, 3.0))
        for bad in ("false", "yes", 1, None):                 # bool("false") is True: only real booleans count
            p, repo = self.write({"people": self.PEOPLE, "quit_app_after_run": bad})
            with self.assertRaises(ConfigError, msg=repr(bad)):
                load_config(p, repo)

    def test_the_calendar_audit_is_off_unless_asked_and_a_typo_is_refused(self):
        p, repo = self.write({"people": self.PEOPLE})
        self.assertEqual(load_config(p, repo).calendar_audit, "off")
        for mode in ("off", "report", "write"):
            p, repo = self.write({"people": self.PEOPLE, "calendar_audit": mode})
            self.assertEqual(load_config(p, repo).calendar_audit, mode)
        p, repo = self.write({"people": self.PEOPLE, "calendar_audit": "yes"})
        with self.assertRaises(ConfigError):
            load_config(p, repo)

    def test_the_booked_tab_position_and_the_questions_switch(self):
        p, repo = self.write({"people": self.PEOPLE})
        cfg = load_config(p, repo)
        self.assertEqual((cfg.coords.subtab_booked, cfg.coords.booked_underline), ((170, 108), (170, 126)))
        self.assertEqual(cfg.questions, "off")
        p, repo = self.write({"people": self.PEOPLE, "questions": "on"})
        self.assertEqual(load_config(p, repo).questions, "on")
        p, repo = self.write({"people": self.PEOPLE, "questions": "maybe"})
        with self.assertRaises(ConfigError):
            load_config(p, repo)

    def test_a_person_asks_only_when_the_config_says_so(self):
        people = [dict(self.PEOPLE[0], asks=True), self.PEOPLE[1]]
        p, repo = self.write({"people": people})
        cfg = load_config(p, repo)
        self.assertEqual([x.asks for x in cfg.people], [True, False])

    def test_the_several_classes_question_is_on_unless_switched_off(self):
        p, repo = self.write({"people": self.PEOPLE})
        self.assertTrue(load_config(p, repo).questions_several)
        p, repo = self.write({"people": self.PEOPLE, "questions_several": False})
        self.assertFalse(load_config(p, repo).questions_several)
        p, repo = self.write({"people": self.PEOPLE, "questions_several": "no"})
        with self.assertRaises(ConfigError):
            load_config(p, repo)

    def test_a_person_can_ask_about_overlaps_only(self):
        # 2026-10-15: the club refuses two overlapping classes ("לא ניתן להירשם לשני שיעורים חופפים"); the sons' overlaps are
        # a decision for the user, but several classes in a day are not (they have many)
        people = [dict(self.PEOPLE[0], asks_overlaps=True), self.PEOPLE[1]]
        p, repo = self.write({"people": people})
        cfg = load_config(p, repo)
        self.assertEqual([(x.asks, x.asks_overlaps) for x in cfg.people], [(False, True), (False, False)])

    def test_overrides(self):
        p, repo = self.write({"people": self.PEOPLE, "horizon_days": 3, "max_run_seconds": 600, "max_person_failures": 5,
                              "coords": {"selector_open": [1, 2]}})
        cfg = load_config(p, repo)
        self.assertEqual(cfg.horizon_days, 3)
        self.assertEqual(cfg.max_person_failures, 5)
        self.assertEqual(cfg.max_run_seconds, 600)
        self.assertEqual(cfg.coords.selector_open, (1, 2))

    def test_rejects_bad_input(self):
        p, repo = self.write({"people": []})
        with self.assertRaises(ConfigError):
            load_config(p, repo)
        p, repo = self.write({"people": self.PEOPLE + [self.PEOPLE[0]]})
        with self.assertRaises(ConfigError):
            load_config(p, repo)
        p, repo = self.write({"people": self.PEOPLE, "coords": {"nope": [1, 2]}})
        with self.assertRaises(ConfigError):
            load_config(p, repo)
        with self.assertRaises(ConfigError):
            load_config(Path("/nonexistent/config.json"), Path("/tmp"))

    def test_unknown_person(self):
        p, repo = self.write({"people": self.PEOPLE})
        with self.assertRaises(ConfigError):
            load_config(p, repo).person("zed")


if __name__ == "__main__":
    unittest.main()
