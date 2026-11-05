"""Updates the club app through the App Store (2026-11-05: the user asked for it).

The club app can show a popup, "a new version is available, update to continue", and then it cannot be logged in or used.
The program opens the app's page in the App Store and presses "Update" ONCE, only when the page offers it. The page was
read on the real App Store (English, macOS 27): one button in the details list, "Open" when the app is current,
"Update" when a newer version exists (the "Update" label is from the field notes of 2026-10-15; this step has not been
tried against a real update yet). While the update runs the button changes (a progress state); when it says "Open"
again, the update is done. A password or Touch ID prompt of the Mac is not part of this window: the wait then ends in
"timeout" and the person must confirm it.

The program never presses anything else in the App Store, and never repeats the press.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

PAGE_URL = "macappstore://apps.apple.com/app/id{}"
# The labels the main button of an app page can have. The sidebar item "Updates" is not one of them (exact match).
BUTTON_LABELS = ("Update", "Open", "Get", "Install", "Cancel", "Pause", "Resume", "Waiting", "Loading", "Redownload")


@dataclass(frozen=True)
class UpdateResult:
    status: str      # updated | up_to_date | no_page | no_button | timeout | failed
    detail: str = ""

    @property
    def updated(self) -> bool:
        return self.status == "updated"


class AppStoreUpdater:
    def __init__(self, ax, open_url: Callable[[str], object], store_id: int,
                 sleep: Callable[[float], None] = time.sleep, close: Callable[[], None] = lambda: None,
                 page_wait: float = 30.0, update_wait: float = 900.0, poll: float = 3.0) -> None:
        """ax: the accessibility client of the process "App Store". open_url(url) opens the page. close() quits the App
        Store again (best effort)."""
        self.ax, self.open_url, self.store_id = ax, open_url, store_id
        self.sleep, self.close = sleep, close
        self.page_wait, self.update_wait, self.poll = page_wait, update_wait, poll

    def run(self) -> UpdateResult:
        """Never raises."""
        try:
            return self._run()
        except Exception as exc:                     # the helper, the system: anything unexpected is a failure, not a crash
            return UpdateResult("failed", f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self.close()
            except Exception:
                pass

    def _button(self) -> Optional[Tuple[str, int]]:
        """(label, middle line) of the main button of the app page, or None (no window yet, or no page yet)."""
        try:
            dump = self.ax.dump()
        except Exception:
            return None
        for e in dump.elements:
            if e.role == "AXButton" and e.desc in BUTTON_LABELS and e.h > 0:
                return e.desc, int(e.y + e.h / 2)
        return None

    def _wait_for_button(self) -> Optional[Tuple[str, int]]:
        waited = 0.0
        while True:
            found = self._button()
            if found or waited >= self.page_wait:
                return found
            self.sleep(self.poll)
            waited += self.poll

    def _run(self) -> UpdateResult:
        self.open_url(PAGE_URL.format(self.store_id))
        found = self._wait_for_button()
        if found is None:
            return UpdateResult("no_page", "the App Store did not show the page of the app")
        label, y = found
        if label == "Open":
            return UpdateResult("up_to_date", "the App Store has no newer version of the app")
        if label != "Update":
            return UpdateResult("no_button", f"the page of the app shows the button {label!r}, not Update")
        self.ax.press("AXButton", "Update", y, 25)      # exactly once, never repeated
        waited = 0.0
        while waited < self.update_wait:
            self.sleep(self.poll)
            waited += self.poll
            now = self._button()
            if now and now[0] == "Open":
                return UpdateResult("updated")
        return UpdateResult("timeout", f"the update did not end in {int(self.update_wait)} seconds "
                                       "(a password or Touch ID prompt of the Mac may wait for a person)")
