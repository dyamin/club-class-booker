"""The program logs the club app in by itself (the user decided on 2026-10-15: "it should be independent").

This replaces the old rule "an unattended run never logs in". The rule existed because a code request is a counted
action (3 in 10 minutes lock the login for 10 minutes) and because the code goes to a phone. Now the code is read from
the Mac's Messages database (sms.py), and the risk is kept small by these rules:
- it acts only on the login screen, and only when a class is waiting (the caller decides that);
- it never asks for a code that it cannot read: the snapshot of the Messages database comes first, and a database that
  cannot be read means no request;
- its own budget: one request in 45 minutes and three in 24 hours, written down BEFORE the request, on top of the
  login guard of the app (2 requests in 10 minutes), which counts manual requests too;
- one request per attempt, one code typed per request, never a second try: when no SMS arrives it stops and the phone is
  told why;
- the code must come in a message that is newer than the snapshot and than the request, and a ROWID is used once;
- the code and the message are never logged or kept (only the ROWID).
A code screen that is not the program's own fresh request is left to a person: nothing is typed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .login import LoginBlocked, LoginError, LoginGuard
from .sms import SmsError

CODE_VALID = timedelta(minutes=5)          # the app's code is valid for 5 minutes


@dataclass(frozen=True)
class AutoResult:
    status: str      # logged_in | not_login_screen | no_access | no_phone | budget | blocked | login_error | no_sms | bad_code | stale_code_screen
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "logged_in"


def _minutes(wait: timedelta) -> int:
    return int(wait.total_seconds() // 60) + 1


class AutoLogin:
    def __init__(self, helper, inbox, hour_budget: LoginGuard, day_budget: LoginGuard, state_path: Path,
                 phone: Callable[[], str], log: Callable[..., None], screen: Callable[[], str],
                 sleep: Callable[[float], None] = time.sleep,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 wait: float = 120.0, poll: float = 4.0) -> None:
        self.helper, self.inbox = helper, inbox
        self.hour_budget, self.day_budget, self.state_path = hour_budget, day_budget, Path(state_path)
        self.phone, self.log, self.screen, self.sleep, self.now_fn = phone, log, screen, sleep, now_fn
        self.wait, self.poll = wait, poll

    # -- the small state file: the open request and the last ROWID that was used -------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, **fields) -> None:
        data = self._load()
        data.update(fields)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    # -- the attempt --------------------------------------------------------------------------------------
    def run(self) -> AutoResult:
        result = self._run()
        self.log("auto_login", status=result.status)         # never the code, the number or a message
        return result

    def _run(self) -> AutoResult:
        kind = self.screen()
        if kind == "code":
            return self._open_request()
        if kind != "login":
            return AutoResult("not_login_screen", f"the app shows: {kind}")
        now = self.now_fn()
        wait = self.helper.guard.check(now)
        if wait:                                             # the app's own counter (manual requests count too)
            return AutoResult("blocked", f"too many code requests: wait {_minutes(wait)} minutes")
        for guard in (self.hour_budget, self.day_budget):
            wait = guard.check(now)
            if wait:
                return AutoResult("budget", f"a code was asked for lately: the next automatic request is allowed in "
                                            f"{_minutes(wait)} minutes")
        try:
            snapshot = self.inbox.newest_rowid()
        except SmsError as exc:
            return AutoResult("no_access", str(exc))
        try:
            phone = self.phone()
        except Exception:
            return AutoResult("no_phone", "the phone number is not in the Keychain (service club-booking-phone)")
        # everything is written down BEFORE the code is asked for
        self.hour_budget.record_request(now)
        self.day_budget.record_request(now)
        self._save(request={"at": now.isoformat(), "snapshot": snapshot})
        try:
            shown = self.helper.request_code(phone)
        except LoginBlocked as exc:
            return AutoResult("blocked", str(exc))
        except LoginError as exc:
            return AutoResult("login_error", str(exc))
        if shown != "code_screen":
            return AutoResult("login_error", "the code screen did not appear after the request")
        return self._wait_and_enter(now, snapshot)

    def _open_request(self) -> AutoResult:
        """The app already shows the code screen. Only the program's own fresh request is continued."""
        request = self._load().get("request") or {}
        try:
            at, snapshot = datetime.fromisoformat(request["at"]), int(request["snapshot"])
        except (KeyError, TypeError, ValueError):
            return AutoResult("stale_code_screen", "the code screen is open and no request of the program is known: "
                                                   "a person must go back to the login screen")
        if self.now_fn() - at > CODE_VALID:
            return AutoResult("stale_code_screen", "the code screen is open and its code is no longer valid: "
                                                   "a person must go back to the login screen")
        return self._wait_and_enter(at, snapshot)

    def _pairing_hint(self) -> str:
        """Why no SMS came: when the Mac has received nothing for days, the iPhone and the Mac are not paired any more."""
        try:
            age = self.inbox.last_message_age(self.now_fn())
        except Exception:
            age = None
        if age is not None and age > 3 * 86400:
            return (f"this Mac has received no message of any kind for {int(age // 86400)} days: the iPhone and the Mac are "
                    "not paired (iPhone: Text Message Forwarding, switch the Mac off and on)")
        return "is Text Message Forwarding on for this Mac?"

    def _wait_and_enter(self, requested_at: datetime, snapshot: int) -> AutoResult:
        waited = 0.0
        while True:
            after = max(snapshot, int(self._load().get("used_rowid", 0)))
            try:
                found = self.inbox.fresh_codes(after, requested_at)
            except SmsError as exc:
                return AutoResult("no_access", str(exc))
            if found:
                rowid, code = found[0]
                break
            if waited >= self.wait:
                return AutoResult("no_sms", f"no SMS from {self.inbox.sender} arrived in {int(self.wait)} seconds: "
                                            f"{self._pairing_hint()}")
            self.sleep(self.poll)
            waited += self.poll
        self._save(used_rowid=rowid)                          # before the typing: a code is used once
        self.log("auto_login_code", rowid=rowid, waited=int(waited))
        try:
            outcome = self.helper.enter_code(code)
        except LoginBlocked as exc:
            return AutoResult("blocked", str(exc))
        except LoginError as exc:
            return AutoResult("login_error", str(exc))
        if outcome == "logged_in":
            return AutoResult("logged_in")
        return AutoResult("bad_code", "the app did not accept the code")
