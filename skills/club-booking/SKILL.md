---
name: club-booking
description: Use when the user asks about the club booking program (the one that books gym classes in the Hasmonaim club app "Runner 2" for Alex, Sam and Robin), for example to check or review a run, add or check favorites, log the app in, answer or repair a stuck step, change settings, or remove the setup.
---

# club-booking: operate, review, repair

The program lives in the folder where it was installed (the repo `club-class-booker`). It books each person's favorite classes 24 hours before they start, adds the calendar events, tells the iPhone, asks questions on the iPhone, and wakes itself with launchd. **The daily runs do not need Claude.** This skill is for when a person and Claude are together.

Run everything from the repo folder with the virtual environment: `.venv/bin/python -m club_booking <command>`.

## First, read these (they cost two minutes and prevent the mistakes we already made)

- `docs/lessons.md`: every problem, its cause, and the rule that stops it.
- `docs/club-app-field-notes.md`: what the real app looks like (screens, texts, positions, login, update, the booked tab, the class page).
- `README.md`: the commands and the settings.

## Rules for the real app and the real accounts

1. **A counted or irreversible action runs exactly once.** A login code request (3 in 10 minutes lock the login for 10 minutes), a booking tap, a cancel. Keep the result in a variable. Never put such a call inside a retry loop, a `print(...)` line, or a "to be sure" repeat.
2. **Use the program's guarded commands, not ad-hoc scripts:** `python -m club_booking login request` and `login code <6 digits>` for the login, or `login auto` (the whole login: it reads the SMS from the Mac's Messages). `login request` and the automatic login refuse a third request in 10 minutes. **Do not read the Messages database yourself** (a `sqlite3` call on `chat.db`): the user's tool permission refuses it as private data, and only the program's `MessagesInbox` may read it, for one sender.
3. **One action, then look, then decide.** Take a screenshot or `tools/bin/ax dump Runner`. Do not stack blind actions, especially while the user also uses the Mac. **The user comes first:** before a real click, check `idle_seconds()` (`club_booking.system`). If it is near 0 the user is at the Mac: do not click. The program itself does this before every click (`attention.py`); a run that stood back is logged as `stood_back` and is no failure.
4. **Typing goes to the front app.** Check the front app right before typing. The program's `type_into` does this in one step.
5. **Never cancel anything the program did not book itself.** The only cancel is `Runner.cancel`, after the user answered a question. A class the user registered by hand is never touched. Never tap a heart (favorite) unless the user asks for it in so many words.
6. **The screen must be on.** A locked Mac, or a Mac whose display is asleep, cannot be read or clicked.
7. **Pause everything:** `touch ~/Library/Application\ Support/club-booking/PAUSE` (remove the file to continue). Wakes and `run` stop at once.
8. Write a fake mode and a test that fails first for every real defect, then fix it (`tests/fake_app.py`, `tests/fake_mirroir.py`). Edit scripts are all-or-nothing: check every anchor before writing.

## Look at the state

| What | Command or file |
|---|---|
| Is everything set up? | `python -m club_booking doctor` (every line must say OK) |
| Plan, next opening, armed wake-ups | `python -m club_booking status` |
| What the last runs did | `tail ~/Library/Application\ Support/club-booking/runs.jsonl` (events: `tick`, `run`, `person_failed`, `logged_out`, `stood_back`, `calendar_audit_*` (`_added`, `_removed`, `_failed`), `question_*`) |
| What the program booked, joined, cancelled | `bookings.jsonl` in the same folder (`attempted`, `confirmed`, `waitlisted`, `cancel_attempted`, `cancelled`) |
| Pictures of each confirmation | `evidence/` in the same folder |
| What the phone was told | the Reminders list "Club bookings" |
| launchd | `launchctl list \| grep club-booking`; `launchd.log` |

## Common jobs

**Review a run.** Read the last `run` and `person_failed` lines in `runs.jsonl`, the phone reminders, and `bookings.jsonl`. A `locked` message means the Mac was locked or its screen was off: the user got a reminder naming the class and may have registered by hand (the program then skips it).

