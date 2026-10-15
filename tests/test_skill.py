import re
import unittest
from pathlib import Path

from club_booking.cli import parser

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "club-booking" / "SKILL.md"


class SkillTests(unittest.TestCase):
    def text(self):
        self.assertTrue(SKILL.exists(), "the skill file is missing")
        return SKILL.read_text(encoding="utf-8")

    def test_the_skill_has_a_name_and_a_description_that_says_when_to_use_it(self):
        head = self.text().split("---")[1]
        self.assertRegex(head, r"(?m)^name: club-booking$")
        description = re.search(r"(?m)^description: (.+)$", head)
        self.assertIsNotNone(description)
        self.assertIn("Use when", description.group(1))

    def test_every_command_the_skill_names_is_a_real_command(self):
        commands = set(re.findall(r"python -m club_booking ([a-z][a-z-]*)", self.text()))
        self.assertTrue(commands, "the skill names no command")
        real = set(parser()._subparsers._group_actions[0].choices)
        self.assertEqual(commands - real, set(), "the skill names a command the program does not have")

    def test_the_skill_carries_the_rules_that_protect_the_real_account(self):
        text = self.text()
        for rule in ("login request", "exactly once", "never cancel", "PAUSE", "docs/lessons.md"):
            self.assertIn(rule, text.replace("Never cancel", "never cancel"), rule)


if __name__ == "__main__":
    unittest.main()
