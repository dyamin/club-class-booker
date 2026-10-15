import io
import subprocess
import unittest
from types import SimpleNamespace

from PIL import Image, ImageDraw

from club_booking.ocr import OcrError, TextHit, find_text, run_ocr
from club_booking.pixels import (PixelError, band_changed, blue_at, is_link_blue, is_selected_blue, looks_like_selector,
                                 refine_button_center, selected_row)
from club_booking.secrets import SecretError, get_api_key

ROWS = [176, 214, 253]
BLUE = (76, 159, 213)


def png_of(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def selector_png(selected):
    img = Image.new("RGB", (1024, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i, y in enumerate(ROWS):
        draw.rectangle([8, y - 15, 1016, y + 15], fill=BLUE if i == selected else (255, 255, 255))
    return png_of(img)


class StoreSecretTests(unittest.TestCase):
    def test_a_secret_is_added_or_updated_through_the_security_tool(self):
        from club_booking.secrets import store_secret
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"returncode": 0, "stderr": ""})()
        store_secret("club-booking-phone", "0501112223", account="dana", runner=runner)
        self.assertEqual(calls[0][:2], ["/usr/bin/security", "add-generic-password"])
        self.assertIn("-U", calls[0], "an existing item is updated, not refused")
        self.assertEqual(calls[0][calls[0].index("-s") + 1], "club-booking-phone")
        self.assertEqual(calls[0][calls[0].index("-a") + 1], "dana")

    def test_a_refusal_of_the_tool_is_an_error_that_does_not_show_the_secret(self):
        from club_booking.secrets import SecretError, store_secret
        runner = lambda cmd, **kw: type("R", (), {"returncode": 1, "stderr": "denied"})()
        with self.assertRaises(SecretError) as ctx:
            store_secret("club-booking-phone", "0501112223", account="dana", runner=runner)
        self.assertNotIn("0501112223", str(ctx.exception))


class OcrSourceTests(unittest.TestCase):
    def test_the_ocr_helper_falls_back_to_the_fast_recognizer(self):
        # 2026-10-15 (macOS 27.2 beta): the accurate recognizer threw "e5rtError" for every picture; the fast one works
        from pathlib import Path
        source = (Path(__file__).resolve().parent.parent / "tools" / "ocr.swift").read_text(encoding="utf-8")
        self.assertIn("recognize(.accurate)", source)
        self.assertIn("catch", source)
        self.assertIn("recognize(.fast)", source)


class SecretsTests(unittest.TestCase):
    def test_returns_the_key(self):
        runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout="sk-test\n", stderr="")
        self.assertEqual(get_api_key("svc", runner), "sk-test")

    def test_missing_key_explains_how_to_add_it(self):
        runner = lambda *a, **k: SimpleNamespace(returncode=44, stdout="", stderr="not found")
        with self.assertRaises(SecretError) as ctx:
            get_api_key("svc", runner)
        self.assertIn("security add-generic-password", str(ctx.exception))


