import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from club_booking.screen import MirroirScreen, ScreenError

FAKE = [sys.executable, str(Path(__file__).with_name("fake_mirroir.py"))]


class ScreenTests(unittest.TestCase):
    def test_tap_swipe_screenshot_status(self):
        with MirroirScreen(FAKE, timeout=10) as screen:
            self.assertEqual(screen.tap(520, 68), "Tapped at (520, 68)")
            self.assertIn("Swiped from (250,680) to (250,300)", screen.swipe(250, 680, 250, 300))
            shot = Image.open(io.BytesIO(screen.screenshot()))
            self.assertEqual(shot.size, (1024, 800))
            self.assertEqual(screen.window_region(), (-1377, 101, 1024, 800))

    def test_unknown_tool_error_and_tool_error(self):
        with MirroirScreen(FAKE, timeout=10) as screen:
            with self.assertRaises(ScreenError):
                screen._call_tool("nope", {})
            with self.assertRaises(ScreenError) as ctx:
                screen._call_tool("broken", {})
            self.assertIn("it broke", str(ctx.exception))

    def test_timeout_when_the_tool_never_answers(self):
        screen = MirroirScreen(FAKE, timeout=1)
        screen.start()
        try:
            with self.assertRaises(ScreenError) as ctx:
                screen._call_tool("hang", {})
            self.assertIn("no answer", str(ctx.exception))
        finally:
            screen.close()

    def test_the_tool_dies(self):
        screen = MirroirScreen(FAKE, timeout=5)
        screen.start()
        try:
            with self.assertRaises(ScreenError):
                screen._call_tool("die", {})
        finally:
            screen.close()

    def test_a_screenshot_on_a_retina_screen_is_scaled_to_window_points(self):
        # the real tool returns native pixels (2048x1600); every position the program uses is in points
        with MirroirScreen(FAKE + ["--retina"], timeout=10) as screen:
            self.assertEqual(Image.open(io.BytesIO(screen.screenshot())).size, (1024, 800))

    def test_reconnect_starts_the_tool_again(self):
        screen = MirroirScreen(FAKE, timeout=10)
        screen.start()
        try:
            first_process = screen._proc
            screen.reconnect()
            self.assertIsNot(screen._proc, first_process)
            self.assertEqual(screen.tap(1, 2), "Tapped at (1, 2)")
        finally:
            screen.close()

    def test_a_screenshot_right_after_the_app_was_activated_is_tried_again(self):
        # the real tool says "Target 'club' is not running" for a moment after the app came to the front
        sleeps = []
        with MirroirScreen(FAKE + ["--flaky"], timeout=10, sleep=sleeps.append) as screen:
            self.assertEqual(Image.open(io.BytesIO(screen.screenshot())).size, (1024, 800))
        self.assertEqual(len(sleeps), 1)

    def test_a_tap_while_the_tool_has_lost_the_window_is_tried_again(self):
        # seen on the real app (2026-10-13, 19:31): "Target 'club' window not found. Is the app running?" once, then fine.
        # The tool says this before it acts, so nothing was tapped
        sleeps = []
        with MirroirScreen(FAKE + ["--lostwindow"], timeout=10, sleep=sleeps.append) as screen:
            self.assertEqual(screen.tap(1, 2), "Tapped at (1, 2)")
        self.assertEqual(len(sleeps), 1)

    def test_a_tool_that_stays_not_running_is_an_error_after_the_tries(self):
        sleeps = []
        with MirroirScreen(FAKE + ["--down"], timeout=10, sleep=sleeps.append) as screen:
            with self.assertRaises(ScreenError) as ctx:
                screen.screenshot()
        self.assertIn("not running", str(ctx.exception))
        self.assertEqual(len(sleeps), 3, "three more tries, then the error")

    def test_the_last_window_region_is_used_while_the_tool_finds_no_window(self):
        # seen on the real app while the calendar editor is open: the window did not move, the status lost it
        with MirroirScreen(FAKE + ["--nowindow"], timeout=10) as screen:
            first = screen.window_region()
            self.assertEqual(screen.window_region(), first)
            self.assertEqual(screen.window_region(), first)

    def test_a_restart_forgets_the_old_region(self):
        with MirroirScreen(FAKE + ["--nowindow"], timeout=10) as screen:
            screen.window_region()
            screen._last_region = (1, 2, 3, 4)
            screen._points = (3, 4)
            screen.reconnect()
            self.assertIsNone(screen._last_region)

    def test_without_an_earlier_region_no_window_is_an_error(self):
        with MirroirScreen(FAKE + ["--nowindow"], timeout=10) as screen:
            screen.status()                      # the one good answer is used up
            with self.assertRaises(ScreenError):
                screen.window_region()

    def test_a_screenshot_in_points_is_not_touched(self):
        with MirroirScreen(FAKE, timeout=10) as screen:
            first = screen.screenshot()
            self.assertEqual(screen.screenshot(), first)

    @staticmethod
    def _png(width, height):
        buf = io.BytesIO()
        Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
        return buf.getvalue()

    def _region(self, width, height):
        seen = {}
        data = self._png(width, height)

        def runner(cmd, **kwargs):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(data)
            return SimpleNamespace(returncode=0, stderr="")

        with MirroirScreen(FAKE, timeout=10, region_runner=runner) as screen:
            return screen.region_png(), seen["cmd"], data

    def test_region_png_uses_the_window_position(self):
        png, cmd, data = self._region(1024, 800)
        self.assertEqual(cmd[:4], ["/usr/sbin/screencapture", "-x", "-R", "-1377,101,1024,800"])
        self.assertEqual(png, data, "already in points: not touched")

    def test_region_png_on_a_retina_screen_is_scaled_to_window_points(self):
        # screencapture writes native pixels (2048x1600); taps and OCR positions are in points (1024x800)
        png, _cmd, _data = self._region(2048, 1600)
        self.assertEqual(Image.open(io.BytesIO(png)).size, (1024, 800))

    def test_not_started(self):
        with self.assertRaises(ScreenError):
            MirroirScreen(FAKE).tap(1, 1)


if __name__ == "__main__":
    unittest.main()
