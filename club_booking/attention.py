"""The program shares the Mac with the user: it must not fight for the mouse.

Before every tap or swipe (and before it brings the club app to the front) the program asks `Attention.checkpoint()`.
It knows the user touched the keyboard or the mouse when the time since the last input is shorter than the time since
the program's own last action: the program's own clicks reset the idle time too, so only the difference is the user.

- A non-urgent run (a plan refresh, applying an answer) stands back at once: `UserActive` is raised and the run ends,
  without a failure and without a message.
- An urgent run (a class is opening) waits for a pause in the user's typing, up to `max_wait` seconds, and then goes on:
  a booking must not be lost because the user works.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Optional


class UserActive(BaseException):
    """The user is using the Mac: a non-urgent run stands back.

    It is a BaseException on purpose. The runner has many `except Exception` that mean "this step failed, go on with the
    next one": stepping aside must pass through all of them, and is caught only where a run ends."""


class Attention:
    def __init__(self, idle_fn: Callable[[], float], now_fn: Callable[[], datetime],
                 sleep: Callable[[float], None], patient: bool, quiet: float = 2.0, max_wait: float = 90.0,
                 tolerance: float = 2.0) -> None:
        self.idle_fn, self.now_fn, self.sleep = idle_fn, now_fn, sleep
        self.patient, self.quiet, self.max_wait, self.tolerance = patient, quiet, max_wait, tolerance
        self._last: Optional[datetime] = None          # when the program last acted
        self._held = 0                                  # inside hold(): no checkpoint stops anything
        self._gave_up_waiting = False                   # an urgent run that waited in vain does not wait again

    def _touched(self) -> bool:
        idle = self.idle_fn()
        if self._last is None:
            return idle < self.quiet                  # the first action: the user must be quiet
        since = (self.now_fn() - self._last).total_seconds()
        return idle + self.tolerance < since          # input arrived after the program's own last action

    def checkpoint(self) -> None:
        """Call before an action that moves the mouse or takes the front window."""
        if self._held or self._gave_up_waiting or not self._touched():
            return
        if not self.patient:
            raise UserActive("the user is using the Mac")
        waited = 0.0
        while waited < self.max_wait:                 # wait for a pause, then go on whatever happens
            self.sleep(1.0)
            waited += 1.0
            if self.idle_fn() >= self.quiet:
                return
        self._gave_up_waiting = True                  # the user never paused: the booking goes on, the same for the rest

    @contextmanager
    def hold(self) -> Iterator[None]:
        """A step that must be finished once it has begun (a booking after its register tap): no stop inside."""
        self._held += 1
        try:
            yield
        finally:
            self._held -= 1

    def note(self) -> None:
        """Call right after the action."""
        self._last = self.now_fn()


class GuardedScreen:
    """The screen tool, with a checkpoint before every action that moves the mouse. Pictures do not disturb the user."""

    def __init__(self, screen: Any, attention: Attention) -> None:
        self._screen, self._attention = screen, attention

    def tap(self, *args: Any, **kwargs: Any) -> Any:
        self._attention.checkpoint()
        result = self._screen.tap(*args, **kwargs)
        self._attention.note()
        return result

    def swipe(self, *args: Any, **kwargs: Any) -> Any:
        self._attention.checkpoint()
        result = self._screen.swipe(*args, **kwargs)
        self._attention.note()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._screen, name)
