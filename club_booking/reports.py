"""Tells the user what happened: a reminder in the list "Club bookings" (it reaches the iPhone
through iCloud) and a notification on the Mac. Only that one list is ever touched."""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Sequence

from .osa import AppleScriptError, osa_for

reminders_osa = osa_for("Reminders")      # starts Reminders first: a scheduled job cannot launch it by itself

# argv: 1 list name, 2 title, 3 body
_POST = '''
on run argv
    set whenDue to (current date) + 5
    tell application "Reminders"
        tell account "iCloud"
            if not (exists list (item 1 of argv)) then make new list with properties {name:(item 1 of argv)}
            set l to list (item 1 of argv)
            make new reminder at end of reminders of l with properties {name:(item 2 of argv), body:(item 3 of argv), due date:whenDue, remind me date:whenDue}
        end tell
    end tell
    return "ok"
end run
'''

# argv: 1 list name, 2 age in days. Deletes reminders of that list older than the age.
_PRUNE = '''
on run argv
    set cutoff to (current date) - ((item 2 of argv) as integer) * days
    set removed to 0
    tell application "Reminders"
        tell account "iCloud"
            if not (exists list (item 1 of argv)) then return "0"
            set rs to every reminder of list (item 1 of argv)
            repeat with i from (count of rs) to 1 by -1
                set r to item i of rs
                if (creation date of r) < cutoff then
                    delete r
                    set removed to removed + 1
                end if
            end repeat
        end tell
    end tell
    return removed as string
end run
'''

# A question is one reminder per option. Each body starts with a tag "[club-booking <id> #<n>]" that finds it again.
# argv: 1 list name, 2 question id, 3 question text, 4... the options (one reminder each)
_ASK = '''
on run argv
    set whenDue to (current date) + 5
    tell application "Reminders"
        tell account "iCloud"
            if not (exists list (item 1 of argv)) then make new list with properties {name:(item 1 of argv)}
            set l to list (item 1 of argv)
            repeat with i from 4 to (count of argv)
                set tagText to "[club-booking " & (item 2 of argv) & " #" & (i - 3) & "] "
                make new reminder at end of reminders of l with properties {name:(item i of argv), body:(tagText & (item 3 of argv)), due date:whenDue, remind me date:whenDue}
            end repeat
        end tell
    end tell
    return "ok"
end run
'''

# argv: 1 list name, 2 question id. One line per option: completed (true or false), a tab, the first line of the body.
_ANSWERS = '''
on run argv
    set tagText to "[club-booking " & (item 2 of argv) & " #"
    set out to ""
    tell application "Reminders"
        tell account "iCloud"
            if not (exists list (item 1 of argv)) then return ""
            set rs to (every reminder of list (item 1 of argv) whose body contains tagText)
            repeat with r in rs
                set bodyText to (body of r) as text
                set out to out & ((completed of r) as text) & tab & (paragraph 1 of bodyText) & linefeed
            end repeat
        end tell
    end tell
    return out
end run
'''

# argv: 1 list name, 2 question id. Deletes the reminders of that question and says how many.
_CLOSE = '''
on run argv
    set tagText to "[club-booking " & (item 2 of argv) & " #"
    set removed to 0
    tell application "Reminders"
        tell account "iCloud"
            if not (exists list (item 1 of argv)) then return "0"
            set rs to (every reminder of list (item 1 of argv) whose body contains tagText)
            repeat with i from (count of rs) to 1 by -1
                delete (item i of rs)
                set removed to removed + 1
            end repeat
        end tell
    end tell
    return removed as text
end run
'''

# argv: 1 list name, 2 message id. Deletes the reminders of that message (its body ends with the tag) and says how many.
_RESOLVE = '''
on run argv
    set tagText to "[club-booking m:" & (item 2 of argv) & "]"
    set removed to 0
    tell application "Reminders"
        tell account "iCloud"
            if not (exists list (item 1 of argv)) then return "0"
            set rs to (every reminder of list (item 1 of argv) whose body contains tagText)
            repeat with i from (count of rs) to 1 by -1
                delete (item i of rs)
                set removed to removed + 1
            end repeat
        end tell
    end tell
    return removed as text
end run
'''

_NOTIFY = '''
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
    return "ok"
end run
'''


class Reporter:
    def __init__(self, list_name: str, osa: Callable = reminders_osa) -> None:
        self.list_name = list_name
        self.osa = osa

    def phone(self, title: str, body: str = "") -> None:
        self.osa(_POST, [self.list_name, title, body])

    def mac(self, title: str, text: str) -> None:
        self.osa(_NOTIFY, [title, text])

    def tell(self, title: str, body: str = "", tag: str = "") -> List[str]:
        """One message, one place: the phone (the Reminders list reaches the Mac too, so a banner on top of it is only
        a second copy). The Mac gets it only when the phone could not. Returns the problems.
        A message with a tag ends with it, so that `resolve` can take the message back when its problem is gone."""
        try:
            self.phone(title, f"{body}\n[club-booking m:{tag}]" if tag else body)
            return []
        except AppleScriptError as exc:
            problems = [f"phone: {exc}"]
        try:
            self.mac(title, body or title)
        except AppleScriptError as exc:
            problems.append(f"mac: {exc}")
        return problems

    def resolve(self, tag: str) -> int:
        """Delete the reminders of a message that was sent with this tag. Returns how many."""
        return int(self.osa(_RESOLVE, [self.list_name, tag]) or 0)

    def prune(self, days: int = 7) -> int:
        return int(self.osa(_PRUNE, [self.list_name, str(days)]) or 0)

    # -- questions: the user answers by marking one option as completed on the iPhone ----------------
    def ask(self, qid: str, text: str, options: Sequence[str]) -> None:
        if len(options) < 2:
            raise ValueError("a question needs at least two options")
        self.osa(_ASK, [self.list_name, qid, text, *options])

    def answers(self, qid: str) -> Dict[int, bool]:
        """Option number (from 1) -> was it marked as completed. Empty when the question is not there."""
        out = self.osa(_ANSWERS, [self.list_name, qid])
        found: Dict[int, bool] = {}
        for line in out.splitlines():
            done, _, body = line.partition("\t")
            tag = re.search(r"\[club-booking " + re.escape(qid) + r" #(\d+)\]", body)
            if tag:
                found[int(tag.group(1))] = done.strip().lower() == "true"
        return dict(sorted(found.items()))

    def close_question(self, qid: str) -> int:
        """Delete the reminders of a question. Returns how many."""
        return int(self.osa(_CLOSE, [self.list_name, qid]) or 0)
