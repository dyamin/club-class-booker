import unittest
from pathlib import Path
from types import SimpleNamespace

from club_booking.ax import AxClient, AxElement, AxError, parse_dump

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def runner_returning(stdout="", returncode=0, stderr="", raises=None):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


class AxClientTests(unittest.TestCase):
    def client(self, run):
        return AxClient(Path("tools/bin/ax"), "Runner", runner=run)

    def test_dump_parses_the_helper_output(self):
        run = runner_returning(fixture("ax_favorites_top.json"))
        dump = self.client(run).dump()
        self.assertEqual(run.calls[0], ["tools/bin/ax", "dump", "Runner"])
        self.assertEqual((dump.window.w, dump.window.h), (1024, 800))
        self.assertTrue(any(e.role == "AXScrollArea" for e in dump.elements))
        self.assertIsInstance(dump.elements[0], AxElement)

    def test_a_failing_helper_is_an_error_with_its_message(self):
        with self.assertRaises(AxError) as ctx:
            self.client(runner_returning(returncode=1, stderr="the app Runner is not running")).dump()
        self.assertIn("not running", str(ctx.exception))

    def test_output_that_is_not_json_is_an_error(self):
        with self.assertRaises(AxError):
            self.client(runner_returning("not json")).dump()
        with self.assertRaises(AxError):
            parse_dump('{"window": {}}')

    def test_a_missing_helper_is_an_error(self):
        with self.assertRaises(AxError):
            self.client(runner_returning(raises=FileNotFoundError("no ax"))).dump()

    def test_press_passes_the_role_the_exact_text_and_the_position(self):
        run = runner_returning("pressed\n")
        self.client(run).press("AXButton", "שלחו לי קוד", 767)
        self.assertEqual(run.calls[0], ["tools/bin/ax", "press", "Runner", "AXButton", "שלחו לי קוד", "767", "25"])
        with self.assertRaises(AxError):
            self.client(runner_returning(returncode=1, stderr="no such element")).press("AXButton", "x", 1)

    def test_press_in_card_passes_the_card_text_and_the_button_text(self):
        run = runner_returning("pressed\n")
        self.client(run).press_in_card("רביעי\n14/10\nשעה: 09:50 עד 10:30", "ביטול")
        self.assertEqual(run.calls[0][1:], ["press-in-card", "Runner", "רביעי\n14/10\nשעה: 09:50 עד 10:30", "ביטול"])

    def test_scroll_goes_down_or_up_by_page(self):
        run = runner_returning("scrolled\n")
        self.client(run).scroll("down")
        self.assertEqual(run.calls[0], ["tools/bin/ax", "scroll", "Runner", "down"])
        with self.assertRaises(AxError):
            self.client(run).scroll("sideways")


if __name__ == "__main__":
    unittest.main()
