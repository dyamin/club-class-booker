"""The login code comes by SMS. With Text Message Forwarding on, the Mac's Messages database holds it. These tests use
a made-up database (the real one is never read in a test). The blob that holds the text of newer messages is built by
hand and its real shape is NOT verified until the first live run."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from club_booking import sms
from club_booking.sms import MessagesInbox, SmsError

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def typed_stream(text: str) -> bytes:
    """A hand-made imitation of the attributedBody blob (NSKeyedArchiver "typedstream") around a text."""
    body = text.encode("utf-8")
    length = bytes([len(body)]) if len(body) < 0x80 else b"\x81" + len(body).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00"
            b"\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + body +
            b"\x86\x84\x02iI\x01" + bytes([len(body)]) + b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92"
            b"\x84\x96\x96\x1d__kIMMessagePartAttributeName\x86\x86")


def apple_ns(moment: datetime) -> int:
    return int((moment - APPLE_EPOCH).total_seconds() * 1e9)


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "chat.db"
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER, text TEXT, attributedBody BLOB,
                                  date INTEGER, is_from_me INTEGER DEFAULT 0);
            INSERT INTO handle (ROWID, id) VALUES (1, 'Hashmonaim.'), (2, '+972501234567'), (3, 'Bank');
        """)
        db.commit()
        db.close()
        self.inbox = MessagesInbox(self.path, "Hashmonaim")

    def add(self, handle, text=None, blob=None, when=NOW, mine=0, raw_date=None):
        db = sqlite3.connect(self.path)
        cur = db.execute("INSERT INTO message (handle_id, text, attributedBody, date, is_from_me) VALUES (?,?,?,?,?)",
                         (handle, text, blob, apple_ns(when) if raw_date is None else raw_date, mine))
        db.commit()
        rowid = cur.lastrowid
        db.close()
        return rowid

    def codes(self, after=0, since=NOW - timedelta(minutes=1)):
        return self.inbox.fresh_codes(after, since)


class CodeTests(InboxCase):
    def test_the_code_in_the_text_of_the_right_sender_is_found(self):
        rowid = self.add(1, "Your code is 123456")
        self.assertEqual(self.codes(), [(rowid, "123456")])

    def test_the_code_in_the_attributed_body_is_found_when_the_text_is_empty(self):
        rowid = self.add(1, None, typed_stream("Hashmonaim: 654321 is your code"))
        self.assertEqual(self.codes(), [(rowid, "654321")])

    def test_a_long_text_in_the_blob_is_read_with_its_two_byte_length(self):
        text = "Hello " * 30 + "code 246810"
        rowid = self.add(1, None, typed_stream(text))
        self.assertEqual(self.codes(), [(rowid, "246810")])

    def test_a_blob_in_an_unknown_shape_still_gives_a_clear_code(self):
        rowid = self.add(1, None, b"garbage\x00\x01 code 112233 garbage")
        self.assertEqual(self.codes(), [(rowid, "112233")])

    def test_the_sender_name_is_matched_without_dots_spaces_and_case(self):
        self.assertEqual(MessagesInbox(self.path, "HASHMONAIM.").sender, "hashmonaim")
        rowid = self.add(1, "code 111222")
        self.assertEqual(MessagesInbox(self.path, "hashmonaim ").fresh_codes(0, NOW - timedelta(minutes=1)),
                         [(rowid, "111222")])

    def test_another_sender_is_never_read(self):
        self.add(2, "code 999999")
        self.add(3, "code 888888")
        self.assertEqual(self.codes(), [])

    def test_a_message_I_sent_is_not_a_code(self):
        self.add(1, "code 123456", mine=1)
        self.assertEqual(self.codes(), [])

    def test_a_message_before_the_request_is_not_used_even_with_a_higher_rowid(self):
        # a late delivery of an old SMS: the date says it is old
        self.add(1, "code 123456", when=NOW - timedelta(minutes=20))
        self.assertEqual(self.codes(), [])

    def test_a_message_at_or_below_the_snapshot_is_not_used(self):
        old = self.add(1, "code 123456")
        self.assertEqual(self.codes(after=old), [])
        new = self.add(1, "code 654321")
        self.assertEqual(self.codes(after=old), [(new, "654321")])

    def test_the_newest_message_comes_first(self):
        a = self.add(1, "code 111111")
        b = self.add(1, "code 222222")
        self.assertEqual(self.codes(), [(b, "222222"), (a, "111111")])

    def test_two_different_numbers_in_one_message_are_refused_and_the_same_number_twice_is_one_code(self):
        self.add(1, "code 123456 or 654321")
        rowid = self.add(1, "123456 is your code. Again: 123456")
        self.assertEqual(self.codes(), [(rowid, "123456")])

    def test_only_a_six_digit_run_counts(self):
        self.add(1, "order 12345 and 1234567 and a1234567b")
        self.assertEqual(self.codes(), [])

    def test_the_date_may_be_in_seconds_or_in_nanoseconds(self):
        seconds = int((NOW - APPLE_EPOCH).total_seconds())
        a = self.add(1, "code 111111", raw_date=seconds)
        b = self.add(1, "code 222222")
        self.assertEqual(sorted(r for r, _ in self.codes()), [a, b])

    def test_the_candidates_are_few(self):
        for i in range(30):
            self.add(1, f"code {100000 + i}")
        self.assertLessEqual(len(self.codes()), 5)


