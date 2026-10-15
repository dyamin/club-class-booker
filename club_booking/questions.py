"""Questions on the iPhone (spec R17): one reminder per option; the user marks one as completed.

Each question knows the bookings the program made for it. Only those can ever be cancelled (R18)."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Option:
    label: str                       # the title of the reminder
    keep: Tuple[str, ...] = ()       # idents of the program's bookings that stay when this option is chosen
    book: Tuple[str, ...] = ()       # idents to book first (the other class of an overlap)


@dataclass
class Question:
    id: str
    kind: str                        # several | overlap (a waitlist question existed until 2026-10-14: see cli._read_answers)
    person: str                      # person key
    text: str
    options: List[Option]
    bookings: List[str]              # idents the program made for this question: the only ones it may cancel
    deadline: str                    # ISO time of the earliest class: after it the question is deleted
    status: str = "open"             # open | decided | done | expired
    answer: Optional[int] = None     # the chosen option, from 1
    asked: bool = False              # were the reminders created (a question that could not be sent is sent again)
    asked_at: str = ""               # ISO time they were created: the program wakes 10 minutes later to read the answer
    tries: int = 0                   # how often applying the answer failed before anything was pressed
    result: str = ""                 # what was done about the answer

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Question":
        fields = dict(data)
        fields["options"] = [Option(o["label"], tuple(o.get("keep", ())), tuple(o.get("book", ())))
                             for o in fields["options"]]
        return Question(**fields)


@dataclass(frozen=True)
class Decision:
    cancel: Tuple[str, ...]          # bookings (or waitlist spots) to give up
    book: Tuple[str, ...]            # classes to book first


def new_id() -> str:
    return uuid.uuid4().hex[:6]


def chosen_option(answers: Dict[int, bool]) -> Optional[int]:
    """The option the user completed, only when exactly one was."""
    done = [n for n, ok in answers.items() if ok]
    return done[0] if len(done) == 1 else None


def decide(question: Question, number: int) -> Decision:
    """What the answer asks for. It can only name the question's own bookings."""
    if not 1 <= number <= len(question.options):
        raise ValueError(f"the question has no option {number}")
    chosen = question.options[number - 1]
    cancel = tuple(b for b in question.bookings if b not in chosen.keep)
    return Decision(cancel, tuple(chosen.book))


_TAP = "Tap Mark as Completed on the option you choose. Nothing is cancelled until you do."


def several_question(person: str, name: str, bookings: Sequence[Tuple[str, str]], deadline: datetime) -> Question:
    """The person has several classes in one day: all are booked; which to keep? (R16, R17a)"""
    options = [Option(f"Keep: {label}", keep=(ident,)) for ident, label in bookings]
    options.append(Option("Keep all", keep=tuple(i for i, _ in bookings)))
    lines = "\n".join(f"- {label}" for _, label in bookings)
    return Question(new_id(), "several", person, f"{name} has {len(bookings)} classes on one day, all booked:\n{lines}\n{_TAP}",
                    options, [i for i, _ in bookings], deadline.isoformat())


def overlap_question(person: str, name: str, default: Tuple[str, str], others: Sequence[Tuple[str, str]],
                     deadline: datetime) -> Question:
    """Two classes overlap: the more crowded one is booked; keep it or switch? (R17b, R19)"""
    options = [Option(f"Keep: {default[1]}", keep=(default[0],))]
    options += [Option(f"Switch to: {label}", book=(ident,)) for ident, label in others]
    others_text = "\n".join(f"- {label}" for _, label in others)
    return Question(new_id(), "overlap", person,
                    f"{name} has classes that overlap (the club allows only one). Booked: {default[1]}. Not booked:\n{others_text}\n{_TAP}",
                    options, [default[0]], deadline.isoformat())

