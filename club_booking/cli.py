"""The command line. One word per job: doctor, run, tick, status, install, uninstall, arm-test,
wake-command, prune-reports, init-mirroir."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Collection, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from . import planner
from .calendar_app import CalendarService, calendar_names
from .calendar_audit import CalendarAudit, format_findings
from . import system
from .ax import AxClient, AxError
from .ax_reader import AxReader, screen_kind
from .club_app import ClubApp
from .login import LoginError, LoginGuard, LoginHelper
from .config import Config, ConfigError, Person, load_config, resolve
from .models import CardState
from .ocr import ocr_png
from .osa import AppleScriptError
from .planner import Opening, Plan, persons_in, plan_is_stale
from .questions import Question, chosen_option, decide, overlap_question, several_question
from .reports import Reporter
from .attention import Attention, UserActive
from .runner import (BLOCKED, LOGGED_OUT, OVERLAP_REFUSAL, USER_ACTIVE, WINDOW_HIDDEN, LoggedOutError, PersonResult, RunResult, Runner, UPDATE_NEEDED,
                     format_report, is_age_refusal)
from .scheduler import LaunchAgents, OPENING_PREFIX, TEST_LABEL, label_time, wake_commands
from .screen import MirroirScreen, ScreenError
from .secrets import SecretError, get_api_key, store_secret
from .sms import MessagesInbox, SmsError, python_app_path
from .autologin import AutoLogin
from .setup import check_mirroir_config, check_mirroir_permissions, init_mirroir_config
from .state import LockBusy, Store
from .system import idle_seconds, keep_display_awake, screen_unavailable, wake_if_only_asleep
from .vision import Vision, make_client

GRACE = timedelta(seconds=30)    # launchd may fire a few seconds before the class opens
AUDIT_MARGIN = timedelta(minutes=10)    # no calendar audit when a class opens sooner than this
RETRY_SOON = timedelta(minutes=1)       # a class that should be open but is not: look again
RETRY_ERRORED = timedelta(minutes=5)    # a run that failed (locked, logged out, a screen problem): try again
MAX_RETRIES = 6                         # per opening: a class that never works does not keep a chain of agents alive
OPENING_WAIT = 20 * 60                  # an opening agent waits this long for the user to unlock the Mac
OPENING_POLL = 15                       # ... looking every 15 seconds
PAIRING_STALE_DAYS = 14           # a Mac that has received no message for this long is not paired with the iPhone
COUNTDOWN_IF_IDLE_BELOW = 120.0   # the "keep your hands off the mouse" banner only when the user is at the Mac
MIN_IDLE = 600.0                 # default: a non-urgent step waits until the user has been away this long


@dataclass(frozen=True)
class TickDecision:
    action: str                       # nothing | run_due | replan | locked | wait_idle
    persons: Tuple[str, ...] = ()
    due: Tuple[Opening, ...] = ()


def decide_tick(plan: Optional[Plan], handled: set, now: datetime, locked: bool, idle: float,
                replan_hours: int, grace: timedelta = GRACE, min_idle: float = MIN_IDLE,
                force_hours: int = 28) -> TickDecision:
    """What to do when the scheduler wakes the program up."""
    due = tuple(planner.due(plan.openings if plan else (), now + grace, handled))
    stale = plan_is_stale(plan, now, replan_hours)
    if not due and not stale:
        return TickDecision("nothing")
    if locked:
        return TickDecision("locked", tuple(persons_in(due)), due) if due else TickDecision("nothing")
    if due:
        return TickDecision("run_due", tuple(persons_in(due)), due)
    forced = plan is not None and now - plan.created_at > timedelta(hours=force_hours)   # never waits for ever
    if idle < min_idle and not forced:
        return TickDecision("wait_idle")
    return TickDecision("replan")


def describe_cards(cfg: Config, result: RunResult) -> str:
    """Every class that was read, so a dry run can be compared with the screen."""
    names = {p.key: p.first_name for p in cfg.people}
    lines: List[str] = []
    for r in result.people:
        problem = f" (problem: {r.error})" if r.error else ""
        lines.append(f"Read for {names.get(r.person, r.person)}: {len(r.cards)} classes{problem}")
        for c in r.cards:
            opens = f", opens {c.opens_at:%a %d/%m %H:%M}" if c.state == CardState.NOT_OPEN else ""
            lines.append(f"  {c.state.value:10s} {c.start:%a %d/%m %H:%M}  {c.name}{opens}")
    return "\n".join(lines)


def _age_text(seconds: Optional[float]) -> str:
    """'45 seconds', '12 minutes', '2 hours', '3 days': for a doctor line."""
    if seconds is None:
        return "unknown"
    for limit, unit, size in ((90, "second", 1), (5400, "minute", 60), (172800, "hour", 3600)):
        if seconds < limit:
            n = max(1, round(seconds / size))
            return f"{n} {unit}" + ("" if n == 1 else "s")
    n = round(seconds / 86400)
    return f"{n} days"


def local_naive(when: datetime) -> datetime:
    """launchd works in the Mac's own local time, without a zone."""
    return when.astimezone().replace(tzinfo=None)


