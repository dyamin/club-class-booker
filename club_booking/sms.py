"""The login code of the club app comes by SMS. With Text Message Forwarding on, the SMS also reaches the Mac, and the
Messages database (`~/Library/Messages/chat.db`) holds it. This module reads ONLY the code of ONE sender.

Privacy rules (the user allowed the automatic login on 2026-10-15, and nothing more than that):
- the query names the sender: the text of every other message is never loaded;
- only messages that arrived after a snapshot of the newest ROWID (taken right before the code was requested) and after
  the time of the request count, and a message with two different 6-digit numbers is refused;
- the database is opened read-only, in place (never `immutable=1`: the newest messages sit in the write-ahead log);
- nothing is printed or logged here, and the code is never kept: the caller records the ROWID that it used, not the text.

The newer messages keep their text in the `attributedBody` blob (a "typedstream"). Its shape here is an educated reading
and is NOT verified against a real message until the first live run; an unknown shape falls back to the plain bytes.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
CLOCK_SKEW = timedelta(seconds=30)          # the phone and the Mac may disagree a little about the time of an SMS
_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_SENDER_SQL = "lower(replace(replace(replace(h.id, '.', ''), ' ', ''), '-', '')) = ?"


class SmsError(Exception):
    pass


def python_app_path() -> str:
    """The Python.app that Full Disk Access must be given to. A launchd job counts as this program, not as Terminal.
    It changes when Python is upgraded with brew: then the permission must be given again."""
    bundle = Path(sys.base_prefix) / "Resources" / "Python.app"
    return str(bundle) if bundle.exists() else os.path.realpath(sys.executable)


def normal_name(name: str) -> str:
    """A sender name without case, dots, spaces and dashes: 'Hashmonaim.' and 'hashmonaim' are the same."""
    return re.sub(r"[\s.\-]", "", name).lower()


def _when(value: int) -> datetime:
    """The date column counts from 2001-01-01: nanoseconds on a recent macOS, seconds on an old one."""
    seconds = value / 1e9 if value > 1e11 else float(value)
    return APPLE_EPOCH + timedelta(seconds=seconds)


def _blob_text(blob: bytes) -> str:
    marker = blob.find(b"NSString")
    if marker >= 0:
        plus = blob.find(b"\x01+", marker)
        if 0 <= plus - marker < 16 and plus + 2 < len(blob):
            at = plus + 2
            if blob[at] == 0x81 and at + 3 <= len(blob):
                length, start = int.from_bytes(blob[at + 1:at + 3], "little"), at + 3
            else:
                length, start = blob[at], at + 1
            raw = blob[start:start + length]
            if len(raw) == length:
                return raw.decode("utf-8", "replace")
    return blob.decode("utf-8", "ignore")          # an unknown shape: a code is still a clear run of six digits


def _text(text: Optional[str], blob: Optional[bytes]) -> str:
    if text:
        return text
    return _blob_text(bytes(blob)) if blob else ""


def _the_code(text: str) -> Optional[str]:
    """The one 6-digit number of a message. Two different numbers: no code (a person must look)."""
    found = set(_CODE.findall(text))
    return found.pop() if len(found) == 1 else None


class MessagesInbox:
    def __init__(self, db_path: Path, sender: str, connect: Callable = sqlite3.connect) -> None:
        self.path, self.sender, self._connect = Path(db_path), normal_name(sender), connect

    def _open(self):
        uri = "file:" + quote(str(self.path)) + "?mode=ro"
        try:
            return self._connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            raise SmsError(self._help(exc))

    def _help(self, exc: Exception) -> str:
        return (f"cannot read the Messages database ({exc}). Give Full Disk Access to the Python.app that runs the "
                "program (and to Terminal, for a test that you start there), and turn on Text Message Forwarding on the iPhone")

    def _query(self, sql: str, args: tuple) -> list:
        db = self._open()
        try:
            return db.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise SmsError(self._help(exc))
        finally:
            db.close()

    def newest_rowid(self) -> int:
        """The snapshot: the newest message of ANY sender, as a number only (nothing of it is read)."""
        rows = self._query("SELECT COALESCE(MAX(ROWID), 0) FROM message", ())
        return int(rows[0][0])

    def probe(self, now: datetime) -> Tuple[int, Optional[float]]:
        """For the doctor: how many messages this sender has sent, and the age in seconds of the newest. No text."""
        rows = self._query("SELECT COUNT(*), MAX(m.date) FROM message m JOIN handle h ON m.handle_id = h.ROWID "
                           f"WHERE m.is_from_me = 0 AND {_SENDER_SQL}", (self.sender,))
        count, newest = int(rows[0][0]), rows[0][1]
        return count, (None if newest is None else (now - _when(int(newest))).total_seconds())

    def fresh_codes(self, after_rowid: int, since: datetime) -> List[Tuple[int, str]]:
        """(rowid, code) of the messages of this sender that are newer than the snapshot AND than `since`, newest first."""
        rows = self._query("SELECT m.ROWID, m.date, m.text, m.attributedBody FROM message m "
                           "JOIN handle h ON m.handle_id = h.ROWID "
                           f"WHERE m.is_from_me = 0 AND m.ROWID > ? AND {_SENDER_SQL} ORDER BY m.ROWID DESC LIMIT 5",
                           (after_rowid, self.sender))
        out: List[Tuple[int, str]] = []
        for rowid, date, text, blob in rows:
            if date is None or _when(int(date)) < since - CLOCK_SKEW:
                continue
            code = _the_code(_text(text, blob))
            if code:
                out.append((int(rowid), code))
        return out
