import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from club_booking.setup import check_mirroir_config, check_mirroir_permissions, init_mirroir_config


class SetupTests(unittest.TestCase):
    def test_init_creates_files_and_never_overwrites(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(len(init_mirroir_config(home)), 2)
        self.assertEqual(check_mirroir_config(home), [])
        perms = home / ".mirroir-mcp" / "permissions.json"
        perms.write_text(json.dumps({"allow": ["tap"]}))
        self.assertEqual(init_mirroir_config(home), [])
        self.assertEqual(json.loads(perms.read_text()), {"allow": ["tap"]})
        self.assertIn("permissions.json must allow the tools tap and swipe", check_mirroir_config(home))

    def test_missing_files(self):
        self.assertEqual(len(check_mirroir_config(Path(tempfile.mkdtemp()))), 2)

    def test_permission_check_reads_the_tool_output(self):
        bad = "  \u2713 running\n  \u2717 not permitted\n    \u2192 Open System Settings\n  \u2717 not granted\n"
        good = "  \u2713 permitted\n  \u2713 permitted\n"
        run = lambda out: (lambda *a, **k: SimpleNamespace(stdout=out))
        self.assertEqual(check_mirroir_permissions("x", run(bad)),
                         ["Screen Recording is not allowed for this program",
                          "Accessibility is not allowed for this program"])
        self.assertEqual(check_mirroir_permissions("x", run(good)), [])

    def test_permission_check_when_the_tool_cannot_run(self):
        def boom(*a, **k):
            raise OSError("no such file")
        self.assertIn("could not run", check_mirroir_permissions("x", boom)[0])


if __name__ == "__main__":
    unittest.main()