class App:
    def __init__(self, cfg: Config, store: Store, reporter, agents, screen_factory: Callable,
                 vision_factory: Callable, calendar_factory: Callable,
                 now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 sleep: Callable[[float], None] = time.sleep,
                 locked_fn: Callable[[], bool] = screen_unavailable, idle_fn: Callable[[], float] = idle_seconds,
                 running_label: Optional[str] = None, out: Callable[[str], None] = print,
                 club_app_factory: Callable = lambda: None, login_factory: Callable = lambda: None,
                 ax_factory: Callable = lambda: None,
                 audit_factory: Callable[[str], object] = lambda mode: None,
                 wake_fn: Callable[[], bool] = wake_if_only_asleep,
                 awake_fn: Callable = keep_display_awake,
                 auto_login_factory: Optional[Callable] = None, inbox_factory: Optional[Callable] = None) -> None:
        self.cfg, self.store, self.reporter, self.agents = cfg, store, reporter, agents
        self.screen_factory, self.vision_factory, self.calendar_factory = screen_factory, vision_factory, calendar_factory
        self.now_fn, self.sleep, self.locked_fn, self.idle_fn = now_fn, sleep, locked_fn, idle_fn
        self.running_label = running_label
        self.out = out
        self.club_app_factory = club_app_factory
        self.login_factory, self.ax_factory = login_factory, ax_factory
        self.audit_factory = audit_factory
        self.wake_fn = wake_fn          # wakes a sleeping display; True when the screen is usable then
        self.awake_fn = awake_fn        # context manager: holds the display on while the program uses the screen
        self.auto_login_factory = auto_login_factory    # attention -> AutoLogin; used only when cfg.auto_login is on
        self.inbox_factory = inbox_factory              # -> MessagesInbox: for the doctor and the wake-up test

    # -- messages ------------------------------------------------------------------------------
    def _send(self, title: str, body: str, kind: str, persons: Sequence[str], ident: str) -> List[str]:
        """A message about a problem (kind: problem, locked, login, update) is tagged and remembered, so that
        `_clear_solved` can take it back from the phone when the problem is gone."""
        if not kind:
            return self.reporter.tell(title, body)
        mid = hashlib.sha1(f"{title}\n{body}\n{self.now_fn().isoformat()}".encode("utf-8")).hexdigest()[:12]
        problems = self.reporter.tell(title, body, tag=mid)
        if not any(p.startswith("phone") for p in problems):
            self.store.remember_message(mid, kind, sorted(persons), ident, self.now_fn())
        return problems

    def _clear_solved(self, result: RunResult) -> None:
        """Take back the messages whose problem is gone (2026-10-14: "the Mac is locked" and "Problem" stayed on the phone
        for hours after the class was booked). A message about people goes when every one of them has run without error;
        a login message goes when any person could run. A run that stood back proves nothing. The 'told today' counter
        of the message is cleared, so the same problem is told again if it comes back."""
        ok = {r.person for r in result.people if not r.error}
        if not ok:
            return
        blocked_now = any(r.error in (LOGGED_OUT, UPDATE_NEEDED) for r in result.people)    # this run found it blocked
        for mid, message in sorted(self.store.messages().items()):
            kind, persons = message.get("kind"), set(message.get("persons", []))
            if not ((kind in ("login", "update") and not blocked_now)
                    or (kind in ("problem", "locked") and persons and persons <= ok)):
                continue
            try:
                self.reporter.resolve(mid)
            except Exception as exc:               # the phone did not answer: the next run tries again
                self.store.log("message_not_taken_back", mid=mid, error=f"{type(exc).__name__}: {exc}")
                continue
            self.store.forget_message(mid)
            if message.get("ident"):
                self.store.clear_attempt(message["ident"])
            self.store.log("message_taken_back", kind=kind, persons=sorted(persons))

    def _tell_once(self, title: str, body: str = "", kind: str = "", persons: Sequence[str] = ()) -> List[str]:
        """Send a message to the phone, but the same message only once a day. A problem that comes back every few
        minutes must not flood the phone. Returns the problems of the channels."""
        day = f"{self.now_fn():%Y%m%d}"
        digest = hashlib.sha1(f"{title}\n{body}".encode("utf-8")).hexdigest()[:12]
        ident = f"report|{day}|{digest}"
        if self.store.bump_attempt(ident) > 1:
            self.store.log("report_repeated", title=title)
            return []
        problems = self._send(title, body, kind, persons, ident)
        if any(p.startswith("phone") for p in problems):
            self.store.drop_attempt(ident)       # the phone did not get it: the next run says it again
        return problems

    def _tell_daily(self, key: str, title: str, body: str = "", kind: str = "",
                    persons: Sequence[str] = ()) -> List[str]:
        """At most one message a day for a kind of problem, whichever step found it (a logged-out app is found by the
        look before a run and by the look before an opening: one message, not two)."""
        ident = f"daily|{self.now_fn():%Y%m%d}|{key}"
        if self.store.bump_attempt(ident) > 1:
            self.store.log("report_repeated", title=title)
            return []
        problems = self._send(title, body, kind, persons, ident)
        if any(p.startswith("phone") for p in problems):
            self.store.drop_attempt(ident)
        return problems

    # -- running -----------------------------------------------------------------------------
    def _countdown(self) -> None:
        if self.idle_fn() >= COUNTDOWN_IF_IDLE_BELOW:
            return                       # nobody is at the Mac to warn (the attention guard still protects the user)
        n = self.cfg.countdown_seconds
        try:
            self.reporter.mac("Club booking", f"Starting in {n} seconds. Please keep your hands off the mouse until it finishes.")
        except AppleScriptError:
            pass
        self.sleep(n)

    def _attention(self, urgent: bool) -> Attention:
        """Watches for the user at the keyboard and the mouse. A class that is opening is urgent: the run waits for a
        pause in the typing and then goes on. Anything else stands back at once (see attention.py)."""
        return Attention(self.idle_fn, self.now_fn, self.sleep, patient=urgent)

    def do_run(self, persons: Sequence[Person], dry_run: bool, countdown: bool, urgent: bool = True,
               guard: bool = True, phone: bool = True, tell_blocked: bool = True) -> RunResult:
        """guard=False is for a run the user started and watches: nobody stands back for them, and phone=False: he reads
        the result on the console, so the phone gets no message (the user's rule of 2026-10-15). tell_blocked=False: the
        caller deals with a logged-out app itself (the tick logs in and runs again before it tells anyone)."""
        with self.store.run_lock():
            if countdown:
                self._countdown()
            with self.awake_fn(), self.screen_factory() as screen:
                runner = Runner(self.cfg, screen, self.vision_factory(), self.store, self.calendar_factory(screen),
                                now_fn=self.now_fn, sleep=self.sleep, dry_run=dry_run,
                                club_app=self.club_app_factory(),
                                attention=self._attention(urgent) if guard else None)
                result = runner.run(persons)
        self.store.prune_evidence(self.cfg.keep_evidence_days)
        if dry_run:
            self.out(describe_cards(self.cfg, result))
        self._report_run(result, dry_run, phone, tell_blocked)
        self._ask_questions(result, dry_run)
        self.rearm({r.person for r in result.people if r.error and r.error != USER_ACTIVE})     # after the questions: they arm wake-ups
        self._audit_calendars(result, dry_run)       # last: a pass over a calendar takes about 20 seconds
        return result

    def _report_run(self, result: RunResult, dry_run: bool, phone_too: bool = True, tell_blocked: bool = True) -> None:
        """Tell the phone what the run did. A logged-out or outdated app is not a line of the report: it is the one login
        message of the day (2026-10-15: the phone had both "Problem (Alex): the club app is logged out" and "the club
        app is logged out", two reminders for one cause)."""
        blocked = [r for r in result.people if r.error in (LOGGED_OUT, UPDATE_NEEDED)]
        shown = RunResult(result.run_id, [replace(r, error="") if r.error in (LOGGED_OUT, UPDATE_NEEDED) else r
                                          for r in result.people], result.plan, result.warnings)
        report = format_report(self.cfg, shown)
        if report:
            self.out(report[0] + "\n" + report[1])           # everything, for the console
        # The phone gets only what needs the user: a class that was NOT booked (the user's rule of 2026-10-15: a message
        # only for a decision or an action, never a confirmation). A booking is in the calendar and in the log.
        phone = format_report(self.cfg, shown, problems_only=True)
        if phone and not dry_run and phone_too:
            failed = sorted({r.person for r in shown.people if r.error and r.error != USER_ACTIVE})
            problems = self._tell_once(*phone, kind="problem" if failed else "", persons=failed)
            if problems:
                self.store.log("report_problem", problems=problems)
        if blocked and not phone_too:
            self.out(blocked[0].error[:1].upper() + blocked[0].error[1:])        # a run by hand: the console says it
        if blocked and not dry_run and phone_too and tell_blocked:
            names = {p.key: p.first_name for p in self.cfg.people}
            self._tell_blocked("update" if any(r.error == UPDATE_NEEDED for r in blocked) else "login",
                               ", ".join(names.get(r.person, r.person) for r in blocked))
        if not dry_run:
            self._clear_solved(result)

    def _may_log_in_by_itself(self, kind: str, decision: TickDecision, dry_run: bool) -> bool:
        """Only on the login or the code screen, only when a class is waiting (not for a plan refresh), only when it is
        switched on, never in a dry run or while the program is paused. Not on the app's own 10-minute lock ("locked")."""
        return (self.cfg.auto_login and self.auto_login_factory is not None and not dry_run
                and kind in ("login", "code") and decision.action == "run_due" and not self.store.paused())

    def _log_in_by_itself(self):
        """The banner first (the keyboard is taken for a minute), then the attempt under the attention guard of an urgent
        run. Never raises: a failure is logged and the phone is told the usual way."""
        try:
            self._countdown()
            result = self.auto_login_factory(self._attention(urgent=True)).run()
        except Exception as exc:
            self.store.log("auto_login_failed", error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())
            return None
        return result

    def _tell_blocked(self, kind: str, waiting: str, note: str = "") -> None:
        """The app is logged out ("login", "code", "locked") or needs an update ("update"): one message a day, whichever
        step finds it first (the look before a run, or a run that finds it halfway)."""
        update = kind == "update"
        self._tell_daily("update" if update else "login",
                         "Club booking: the club app needs an update" if update else "Club booking: the club app is logged out",
                         f"{BLOCKED[kind].capitalize()}.\n"
                         + (f"Waiting to book: {waiting}.\n" if waiting else "")
                         + (f"Automatic login did not work: {note}.\n" if note else "")
                         + "The booking runs by itself right after you have fixed it.",
                         kind="update" if update else "login")

    # -- questions on the iPhone (spec R14 to R19) ------------------------------------------------------
    @staticmethod
    def _label(name: str, start: datetime) -> str:
        return f"{name}, {start:%a %d/%m %H:%M}"

    def _has_question(self, kind: str, ident: str) -> bool:
        """Was a question of this kind asked about this booking already (in any state)? It is never asked twice."""
        return any(q.get("kind") == kind and ident in q.get("bookings", []) for q in self.store.questions().values())

    def _new_question(self, question: Question) -> None:
        """Saved first, then sent: a question that could not be sent is sent again at the next run."""
        data = question.to_dict()
        data["asked"] = False
        self.store.save_question(data)
        self._send_question(question.id, question.text, [o.label for o in question.options])

    def _send_question(self, qid: str, text: str, labels: List[str]) -> None:
        try:
            self.reporter.ask(qid, text, labels)
        except AppleScriptError as exc:
            self.store.log("question_not_sent", question=qid, error=str(exc))
            return
        self.store.update_question(qid, asked=True, asked_at=self.now_fn().isoformat())

    def _ask_questions(self, result: RunResult, dry_run: bool) -> None:
        """After a run: ask about several classes in a day and overlapping classes of a person who asks, and note
        the sons' overlaps. A waitlist spot is never asked about: it is kept (the user's decision of 2026-10-14).
        A problem here never breaks a run."""
        if dry_run or self.cfg.questions != "on":
            return
        for step in (self._ask_overlaps, self._ask_refused_overlaps, lambda r: self._ask_several(),
                     lambda r: self._resend_unsent()):
            try:
                step(result)
            except Exception as exc:
                self.store.log("questions_failed", step=getattr(step, "__name__", "step"),
                               error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())

    def _ask_overlaps(self, result: RunResult) -> None:
        for r in result.people:
            if r.error:
                continue
            standing = self.store.standing_bookings()
            for default, others, was_booked in r.overlaps:
                ident = f"{r.person}|{default.key}"
                if was_booked and ident not in standing:
                    continue          # the class that is held was registered by hand: no question (nothing to decide)
                booked_now = any(o.status == "booked" and o.card_key == default.key for o in r.outcomes)
                if not others or not (was_booked or booked_now) or self._has_question("overlap", ident):
                    continue
                name = self.cfg.person(r.person).first_name
                self._new_question(overlap_question(
                    r.person, name, (ident, self._label(default.name, default.start)),
                    [(f"{r.person}|{o.key}", self._label(o.name, o.start)) for o in others],
                    min(c.start for c in [default, *others])))

    def _ask_refused_overlaps(self, result: RunResult) -> None:
        """The club allows only one of two overlapping classes. When it refused a class for that reason and the person
        holds another one that the program booked, the user decides which to keep (the user's rule of 2026-10-15)."""
        standing, refused, now = self.store.standing_bookings(), self.store.refused(), self.now_fn()
        for r in result.people:
            if r.error:
                continue
            cards = {c.key: c for c in [*r.cards, *r.booked]}
            for ident, info in refused.items():
                person, _, key = ident.partition("|")
                other = cards.get(key)
                if (person != r.person or OVERLAP_REFUSAL not in info.get("message", "") or other is None
                        or other.state != CardState.OPEN or other.start <= now):
                    continue
                for held in cards.values():
                    held_ident = f"{person}|{held.key}"
                    if (held.state == CardState.REGISTERED and held.key != key and held_ident in standing
                            and held.start < other.end and other.start < held.end
                            and not self._has_question("overlap", held_ident)):
                        self._new_question(overlap_question(
                            person, self.cfg.person(person).first_name, (held_ident, self._label(held.name, held.start)),
                            [(ident, self._label(other.name, other.start))], min(held.start, other.start)))

    def _ask_several(self) -> None:
        """A person who asks has several classes on one day, all booked by the program: which to keep? (R16)"""
        if not self.cfg.questions_several:
            return
        now, tz = self.now_fn(), ZoneInfo(self.cfg.timezone)
        asks = {p.key: p for p in self.cfg.people if p.asks}
        by_day: Dict[Tuple[str, str], List[Tuple[str, str, datetime]]] = {}
        for ident, row in self.store.standing_bookings().items():
            if row.get("status") != "confirmed" or row["person"] not in asks:
                continue
            start = datetime.strptime(row["card_key"].split("|")[0], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
            if start > now:
                by_day.setdefault((row["person"], f"{start:%Y-%m-%d}"), []).append((ident, row["name"], start))
        for (person, _day), items in sorted(by_day.items()):
            idents = {i for i, _, _ in items}
            mine = [q for q in self.store.questions().values() if q.get("kind") == "several" and q["person"] == person]
            if len(items) < 2 or any(set(q["bookings"]) >= idents for q in mine):
                continue
            if any(q["status"] == "decided" and set(q["bookings"]) & idents for q in mine):
                continue              # an answer that waits would cancel classes this question offers: wait for it
            for q in mine:                     # an open question about fewer classes of that day is replaced
                if q["status"] == "open" and set(q["bookings"]) < idents:
                    self.reporter.close_question(q["id"])
                    self.store.update_question(q["id"], status="expired")
            items.sort(key=lambda i: i[2])
            self._new_question(several_question(person, asks[person].first_name,
                                                [(i, self._label(n, st)) for i, n, st in items], items[0][2]))

    # -- the user's answers (spec R17, R18) -------------------------------------------------------------
    def cmd_answers(self) -> int:
        """Read the answers on the iPhone and act on them now (the tick does this by itself)."""
        if self.store.paused():
            self.out("Paused: remove the file PAUSE in the data folder to continue.")
            return 0
        self._process_answers(self.now_fn(), require_away=False)       # the user asked for it: no wait for them to leave
        waiting = [q for q in self.store.open_questions()]
        self.out(f"{len(waiting)} question(s) still open." if waiting else "No question is open.")
        return 0

    def _process_answers(self, now: datetime, require_away: bool = True) -> None:
        """Read the answers; act on a decided one when the Mac is unlocked and the user is away (and no class is about
        to open: the run lock is held for minutes). Never raises."""
        if self.cfg.questions != "on" or not self.store.open_questions():
            return
        try:
            self._read_answers(now)
            decided = [q for q in self.store.open_questions() if q["status"] == "decided"]
            soon = any(datetime.fromisoformat(q["deadline"]) - now < timedelta(hours=3) for q in decided)   # the class is near
            if not decided or self.locked_fn() or (require_away and not soon and self.idle_fn() < self.cfg.min_idle_seconds):
                return
            if self._opening_is_near():
                self.store.log("answers_deferred", reason="a class opens within 10 minutes: booking comes first")
                return
            self._apply_decisions(decided, guard=require_away, urgent=soon)       # a manual `answers` is watched: no guard
        except LockBusy:
            self.store.log("answers_busy")
        except Exception as exc:
            self.store.log("answers_failed", error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())

    def _finish(self, q: dict, status: str, result: str) -> None:
        self.store.update_question(q["id"], status=status, result=result)
        try:
            self.reporter.close_question(q["id"])
        except AppleScriptError as exc:
            self.store.log("question_not_closed", question=q["id"], error=str(exc))

    def _read_answers(self, now: datetime) -> None:
        for q in self.store.open_questions():
            if q.get("kind") == "several" and not self.cfg.questions_several:
                self._finish(q, "done", "several-class questions are switched off: all the classes stay")
                continue
            if q.get("kind") == "waitlist":
                # the user decided on 2026-10-14: a waitlist spot is always kept and nothing is asked. An old question
                # is closed whatever it says: its "Remove" must never cancel anything.
                self._finish(q, "done", "waitlist questions were removed: the spot is kept")
                continue
            if datetime.fromisoformat(q["deadline"]) <= now:
                if q["status"] == "decided":      # the user answered, and the answer could not be applied in time
                    self.reporter.tell(f"Club booking: {self.cfg.person(q['person']).first_name}'s answer was not applied",
                                       "The Mac was not available (locked, screen off, or in use) before the class "
                                       "started, so nothing was changed; the classes stay booked.")
                self._finish(q, "expired", "the class started; the booking stays")
                continue
            if q["status"] != "open" or not q.get("asked"):
                continue
            try:
                number = chosen_option(self.reporter.answers(q["id"]))
            except AppleScriptError as exc:
                self.store.log("answers_not_read", question=q["id"], error=str(exc))
                continue
            if number is None:
                continue
            question = Question.from_dict(q)
            decision = decide(question, number)
            self.store.update_question(q["id"], status="decided", answer=number)
            if not decision.cancel and not decision.book:
                chosen = question.options[number - 1].label
                self._finish({**q, "answer": number}, "done", f"nothing to change ({chosen})")

    def _apply_decisions(self, decided: List[dict], guard: bool = False, urgent: bool = False) -> None:
        applied: Set[str] = set()
        with self.store.run_lock():
            with self.awake_fn(), self.screen_factory() as screen:
                runner = Runner(self.cfg, screen, self.vision_factory(), self.store, self.calendar_factory(screen),
                                now_fn=self.now_fn, sleep=self.sleep, club_app=self.club_app_factory(),
                                attention=self._attention(urgent=urgent) if guard else None)
                stood_back = False
                try:
                    for q in decided:
                        try:
                            self._apply_one(runner, q)
                            if self.store.questions().get(q["id"], {}).get("status") == "done":
                                applied.add(q["person"])
                        except LoggedOutError:
                            raise
                        except Exception as exc:        # one damaged question must not block the others
                            tries = q.get("tries", 0) + 1
                            self.store.update_question(q["id"], tries=tries)
                            self.store.log("answer_failed", question=q["id"], error=f"{type(exc).__name__}: {exc}")
                            if tries >= 3:
                                self._finish(q, "done", f"could not apply the answer: {type(exc).__name__}: {exc}")
                except UserActive:
                    stood_back = True        # the user came back: the answers wait for the next quiet time, no count
                    self.store.log("answers_stood_back", questions=[q["id"] for q in decided])
                finally:
                    if not stood_back:
                        try:
                            runner.ensure_person(self.cfg.home_person)
                        except UserActive:
                            pass
                        except Exception as exc:
                            self.store.log("restore_failed", error=f"{type(exc).__name__}: {exc}")
        if applied and not stood_back and self.cfg.calendar_audit == "write":
            # the calendar follows the answer: the new class gets its event, the cancelled one loses it (2026-10-15 16:10)
            try:
                self.do_run([self.cfg.person(k) for k in sorted(applied)], False, countdown=False, urgent=urgent, guard=guard)
            except Exception as exc:
                self.store.log("answer_followup_failed", error=f"{type(exc).__name__}: {exc}")

    def _gave_up(self, ident: str) -> None:
        """The program itself cancelled this class on the user's answer: its calendar event goes at the next pass, without
        the second reading 30 minutes later that a class dropped by the user needs."""
        self.store.mark_calendar_dropped(ident, self.now_fn() - timedelta(minutes=31))

    def _apply_one(self, runner: Runner, q: dict) -> None:
        """Book what the answer wants first, and give up the rest only when that worked and the class the user kept
        still stands. Everything cancelled goes through Runner.cancel, which refuses anything the program did not
        book. A step that failed before anything was pressed is tried again at the next tick (3 times)."""
        question = Question.from_dict(q)
        decision = decide(question, q["answer"])
        chosen = question.options[q["answer"] - 1]
        person = self.cfg.person(q["person"])
        standing = self.store.standing_bookings()

        def label(ident: str) -> str:
            row = standing.get(ident)
            if row is None:
                return ident
            start = datetime.strptime(row["card_key"].split("|")[0], "%Y-%m-%dT%H:%M")
            return self._label(row["name"], start)

        notes: List[str] = []
        retry: List[str] = []
        proceed = True
        if question.kind == "overlap" and decision.book and decision.cancel:
            return self._apply_switch(runner, q, question, decision, person)
        if decision.book:
            wanted = list(decision.book)
            for ident in wanted:
                self.store.mark_chosen(ident)
            try:
                result = runner.run_person(person)
            finally:
                for ident in wanted:
                    self.store.unmark_chosen(ident)
            self._ask_questions(RunResult(runner.run_id, [result], None, []), dry_run=False)   # an overlap
            if result.error == USER_ACTIVE:
                raise UserActive("the user came back")      # the answer waits: what was begun is idempotent
            for o in result.outcomes:
                if o.status == "booked":
                    notes.append(f"Booked: {self._label(o.name, o.start)}")
            after = self.store.standing_bookings()
            if not all(i in after and after[i]["status"] == "confirmed" for i in wanted):
                proceed = False
                if result.error:
                    retry.append(result.error)
                else:
                    notes.append("Could not book the class you chose, so what was booked is kept.")
        if proceed and decision.cancel and chosen.keep:
            registered = runner.registered_keys(person)         # the class the user kept must still be booked
            if registered is None:
                proceed = False
                retry.append("the booked tab could not be read")
            elif any(i.split("|", 1)[1] not in registered for i in chosen.keep):
                proceed = False
                notes.append("The class you kept is not booked any more, so nothing was cancelled.")
        if proceed:
            for ident in decision.cancel:
                if ident not in standing:                       # given up already, or never the program's
                    notes.append(f"{label(ident)}: left alone (the program did not book it, or it is given up already)")
                    continue
                answer = runner.cancel(person, ident)
                notes.append(f"{label(ident)}: {answer}")
                if answer == "cancelled":
                    self._gave_up(ident)
                if answer.startswith("retry"):
                    retry.append(answer)
        if retry:
            tries = q.get("tries", 0) + 1
            self.store.update_question(q["id"], tries=tries)
            if tries < 3:
                return                                          # still decided: the next tick tries again
            notes.append("Gave up after 3 tries.")
        self._finish(q, "done", "; ".join(notes) or "nothing to change")
        bad = [n for n in notes if not n.endswith(": cancelled")]
        if bad:               # the user's own answer is no news; a part that could not be done is (they must do it by hand)
            self.reporter.tell(f"Club booking: {person.first_name}'s answer was only partly applied", "\n".join(bad))

    def _apply_switch(self, runner: Runner, q: dict, question: Question, decision, person: Person) -> None:
        """Overlap, "Switch to": the club refuses two overlapping registrations, so the held class goes FIRST. It is never
        given up for a class that cannot be booked: the chosen class must show "register" before anything is cancelled.
        If the booking fails after the cancel, the user is told loudly (he must register by hand)."""
        held, wanted = decision.cancel[0], decision.book[0]
        held_label = question.options[0].label.split(": ", 1)[-1]
        wanted_label = question.options[q["answer"] - 1].label.split(": ", 1)[-1]
        notes: List[str] = []
        retry: List[str] = []
        if not runner.card_is_open(person, wanted.split("|", 1)[1]):
            notes.append(f"{wanted_label} cannot be booked now (full, or not on the list): nothing was changed, "
                         f"{held_label} is kept.")
        else:
            answer = runner.cancel(person, held)
            if answer.startswith("retry"):
                retry.append(answer)
            elif answer not in ("cancelled", "already_gone"):
                notes.append(f"{held_label}: {answer}. Nothing else was changed.")
            else:
                self._gave_up(held)
                self.store.unrefuse(wanted)
                self.store.mark_chosen(wanted)
                try:
                    result = runner.run_person(person)
                finally:
                    self.store.unmark_chosen(wanted)
                self._ask_questions(RunResult(runner.run_id, [result], None, []), dry_run=False)   # an overlap
                if result.error == USER_ACTIVE:
                    raise UserActive("the user came back")      # the booking waits: what was begun is idempotent
                after = self.store.standing_bookings()
                if wanted not in after or after[wanted]["status"] != "confirmed":
                    notes.append(f"{held_label} was cancelled but {wanted_label} could not be booked"
                                 f"{': ' + result.error if result.error else ''}. Register {wanted_label} by hand.")
        if retry:
            tries = q.get("tries", 0) + 1
            self.store.update_question(q["id"], tries=tries)
            if tries < 3:
                return                                          # nothing was pressed: the next wake tries again
            notes.append("Gave up after 3 tries; nothing was changed.")
        self._finish(q, "done", "; ".join(notes) or f"switched to {wanted_label}")
        if notes:
            self.reporter.tell(f"Club booking: {person.first_name}'s answer was only partly applied", "\n".join(notes))

    def _resend_unsent(self) -> None:
        for q in self.store.open_questions():
            if q.get("asked") is False and q.get("status") == "open":
                self._send_question(q["id"], q["text"], [o["label"] for o in q["options"]])

    def _opening_is_near(self) -> bool:
        """The audit holds the run lock for a minute or more. A class that opens meanwhile would find the lock busy
        and wait for the next 5-minute tick, so the audit waits for a quiet moment (the next run does it)."""
        plan = self.store.load_plan()
        if plan is None:
            return False
        return bool(planner.due(plan.openings, self.now_fn() + AUDIT_MARGIN, self.store.handled()))

    def _attending_idents(self) -> Set[str]:
        """Classes that the club refused for age and whose person goes anyway (`age_refusal_ok`): they are on no booked
        tab, and they still belong in the calendar."""
        ok = {p.key for p in self.cfg.people if p.age_refusal_ok}
        return {ident for ident, info in self.store.refused().items()
                if ident.partition("|")[0] in ok and is_age_refusal(info.get("message", ""))}

    def _audit_calendars(self, result: RunResult, dry_run: bool) -> None:
        """Every class a person is registered for gets a calendar event, whoever booked it. A class the
        person unregistered from loses its event (the audit decides, and is strict: see calendar_audit.remove_dropped).

        Runs after the booking report and the next wake-up are done, and never breaks the run. A dry run only
        reports. Under the run lock, so a second run cannot add the same event at the same time."""
        if self.cfg.calendar_audit not in ("report", "write"):
            return
        mode = "report" if dry_run else self.cfg.calendar_audit
        entries, complete = [], []
        attending = self._attending_idents()
        for r in result.people:
            # the favorites list and the booked tab (every class the person is registered for), each class once. A class
            # the club refused for age counts as registered for a person who goes anyway (2026-10-19): it gets its event
            cards = [replace(c, state=CardState.REGISTERED) if f"{r.person}|{c.key}" in attending else c for c in r.cards]
            merged = list({c.key: c for c in [*cards, *r.booked]}.values())
            if not r.error and merged:
                entries.append((self.cfg.person(r.person), merged))
            if not r.error and r.booked_complete:         # judged only on a list that was read to its end
                complete.append((self.cfg.person(r.person), merged))
        if not entries and not complete:
            return
        if mode == "write" and self._opening_is_near():     # a read-only report does not hold up a booking
            self.store.log("calendar_audit_deferred", reason="a class opens within 10 minutes: booking comes first")
            return
        removal_error = ""
        try:
            audit = self.audit_factory(mode)
            with self.store.run_lock():
                findings = []
                if complete:         # FIRST the events of dropped classes: one at the same minute would count as the new class's
                    try:
                        findings += list(audit.remove_dropped(complete, keep=attending))
                    except LockBusy:
                        raise
                    except Exception as exc:                  # the events that are added next are still reported
                        removal_error = f"{type(exc).__name__}: {exc}"
                        self.store.log("calendar_audit_failed", error=removal_error, trace=traceback.format_exc())
                if entries:
                    findings += list(audit.check_all(entries, self.cfg.horizon_days))
        except LockBusy:
            self.store.log("calendar_audit_skipped", reason="another run is in progress")
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.store.log("calendar_audit_failed", error=message, trace=traceback.format_exc())
            return                      # in the log (calendar_audit_failed): no reminder, the calendar is no decision
        added = [f"{f.person}|{f.name}|{f.start:%Y-%m-%dT%H:%M}" for f in findings if f.status == "added"]
        if added:
            self.store.log("calendar_audit_added", events=added)
        removed = [f"{f.person}|{f.name}|{f.start:%Y-%m-%dT%H:%M}" for f in findings if f.status == "removed"]
        if removed:
            self.store.log("calendar_audit_removed", events=removed)
        report = format_findings(findings, {p.key: p.first_name for p in self.cfg.people}, mode)
        if report:
            self.out(report[0] + "\n" + report[1])
            if not dry_run:       # the calendar is no decision (the user's rule of 2026-10-15): the log, not the phone
                self.store.log("calendar_audit_report", title=report[0], body=report[1])

    def rearm(self, errored: Collection[str] = ()) -> None:
        """Make the opening agents match the plan: one persistent agent for EVERY future opening of any class of the
        three people, so the program wakes at each opening time and never before it. There is no timer.

        A class that is due but not booked (a failed run, a locked Mac, a logged-out app) gets a retry agent a few
        minutes later, at most MAX_RETRIES times; the failure counter (see _give_up_on_failing) ends it earlier.
        Nothing is armed before `install`: the user approves the launchd agents once, and a manual run must not
        add one."""
        if not self.agents.is_installed():
            self.store.log("rearm_skipped", reason="the scheduler is not installed")
            return
        now = self.now_fn()
        local_now = local_naive(now)
        plan = self.store.load_plan()
        handled = self.store.handled()
        openings = plan.openings if plan else ()
        times = [local_naive(o.opens_at) for o in openings if o.ident not in handled and o.opens_at > now]
        times += self._answer_wake_times(now)
        leftover = planner.due(openings, now + GRACE, handled)
        if leftover and max(self.store.bump_attempt(f"retry|{o.ident}") for o in leftover) <= MAX_RETRIES:
            failing = any(o.person in errored for o in leftover)
            times.append(local_naive(now + (RETRY_ERRORED if failing else RETRY_SOON)))
        if times:
            self.agents.sync_openings(times, self.running_label, local_now)
        else:
            self.agents.disarm_openings(self.running_label, local_now)
        self.agents.prune(local_now, self.running_label)

    def _answer_wake_times(self, now: datetime) -> List[datetime]:
        """The reminders are two-way, but the program wakes only for openings. While a decision is open it also wakes to
        read the answer and carry it out: 10 minutes after the question, and at 20, 8, 10 and 12 o'clock until 45 minutes
        before the class. A few wake-ups, only while a question is open; they touch the app only when an answer is waiting."""
        out: List[datetime] = []
        for q in self.store.open_questions():
            if q.get("status") not in ("open", "decided") or not q.get("asked"):
                continue
            deadline = datetime.fromisoformat(q["deadline"])
            last = local_naive(deadline - timedelta(minutes=45))
            candidates: List[datetime] = []
            if q.get("asked_at"):
                candidates.append(local_naive(datetime.fromisoformat(q["asked_at"]) + timedelta(minutes=10)))
            day = local_naive(now).date()
            while day <= last.date():
                candidates += [datetime(day.year, day.month, day.day, hour) for hour in (8, 10, 12, 20)]
                day += timedelta(days=1)
            out += [t for t in candidates if local_naive(now) + timedelta(minutes=1) < t <= last]
        return sorted(set(out))

    def _prune_old_reports(self, now: datetime) -> None:
        """Once a day: delete reports older than two days from the Reminders list (the list is for what is new)."""
        if self.store.bump_attempt(f"prune|{now:%Y%m%d}") != 1:
            return
        try:
            self.reporter.prune(2)
        except Exception as exc:
            self.store.log("prune_failed", error=f"{type(exc).__name__}: {exc}")

    def _is_opening_agent(self) -> bool:
        return bool(self.running_label) and self.running_label.startswith(OPENING_PREFIX)

    def _blocked_kind(self) -> Optional[str]:
        """One look at the club app: login, code, locked or update when it cannot be used, else None. Never raises."""
        ax = self.ax_factory()
        if ax is None:
            return None
        try:
            kind = screen_kind(ax.dump())
        except Exception:                      # no window, no permission: this look says nothing
            return None
        return kind if kind in BLOCKED else None

    def _wait_until_usable(self, dry_run: bool, notify: bool) -> int:
        """The agent that was started at an opening time found the Mac locked, or the club app logged out. It stays and
        looks every 15 seconds, up to 20 minutes, and books at once when the user unlocks or logs in (a sleeping display
        is woken). A timer would cost up to its interval, and classes fill in minutes. It stops when nothing is due any
        more (the user registered by hand) or when the program is paused."""
        waited = 0
        while waited < OPENING_WAIT:
            self.sleep(OPENING_POLL)
            waited += OPENING_POLL
            if self.store.paused():
                return 0
            decision = decide_tick(self.store.load_plan(), self.store.handled(), self.now_fn(), self.locked_fn(),
                                   self.idle_fn(), self.cfg.replan_after_hours, min_idle=self.cfg.min_idle_seconds,
                                   force_hours=self.cfg.replan_force_hours)
            if decision.action == "locked" and not self.wake_fn():
                continue
            if decision.action in ("run_due", "replan") and self._blocked_kind() is not None:
                continue
            return self.cmd_tick(dry_run, notify, _waited=True)         # usable, or nothing due any more
        return 0                                                         # still not usable: the retry agent goes on

    def _logged_out_now(self, decision: TickDecision, dry_run: bool = False) -> bool:
        """One look at the club app before a run. When it is logged out (or needs an update) no run is started: a run would
        only move the mouse and fail, five minutes after five minutes, and the failures would drop the class for the day
        (2026-10-14). With `auto_login` on, an opening that finds the login screen first logs in by itself (2026-10-15) and
        goes on at once. If that fails, or it is off, the phone is told once (with the reason); the booking runs by itself
        at the first tick after the login."""
        with self.awake_fn():                    # the look and the login need the display on (2026-10-18)
            kind = self._blocked_kind()
            if kind is None:
                return False
            note = ""
            if self._may_log_in_by_itself(kind, decision, dry_run):
                result = self._log_in_by_itself()
                if result is not None:
                    if result.ok:
                        return False             # logged in: no message, the run goes on in this wake-up
                    note = result.detail or result.status
        names = {p.key: p.first_name for p in self.cfg.people}
        waiting = "; ".join(f"{names.get(o.person, o.person)} - {o.name}" for o in decision.due)
        self._tell_blocked(kind, waiting, note)
        self.store.log("tick", action="logged_out_wait", kind=kind, persons=list(decision.persons))
        return True

    def _give_up_on_failing(self, decision: TickDecision, result: RunResult) -> None:
        """A person whose run failed max_person_failures times today is left alone for the day: their due openings
        count as handled and the user is told once. Without this, a failure that does not go away would make the
        program run, and message, every few minutes."""
        day = f"{self.now_fn():%Y%m%d}"
        given_up: List[str] = []
        for r in result.people:
            if not r.error or r.error in (LOGGED_OUT, UPDATE_NEEDED, USER_ACTIVE, WINDOW_HIDDEN):
                continue                       # a problem only the user can fix is not a reason to drop the class
            if self.store.bump_attempt(f"fail|{r.person}|{day}") < self.cfg.max_person_failures:
                continue
            mine = [o for o in decision.due if o.person == r.person]
            for o in mine:
                self.store.mark_handled(o.ident)
            if mine:
                given_up.append(f"{self.cfg.person(r.person).first_name}: {r.error}")
        if given_up:
            self._tell_once("Club booking: giving up for today",
                            "\n".join(given_up) + "\nThe classes that opened were not booked. "
                            "Fix the problem, then run: python -m club_booking run")

    # -- commands ------------------------------------------------------------------------------
    def cmd_run(self, keys: Sequence[str], dry_run: bool, countdown: bool) -> int:
        if self.store.paused():
            self.out("Paused: remove the file PAUSE in the data folder to continue.")
            return 0
        if self.locked_fn():
            self.out("The Mac is locked. Unlock it and run again.")
            return 1
        persons = [self.cfg.person(k) for k in keys] if keys else list(self.cfg.people)
        try:
            self.do_run(persons, dry_run, countdown, guard=False, phone=False)       # the user started it: he sees the console
        except LockBusy:
            self.out("Another run is in progress.")
            return 0
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.out(f"The run stopped: {message}")
            self.store.log("run_failed", error=message, trace=traceback.format_exc())
            return 1
        return 0

    def cmd_tick(self, dry_run: bool, notify: bool, _waited: bool = False) -> int:
        """What the scheduler runs. Nothing may escape: a crash would repeat at every tick without a word."""
        try:
            return self._tick(dry_run, notify, _waited)
        except Exception as exc:
            return self._tick_crashed(exc, {p.key for p in self.cfg.people})

    def _tick_crashed(self, exc: Exception, persons: Collection[str]) -> int:
        """Log and tell once a day. Do not re-arm for a minute later: the 5-minute tick tries again."""
        message = f"{type(exc).__name__}: {exc}"
        try:
            self.store.log("run_failed", error=message, trace=traceback.format_exc())
            self._tell_once("Club booking problem", message)
        except Exception:
            pass
        try:
            self.rearm(persons)
        except Exception:
            pass
        return 1

    def _tick(self, dry_run: bool, notify: bool, waited: bool = False) -> int:
        now = self.now_fn()
        if self.store.paused():
            return 0
        plan, handled = self.store.load_plan(), self.store.handled()
        if dry_run:
            due = planner.due(plan.openings if plan else (), now + GRACE, handled)
            self.store.log("tick", dry_run=True, due=[o.ident for o in due], plan_created=plan.created_at if plan else None)
            if notify:
                problems = self._wake_test_problems()
                text = (f"The scheduler started the program at {now:%H:%M}. "
                        + ("All permissions are fine." if not problems else "Problems: " + "; ".join(problems)))
                self.store.log("wake_test", problems=problems)       # the answer is a Mac banner, not a reminder on the phone
                try:
                    self.reporter.mac("Club booking: wake-up test", text)
                except AppleScriptError as exc:
                    self.store.log("report_problem", problems=[f"mac: {exc}"])
                if self.running_label != TEST_LABEL:
                    self.agents.remove_test()   # a job must not remove itself while it runs; a later tick does it
            else:
                self.rearm()
            return 0
        decision = decide_tick(plan, handled, now, self.locked_fn(), self.idle_fn(), self.cfg.replan_after_hours,
                                min_idle=self.cfg.min_idle_seconds, force_hours=self.cfg.replan_force_hours)
        if decision.action == "locked" and self.wake_fn():        # a class is due and the display was only asleep
            decision = decide_tick(plan, handled, now, False, self.idle_fn(), self.cfg.replan_after_hours,
                                    min_idle=self.cfg.min_idle_seconds, force_hours=self.cfg.replan_force_hours)
        self.store.log("tick", action=decision.action, persons=list(decision.persons))
        if decision.action == "nothing":
            self._prune_old_reports(now)
            self._process_answers(now)
            self.rearm()
            return 0
        if decision.action == "wait_idle":
            self._process_answers(now)         # a stale plan must not keep an answer unread (2026-10-15 16:03)
            self.rearm()
            return 0
        if decision.action in ("run_due", "replan") and self._logged_out_now(decision, dry_run):
            if self._is_opening_agent() and not waited and not dry_run:
                return self._wait_until_usable(dry_run, notify)        # the user may log in within minutes
            return 0
        if decision.action == "replan":
            tried = self.store.bump_attempt(f"replan|{now:%Y%m%d}")
            if tried > self.cfg.max_replans_per_day:
                self.store.log("tick", action="replan_skipped", reason="the daily limit is reached")
                return 0
        if decision.action == "locked":
            fresh = [o for o in decision.due if self.store.bump_attempt(f"locked|{o.ident}") == 1]
            if fresh:
                lines = [f"{self.cfg.person(o.person).first_name} - {o.name}, {o.start:%a %d/%m %H:%M}"
                         for o in sorted(fresh, key=lambda o: (o.start, o.person))]
                self._tell_daily("locked", "Club booking: the Mac is locked",      # one a day: five openings, five
                                 "The Mac is locked or its screen is off. Open now:\n" + "\n".join(lines)       # reminders on 2026-10-17
                                 + "\nA class that opens while the Mac is locked is missed."
                                 + "\nRegister by hand, or unlock: it books as soon as you unlock.",
                                 kind="locked", persons=sorted({o.person for o in fresh}))
            if self._is_opening_agent() and not waited and not dry_run:
                return self._wait_until_usable(dry_run, notify)
            return 0
        persons = ([self.cfg.person(k) for k in decision.persons] if decision.action == "run_due"
                   else list(self.cfg.people))
        # An opening is urgent. A refresh is not and stands back when the user comes back, unless the plan is a week old
        # (replan_force_hours): nothing else wakes the program when the plan has run out, and classes would be missed.
        overdue = plan is not None and now - plan.created_at > timedelta(hours=self.cfg.replan_force_hours)
        urgent = decision.action == "run_due" or overdue
        retry_login = decision.action == "run_due" and self.cfg.auto_login and self.auto_login_factory is not None
        try:
            result = self.do_run(persons, dry_run=False, countdown=urgent, urgent=urgent, tell_blocked=not retry_login)
            if retry_login:
                result, again = self._after_logged_out_run(decision, persons, result, urgent)
                if again:
                    return self._wait_until_usable(False, notify) if self._is_opening_agent() and not waited else 0
        except LockBusy:
            self.store.log("tick_busy")
            return 0
        except Exception as exc:
            # The whole run failed (the screen tool, the Keychain, ...). It counts against everyone it was for.
            failed = RunResult("failed", [PersonResult(p.key, error=f"{type(exc).__name__}: {exc}") for p in persons])
            code = self._tick_crashed(exc, {p.key for p in persons})
            try:
                self._give_up_on_failing(decision, failed)
            except Exception:
                pass
            return code
        if result.people and all(r.error == USER_ACTIVE for r in result.people):
            if decision.action == "replan":
                self.store.drop_attempt(f"replan|{now:%Y%m%d}")      # standing back does not use up a refresh
            return 0
        self._give_up_on_failing(decision, result)
        if decision.action == "run_due" and not waited:
            self._refresh_plan_when_quiet()
        return 0

    def _after_logged_out_run(self, decision: TickDecision, persons: Sequence[Person], result: RunResult,
                              urgent: bool) -> Tuple[RunResult, bool]:
        """A run that finds the app logged out halfway (the look before it saw a normal screen: 2026-10-20 and 21, three
        times) logs in by itself and runs again for those people, at once: a class that opens fills in minutes, and the
        next retry comes 5 to 12 minutes later. Nothing has been told to the phone yet (do_run was told not to).
        Returns (the result to go on with, True when the app is still blocked: the phone was told and the caller waits)."""
        blocked = {r.person for r in result.people if r.error in (LOGGED_OUT, UPDATE_NEEDED)}
        if not blocked:
            return result, False
        if self._logged_out_now(decision):
            return result, True                        # the login failed or is not allowed: told once, the opening agent waits
        again = self.do_run([p for p in persons if p.key in blocked], dry_run=False, countdown=False, urgent=urgent)
        merged = RunResult(again.run_id, [r for r in result.people if r.person not in blocked] + list(again.people),
                           again.plan or result.plan, result.warnings)
        return merged, False

    def _refresh_plan_when_quiet(self) -> None:
        """No timer wakes the program to refresh its plan. It does it here, right after an opening run: when the plan is
        old and the user has been away long enough (or the plan is a week old). Never raises."""
        try:
            now = self.now_fn()
            decision = decide_tick(self.store.load_plan(), self.store.handled(), now, self.locked_fn(), self.idle_fn(),
                                   self.cfg.replan_after_hours, min_idle=self.cfg.min_idle_seconds,
                                   force_hours=self.cfg.replan_force_hours)
            if decision.action == "replan":
                self._tick(False, False, True)         # the same limits (twice a day), the same checks
        except Exception as exc:
            self.store.log("refresh_failed", error=f"{type(exc).__name__}: {exc}")

    def _wake_test_problems(self) -> List[str]:
        """What a job started by the scheduler cannot do. Its permissions are not Terminal's."""
        problems = check_mirroir_permissions(resolve(self.cfg, self.cfg.mirroir_binary))
        ax = self.ax_factory()
        if ax is not None:
            try:
                ax.dump()
            except AxError as exc:
                problems.append(f"Accessibility: {exc}")
        try:
            names = calendar_names()
            problems += [f"calendar {p.calendar!r} not found" for p in self.cfg.people if p.calendar not in names]
        except AppleScriptError as exc:
            problems.append(f"Calendar: {exc}")
        try:
            get_api_key(self.cfg.keychain_service)       # a failing read would stop every due run before the first tap
        except SecretError as exc:
            problems.append(f"Keychain: {exc}")
        club = self.club_app_factory()
        if club is not None:
            reason = club.can_control()
            if reason:
                problems.append(f"club app: {reason}")
        if self.cfg.auto_login:            # the automatic login needs these two, and Terminal's permissions do not count here
            inbox = self.inbox_factory() if self.inbox_factory else None
            if inbox is not None:
                try:
                    inbox.probe(self.now_fn())
                except SmsError as exc:
                    problems.append(f"Messages: {exc}")
            reason = system.system_events_problem()
            if reason:
                problems.append(f"System Events: {reason}")
        return problems

    def cmd_status(self) -> int:
        now = self.now_fn()
        plan, handled = self.store.load_plan(), self.store.handled()
        self.out(f"Now: {now:%a %d/%m %H:%M}   Paused: {self.store.paused()}   Mac locked: {self.locked_fn()}")
        if plan is None:
            self.out("Plan: none yet. Run: python -m club_booking run")
        else:
            nxt = planner.next_opening(plan.openings, now, handled)
            self.out(f"Plan made {plan.created_at:%a %d/%m %H:%M}; {len(plan.openings)} openings; "
                     f"stale: {plan_is_stale(plan, now, self.cfg.replan_after_hours)}")
            if nxt:
                who = self.cfg.person(nxt.person).first_name if any(p.key == nxt.person for p in self.cfg.people) else nxt.person
                self.out(f"Next opening: {nxt.opens_at:%a %d/%m %H:%M} ({who} - {nxt.name}, class at {nxt.start:%a %d/%m %H:%M})")
        labels = self.agents.opening_labels()
        when = [t for t in (label_time(x) for x in labels) if t is not None]
        self.out(f"Armed wake-ups: {len(labels)}" + (" (next: " + ", ".join(f"{t:%a %d/%m %H:%M}" for t in sorted(when)[:4]) + ")"
                                                      if when else ""))
        return 0

    def cmd_wake_command(self) -> int:
        plan = self.store.load_plan()
        now, handled = self.now_fn(), self.store.handled()
        times = sorted({local_naive(o.opens_at) for o in plan.openings
                        if o.ident not in handled and o.opens_at > now}) if plan else []
        if not times:
            self.out("No opening is planned.")
            return 1
        self.out("To wake the Mac from sleep for every planned opening, run this once (it needs an administrator; "
                 "a woken Mac that is locked still needs your unlock):")
        self.out(wake_commands(times))
        return 0

    def cmd_login_request(self, phone: str) -> int:
        """Supervised login, step 1: fill the number and ask for the code. The guard allows 2 requests in 10 minutes."""
        if not phone:
            self.out("No phone number. Pass --phone, or store it once: "
                     f"security add-generic-password -a \"$USER\" -s {self.cfg.phone_keychain_service} -w")
            return 1
        try:
            result = self.login_factory().request_code(phone)
        except LoginError as exc:
            self.out(f"Login stopped: {exc}")
            return 1
        if result == "code_screen":
            self.out("The code was sent. Read the 6 digits from the SMS, then run: python -m club_booking login code <digits>")
            return 0
        self.out("The code screen did not appear. The request was counted. Look at the app before you try again.")
        return 1

    def cmd_login_auto(self) -> int:
        """The whole automatic login, started by hand (also before `auto_login` is switched on in the config): one code
        request, the code read from the Messages, typed once. The same limits as the unattended one."""
        if self.store.paused():
            self.out("Paused: remove the file PAUSE in the data folder to continue.")
            return 1
        if self.locked_fn():
            self.out("The Mac is locked or its screen is off: nothing can be typed. Unlock it and try again.")
            return 1
        if self.auto_login_factory is None:
            self.out("The automatic login is not available.")
            return 1
        try:
            with self.awake_fn():
                result = self.auto_login_factory(self._attention(urgent=True)).run()
        except Exception as exc:
            self.out(f"Login stopped: {type(exc).__name__}: {exc}")
            return 1
        if result.ok:
            self.out("Logged in.")
            return 0
        self.out(f"The automatic login did not work ({result.status}): {result.detail}")
        return 1

    def cmd_login_code(self, code: str) -> int:
        """Supervised login, step 2: type the 6 digits."""
        try:
            result = self.login_factory().enter_code(code)
        except LoginError as exc:
            self.out(f"Login stopped: {exc}")
            return 1
        self.out("Logged in." if result == "logged_in" else "The app did not accept the code. Look at it before you try again.")
        return 0 if result == "logged_in" else 1

    def cmd_arm_test(self, minutes: int) -> int:
        when = local_naive(self.now_fn() + timedelta(minutes=minutes))
        self.agents.arm_test(when)
        self.out(f"A test wake-up is armed for {when:%H:%M}. A notification should appear then. "
                 f"Lock the Mac or let it sleep to test that case too.")
        return 0

    def cmd_prune_reports(self, days: int) -> int:
        self.out(f"Removed {self.reporter.prune(days)} old reminders from the list {self.cfg.reminders_list!r}.")
        return 0

    def cmd_doctor(self) -> int:
        checks: List[Tuple[bool, str]] = []

        def check(ok: bool, text: str) -> None:
            checks.append((ok, text))

        check(True, f"config loaded: {len(self.cfg.people)} people, data folder {self.cfg.home}")
        try:
            get_api_key(self.cfg.keychain_service)
            check(True, "API key found in the Keychain")
        except SecretError as exc:
            check(False, str(exc))
        for name, path in (("screen tool", resolve(self.cfg, self.cfg.mirroir_binary)),
                           ("OCR helper", resolve(self.cfg, self.cfg.ocr_binary))):
            check(path.exists(), f"{name}: {path}" + ("" if path.exists() else "  (build it with the script in scripts/)"))
        problems = check_mirroir_config(Path.home())
        check(not problems, "screen tool settings are fine" if not problems else "; ".join(problems))
        permission_problems = check_mirroir_permissions(resolve(self.cfg, self.cfg.mirroir_binary))
        check(not permission_problems, "Screen Recording and Accessibility are allowed" if not permission_problems
              else "; ".join(sorted(set(permission_problems))))
        locked = self.locked_fn()
        check(not locked, "the Mac is unlocked" if not locked else "the Mac is locked (unlock it to test the screen)")
        if not locked:
            try:
                with self.screen_factory() as screen:
                    x, y, w, h = screen.window_region()
                    shot = screen.screenshot()
                check(True, f"club app window found ({w}x{h} at {x},{y}); a screenshot works ({len(shot)} bytes)")
            except ScreenError as exc:
                check(False, f"club app window: {exc}")
        ax = self.ax_factory()
        if ax is not None and not locked:
            try:
                dump = ax.dump()
                check(True, f"accessibility works: {len(dump.elements)} elements, the screen is {screen_kind(dump)!r}")
            except AxError as exc:
                check(False, f"accessibility: {exc}")
        try:
            names = calendar_names()
            for p in self.cfg.people:
                check(p.calendar in names, f"calendar {p.calendar!r} for {p.first_name}")
        except AppleScriptError as exc:
            check(False, f"Calendar: {exc}")
        if self.cfg.auto_login:
            self._doctor_auto_login(check)
        for ok, text in checks:
            self.out(("OK    " if ok else "FAIL  ") + text)
        return 0 if all(ok for ok, _ in checks) else 1

    def _doctor_auto_login(self, check: Callable[[bool, str], None]) -> None:
        """The automatic login needs the phone number and the Messages database. Only a count and an age are read."""
        service = self.cfg.phone_keychain_service
        try:
            get_api_key(service)
            check(True, f"phone number found in the Keychain ({service})")
        except SecretError:
            check(False, f"no phone number in the Keychain. Add it once: security add-generic-password -a \"$USER\" -s {service} -w")
        inbox = self.inbox_factory() if self.inbox_factory else None
        if inbox is None:
            check(False, "the Messages reader is not available")
            return
        try:
            count, age = inbox.probe(self.now_fn())
        except SmsError as exc:
            check(False, f"{exc}. Allow the Python.app at {python_app_path()}")
            return
        sender = self.cfg.sms_sender
        try:
            last = inbox.last_message_age(self.now_fn())
        except SmsError:
            last = None
        if last is None or last > PAIRING_STALE_DAYS * 86400:
            check(False, f"the Mac has received no message for {'ever' if last is None else str(int(last // 86400)) + ' days'}: "
                         "the iPhone and the Mac are not paired. iPhone: Settings > Apps > Messages > Text Message "
                         "Forwarding: switch the Mac off and on, then restart Messages on the Mac")
        else:
            check(True, f"the Mac receives messages (the newest is {_age_text(last)} old)")
        if not count:
            check(True, f"the Messages database can be read; no message from {sender} yet "
                        "(the first login SMS shows whether Text Message Forwarding works)")
            return
        check(True, f"the Messages database can be read; {count} messages from {sender}, the newest {_age_text(age)} old")


def find_repo() -> Path:
    return Path(__file__).resolve().parent.parent


def build_app(cfg: Config) -> App:
    store = Store(cfg.home)
    mirroir = resolve(cfg, cfg.mirroir_binary)
    ocr = resolve(cfg, cfg.ocr_binary)
    agents = LaunchAgents(Path(sys.executable), cfg.repo, cfg.home / "launchd.log")
    ax = AxClient(resolve(cfg, cfg.ax_binary), cfg.club_app_process)
    club_app = ClubApp(cfg.club_app_name, cfg.club_app_process)

    def read_field() -> str:
        """The text of the phone field, read from the screen (accessibility does not show it)."""
        with MirroirScreen([str(mirroir)]) as screen:
            hits = ocr_png(ocr, screen.screenshot())
        return " ".join(h.text for h in hits if 240 <= h.y <= 290)

    def reader():
        vision = Vision(make_client(get_api_key(cfg.keychain_service)), cfg.model, ZoneInfo(cfg.timezone))
        return AxReader(ax, vision, ZoneInfo(cfg.timezone)) if cfg.reader == "ax" else vision

    def inbox() -> MessagesInbox:
        return MessagesInbox(cfg.messages_db, cfg.sms_sender)

    def auto_login(attention: Attention) -> AutoLogin:
        """The typing of the code goes through the attention guard: an urgent run waits for a pause in the user's typing."""
        def typing(text: str, clear: bool) -> bool:
            attention.checkpoint()
            done = system.type_into(cfg.club_app_name, cfg.club_app_process, text, clear)
            attention.note()
            return done
        helper = LoginHelper(ax, LoginGuard(cfg.home / "login.json"), read_field=read_field, type_into=typing)
        return AutoLogin(helper, inbox(), LoginGuard(cfg.home / "autologin-hour.json", 2, timedelta(minutes=45)),
                         LoginGuard(cfg.home / "autologin-day.json", 4, timedelta(hours=24)), cfg.home / "autologin.json",
                         phone=lambda: get_api_key(cfg.phone_keychain_service), log=store.log,
                         screen=lambda: screen_kind(ax.dump()), wait=float(cfg.sms_wait_seconds))

    return App(
        cfg, store, Reporter(cfg.reminders_list), agents,
        screen_factory=lambda: MirroirScreen([str(mirroir)]),
        vision_factory=reader,
        calendar_factory=lambda screen: CalendarService(screen, cfg.coords, lambda png: ocr_png(ocr, png)),
        running_label=os.environ.get("XPC_SERVICE_NAME"),
        club_app_factory=lambda: club_app,
        login_factory=lambda: LoginHelper(
            ax, LoginGuard(cfg.home / "login.json"), read_field=read_field,
            type_into=lambda text, clear: system.type_into(cfg.club_app_name, cfg.club_app_process, text, clear)),
        ax_factory=(lambda: ax) if cfg.reader == "ax" else (lambda: None),
        audit_factory=lambda mode: CalendarAudit(store, cfg.people, mode=mode),
        auto_login_factory=auto_login, inbox_factory=inbox,
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="club_booking", description="Books gym classes in the club app.")
    p.add_argument("--config", type=Path, help="config file (default: config.json in the repo folder)")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="read the favorites of everyone now and book what is open")
    run.add_argument("--person", action="append", default=[], help="only this person key (can repeat)")
    run.add_argument("--dry-run", action="store_true", help="read and report, but book nothing")
    run.add_argument("--no-countdown", action="store_true", help="skip the warning before the mouse moves")
    tick = sub.add_parser("tick", help="what the scheduler runs: book what has opened, or re-plan")
    tick.add_argument("--dry-run", action="store_true")
    tick.add_argument("--notify", action="store_true", help="show a Mac notification (used by arm-test)")
    for name, text in (("doctor", "check the setup"), ("status", "show the plan and the armed wake-ups"),
                       ("answers", "read the answers to the questions on the iPhone and act on them"),
                       ("install", "install the safety tick and arm the next opening"),
                       ("uninstall", "remove all launchd agents"), ("wake-command", "print the command that wakes the Mac"),
                       ("init-mirroir", "create the screen tool settings if missing")):
        sub.add_parser(name, help=text)
    login = sub.add_parser("login", help="login of the club app: step by step, or `auto` (a scheduled run logs in by itself only when auto_login is on)")
    steps = login.add_subparsers(dest="step", required=True)
    request = steps.add_parser("request", help="fill the phone number and ask for the SMS code (once)")
    request.add_argument("--phone", default="", help="the phone number (default: from the Keychain)")
    steps.add_parser("auto", help="the whole login by itself: ask for the code once, read the SMS from the Messages, type it")
    code = steps.add_parser("code", help="type the 6-digit code")
    code.add_argument("code")
    sub.add_parser("setup", help="guided setup: people, calendars, phone number and key; writes config.json")
    arm = sub.add_parser("arm-test", help="arm a one-time test wake-up")
    arm.add_argument("--in-minutes", type=int, default=2)
    prune = sub.add_parser("prune-reports", help="delete old reminders in the reports list")
    prune.add_argument("--days", type=int, default=7)
    return p


