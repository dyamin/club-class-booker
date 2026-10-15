"""Guided setup for a new user: a few questions, then config.json, the phone number and the API key in the Keychain.

All input and output goes through functions that the caller gives, so the questions are tested without a keyboard and
nothing reaches the Keychain in a test. The secrets never go into config.json.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_FIRST_ROW, ROW_STEP = 176, 38        # window points of the person selector's rows (measured on one real app)
PHONE_SERVICE, KEY_SERVICE = "club-booking-phone", "club-booking-anthropic"


def slug(name: str, taken: Set[str]) -> str:
    """A key for a person: lowercase letters and digits of the name, unique among `taken`."""
    base = re.sub(r"[^a-z0-9]", "", name.lower()) or f"person{len(taken) + 1}"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}{n}", n + 1
    return key


class Wizard:
    def __init__(self, ask: Callable[[str], str], secret: Callable[[str], str], out: Callable[[str], None],
                 calendars: Callable[[], List[str]], store_secret: Callable[[str, str], None],
                 find_rows: Callable[[], List[Tuple[str, int]]], config_path: Path) -> None:
        self.ask, self.secret, self.out = ask, secret, out
        self.calendars, self.store_secret, self.find_rows, self.config_path = calendars, store_secret, find_rows, Path(config_path)

    # -- small questions ---------------------------------------------------------------------
    def _yes(self, question: str, default: bool) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")

    def _calendar(self, name: str, names: List[str]) -> str:
        while True:
            answer = self.ask(f"Calendar for {name} (number or name) [1]: ").strip()
            if not answer:
                return names[0]
            if answer.isdigit() and 1 <= int(answer) <= len(names):
                return names[int(answer) - 1]
            if answer in names:
                return answer
            self.out(f"{answer!r} is not one of the calendars above.")

    def _people(self) -> List[Dict[str, object]]:
        names = self.calendars() or ["Calendar"]
        self.out("Your calendars:")
        for i, c in enumerate(names, 1):
            self.out(f"  {i}. {c}")
        people: List[Dict[str, object]] = []
        while True:
            name = self.ask(f"First name of person {len(people) + 1} (empty to finish): ").strip()
            if not name:
                return people
            calendar = self._calendar(name, names)
            shared = any(p["calendar"] == calendar for p in people)
            default = f"{name} - " if shared else ""
            prefix = self.ask(f"Start of the event titles for {name} [{default!r}]: ")
            people.append({"key": slug(name, {str(p["key"]) for p in people}), "first_name": name, "selector_row_y": 0,
                           "calendar": calendar, "title_prefix": prefix if prefix.strip() else default,
                           "asks_overlaps": self._yes(f"Ask you which class to keep when two classes of {name} overlap", True)})

    def _rows(self, people: List[Dict[str, object]]) -> None:
        rows = self.find_rows()
        if not rows:
            for i, p in enumerate(people):
                p["selector_row_y"] = DEFAULT_FIRST_ROW + ROW_STEP * i
            return
        self.out("Profiles found in the club app:")
        for i, (label, _) in enumerate(rows, 1):
            self.out(f"  {i}. {label}")
        for i, p in enumerate(people):
            default = min(i + 1, len(rows))
            answer = self.ask(f"Which profile is {p['first_name']}? [{default}]: ").strip()
            number = int(answer) if answer.isdigit() and 1 <= int(answer) <= len(rows) else default
            p["selector_row_y"] = rows[number - 1][1]

    def _store(self, service: str, question: str, digits: bool = False) -> None:
        while True:
            value = self.secret(question).strip()
            if digits:
                value = re.sub(r"[\s\-]", "", value)
            if not value:
                return
            if not digits or value.isdigit():
                self.store_secret(service, value)
                return
            self.out("The phone number must be digits only.")

    # -- the whole setup ---------------------------------------------------------------------
    def run(self) -> int:
        self.out("Club booking setup. Your answers go to config.json; the phone number and the key go to the Keychain.")
        people = self._people()
        if not people:
            self.out("No person was given: nothing was written.")
            return 1
        self._rows(people)
        self._store(PHONE_SERVICE, "Your phone number for the club app login (digits only, empty to skip): ", digits=True)
        self._store(KEY_SERVICE, "Anthropic API key (empty to skip, for example when it is in the Keychain already): ")
        auto = self._yes("Log the club app in by itself (needs Text Message Forwarding and Full Disk Access: see the guide)",
                         False)
        questions = self._yes("Ask you on the phone when a decision is needed (for example two overlapping classes)", True)
        config = {"people": people, "calendar_audit": "write", "questions": "on" if questions else "off",
                  "questions_several": False, "auto_login": auto}
        target = self.config_path
        if target.exists() and not self._yes(f"{target.name} exists. Overwrite it", False):
            target = target.with_name("config.new.json")
            self.out(f"The old file stays. The new one is {target.name}: compare them, then rename it.")
        target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.out(f"Written: {target}")
        self.out("Next: 1) python -m club_booking doctor (every line must say OK)  2) python -m club_booking login request "
                 "(first login)  3) python -m club_booking install (the program then runs by itself).")
        return 0
