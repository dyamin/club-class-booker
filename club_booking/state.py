"""Files the program keeps: the plan, what was booked, a log, and evidence screenshots."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

from .planner import Opening, Plan


class LockBusy(Exception):
    """Another run holds the lock."""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    max_log_bytes = 5_000_000      # runs.jsonl is moved to runs.jsonl.1 when it grows past this

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "evidence").mkdir(exist_ok=True)

    # -- small JSON helpers -------------------------------------------------
    def _read_json(self, name: str, default: Any) -> Any:
        """A file that cannot be read is set aside, and treated as empty. One bad file must not stop every run."""
        path = self.home / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            path.rename(path.with_name(f"{path.name}.bad-{datetime.now():%Y%m%d-%H%M%S}"))
            self.log("state_file_unreadable", file=name)
            return default

    def _write_json(self, name: str, data: Any) -> None:
        """Write to a temp file that no other writer uses, then move it into place."""
        path = self.home / name
        fd, tmp = tempfile.mkstemp(dir=self.home, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One writer at a time for read-modify-write updates (a tick and a manual run can overlap)."""
        with (self.home / "state.lock").open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield

    # -- the plan -----------------------------------------------------------
    def save_plan(self, plan: Plan) -> None:
        self._write_json("plan.json", {
            "created_at": plan.created_at.isoformat(),
            "openings": [
                {"person": o.person, "card_key": o.card_key, "name": o.name,
                 "start": o.start.isoformat(), "opens_at": o.opens_at.isoformat()}
                for o in plan.openings
            ],
        })

    def load_plan(self) -> Optional[Plan]:
        raw = self._read_json("plan.json", None)
        if raw is None:
            return None
        openings = tuple(
            Opening(o["person"], o["card_key"], o["name"],
                    datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["opens_at"]))
            for o in raw["openings"]
        )
        return Plan(datetime.fromisoformat(raw["created_at"]), openings)

    # -- openings that were handled, and retry counters -----------------------
    def handled(self) -> Set[str]:
        return set(self._read_json("handled.json", []))

    def mark_handled(self, ident: str) -> None:
        with self._locked():
            done = self.handled()
            done.add(ident)
            self._write_json("handled.json", sorted(done))

    def attempt_count(self, ident: str) -> int:
        attempts: Dict[str, int] = self._read_json("attempts.json", {})
        return attempts.get(ident, 0)

    def bump_attempt(self, ident: str) -> int:
        with self._locked():
            attempts: Dict[str, int] = self._read_json("attempts.json", {})
            attempts[ident] = attempts.get(ident, 0) + 1
            self._write_json("attempts.json", attempts)
            return attempts[ident]

    def clear_attempt(self, ident: str) -> None:
        with self._locked():
            attempts: Dict[str, int] = self._read_json("attempts.json", {})
            if attempts.pop(ident, None) is not None:
                self._write_json("attempts.json", attempts)

    def drop_attempt(self, ident: str) -> None:
        """Take one count back (a message that did not arrive is not counted as sent)."""
        with self._locked():
            attempts: Dict[str, int] = self._read_json("attempts.json", {})
            if attempts.get(ident, 0) > 0:
                attempts[ident] -= 1
                self._write_json("attempts.json", attempts)

    # -- classes seen registered: never booked again by the program --------------------
    def seen_registered(self) -> Set[str]:
        return set(self._read_json("seen_registered.json", []))

    def mark_seen_registered(self, ident: str) -> None:
        with self._locked():
            seen = self.seen_registered()
            seen.add(ident)
            self._write_json("seen_registered.json", sorted(seen))

    # -- classes whose calendar event was checked: the audit never adds them again -----------------
    def calendar_done(self) -> Set[str]:
        return set(self._read_json("calendar_done.json", {}))

    def calendar_done_starts(self) -> Dict[str, str]:
        """ident -> start ('YYYY-MM-DDTHH:MM') of every class whose calendar event was checked."""
        return dict(self._read_json("calendar_done.json", {}))

    def unmark_calendar_done(self, ident: str) -> None:
        """The event is gone (the user unregistered): a new registration for the class gets an event again."""
        with self._locked():
            done: Dict[str, str] = self._read_json("calendar_done.json", {})
            if done.pop(ident, None) is not None:
                self._write_json("calendar_done.json", done)

    # -- classes that were missing from the booked tab: ident -> when they were first missing ------------
    def calendar_dropped(self) -> Dict[str, str]:
        return dict(self._read_json("calendar_dropped.json", {}))

    def mark_calendar_dropped(self, ident: str, now: datetime) -> None:
        """Keeps the FIRST time: the second reading is judged against it."""
        with self._locked():
            dropped: Dict[str, str] = self._read_json("calendar_dropped.json", {})
            dropped.setdefault(ident, now.isoformat())
            self._write_json("calendar_dropped.json", dropped)

    def clear_calendar_dropped(self, ident: str) -> None:
        with self._locked():
            dropped: Dict[str, str] = self._read_json("calendar_dropped.json", {})
            if dropped.pop(ident, None) is not None:
                self._write_json("calendar_dropped.json", dropped)

    def mark_calendar_done(self, ident: str, start: str, now: datetime, keep_days: int = 14) -> None:
        """start is 'YYYY-MM-DDTHH:MM'. Entries of classes that ended long ago are dropped."""
        with self._locked():
            done: Dict[str, str] = self._read_json("calendar_done.json", {})
            done[ident] = start
            limit = (now - timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M")
            self._write_json("calendar_done.json", {k: v for k, v in done.items() if v >= limit})

    # -- messages on the phone that the program takes back when their problem is gone ----------------------
    def messages(self) -> Dict[str, Dict[str, Any]]:
        """id -> {kind: problem | locked | login | update, persons, ident (the 'told today' counter), ts}."""
        return dict(self._read_json("messages.json", {}))

    def remember_message(self, mid: str, kind: str, persons: List[str], ident: str, now: datetime,
                         keep_days: int = 3) -> None:
        with self._locked():
            messages: Dict[str, Dict[str, Any]] = self._read_json("messages.json", {})
            messages[mid] = {"kind": kind, "persons": list(persons), "ident": ident, "ts": now.isoformat()}
            limit = now - timedelta(days=keep_days)        # the reminders themselves are pruned after two days
            self._write_json("messages.json", {k: v for k, v in messages.items()
                                               if datetime.fromisoformat(v["ts"]) >= limit})

    def forget_message(self, mid: str) -> None:
        with self._locked():
            messages: Dict[str, Dict[str, Any]] = self._read_json("messages.json", {})
            if messages.pop(mid, None) is not None:
                self._write_json("messages.json", messages)

    # -- questions asked on the iPhone (one reminder per option) -----------------------------------------
    def questions(self) -> Dict[str, Dict[str, Any]]:
        return self._read_json("questions.json", {})

    def save_question(self, question: Dict[str, Any]) -> None:
        with self._locked():
            data = self.questions()
            data[question["id"]] = question
            self._write_json("questions.json", data)

    def update_question(self, qid: str, **fields: Any) -> None:
        with self._locked():
            data = self.questions()
            if qid in data:
                data[qid].update(fields)
                self._write_json("questions.json", data)

    def open_questions(self) -> List[Dict[str, Any]]:
        """Questions still waiting for an answer, or decided but not finished yet."""
        return [q for q in self.questions().values() if q.get("status") in ("open", "decided")]

    # -- classes the user chose by answering a question: the overlap rule lets them through ----------------
    def chosen(self) -> Set[str]:
        return set(self._read_json("chosen.json", []))

    def mark_chosen(self, ident: str) -> None:
        with self._locked():
            data = self.chosen()
            data.add(ident)
            self._write_json("chosen.json", sorted(data))

    def unmark_chosen(self, ident: str) -> None:
        with self._locked():
            data = self.chosen()
            data.discard(ident)
            self._write_json("chosen.json", sorted(data))

    # -- classes the club refused (for example: the age does not fit): never tried again -----------
    def refused(self) -> Dict[str, Dict[str, str]]:
        return self._read_json("refused.json", {})

    def unrefuse(self, ident: str) -> None:
        """The user chose this class in an answer: the club's earlier refusal (for example an overlap) is over."""
        with self._locked():
            data = self.refused()
            if data.pop(ident, None) is not None:
                self._write_json("refused.json", data)

    def mark_refused(self, ident: str, message: str) -> None:
        with self._locked():
            data = self.refused()
            data[ident] = {"message": message, "ts": _now_iso()}
            self._write_json("refused.json", data)

    # -- bookings the program made (needed later to cancel only its own) -------
    def record_booking(self, run_id: str, person: str, card_key: str, name: str, status: str = "confirmed") -> None:
        """status: attempted (written before the tap), confirmed, or unexpected (another class was booked)."""
        with (self.home / "bookings.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "run_id": run_id, "person": person, "card_key": card_key,
                                 "name": name, "status": status}, ensure_ascii=False) + "\n")

    def bookings(self) -> List[Dict[str, Any]]:
        path = self.home / "bookings.jsonl"
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                self.log("bookings_line_unreadable")
        return rows

    def standing_bookings(self) -> Dict[str, Dict[str, Any]]:
        """The program's own bookings and waitlist spots that still stand: ident -> its newest row. The only things
        the program may ever cancel (spec R18). 'confirmed' and 'waitlisted' stand; a later 'cancel_attempted' or
        'cancelled' row ends them."""
        newest: Dict[str, Dict[str, Any]] = {}
        for row in self.bookings():
            newest[f"{row['person']}|{row['card_key']}"] = row
        return {i: r for i, r in newest.items() if r.get("status") in ("confirmed", "waitlisted")}

    def booked_idents(self) -> Set[str]:
        return {f"{b['person']}|{b['card_key']}" for b in self.bookings()}

    # -- log and evidence -----------------------------------------------------
    def log(self, event: str, **fields: Any) -> None:
        record = {"ts": _now_iso(), "event": event, **fields}
        path = self.home / "runs.jsonl"
        try:
            if path.exists() and path.stat().st_size > self.max_log_bytes:
                os.replace(path, path.with_name("runs.jsonl.1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def save_evidence(self, label: str, png: bytes) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.home / "evidence" / f"{stamp}-{safe}.png"
        path.write_bytes(png)
        return path

    def prune_evidence(self, days: int) -> int:
        """Delete evidence pictures older than `days`. Returns how many were removed."""
        limit = time.time() - days * 86400
        removed = 0
        for path in (self.home / "evidence").glob("*.png"):
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    # -- pause switch and run lock ----------------------------------------------
    def paused(self) -> bool:
        return (self.home / "PAUSE").exists()

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        fh = (self.home / "run.lock").open("w")
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusy("another run is in progress")
            yield
        finally:
            fh.close()
