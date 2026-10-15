import base64
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from club_booking.models import CardState, opening_time
from club_booking.vision import ListRead, Vision, VisionError, infer_datetime, make_client

TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)


class FakeClient:
    """Duck-types anthropic.Anthropic().messages.create."""

    def __init__(self, text=None, stop_reason="end_turn", raises=None):
        self.calls = []
        self._text, self._stop, self._raises = text, stop_reason, raises
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        content = [SimpleNamespace(type="text", text=self._text)] if self._text is not None else []
        return SimpleNamespace(content=content, stop_reason=self._stop,
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=400))


def card(**over):
    base = {"title": "METCON", "date": "13/10", "start": "08:50", "end": "09:35",
            "state": "open", "opens": "", "button_x": 512, "button_y": 557}
    base.update(over)
    return base


def listing(cards, screen="favorites"):
    return json.dumps({"screen": screen, "cards": cards})


class MakeClientTests(unittest.TestCase):
    def test_the_key_goes_only_to_the_official_address(self):
        with mock.patch("anthropic.Anthropic") as ctor:
            make_client("sk-test")
        self.assertEqual(ctor.call_args.kwargs["base_url"], "https://api.anthropic.com",
                         "an inherited ANTHROPIC_BASE_URL must not receive the key")


class InferDatetimeTests(unittest.TestCase):
    def test_same_year(self):
        self.assertEqual(infer_datetime(13, 10, 8, 50, NOW, TZ), datetime(2026, 10, 13, 8, 50, tzinfo=TZ))

    def test_rolls_into_next_year(self):
        now = datetime(2026, 12, 30, 10, 0, tzinfo=TZ)
        self.assertEqual(infer_datetime(2, 1, 7, 0, now, TZ).year, 2027)

    def test_invalid_date(self):
        with self.assertRaises(ValueError):
            infer_datetime(31, 2, 1, 0, NOW, TZ)


