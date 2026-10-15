# Club booking

Books your favorite classes in a country club's app (built for the Hasmonaim club app, which runs on the Mac as an iPhone app)
for the people you list. It reads each person's Favorites list, books every class that is open **24 hours before it starts**,
adds it to the right calendar with an alert one hour before, and wakes itself at the next opening of any class. It writes to your
phone **only when it needs a decision or an action**.

> **Unofficial.** This is not made by, or connected to, the club or the app's maker. It presses buttons in your own
> account, like you do: use it at your own risk, and read your club's terms first.

**New here? Start with [`docs/INSTALL.md`](docs/INSTALL.md)** (what you need, the one-command install, the setup questions,
every macOS permission, the first login, daily life, troubleshooting, and what leaves your Mac).

Design: `docs/design.md`. Lessons learned: `docs/lessons.md`. The club app's screens: `docs/club-app-field-notes.md`.
`config.json` holds the first names of the people: it is not in git (see `.gitignore`).

## Setup (once)

```
bash scripts/install.sh                          # the environment and the three helper tools
.venv/bin/python -m club_booking setup           # people, calendars, phone number and key (asks; writes config.json)
.venv/bin/python -m club_booking doctor          # every line must say OK (permissions: see docs/INSTALL.md)
```
Then the first login (`login request`, `login code`, or the automatic login below) and `install`.

## Use

| Command | What it does |
|---|---|
| `python -m club_booking setup` | Guided setup for a new user: asks, writes `config.json`, stores the phone number and the key in the Keychain. |
| `python -m club_booking run` | Read everyone's favorites now and book what is open. Also `scripts/run-now.command`. |
| `python -m club_booking run --dry-run` | Read and print, book nothing. Add `--person sam` for one person. |
| `python -m club_booking status` | Show the plan and the armed wake-ups. |
| `python -m club_booking install` | Install the login agent and one persistent wake-up agent for every planned opening. |
| `python -m club_booking arm-test` | Prove that the wake-up works (it sends a test message to the iPhone). |
| `python -m club_booking uninstall` | Remove every launchd agent. |
| `python -m club_booking answers` | Read your answers to the questions on the iPhone and act on them now (the program also does this each time it wakes). |
| `python -m club_booking login request` | Supervised login, step 1: fill the phone number (`--phone`, or the Keychain item `club-booking-phone`) and press "send me a code" **once**. |
| `python -m club_booking login code 123456` | Supervised login, step 2: type the 6 digits. |
| `python -m club_booking login auto` | The whole login by itself (see "Automatic login"). Use it to try the setup before you switch `auto_login` on. |

Run everything with `.venv/bin/python`. While a run is in progress, do not touch the mouse.
`run` and `tick` arm no wake-up until you have run `install` once. `uninstall` stops all arming. `status` shows how many wake-ups are armed and the next ones.

## When the program wakes (and when it does not)

The program wakes **only at the time a class opens** (24 hours before it starts) and not before. Tomorrow's 16:30 class means one wake today at 16:30 and nothing earlier: there is no timer, no polling, and no look at the app in advance.

- **One persistent agent per opening.** `install` puts a launchd agent in `~/Library/LaunchAgents` for every opening in the plan, for the whole week. They survive a restart. After each run the program makes them match the plan again (it adds the new openings and removes the ones that are gone).
- **If the Mac is asleep** at that time, launchd runs the agent when the Mac wakes. To wake the Mac for the openings, run the lines that `python -m club_booking wake-command` prints once (they need an administrator; a woken Mac that is locked still needs your unlock).
- **If the Mac is locked, or the club app is logged out,** the agent tells the phone once and waits up to 20 minutes, looking every 15 seconds, and books the moment you unlock or log in. After that it tries again every 5 minutes, at most 6 times.
- **A login agent** runs once when you log in (a catch-up after the Mac was off).
- **The plan** (the list of opening times) is refreshed right after an opening run, only when it is more than two days old and you have been away for 10 minutes (a week old: always). Run `python -m club_booking run` yourself after you change a favorites list.
- **Nothing takes the mouse** unless a class is opening, or you were away for 10 minutes (`min_idle_seconds`).
- **The favorites are a dynamic list** (your rule of 2026-10-14). The program reads them each time it wakes to book a class, and an opening run merges what it read into the plan, so a class you removed or added shows up in the plan by itself. It never wakes only to test the favorites. The wake-up of a class that you removed stays in the plan until the next wake of that person reads the list (a wake-up for a class that is no longer a favorite finds nothing to book). If you tell the program that a class is gone, the opening can be taken out of the plan by hand (`plan_edited` in the log) so that no wake-up is spent on it; do it only for a class you named, never to guess.

