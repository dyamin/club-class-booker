"""Checks that the Claude API call works: the key, the model, the request shape, and a JSON answer.

It draws a small English class card and asks the reader to read it. This costs a fraction of a cent.
Any answer without an error counts as a pass. How well it reads the real Hebrew screens is checked
later, on the real app.
"""
import io
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from club_booking.config import load_config  # noqa: E402
from club_booking.secrets import get_api_key  # noqa: E402
from club_booking.vision import Vision, make_client  # noqa: E402


def card_png() -> bytes:
    img = Image.new("RGB", (1024, 800), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    big, small = ImageFont.load_default(size=34), ImageFont.load_default(size=26)
    draw.rounded_rectangle([8, 250, 1016, 400], radius=12, fill=(255, 255, 255))
    draw.text((20, 262), "Tue 13/10 | 08:50 - 09:35", fill=(110, 110, 110), font=small)
    draw.text((20, 300), "METCON", fill=(20, 20, 20), font=big)
    draw.text((20, 350), "Register", fill=(62, 150, 210), font=small)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    cfg = load_config(REPO / "config.json", REPO)
    vision = Vision(make_client(get_api_key(cfg.keychain_service)), cfg.model, ZoneInfo(cfg.timezone))
    listing = vision.read_list(card_png())
    print(f"model {cfg.model}: the call worked; screen kind {listing.screen!r}, {len(listing.cards)} card(s)")
    for card in listing.cards:
        print("  ", card)
    for warning in vision.warnings:
        print("  warning:", warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