**The app is logged out.** With `auto_login` on, the program logs in by itself at an opening (README, "Automatic login"; the user decided this on 2026-10-15, so never remove it as a violation of the old rule "an unattended run never logs in"). If it fails, the phone says why (usually no SMS: Text Message Forwarding or Full Disk Access). Otherwise (the program tells the phone; a session lasts a few hours) log in with the guarded commands, one step at a time:
1. `python -m club_booking login auto`: the whole login. It asks for one code, reads the SMS from the Mac's Messages (Text Message Forwarding), and types it. Needs the phone number in the Keychain (`club-booking-phone`) and Full Disk Access for the Python app (`doctor` says if something is missing).
2. Without Messages on the Mac: `python -m club_booking login request` once, read the 6 digits on the iPhone, `python -m club_booking login code <6 digits>` once. The user's number is in the Keychain or in his own notes, never in the repo.
3. **iPhone Mirroring is not used any more** (the Mac gets the SMS). Never read `chat.db` yourself: only the program's `MessagesInbox` does, for one sender, and `doctor` tells you if the newest message is old (a broken pairing).

**Reminders that stay on the phone.** A message about a problem is deleted by the program when the problem is gone (`message_taken_back` in the log; `message_not_taken_back` when the phone did not answer). The tag `[club-booking m:...]` on its last line finds it. If a stale one is left, delete it by hand in the list "Club bookings": it is only a message.

**The favorites are a dynamic list.** The program reads them at every wake-up for a class and merges them into the plan; it never wakes only to test them. Do not run a refresh just to check the favorites (it takes the mouse). A class the user says is gone may be taken out of the plan by hand (`Store.save_plan` without that opening, then `App.rearm()`, and log `plan_edited`); never remove an opening on a guess: a wasted wake-up costs two minutes, a missed class costs the class.

**A full class.** The program joins the waitlist and keeps the spot, with no question and no phone message (the user's decision of 2026-10-14; `waitlisted` in `bookings.jsonl`). Never remove a waitlist spot for the user: they do it in the club app.

**A class the user unregistered.** The program does not book that date again, and it deletes the calendar event it made, after two readings 30 minutes apart (`calendar_audit_removed` in the log). Never delete a calendar event by hand-made script: Calendar scripting cannot delete a repeating event (it does nothing, without an error), and the program's own delete refuses a series. The rules are in `club_booking/calendar_audit.py` (`remove_dropped`).

**What the phone gets.** Only a decision (questions) or an action (a class not booked, the locked Mac, a logged-out app, giving up, a crash). Never a confirmation, a waitlist join, a calendar message or the wake-up test (the user's rule of 2026-10-15). Everything else is in `runs.jsonl`. Do not add a phone message for information.

**Answers to questions.** The user answers on the iPhone by completing one reminder. The program acts on it the next time it wakes; `python -m club_booking answers` does it now.

**Run now.** `python -m club_booking run` books what is open now. `run --dry-run` reads and reports only.

**Add favorites for a person.** Favorites are the program's source of truth and the program never changes them. With the user's explicit request, look at the card's buttons first (`tools/bin/ax dump Runner`): the "add to favorites" button of a card on the schedule tab ("הוסף למועדפים") is pressed through accessibility by its exact text. This has not been tried yet: check the result in the Favorites tab and write the facts into the field notes.

**The app changed its screens** (a new version, new texts). Take a dump (`tools/bin/ax dump Runner > new.json`), compare it with `tests/fixtures/*.json`, change the constants in `club_booking/ax_reader.py`, add the new dump as an anonymized fixture (replace names), write the test first, then fix. Run the whole suite: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -t .`.

**Change a setting.** `config.json` in the repo folder (git-ignored): people, `calendar_audit` (`off|report|write`), `questions` (`off|on`), `reader`. `config.example.json` shows the recommended values.

**Install, test or remove the scheduler.** `install` (a login agent plus one persistent wake-up agent for every planned opening: the program wakes only at the opening times, never on a timer; an agent waits up to 20 minutes for the unlock or the login), `arm-test` (a wake-up test that also checks permissions), `uninstall`.

## Remove the whole setup

`python -m club_booking uninstall`; remove Terminal and Python from System Settings > Privacy & Security (Screen Recording, Accessibility, Automation); delete `~/.mirroir-mcp` and `~/Library/Application Support/club-booking`; delete the Keychain items `club-booking-anthropic` (and `club-booking-phone` if it exists); remove the `club-booking` skill link from `~/.claude/skills`.