class ReadClassListTests(unittest.TestCase):
    def make(self, client):
        return Vision(client, "claude-opus-5-5", TZ, now_fn=lambda: NOW)

    def test_parses_cards(self):
        payload = {"cards": [card(), card(title="Yoga", state="not_open", date="14/10", start="06:45",
                                          end="07:25", button_x=512, button_y=541),
                             card(title="Done", state="registered", button_x=0, button_y=0)]}
        client = FakeClient(listing(payload["cards"]))
        cards = self.make(client).read_list(b"\x89PNG-bytes").cards
        self.assertEqual([c.name for c in cards], ["METCON", "Yoga", "Done"])
        self.assertEqual(cards[0].state, CardState.OPEN)
        self.assertEqual(cards[0].button, (512, 557))
        self.assertEqual(cards[1].state, CardState.NOT_OPEN)
        self.assertEqual(cards[1].start, datetime(2026, 10, 14, 6, 45, tzinfo=TZ))
        self.assertEqual(cards[1].opens_at, datetime(2026, 10, 13, 6, 45, tzinfo=TZ), "computed when the card shows none")
        self.assertIsNone(cards[2].button)

    def test_request_shape(self):
        client = FakeClient(listing([]))
        self.make(client).read_list(b"abc")
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        image = call["messages"][0]["content"][0]
        self.assertEqual(image["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(image["source"]["data"]), b"abc")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", call)

    def test_a_bad_card_is_skipped_and_noted(self):
        payload = {"cards": [card(date="31/02"), card(title="Good")]}
        vision = self.make(FakeClient(listing(payload["cards"])))
        cards = vision.read_list(b"x").cards
        self.assertEqual([c.name for c in cards], ["Good"])
        self.assertEqual(len(vision.warnings), 1)

    def test_the_opening_time_shown_on_the_card_wins(self):
        # Clock-change week: 24 clock hours before 12:00 on 25/10 is 12:00 on 24/10, but 24 absolute hours is 13:00.
        payload = {"cards": [card(title="Late", state="not_open", date="25/10", start="12:00", end="12:45",
                                  opens="24/10 12:00", button_x=512, button_y=541)]}
        got = self.make(FakeClient(listing(payload["cards"]))).read_list(b"x").cards[0]
        self.assertEqual(got.opens_at, datetime(2026, 10, 24, 12, 0, tzinfo=TZ))
        self.assertNotEqual(got.opens_at, opening_time(got.start), "the card's time, not the computed 13:00")

    def test_the_opening_time_may_be_written_time_first(self):
        # the real app writes: פתיחת הרשמה: 07:45 15/10
        payload = {"cards": [card(title="METCON", state="not_open", date="16/10", start="07:45", end="08:30",
                                  opens="07:45 15/10", button_x=512, button_y=700)]}
        got = self.make(FakeClient(listing(payload["cards"]))).read_list(b"x").cards[0]
        self.assertEqual(got.opens_at, datetime(2026, 10, 15, 7, 45, tzinfo=TZ))

    def test_an_unreadable_opening_time_falls_back_to_the_computed_one(self):
        payload = {"cards": [card(state="not_open", opens="soon")]}
        vision = self.make(FakeClient(listing(payload["cards"])))
        got = vision.read_list(b"x").cards[0]
        self.assertEqual(got.opens_at, datetime(2026, 10, 12, 8, 50, tzinfo=TZ))
        self.assertEqual(len(vision.warnings), 1)

    def test_end_after_midnight(self):
        payload = {"cards": [card(start="23:30", end="00:15")]}
        cards = self.make(FakeClient(listing(payload["cards"]))).read_list(b"x").cards
        self.assertGreater(cards[0].end, cards[0].start)

    def test_token_use_is_counted_for_the_cost_check(self):
        vision = self.make(FakeClient(listing([])))
        vision.read_list(b"x")
        vision.read_list(b"y")
        self.assertEqual(vision.usage, {"calls": 2, "input_tokens": 3000, "output_tokens": 800})

    def test_the_kind_of_screen_is_read_with_the_cards(self):
        got = self.make(FakeClient(listing([card()]))).read_list(b"x")
        self.assertIsInstance(got, ListRead)
        self.assertEqual(got.screen, "favorites")
        self.assertEqual(len(got.cards), 1)

    def test_a_screen_that_is_not_the_favorites_list_is_named(self):
        for kind in ("schedule", "selector", "login", "other"):
            got = self.make(FakeClient(listing([card()], screen=kind))).read_list(b"x")
            self.assertEqual(got.screen, kind)

    def test_a_missing_screen_kind_is_treated_as_other(self):
        text = json.dumps({"cards": [card()]})
        self.assertEqual(self.make(FakeClient(text)).read_list(b"x").screen, "other")

    def test_the_schema_asks_for_the_screen_kind(self):
        client = FakeClient(listing([]))
        self.make(client).read_list(b"x")
        schema = client.calls[0]["output_config"]["format"]["schema"]
        self.assertIn("screen", schema["required"])
        self.assertEqual(set(schema["properties"]["screen"]["enum"]), {"favorites", "schedule", "selector", "login", "other"})

    def test_a_bookable_card_without_a_button_position_is_noted(self):
        payload = [card(title="NoButton", state="open", button_x=0, button_y=0),
                   card(title="Fine", state="registered", button_x=0, button_y=0)]
        vision = self.make(FakeClient(listing(payload)))
        got = vision.read_list(b"x")
        self.assertEqual(len(got.cards), 2)
        self.assertEqual(len(vision.warnings), 1)
        self.assertIn("NoButton", vision.warnings[0])

    def test_only_an_open_card_needs_a_button_position(self):
        payload = [card(title="Later", state="not_open", button_x=0, button_y=0),
                   card(title="Crowded", state="full", button_x=0, button_y=0)]
        vision = self.make(FakeClient(listing(payload)))
        vision.read_list(b"x")
        self.assertEqual(vision.warnings, [], "the program taps only open cards; a quiet replan must stay quiet")

    def test_refusal_invalid_json_and_api_errors_fail_closed(self):
        with self.assertRaises(VisionError):
            self.make(FakeClient("{}", stop_reason="refusal")).read_list(b"x")
        with self.assertRaises(VisionError):
            self.make(FakeClient("not json")).read_list(b"x")
        with self.assertRaises(VisionError):
            self.make(FakeClient(None)).read_list(b"x")
        with self.assertRaises(VisionError):
            self.make(FakeClient(raises=RuntimeError("503"))).read_list(b"x")


class ReadDialogTests(unittest.TestCase):
    def test_a_refusal_comes_with_the_text_of_the_popup(self):
        payload = {"kind": "refused", "title": "ג'ודו", "date": "15/10", "start": "17:30", "message": "הגיל אינו מתאים"}
        dialog = Vision(FakeClient(json.dumps(payload)), "m", TZ).read_dialog(b"x")
        self.assertEqual((dialog.kind, dialog.message), ("refused", "הגיל אינו מתאים"))

    def test_the_dialog_schema_has_the_refused_kind_and_a_message(self):
        client = FakeClient(json.dumps({"kind": "list", "title": "", "date": "", "start": "", "message": ""}))
        Vision(client, "m", TZ).read_dialog(b"x")
        schema = client.calls[0]["output_config"]["format"]["schema"]
        self.assertIn("refused", schema["properties"]["kind"]["enum"])
        self.assertIn("message", schema["required"])

    def test_booked_dialog(self):
        payload = {"kind": "booked", "title": "METCON", "date": "13/10", "start": "08:50"}
        dialog = Vision(FakeClient(json.dumps(payload)), "m", TZ).read_dialog(b"x")
        self.assertEqual((dialog.kind, dialog.title, dialog.date, dialog.start),
                         ("booked", "METCON", "13/10", "08:50"))


if __name__ == "__main__":
    unittest.main()
