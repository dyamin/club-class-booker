"""The program logs the club app in by itself (the user decided on 2026-10-15: "it should be independent").

This replaces the old rule "an unattended run never logs in". The rule existed because a code request is a counted
action (3 in 10 minutes lock the login for 10 minutes) and because the code goes to a phone. Now the code is read from
the Mac's Messages database (sms.py), and the risk is kept small by these rules:
- it acts only on the login screen, and only when a class is waiting (the caller decides that);
- it never asks for a code that it cannot read: the snapshot of the Messages database comes first, and a database that
  cannot be read means no request;
- its own budget: one request in 45 minutes and eight in 24 hours (see `budgets`), written down BEFORE the request, on top of the
  login guard of the app (2 requests in 10 minutes), which counts manual requests too;
- one request per attempt, one code typed per request, never a second try: when no SMS arrives it stops and the phone is
  told why;
- the code must come in a message that is newer than the snapshot and than the request, and a ROWID is used once;
- the code and the message are never logged or kept (only the ROWID).
A code screen that is not the program's own fresh request is left to a person: nothing is typed.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .login import LoginBlocked, LoginError, LoginGuard
from .sms import SmsError

CODE_VALID = timedelta(minutes=5)          # the app's code is valid for 5 minutes
HOUR_LIMIT, HOUR_WINDOW = 2, timedelta(minutes=45)   # LoginGuard keeps one request free: one automatic request per 45 minutes
DAY_LIMIT, DAY_WINDOW = 9, timedelta(hours=24)       # eight automatic requests a day (2026-11-05: three, then five; failed requests count, and three of five were used by failures)


def budgets(home: Path):
    """The program's own login budget: (the 45-minute guard, the 24-hour guard). One place for the numbers."""
    return (LoginGuard(Path(home) / "autologin-hour.json", HOUR_LIMIT, HOUR_WINDOW),
            LoginGuard(Path(home) / "autologin-day.json", DAY_LIMIT, DAY_WINDOW))


@dataclass(frozen=True)
class AutoResult:
    status: str      # logged_in | not_login_screen | no_access | no_phone | budget | blocked | login_error | update_needed | no_sms | bad_code | stale_code_screen
    detail: str = ""
    retry_after: Optional[timedelta] = None     # when a request is allowed again (2026-11-05); the caller arms a look for it

    @property
    def ok(self) -> bool:
        return self.status == "logged_in"


def _masked(text: str, limit: int = 200) -> str:
    """The reason of a failure for the log: every digit becomes "#", so that no code and no phone number is kept
    (2026-10-17: a failure was logged without its reason, and nobody could tell what had gone wrong)."""
    return re.sub(r"\d", "#", text)[:limit]


# The statuses after which the program may not ask again at once, and the ones after which a request was made and the
# screen is worth a look (a refusal by the budget made no request, so there is nothing to show).
RETRY_STATUSES = ("budget", "blocked", "login_error", "update_needed", "no_sms", "bad_code", "stale_code_screen")
TRIED_STATUSES = ("login_error", "update_needed", "no_sms", "bad_code", "stale_code_screen")
APP_LOCK = timedelta(minutes=10)               # the app locks the login for 10 minutes after too many requests


def _minutes(wait: timedelta) -> int:
    return int(wait.total_seconds() // 60) + 1


class AutoLogin:
    def __init__(self, helper, inbox, hour_budget: LoginGuard, day_budget: LoginGuard, state_path: Path,
                 phone: Callable[[], str], log: Callable[..., None], screen: Callable[[], str],
                 sleep: Callable[[float], None] = time.sleep,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 wait: float = 120.0, poll: float = 4.0,
                 evidence: Callable[[str], str] = lambda label: "",
                 describe: Callable[[], str] = lambda: "") -> None:
        """evidence(label) saves a picture of the screen and returns its file name; describe() returns the texts on the
        screen. Both are used only after a failed attempt, and a failure of theirs never changes the result."""
        self.evidence, self.describe = evidence, describe
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

    # -- after a failure ------------------------------------------------------------------------------------
    def _next_request_in(self, status: str) -> Optional[timedelta]:
        """How long until the program's own budget (and, after the app's lock, the app) allows the next request."""
        now = self.now_fn()
        waits = [w for w in (self.hour_budget.check(now), self.day_budget.check(now)) if w]
        if status == "blocked":
            waits.append(APP_LOCK)
        return max(waits) if waits else None

    def _keep_evidence(self, result: AutoResult) -> AutoResult:
        """2026-11-05: two requests ended with "the code screen did not appear" and nobody could tell what the app showed.
        Keep the screen type, the texts on the screen (every digit hidden) and a picture. Never raises."""
        fields: dict = {"status": result.status}
        try:
            fields["screen"] = self.screen()
        except Exception as exc:
            fields["screen_error"] = type(exc).__name__
        try:
            fields["texts"] = _masked(self.describe(), 400)
        except Exception as exc:
            fields["texts_error"] = _masked(f"{type(exc).__name__}: {exc}")
        try:
            fields["evidence"] = self.evidence(f"login-{result.status}")
        except Exception as exc:
            fields["evidence_error"] = _masked(f"{type(exc).__name__}: {exc}")
        self.log("auto_login_screen", **fields)
        if result.status == "login_error" and fields.get("screen"):
            return replace(result, detail=f"{result.detail}; the app shows: {fields['screen']}")
        return result

    # -- the attempt --------------------------------------------------------------------------------------
    def run(self) -> AutoResult:
        result = self._run()
        if result.status in TRIED_STATUSES:
            result = self._keep_evidence(result)
        if result.status in RETRY_STATUSES:
            result = replace(result, retry_after=self._next_request_in(result.status))
        if result.ok or not result.detail:
            self.log("auto_login", status=result.status)         # never the code, the number or a message
        else:
            self.log("auto_login", status=result.status, detail=_masked(result.detail))
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
        except Exception as exc:                  # 2026-11-05: "AxError: no such element" ended a login as a crash, uncounted
            return AutoResult("login_error", f"{type(exc).__name__}: {exc}")
        if shown == "update":                     # 2026-11-05: the popup came instead of the code screen
            return AutoResult("update_needed", "the app asked for an update after the request")
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
