"""Looks at pixels: which person is selected, and where exactly a button is."""
from __future__ import annotations

import io
from typing import Optional, Sequence, Tuple

from PIL import Image, ImageChops


class PixelError(Exception):
    pass


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def is_selected_blue(rgb: Tuple[int, int, int]) -> bool:
    """The filled blue of the selected person row, about (76, 159, 213)."""
    r, g, b = rgb
    return b >= 190 and r <= 110 and 130 <= g <= 190


def is_link_blue(rgb: Tuple[int, int, int]) -> bool:
    """The blue of button text such as הרשמה, also its anti-aliased edge pixels."""
    r, g, b = rgb
    return b >= 170 and (b - r) >= 70 and g >= 100


_ROW_SAMPLES = (-450, -380, -300, 300, 380, 450)     # near the two ends of the row: the name in the middle (white or blue) is not read


def _row_kind(img: Image.Image, y: int, x: int) -> str:
    """blue, white or other, by the majority of a few pixels of the row, not by one."""
    width, height = img.size
    if not 0 <= y < height:
        return "other"
    colors = [img.getpixel((min(max(x + dx, 0), width - 1), y)) for dx in _ROW_SAMPLES]
    need = len(colors) // 2 + 1
    if sum(1 for c in colors if is_selected_blue(c)) >= need:
        return "blue"
    if sum(1 for c in colors if min(c) >= 240) >= need:
        return "white"
    return "other"


def selected_row(png: bytes, rows_y: Sequence[int], x: int) -> Optional[int]:
    """Index of the one blue row, or None if there is not exactly one."""
    img = _open(png)
    blue = [i for i, y in enumerate(rows_y) if _row_kind(img, y, x) == "blue"]
    return blue[0] if len(blue) == 1 else None


def looks_like_selector(png: bytes, rows_y: Sequence[int], x: int) -> bool:
    """True when exactly one row is blue and every other row is near-white."""
    img = _open(png)
    kinds = [_row_kind(img, y, x) for y in rows_y]
    return kinds.count("blue") == 1 and kinds.count("white") == len(rows_y) - 1


def refine_button_center(png: bytes, approx: Tuple[int, int], half_w: int = 90,
                         half_h: int = 28, min_pixels: int = 40) -> Tuple[int, int]:
    """Exact center of the blue button text near a rough position given by the model."""
    img = _open(png)
    width, height = img.size
    ax, ay = approx
    total_x = total_y = count = 0
    for y in range(max(0, ay - half_h), min(height, ay + half_h + 1)):
        for x in range(max(0, ax - half_w), min(width, ax + half_w + 1)):
            if is_link_blue(img.getpixel((x, y))):
                total_x += x
                total_y += y
                count += 1
    if count < min_pixels:
        raise PixelError(f"no blue button text near {approx} (found {count} blue pixels)")
    return round(total_x / count), round(total_y / count)


def band_changed(first_png: bytes, second_png: bytes, y_from: int, y_to: int,
                 min_fraction: float = 0.005, min_step: int = 32) -> bool:
    """True when the horizontal band between two screenshots differs. Used to see that a list did not move
    between the screenshot the reader looked at and the screenshot taken just before a tap."""
    first, second = _open(first_png), _open(second_png)
    if first.size != second.size:
        return True
    width, height = first.size
    box = (0, max(0, y_from), width, min(height, y_to))
    diff = ImageChops.difference(first.crop(box), second.crop(box)).convert("L")
    changed = diff.point(lambda v: 255 if v > min_step else 0).histogram()[255]
    return changed > min_fraction * diff.size[0] * diff.size[1]


def blue_at(png: bytes, point: Tuple[int, int]) -> bool:
    """True when the pixel is the blue of a selected tab line. A point outside the picture is not blue."""
    img = _open(png)
    x, y = point
    if not (0 <= x < img.size[0] and 0 <= y < img.size[1]):
        return False
    return is_selected_blue(img.getpixel((x, y)))
