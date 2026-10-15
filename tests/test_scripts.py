import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run-now.command"



class InstallScriptTests(unittest.TestCase):
    def test_the_install_script_is_executable_valid_and_builds_all_three_helpers(self):
        import os
        import subprocess
        from pathlib import Path
        script = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"
        self.assertTrue(os.access(script, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode, 0)
        text = script.read_text(encoding="utf-8")
        for needle in ("build-ax.sh", "build-ocr.sh", "build-mirroir.sh", "init-mirroir", "club_booking setup", "docs/INSTALL.md"):
            self.assertIn(needle, text)
        self.assertNotIn("sudo", text, "no administrator password")

    def test_the_guide_names_every_command_a_new_user_runs(self):
        from pathlib import Path
        guide = (Path(__file__).resolve().parent.parent / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        for needle in ("scripts/install.sh", "club_booking setup", "club_booking doctor", "club_booking login request",
                       "club_booking login auto", "club_booking install", "club_booking arm-test", "Full Disk Access",
                       "Text Message Forwarding", "PAUSE", "uninstall"):
            self.assertIn(needle, guide)

class RunNowScriptTests(unittest.TestCase):
    def test_the_one_key_script_is_executable_and_starts_a_run_from_the_repo(self):
        self.assertTrue(SCRIPT.exists(), "the README and the Shortcut point at this file")
        self.assertTrue(os.access(SCRIPT, os.X_OK), "a .command file must be executable to open in Terminal")
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/bash"))
        self.assertIn(".venv/bin/python -m club_booking run --no-countdown", text)

    def test_it_works_from_any_folder_and_leaves_the_window_open_for_a_key(self):
        # a copy of the repo layout with a fake python: the script must cd to the repo root itself
        root = Path(tempfile.mkdtemp())
        (root / "scripts").mkdir()
        (root / ".venv" / "bin").mkdir(parents=True)
        script = root / "scripts" / "run-now.command"
        script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        script.chmod(0o755)
        fake = root / ".venv" / "bin" / "python"
        fake.write_text('#!/bin/bash\necho "args: $@" > ran.txt\n', encoding="utf-8")
        fake.chmod(0o755)
        done = subprocess.run([str(script)], cwd="/", input="x", text=True, capture_output=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((root / "ran.txt").read_text().strip(), "args: -m club_booking run --no-countdown")
        self.assertIn("read -n 1", script.read_text(), "the window waits for a key, so the result can be read")


if __name__ == "__main__":
    unittest.main()
