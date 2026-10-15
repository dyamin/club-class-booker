"""The guided setup for a new user (2026-10-15: "make sure the app is ready to be delivered"). All input and output is
injected: nothing is read from the keyboard or written to the Keychain in a test."""
import json
import tempfile
import unittest
from pathlib import Path

from club_booking.config import load_config
from club_booking.wizard import DEFAULT_FIRST_ROW, ROW_STEP, Wizard, slug


class Script:
    """Plays the user's answers, in order, and records every question."""

    def __init__(self, *answers):
        self.answers, self.questions = list(answers), []

    def __call__(self, question):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else ""


class WizardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.json"
        self.lines, self.stored = [], {}

    def wizard(self, answers, secrets=("0501112223", "sk-test"), calendars=("Home", "Kids"), rows=None, path=None):
        secret_answers = list(secrets)
        return Wizard(ask=Script(*answers), secret=lambda q: secret_answers.pop(0) if secret_answers else "",
                      out=self.lines.append, calendars=lambda: list(calendars),
                      store_secret=lambda service, value: self.stored.__setitem__(service, value),
                      find_rows=lambda: rows if rows is not None else [], config_path=path or self.path)

    def config(self, path=None):
        return json.loads((path or self.path).read_text(encoding="utf-8"))


class SlugTests(unittest.TestCase):
    def test_a_key_is_lowercase_letters_and_unique(self):
        self.assertEqual(slug("Dana", set()), "dana")
        self.assertEqual(slug("Dana", {"dana"}), "dana2")
        self.assertEqual(slug("דנה", set()), "person1", "a name without Latin letters still gets a key")


class AskingTests(WizardCase):
    def test_one_person_with_the_first_calendar_makes_a_working_config(self):
        w = self.wizard(["Dana", "", "", "", "", "", ""])          # name, calendar, prefix, overlaps, end of list, ...
        self.assertEqual(w.run(), 0)
        cfg = load_config(self.path, self.tmp)                      # the file is a valid config
        [p] = cfg.people
        self.assertEqual((p.key, p.first_name, p.calendar, p.title_prefix, p.asks, p.asks_overlaps),
                         ("dana", "Dana", "Home", "", False, True))
        self.assertEqual(p.selector_row_y, DEFAULT_FIRST_ROW)
        self.assertEqual((cfg.questions, cfg.calendar_audit, cfg.auto_login), ("on", "write", False))

    def test_two_people_who_share_a_calendar_get_a_name_prefix_for_the_events(self):
        w = self.wizard(["Dana", "2", "", "", "Ben", "2", "", "", "", "", ""])
        w.run()
        cfg = load_config(self.path, self.tmp)
        self.assertEqual([(p.calendar, p.title_prefix) for p in cfg.people], [("Kids", ""), ("Kids", "Ben - ")])
        self.assertEqual([p.selector_row_y for p in cfg.people], [DEFAULT_FIRST_ROW, DEFAULT_FIRST_ROW + ROW_STEP])

    def test_a_calendar_can_be_named_and_a_wrong_answer_is_asked_again(self):
        w = self.wizard(["Dana", "Nope", "Kids", "", "", "", "", ""])
        w.run()
        self.assertEqual(load_config(self.path, self.tmp).people[0].calendar, "Kids")
        self.assertTrue(any("not one of" in line for line in self.lines))

    def test_the_rows_found_in_the_app_are_offered_and_chosen_by_number(self):
        rows = [("Club - first profile", 176), ("Club - second profile", 214)]
        w = self.wizard(["Dana", "", "", "", "Ben", "", "", "", "", "2", "1", ""], rows=rows)
        w.run()
        self.assertEqual([p.selector_row_y for p in load_config(self.path, self.tmp).people], [214, 176])

    def test_the_phone_number_and_the_key_go_to_the_keychain_and_never_to_the_file(self):
        self.wizard(["Dana", "", "", "", "", "", ""]).run()
        self.assertEqual(self.stored, {"club-booking-phone": "0501112223", "club-booking-anthropic": "sk-test"})
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("0501112223", text)
        self.assertNotIn("sk-test", text)

    def test_an_empty_secret_is_not_stored(self):
        self.wizard(["Dana", "", "", "", "", "", ""], secrets=("", "")).run()
        self.assertEqual(self.stored, {})

    def test_a_phone_number_must_be_digits(self):
        w = self.wizard(["Dana", "", "", "", "", "", ""], secrets=("abc", "05 0111 2223", "sk"))
        w.run()
        self.assertEqual(self.stored["club-booking-phone"], "0501112223", "spaces are removed; letters are asked again")

    def test_the_automatic_login_is_asked_and_off_by_default(self):
        w = self.wizard(["Dana", "", "", "", "", "y", ""])          # ... end of list, auto login: yes
        w.run()
        self.assertTrue(self.config()["auto_login"])

    def test_without_a_name_nothing_is_written(self):
        self.assertEqual(self.wizard([""]).run(), 1)
        self.assertFalse(self.path.exists())


class OverwriteTests(WizardCase):
    def test_an_existing_config_is_not_overwritten_without_a_yes(self):
        self.path.write_text('{"people": []}', encoding="utf-8")
        self.wizard(["Dana", "", "", "", "", "", "", "n"]).run()
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"people": []}')
        self.assertTrue((self.tmp / "config.new.json").exists(), "the new one is written beside it")
        self.assertTrue(any("config.new.json" in line for line in self.lines))

    def test_a_yes_replaces_the_file(self):
        self.path.write_text('{"people": []}', encoding="utf-8")
        self.wizard(["Dana", "", "", "", "", "", "", "y"]).run()
        self.assertEqual(self.config()["people"][0]["first_name"], "Dana")


class NextStepsTests(WizardCase):
    def test_the_last_lines_say_what_to_do_next(self):
        self.wizard(["Dana", "", "", "", "", "", ""]).run()
        text = "\n".join(self.lines)
        for word in ("doctor", "install", "login"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