class PixelTests(unittest.TestCase):
    def test_colors(self):
        self.assertTrue(is_selected_blue(BLUE))
        self.assertFalse(is_selected_blue((255, 255, 255)))
        self.assertTrue(is_link_blue((62, 150, 210)))
        self.assertFalse(is_link_blue((30, 30, 30)))

    def test_selected_row(self):
        for i in range(3):
            self.assertEqual(selected_row(selector_png(i), ROWS, 512), i)

    def test_selected_row_none_when_nothing_or_two_are_blue(self):
        self.assertIsNone(selected_row(selector_png(-1), ROWS, 512))
        img = Image.new("RGB", (1024, 800), BLUE)
        self.assertIsNone(selected_row(png_of(img), ROWS, 512))

    def test_text_in_the_middle_of_a_row_does_not_hide_the_blue(self):
        img = Image.open(io.BytesIO(selector_png(1))).convert("RGB")
        ImageDraw.Draw(img).rectangle([500, 214 - 6, 524, 214 + 6], fill=(30, 30, 30))    # a name written on the row
        png = png_of(img)
        self.assertEqual(selected_row(png, ROWS, 512), 1)
        self.assertTrue(looks_like_selector(png, ROWS, 512))

    def test_the_real_selector_layout_with_a_wide_white_name_on_the_blue_row(self):
        # seen on the real app: the selected row is blue with a wide white name (and a check mark) in the middle;
        # the other rows are white with a light-blue border and a blue name in the middle
        img = Image.new("RGB", (1024, 800), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for i, y in enumerate(ROWS):
            if i == 1:
                draw.rectangle([8, y - 15, 1016, y + 15], fill=BLUE)
                draw.rectangle([415, y - 8, 610, y + 8], fill=(255, 255, 255))          # the white name
            else:
                draw.rectangle([8, y - 15, 1016, y + 15], outline=(120, 175, 225), fill=(255, 255, 255))
                draw.rectangle([425, y - 8, 600, y + 8], fill=(100, 165, 215))            # the blue name
        png = png_of(img)
        self.assertEqual(selected_row(png, ROWS, 512), 1)
        self.assertTrue(looks_like_selector(png, ROWS, 512))

    def test_a_lone_blue_dot_on_a_white_row_is_not_a_selected_row(self):
        img = Image.new("RGB", (1024, 800), (255, 255, 255))
        ImageDraw.Draw(img).rectangle([510, 174, 514, 178], fill=BLUE)
        png = png_of(img)
        self.assertIsNone(selected_row(png, ROWS, 512))
        self.assertFalse(looks_like_selector(png, ROWS, 512))

    def test_a_picture_too_small_for_the_rows_is_not_a_selector(self):
        small = png_of(Image.new("RGB", (100, 100), (255, 255, 255)))
        self.assertFalse(looks_like_selector(small, ROWS, 512))
        self.assertIsNone(selected_row(small, ROWS, 512))

    def test_looks_like_selector(self):
        self.assertTrue(looks_like_selector(selector_png(1), ROWS, 512))
        self.assertFalse(looks_like_selector(selector_png(-1), ROWS, 512))
        self.assertFalse(looks_like_selector(png_of(Image.new("RGB", (1024, 800), (240, 240, 240))), ROWS, 512))

    def test_refine_button_center_ignores_a_rough_guess(self):
        img = Image.new("RGB", (1024, 800), (255, 255, 255))
        ImageDraw.Draw(img).rectangle([492, 551, 531, 563], fill=(62, 150, 210))   # text-like block
        png = png_of(img)
        for guess in [(512, 557), (520, 569), (500, 543)]:
            x, y = refine_button_center(png, guess)
            self.assertLessEqual(abs(x - 511.5), 1)
            self.assertLessEqual(abs(y - 557), 1)

    def test_refine_fails_when_no_blue_text(self):
        png = png_of(Image.new("RGB", (1024, 800), (255, 255, 255)))
        with self.assertRaises(PixelError):
            refine_button_center(png, (512, 557))


class BlueAtTests(unittest.TestCase):
    def test_the_underline_of_the_selected_sub_tab(self):
        img = Image.new("RGB", (1024, 800), (243, 243, 243))
        ImageDraw.Draw(img).rectangle([341, 125, 683, 127], fill=(90, 164, 215))
        png = png_of(img)
        self.assertTrue(blue_at(png, (512, 126)))
        self.assertFalse(blue_at(png, (860, 126)))
        self.assertFalse(blue_at(png, (5000, 5000)), "a point outside the picture is not blue")


class BandChangedTests(unittest.TestCase):
    def shot(self, **marks):
        img = Image.new("RGB", (1024, 800), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for box, fill in marks.values():
            draw.rectangle(box, fill=fill)
        return png_of(img)

    def test_the_same_screen_has_not_changed(self):
        a = self.shot(t=([100, 500, 900, 560], (0, 0, 0)))
        self.assertFalse(band_changed(a, a, 480, 580))

    def test_a_card_that_moved_in_the_band_is_a_change(self):
        a = self.shot(t=([100, 500, 900, 560], (0, 0, 0)))
        b = self.shot(t=([100, 540, 900, 600], (0, 0, 0)))
        self.assertTrue(band_changed(a, b, 480, 620))

    def test_a_change_outside_the_band_is_ignored(self):
        a = self.shot(t=([100, 500, 900, 560], (0, 0, 0)))
        b = self.shot(t=([100, 500, 900, 560], (0, 0, 0)), u=([0, 0, 900, 40], (200, 0, 0)))
        self.assertFalse(band_changed(a, b, 480, 580))

    def test_small_noise_is_not_a_change(self):
        a = self.shot(t=([100, 500, 900, 560], (0, 0, 0)))
        b = self.shot(t=([100, 500, 900, 560], (3, 3, 3)))
        self.assertFalse(band_changed(a, b, 480, 580))

    def test_a_different_size_counts_as_changed(self):
        a = self.shot()
        b = png_of(Image.new("RGB", (800, 600), (255, 255, 255)))
        self.assertTrue(band_changed(a, b, 480, 580))


class OcrTests(unittest.TestCase):
    def test_parses_lines(self):
        out = "Calendar\t88\t585\nbad line\nAlert\t75\t687\nx\tnotanumber\t1\n"
        runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr="")
        hits = run_ocr("ocr", "x.png", runner)
        self.assertEqual(hits, [TextHit("Calendar", 88, 585), TextHit("Alert", 75, 687)])

    def test_failure_raises(self):
        runner = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with self.assertRaises(OcrError):
            run_ocr("ocr", "x.png", runner)

    def test_find_text(self):
        hits = [TextHit("Alex", 990, 631), TextHit("Kids", 985, 696), TextHit("1 hour be", 993, 561), TextHit("2 hours t", 993, 586)]
        self.assertEqual(find_text(hits, r"^Alex").y, 631)
        self.assertEqual(find_text(hits, r"^1 hour").y, 561)
        self.assertIsNone(find_text(hits, r"^Home"))
        self.assertEqual(find_text(hits, r"hour", min_y=570).y, 586)


if __name__ == "__main__":
    unittest.main()