def run_setup(repo: Path, config_path: Path) -> int:
    """The guided setup (`setup`): asks, writes config.json, stores the phone number and the key in the Keychain."""
    import getpass
    from .ax_reader import selector_rows
    from .wizard import Wizard

    def find_rows() -> List[Tuple[str, int]]:
        try:
            return selector_rows(AxClient(repo / "tools" / "bin" / "ax", "Runner").dump())
        except Exception:                      # the club app is not open or not on the selector: the default rows are used
            return []

    def store(service: str, value: str) -> None:
        try:
            store_secret(service, value)
        except SecretError as exc:
            print(f"Not stored: {exc}")

    return Wizard(ask=input, secret=getpass.getpass, out=print, calendars=calendar_names, store_secret=store,
                  find_rows=find_rows, config_path=config_path).run()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    repo = find_repo()
    if args.command == "setup":
        return run_setup(repo, args.config or repo / "config.json")
    if args.command == "init-mirroir":
        made = init_mirroir_config(Path.home())
        print("created: " + ", ".join(made) if made else "nothing to do: both files exist")
        return 0
    config_path = args.config or repo / "config.json"
    try:
        cfg = load_config(config_path, repo)
    except ConfigError as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
        return 2
    app = build_app(cfg)
    if args.command == "login":
        if args.step == "code":
            return app.cmd_login_code(args.code)
        if args.step == "auto":
            return app.cmd_login_auto()
        phone = args.phone
        if not phone:
            try:
                phone = get_api_key(cfg.phone_keychain_service)
            except SecretError:
                phone = ""
        return app.cmd_login_request(phone)
    if args.command == "run":
        return app.cmd_run(args.person, args.dry_run, not args.no_countdown)
    if args.command == "tick":
        return app.cmd_tick(args.dry_run, args.notify)
    if args.command == "doctor":
        return app.cmd_doctor()
    if args.command == "answers":
        return app.cmd_answers()
    if args.command == "status":
        return app.cmd_status()
    if args.command == "install":
        app.agents.install_tick()
        app.rearm()
        print("Installed. Check with: python -m club_booking status")
        return 0
    if args.command == "uninstall":
        app.agents.uninstall()
        print("Removed all club-booking launchd agents.")
        return 0
    if args.command == "wake-command":
        return app.cmd_wake_command()
    if args.command == "arm-test":
        return app.cmd_arm_test(args.in_minutes)
    if args.command == "prune-reports":
        return app.cmd_prune_reports(args.days)
    return 2