class SnapshotAndProbeTests(InboxCase):
    def test_the_snapshot_is_the_newest_rowid_and_zero_for_an_empty_database(self):
        self.assertEqual(self.inbox.newest_rowid(), 0)
        self.add(2, "private text")
        last = self.add(3, "more private text")
        self.assertEqual(self.inbox.newest_rowid(), last)

    def test_the_probe_counts_the_senders_messages_and_gives_the_age_but_never_the_text(self):
        self.add(1, "code 123456", when=NOW - timedelta(hours=3))
        self.add(2, "private")
        count, age = self.inbox.probe(NOW)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(age, 3 * 3600, delta=2)
        self.assertFalse(any(isinstance(v, str) for v in (count, age)))

    def test_the_probe_of_a_sender_without_messages_has_no_age(self):
        self.assertEqual(self.inbox.probe(NOW), (0, None))


class PythonPathTests(unittest.TestCase):
    def test_the_python_app_that_needs_full_disk_access_is_named(self):
        from unittest import mock
        base = Path(tempfile.mkdtemp())
        (base / "Resources" / "Python.app").mkdir(parents=True)
        with mock.patch("club_booking.sms.sys.base_prefix", str(base)):
            self.assertEqual(sms.python_app_path(), str(base / "Resources" / "Python.app"))

    def test_without_the_app_bundle_the_real_executable_is_named(self):
        from unittest import mock
        with mock.patch("club_booking.sms.sys.base_prefix", str(Path(tempfile.mkdtemp()))):
            self.assertTrue(sms.python_app_path().endswith(Path(sms.os.path.realpath(sms.sys.executable)).name))


class AccessTests(unittest.TestCase):
    def test_a_missing_database_is_a_clear_error_with_the_way_to_fix_it(self):
        with self.assertRaises(SmsError) as ctx:
            MessagesInbox(Path(tempfile.mkdtemp()) / "nope.db", "Hashmonaim").newest_rowid()
        self.assertIn("Full Disk Access", str(ctx.exception))

    def test_a_database_that_may_not_be_read_is_the_same_error(self):
        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")
        with self.assertRaises(SmsError) as ctx:
            MessagesInbox(Path("/x/chat.db"), "Hashmonaim", connect=refuse).newest_rowid()
        self.assertIn("Full Disk Access", str(ctx.exception))

    def test_the_database_is_opened_read_only_and_never_with_immutable(self):
        # immutable=1 ignores the write-ahead log, where the newest messages are
        seen = []

        def spy(target, **kwargs):
            seen.append((target, kwargs))
            return sqlite3.connect(":memory:")
        try:
            MessagesInbox(Path("/x y/chat.db"), "Hashmonaim", connect=spy).newest_rowid()
        except SmsError:
            pass
        uri, kwargs = seen[0]
        self.assertIn("mode=ro", uri)
        self.assertNotIn("immutable", uri)
        self.assertTrue(kwargs.get("uri"))
        self.assertIn("%20", uri, "a space in the path is quoted")

    def test_the_module_does_not_log_or_print_anything(self):
        source = Path(sms.__file__).read_text(encoding="utf-8")
        self.assertNotIn("print(", source)
        self.assertNotIn("logging", source)


if __name__ == "__main__":
    unittest.main()
