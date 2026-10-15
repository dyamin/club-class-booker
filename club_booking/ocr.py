"""Finds English text and its position on a screenshot, with the small Swift helper tools/bin/ocr."""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


class OcrError(Exception):
    pass


@dataclass(frozen=True)
class TextHit:
    text: str
    x: int
    y: int


def run_ocr(binary: Path, png_path: Path, runner: Callable = subprocess.run) -> List[TextHit]:
    try:
        done = runner([str(binary), str(png_path)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OcrError(f"could not run the OCR helper: {exc}")
    if done.returncode != 0:
        raise OcrError((done.stderr or "").strip() or "the OCR helper failed")
    hits: List[TextHit] = []
    for line in done.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            hits.append(TextHit(parts[0], int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return hits


def find_text(hits: List[TextHit], pattern: str, min_y: Optional[int] = None,
              max_y: Optional[int] = None, min_x: Optional[int] = None) -> Optional[TextHit]:
    """The top-most hit whose text matches the regular expression (case does not matter)."""
    rx = re.compile(pattern, re.IGNORECASE)
    found = [h for h in hits
             if (min_y is None or h.y >= min_y) and (max_y is None or h.y <= max_y)
             and (min_x is None or h.x >= min_x) and rx.search(h.text)]
    return min(found, key=lambda h: h.y) if found else None


def ocr_png(binary: Path, png: bytes, runner: Callable = subprocess.run) -> List[TextHit]:
    """OCR for PNG bytes. The helper needs a file, so the bytes go to a temporary one."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "screen.png"
        path.write_bytes(png)
        return run_ocr(binary, path, runner)