### You come first (the guard)

Before every click, and before it brings the club app to the front, the program looks at the keyboard and the mouse. Its own clicks are counted out: it knows the time of its last click, so only a touch after that is you.

- **A refresh of the plan, or applying one of your answers,** stands back at once when you touch the keyboard or the mouse. It gives no warning first, it counts as no failure, and it sends no message. It tries again at the next quiet time. (A plan that is a week old is the exception: nothing else wakes the program when the plan has run out, so that refresh waits for a pause in your typing, like an opening, and then goes on.) Only a booking that was already begun is finished (the confirmation is closed, a pressed cancel is verified): a half-done booking is worse than two more clicks.
- **A class that is opening** is urgent. The banner "Starting in a few seconds, keep your hands off the mouse" appears, the program waits for a pause in your typing (at most 90 seconds; after one full wait in vain it books without waiting again) and then it books.
- **A run you start yourself** (`run`, `answers`) is not guarded: you are watching it.
- If the Mac cannot say when you last used it, you count as away.

## Run now by hand (when the Mac was locked at an opening)

The agent that starts at the opening time waits up to 20 minutes for you to unlock the Mac (or log the app in) and books the moment you do; then it tries again every 5 minutes, a few times. To book at once, double-click `scripts/run-now.command` in Finder, or run `python -m club_booking run`. The window of the script waits for a key at the end, so you can read the result. A keyboard Shortcut for this was not needed (the user's decision on 2026-10-13).

Away from the Mac? The phone reminder "the Mac is locked" names the person, the class and the time. Register in the club app on your phone: the program sees "רשום" (registered) and skips that class, and it never books it again, also if you cancel it later. The calendar check (next section) adds the calendar event for it at the next run.

## What the phone gets (only a decision or an action)

**The user's rule of 2026-10-15:** the phone gets a reminder only when the program needs **a decision** (two overlapping classes of Alex, several classes in one day) or **an action** (something stops it from booking a class). No confirmations, no wake-up messages. Everything else is in the log (`runs.jsonl`) and in the calendar (a booked class has an event with an alert 1 hour before).

| Message | Why it is sent |
|---|---|
| Questions | A **decision**: one reminder per option (see below). |
| `Club booking: N to check` | A class that was **not booked** and needs you: refused by the club, the class is full and the waitlist was not joined, a tap failed, a person's run failed. Only these lines, never a `Booked:` line. |
| `the Mac is locked` | **Action:** unlock it, a class is waiting. Once per opening, naming the class. |
| `the club app is logged out` / `needs an update` | **Action:** log in (with `auto_login` on, only when the automatic login failed, with the reason). At most **once a day**. |
| `giving up for today` | A class was not booked after the retries: register by hand. |
| `<name>'s answer was only partly applied`, `answer was not applied` | Your decision could not be carried out: do that part by hand. |
| `Club booking problem` | The program itself broke (once a day). |

**Not sent (they are in the log):** a booking (`N booked`), a waitlist join, a calendar event added or a calendar problem (`calendar_audit_report`, `calendar_audit_failed`), the confirmation of your own answer, the sons' overlaps, and the wake-up test of `arm-test` (it is a **Mac banner** and the log entry `wake_test`). The Mac banner "Starting in a few seconds, keep your hands off the mouse" before a run stays (it is not a reminder). Reports older than two days are deleted once a day.

**A message goes away when its problem is gone** (2026-10-14: "the Mac is locked" and "Problem" stayed on the phone for hours after the class was booked). A message about a problem is tagged with a short code on its last line (`[club-booking m:...]`) and the program remembers it. When the person it is about has run without an error, the program deletes the message from the phone: a "Mac is locked" message when that person's run is fine, a "Problem (Alex)" report when Alex's next run is fine (a message about two people waits for both), a "logged out" or "needs an update" message when any person can run. Then the same problem is told again if it comes back. A run that stood back (you were at the Mac), a dry run, and a run that fails prove nothing. If the phone does not answer, the message stays and the next run tries again (`message_not_taken_back` in the log; `message_taken_back` when it worked). Booking reports (`N booked`), answers and questions are not touched: they leave after two days, or when you answer.

## Questions on the iPhone (`"questions": "on"`)

The program asks you on the iPhone in the Reminders list "Club bookings": **one reminder per option**, and you answer by tapping **Mark as Completed** on the option you choose. It never waits: it books first, asks after.

| Case | What the program does | The options |
|---|---|---|
| A class is full | Joins the waitlist and **keeps the spot**: no question and no message (your decision of 2026-10-14; the join is in the log). If the spot becomes a registration, the calendar check adds the event. To leave the waitlist, remove the spot in the club app yourself. | none |
| A person who asks (`"asks": true`, Alex) has several classes in one day | Books them all, then asks. | Keep: each class / Keep all |
| Two classes of that person overlap | Books only the **more crowded** one (the people who registered are counted on the class page; the earlier class when it cannot count) and asks. | Keep: the booked one / Switch to: the other |
| Two classes of a person with `"asks_overlaps": true` (in `config.json`) overlap | Books the more crowded one, then asks. The club refuses two overlapping classes ("לא ניתן להירשם לשני שיעורים חופפים", seen 2026-10-15 with Sam's chess and football at 13:15). Several classes in one day are **not** asked about for the sons. | Keep: X / Switch to: Y |

### How you answer (two-way, no Claude session needed)

1. A reminder appears in the list "Club bookings", one reminder per option (for example `Keep: שחמט מתחילים`, `Switch to: כדורגל`). The text of each says which class.
2. You tap the circle (**Mark as Completed**) on the option you choose, on the iPhone or the Mac. Tap exactly **one**; two or none is no answer.
3. iCloud brings it to the Mac. The program reads it when it wakes, and carries it out. While a question is open it wakes **10 minutes after the question, and at 20:00, 08:00, 10:00 and 12:00** until 45 minutes before the class, only to read the answer (it touches the app only when an answer is waiting). When the class is less than 3 hours away, it carries the answer out even if you are at the Mac (it waits for a pause in your typing). You can also run `python -m club_booking answers`.
4. **Switch to:** the club refuses two overlapping classes, so the program checks that the class you chose shows "register", **cancels the class it holds first** (only a class that it booked itself), and then books the chosen one. If the chosen class is full, nothing is cancelled and you are told. If the booking fails after the cancel, you are told to register by hand. **Keep** changes nothing.
5. A question is deleted when its class starts; the booking stays. An answer is no news: you get a message only if a part could not be done.

Several classes in one day are **not** asked about any more (`"questions_several": false` in `config.json`: the user's choice of 2026-10-15; the default in the code is `true`). A class that the club refused because it overlaps a class that the program booked becomes a question by itself.

**The program cancels only what it booked itself** (or a waitlist spot it joined itself), and only after you answered that way. Every cancel passes one guard (`Runner.cancel`):
- the booking must be in the program's own record (`bookings.jsonl`) and still stand there, for that person;
- the class you kept must still be booked, or nothing is cancelled;
- the button is pressed by its exact text **inside that one class's card** through accessibility (the helper refuses when the card is not on the screen exactly once), never by position;
- the attempt is written before the press and is never repeated;
- the message after the press is read first: only a success message ("ההרשמה בוטלה בהצלחה") is closed; any other message (a fee, an "are you sure") is not confirmed: the program stops and tells you;
- the result is checked on the booked tab.

A class you registered by hand has no booking record, so it is never touched. A class the program booked and the list later shows open again (you cancelled it) ends its record at the next run, so it is not the program's to cancel even if you register it again by hand. The one gap: if you cancel and register again between two runs, the program cannot tell, and an answer that drops that class could cancel it.

An answer that cannot be applied at once (the Mac is locked or in use, a class is about to open, a step failed before anything was pressed) waits and is tried again the next time the program wakes; after three failed tries, or when the class starts, you are told.

## If you unregister a class

If the program booked a class and you unregister from it, the program does **not** book it again: it remembers every class it has booked or seen you registered for, by date and time, so that class stays alone. The same class in the next week is a different date and is booked as usual. This is only for that date: you decided (2026-10-14) that you skip one date because you cannot come, not because of the class. The calendar event of the class you dropped is deleted by the program (see the next section for the rules); if you register the class again, the event comes back.

## The screen must be on

A locked Mac, or a Mac whose display is asleep, cannot be read or clicked (the screenshot is black and the club app's window has no size). The program checks both. When a class is due and the display is only asleep, it wakes it once; when the Mac is locked it cannot, and the phone gets a reminder that names the class so you can register by hand.

## Automatic login (`"auto_login": true`)

The club app logs itself out every few hours and each login needs an SMS code. You decided on 2026-10-15 that the program does the login by itself, with no Claude session ("it should be independent"). It replaces the old rule "an unattended run never logs in".

**Setup, once** (the program cannot do it):
1. **iPhone:** Settings > Apps > Messages > Text Message Forwarding > switch on your Mac. The Mac must be signed in to Messages. The SMS then also reaches the Mac.
2. **Mac:** System Settings > Privacy & Security > **Full Disk Access** > add the `Python.app` that runs the program (`doctor` prints its exact path) **and Terminal** (for a test that you start there). The path changes when you upgrade Python with brew: do it again then.
3. **Keychain:** `security add-generic-password -a "$USER" -s club-booking-phone -w` (it asks for your number, hidden; it is never in the repo or a file).
4. Run `python -m club_booking doctor` (every line must say OK) and `arm-test` (it asks the same questions from the scheduled job, where Terminal's permissions do not count), then `python -m club_booking login auto` once while you watch, and then put `"auto_login": true` in `config.json`.

**What it does.** At an opening that finds the login screen (and only then: not for a plan refresh, not for an update popup, not while PAUSE exists, not in a dry run), the program shows the banner, waits for a pause in your typing, reads a snapshot of the newest message number in the Messages database, asks for **one** code, waits up to 120 seconds for the SMS, types the code **once**, and goes on to book in the same wake-up. No message is sent when it works. If it fails, the daily "club app is logged out" message says why (for example "no SMS from hashmonaim arrived in 120 seconds: is Text Message Forwarding on for this Mac?").

**The limits (they protect your login from a lock):**
- It never asks for a code that it cannot read: if the Messages database cannot be opened, nothing is requested.
- Its own budget: **one request in 45 minutes and three in 24 hours**, written down before the request. The app's guard (2 requests in 10 minutes, shared with `login request`) counts too.
- One request per attempt, one code typed per request, no second try. If no SMS comes, the app stays on the code screen: the program types nothing there once the code is older than 5 minutes, and a person must go back to the login screen (quit and open the club app).
- The code must come in a message from the sender **newer than the snapshot and than the request**, with exactly one 6-digit number, and each message is used once.
- Privacy: the query names the sender (`sms_sender`, default `Hashmonaim`), the database is opened read-only, and only a count and an age are read by `doctor`. The code and the message are never logged or kept (only the message number).
- The way the newest messages store their text (`attributedBody`) was built from knowledge, not tested on a real message: **the feature is not proven until the first live run.**

Config keys: `auto_login` (default `false`), `sms_sender`, `sms_wait_seconds` (120), `messages_db`.

## Calendar check

After each run the program checks every class a person is registered for (the cards that show "רשום" in the Favorites list, in the next `horizon_days`) and adds the calendar event when it is missing: the person's own calendar, an alert 1 hour before. This covers a class you registered by hand, and a class whose calendar step failed.

Set `"calendar_audit"` in `config.json`: `"write"` (recommended; it is in `config.example.json`), `"report"` (the phone is told what is missing, nothing is written) or `"off"` (the default in the code). A dry run only reports.

With `"write"` the calendar check is the **only** calendar writer: a booking no longer opens the app's slow calendar editor (about a minute per class, which delayed the next booking), and the events are added after all bookings, about 3 seconds each. With `"off"` or `"report"` the app's own "save to calendar" button is still used after a booking.

- An event counts as present when it is in the person's calendar and starts at the same minute as the class (the end and the title are not compared; a repeating event counts when its time of day is the class's). For the sons (shared calendar Kids) its title must also start with the person's name ("Sam - "). An event with no name at that time is reported, and nothing is added: a duplicate is worse than a missing event.
- A class is checked once. The program never adds the same one again, so an event you deleted on purpose stays deleted.
- It is slow on this Mac: one pass over a calendar takes 20-30 seconds (more than 1000 events), so it runs last, after the booking report and the next wake-up. It waits for a quiet moment: when a class opens within 10 minutes it is skipped, and the next run does it. A calendar that cannot be read is tried 3 times a day.
- It sees every class the person is registered for: the favorites list and the booked tab ("מוזמנים").
- **A class you unregistered loses its event** (your decision of 2026-10-14). Deleting is hard to undo, so the rules are strict: only a class the check had handled and that is still in the future; only when the booked tab of the person was read to its end and the class is not on it (a waitlisted class counts as on it); only on the second reading, 30 minutes after the first, so one bad reading deletes nothing; only an event of that person (the name prefix), at that minute, with the class's name, and exactly one; **never a repeating event** (the Calendar scripting cannot delete a series: the test on the real Calendar did nothing without an error); never more than 3 classes of one person at once (that looks like a misreading, and the phone is told). The delete is counted before it is tried and stops after 3 failures. A deleted event is in the log (`calendar_audit_removed`), not a phone message. If the class is on the list again, the event is added again. In `"report"` mode the phone is told "would delete" and nothing is written.

## Reading the app

The class list is read through macOS accessibility (the helper `tools/ax`, built with `scripts/build-ax.sh`): exact text, page-by-page scroll, no API call. The picture reader (Claude vision) is the fallback and still reads the confirmation dialogs. Set `"reader": "vision"` in `config.json` to use pictures only. `doctor` shows whether accessibility works.

## When something goes wrong

- `docs/club-app-field-notes.md`: what the real app looks like and how it behaves (screens, texts, positions, login, update).
- `docs/lessons.md`: every problem we hit, its cause, and the rule that stops it.
- A logged-out app, a code screen, a login lock, or an update popup stops the run and tells the phone. **The program logs in by itself only when `auto_login` is on** (see "Automatic login"; the user changed the old rule "never" on 2026-10-15). `login request` and the automatic login refuse a third code request in 10 minutes, because the app locks the login for 10 minutes after 3.

## Safety

- Pause: `touch ~/Library/Application\ Support/club-booking/PAUSE` (remove the file to continue).
- The program cancels a class, or leaves a waitlist spot, **only** when it made that booking itself and you answered a question that way (see "Questions on the iPhone"). It never taps a heart.
- It only uses the Reminders list "Club bookings" and the calendars that you chose in `setup`.
- The API key lives only in the Keychain.

## Status and known limits

- Built and used on one Mac (macOS 27.2 beta, Apple silicon) for one family. **Not yet tried on a second Mac**: expect small
  problems in the first install, and please report them.
- The **automatic login** reads the login SMS from the Mac's Messages database. That works only when Messages on the Mac
  really receives your SMS (signed in to iMessage, Text Message Forwarding on). Check with `doctor`: "the newest message"
  must be recent. If your Mac does not receive messages, log in by hand (`login request`, `login code`).
- `setup` finds the profile positions in the club app by itself when the person selector is open; this was written from one
  real screen, so check the positions it prints.
- The club app's screens are read by Hebrew texts. Another club app needs new constants in `club_booking/ax_reader.py`.
- Terms of the club: not checked. Automatic booking may be against them.

## The Claude Code skill

`skills/club-booking/SKILL.md` teaches Claude how to operate, review and repair this program with you (rules for the real account, the login, the state files, repairs). It is linked into `~/.claude/skills/club-booking`. The daily runs do not need it or Claude.

## Remove everything

`python -m club_booking uninstall`, then remove Terminal and Python from System Settings > Privacy & Security
(Screen Recording, Accessibility, Automation), and delete `~/.mirroir-mcp` and the data folder
`~/Library/Application Support/club-booking`.
