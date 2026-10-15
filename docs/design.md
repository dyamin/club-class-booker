# Club Class Booking Automation: Design

Date: 2026-10-12
Status: APPROVED by the user on 2026-10-12.

## 1. Goal

Book gym classes for three people in the Hasmonaim country club app, so that the seats are locked as soon as registration opens. The app runs on this Mac. A tool on the Mac sees the app window and clicks in it.

**People (three profiles in the app):**
- Alex (the user)
- Sam (son, 6)
- Robin (son, 4)

**Personal tool.** No market goal. It runs on a company-managed Mac. The user accepted that risk (see section 9).

## 2. Requirements

These come from the user, in the order given.

| # | Requirement |
|---|---|
| R1 | The source of truth is the **favorites list** (מועדפים) of each profile. The tool never changes a favorites list. |
| R2 | The task covers all three profiles. The user will add favorites for the two sons. |
| R3 | **Every day**, for each profile, register for each favorite class that is open and not yet registered. Each of the three people has **different classes every day**, so each profile is handled on its own. |
| R4 | If a class is not open yet, or registration fails, **try again when it opens**. Registration opens exactly 24 hours before the class starts. |
| R5 | Alex usually trains in the **mornings**. His favorites decide what is booked. v1 has no time filter. |
| R6 | After each successful booking, **add the class to the calendar**. Alex's classes go to the Google calendar **"Alex"** (not shared). The sons' classes go to the shared Google calendar **"Kids"**. The title of a son's event is **"<child's name> - <class>"**, for example "Sam - Judo". Alex's events keep the class name from the app. |
| R7 | Every calendar event has an **alert 1 hour before** the class. |
| R8 | Before each booking, **check which person is selected**. After the run, switch back to Alex. |
| R9 | After each run, tell the user what was booked and what failed. The message must reach the user's **iPhone**, not only the Mac. The channel is **Apple Reminders** through iCloud, in both directions (section 10, item 11). |
| R10 | Keep evidence: save the screenshot of each confirmation dialog. |
| R11 | **Test cleanup.** After the full test, unregister from the test class and delete the test calendar event. This is a one-time step for the current test class (Alex, Tue 13/10, 08:50 METCON). Done on 2026-10-12. |
| R12 | **Trigger: hybrid (option C).** A planner computes the opening time of every favorite class of every profile. A booking run starts at each opening time. If the Mac is awake and unlocked, it runs on its own. If not, the user gets a notification and a hotkey starts the same run. |
| R13 | **Kids: book every favorite**, including several classes on the same day. No daily limit and no time filter. This rule is for the two sons only. |
| R16 | **Alex: several classes in one day.** If Alex has two or more favorites on the same day, the script books them all, then **asks him which to keep** (R17). |
| R17 | **Two-way questions.** The script asks the user on the iPhone in four cases: (a) Alex has several classes in one day; (b) two of Alex's classes overlap; (c) ~~a class is full and the waitlist is the only way in~~ (removed on 2026-10-14: see R14); (d) something unexpected happens. Each option is one reminder. The user answers by tapping **Mark as Completed** on the alert of the option to choose. **Book first, ask after.** The script does not wait. It books all the classes at once. For an overlap it books the default: the more crowded class (R19). Then it sends the question. When the user answers, the script cancels the options that the user did not choose (R18). |
| R18 | **Cancel only what the script booked.** The script may cancel a class, or remove a waitlist spot, only if it created that booking itself in the same run, and the user's answer says to drop it. It logs every booking it makes. It never cancels any other class or waitlist spot. The cancel button on a booked card is **ביטול**. For a waitlist spot the button is **הסרה מהמתנה**. |
| R19 | **Crowd judgment.** For an overlap, the default is the more crowded class. The script judges it by opening the class details page and counting the names of the people who booked it. The user says that the app shows this list. This is **not yet verified** (section 10, item 14). |
| R21 | **Unregistered by the user.** If the program booked a class and the user unregisters from it, the program does not book that class again (it is tracked per date and time, so the same class in the next week is a new one). Added by the user on 2026-10-14: "I probably can't go". The rule is for that date only: the user cannot come on that date, the class itself is not the reason (answer of 2026-10-14). **The calendar event of that class is deleted too** (the user said yes on 2026-10-14). Because deleting is hard to undo: only a class the calendar audit handled, still in the future, missing from a booked tab that was read to its end, on two readings 30 minutes apart; only one event of that person at that minute with that name, never a repeating one; at most 3 classes per person in one pass; counted before it is tried, 3 failures at most. A class that is registered again gets its event again. |
| R23 | **Messages go away when the problem is gone.** A message about a problem (a person's error, the locked Mac, the logged-out or outdated app) is tagged, remembered, and deleted from the phone when the person it is about has run without an error (a login message: when any person can run). A message about several people waits for all of them. The "told today" counter is cleared with it. A run that stood back, a dry run, or a failed run proves nothing. Booking reports, answers and questions are not touched (two-day prune, or the answer). Added on 2026-10-14 after the user asked why there were so many reminders. |
| R26 | **Decisions are two-way and carried out by the program alone.** Decided by the user on 2026-10-15 ("independent, not dependent on the Claude Code session"). The user answers by completing one reminder. While a question is open the program also wakes (10 minutes after it, then 20:00, 08:00, 10:00, 12:00, until 45 minutes before the class) to read the answer; within 3 hours of the class it carries the answer out even when the user is at the Mac. A "Switch to" of an overlap cancels the held class first (the club refuses two overlapping registrations), only after the chosen class shows "register"; a failure after the cancel is told loudly. A class refused by the club for overlap becomes a question by itself. Several classes in a day are not asked about (`questions_several`: false). A booking whose confirmation was not recognized is checked against the booked tab before the user is bothered. |
| R25 | **A reminder only for a decision or an action.** Decided by the user on 2026-10-15: "send me only if it needs my decision on something like two overlapping lessons". The phone gets: questions (overlaps, several classes in a day); a class that was not booked (refused, full and not joined, failed); the locked Mac; the logged-out app (with the reason when the automatic login failed); "giving up for today"; an answer that could not be applied; a crash. Not: a booking confirmation, a waitlist join, a calendar event or problem, the confirmation of the user's own answer, the sons' overlaps, the wake-up test (a Mac banner). All of these stay in the log. |
| R24 | **Automatic login.** Decided by the user on 2026-10-15, replacing "an unattended run never logs in": with `auto_login` on, an opening that finds the login screen logs in by itself. The SMS code is read from the Mac's Messages database (Text Message Forwarding), for one sender only, read-only, never logged. Limits: no request unless the database can be read; one request in 45 minutes and three in 24 hours (written before the request) on top of the app's guard (2 in 10 minutes); one code typed per request; the message must be newer than the snapshot and the request, with exactly one 6-digit number, used once; only for a waiting class (not a plan refresh, not an update popup, not while paused, not in a dry run); under the attention guard (R22); a failure tells the phone why. Not proven until the first live run. |
| R22 | **The user comes first.** The program must not fight for the mouse. Before every click it checks that the user has not touched the keyboard or the mouse since its own last click. A run that is not for a class that is opening (a plan refresh, applying an answer) stands back at once, with no failure and no message. A class that is opening waits once for a pause in the typing and then books. A booking that was begun is finished. Added by the user on 2026-10-14: "I can't work on this computer". |
| R20 | **Wake-up.** The program must wake itself at the next opening of a class of **any of the three** people. One persistent launchd agent exists for every planned opening (the whole week), and nothing runs on a timer. If the Mac is asleep, the job runs when the Mac wakes. If the Mac is locked, the user gets a reminder (R12), and the agent waits up to 20 minutes for the unlock and books at once. The card's own opening time is used, not a guess. |
| R14 | **Full classes.** If a favorite shows "הרשמה להמתנה", the script **joins the waitlist and keeps the spot**. Decided by the user on 2026-10-14: always keep it, with no question and no message (the join is in the log; a spot that becomes a registration gets its calendar event from the audit). The user leaves the waitlist in the club app himself. An old waitlist question is closed and never cancels anything. |
| R15 | **Overlaps.** Changed on 2026-10-15: the club refuses two overlapping classes ("לא ניתן להירשם לשני שיעורים חופפים", Sam's chess and football at 13:15). For a person with `asks` (Alex) or `asks_overlaps` (the sons), of two overlapping favorites the program books the more crowded one and asks which to keep (R17b); several classes in a day are asked about only for `asks`. Before, the sons' overlaps were both booked with a message. |

**Never (hard rules):**
- Tap "הסרה מהמתנה" (remove from waitlist), except to undo a waitlist spot that the script created itself (R18).
- Tap a heart (favorite) icon.
- Cancel any class, except a class that the script booked itself in the same run and that the user chose to drop (R18).
- Make any payment.

## 3. How it works

One run does this:
1. **Trigger** fires (see below).
2. **Preflight.** The Mac is awake and unlocked. The club app is running and its window is visible. The screen is the expected one. If not, notify the user and stop.
3. **For each profile** (Alex, Sam, Robin):
   1. Open the person selector (top center). Tap the profile. Check that it has the check mark.
   2. Open Classes, then Favorites. Scroll through the list and read it.
   3. For each favorite with the button **הרשמה**: tap it once, read the confirmation dialog, check that it says **זהו, נרשמת!** Where a rule says to ask (R17), book first, then send the question. Do not wait.
   4. Create the calendar event (section 6).
4. **Finish.** Switch back to Alex. Return to the Classes tab. Write the log. Notify the user.

**Reading the screen.** The tool's built-in text recognition cannot read Hebrew. So one Claude vision call turns the screenshots of a list into a table: class, time, state, and button position. The script then taps known positions and checks the next screen after every tap. The vision call uses the **Claude API with a key** (decided 2026-10-12). The key rules are in section 7.

**Timing.** Each step needs about 10 seconds, because of screenshot waits. A run for three profiles may take 3 to 6 minutes. This is an estimate. The user said that classes fill in minutes or hours, not seconds. So this speed is enough.

### Trigger (open decision)

When a class opens is known: start time minus 24 hours. For example, a class on Tuesday at 08:50 opens on Monday at 08:50. The Mac must be awake and unlocked at that time. Options:

| Option | How it starts | Weak point |
|---|---|---|
| A. Scheduled | A macOS `launchd` agent starts a run at each opening time. | Fails if the Mac is locked or asleep. A work Mac often locks. |
| B. Manual | The user presses a hotkey or runs one command at the desk. | The user must remember. |
| **C. Hybrid (recommended)** | A scheduled run starts if the Mac is awake and unlocked. If not, the user gets a notification, and presses the hotkey. | Needs both parts. |

**Decision (2026-10-12): option C.**

Each of the three people has different classes every day. So a **planner** works first:
1. Once a day, and on demand with the hotkey, the planner reads the favorites list of each profile.
2. For each favorite that is not open yet, it computes the opening time: start minus 24 hours.
3. It arms one timer per opening time. Several timers per day are normal.
4. At each timer, a **booking run** starts for that person and class. It also books any other open favorites of that person. If the Mac is awake and unlocked, it runs. If not, the user gets a notification, and the hotkey starts the same run.
5. Runs go one after another. If two openings fall in the same minute, the second waits. Classes fill in minutes or hours, so a few minutes of delay are fine.

The planner also needs a trigger of its own. A daily timer, plus the hotkey after the user changes favorites, is enough for v1.

## 4. What we verified in the app

- App: "חשמונאים", on the Fizikal platform. It runs on the Mac as `/Applications/Runner 2.app`. Bundle ID `il.co.offline.hashmonaim`, process `Runner`. Window about 1024×800.
- Bottom tabs: בית (home), שיעורים (classes, about x=640, y=775), אישי, תפריט.
- Classes tab has three sub-tabs: לו"ז שיעורים (schedule, about x=860, y=108), מועדפים (favorites, about x=512), מוזמנים (booked, about x=170). The booked tab was empty while a booking existed in the favorites view, so do not rely on it.
- **Person selector:** tap about (520, 68). Three rows: Alex about y=176, Sam y=214, Robin y=253. The selected row is filled blue and has a check mark. The X at about (1001, 70) closes it.
- **Opening rule:** registration opens at class start minus 24 hours. Before that, the card shows "פתיחת הרשמה: <date time>" and a **תזכורת** button.
- **Card states and buttons:**

| Label | Meaning | Script action |
|---|---|---|
| הרשמה | Open for registration | Tap once |
| הרשמה להמתנה | Class is full; join the waitlist | Tap once (R14) |
| תזכורת | Not open yet | Do nothing |
| רשום | Already registered | Skip |
| בהמתנה, הסרה מהמתנה | On the waitlist | Tap הסרה מהמתנה only to undo a spot the script created itself (R18) |

- **Confirmation dialog** after a booking: "זהו, נרשמת!" with two buttons: **שמירה ללוח שנה** (save to calendar, about (459, 757)) and **מצוין, תודה** (about (579, 757)).
- More facts and the procedures (login, update, refused classes, accessibility): `docs/club-app-field-notes.md` and `docs/lessons.md`.
- Day chips: today is the leftmost chip (about x=990, y=160), tomorrow about x=944, then 888 and so on.

**Quirks seen**
- The favorites list sometimes jumps down by itself. Always check the screen before a tap.
- The first click on an inactive window may only focus it.
- Pop-up menus are separate windows. The tool's window capture does not show them. A capture of the window's screen region does. Pop-ups close if left alone, so open, choose, and capture in one script.
- The calendar editor changes its layout after the calendar changes (a "Show As" row appears).

## 5. Tool and setup facts

- Tool: mirroir-mcp v0.40.1 (open source, Apache-2.0), built from the verified release (SHA-256 `d08c71aa4a972c65c9cfab4b28e4ebaa72296ffa3a2dfcc50e330fa262826e49`) with `swift build`. The Homebrew route fails because it needs full Xcode.
- Config: `~/.mirroir-mcp/targets.json` (target `club`) and `~/.mirroir-mcp/permissions.json` (allows only `tap` and `swipe`).
- Terminal has the macOS permissions Screen Recording and Accessibility.
- Coordinates are window points (the window is 1024 x 800). On a Retina display the screen tool returns native pixels (2048 x 1600), so the program scales every picture to points. The first spec text said "one to one": that was true only on a non-Retina display.
- The tool's text recognition does not support Hebrew.
- Source review: no telemetry; network only for optional AI providers; capture uses `/usr/sbin/screencapture`. I read the key places, not all 233 files.

## 6. Calendar handling

**Problem.** The app's **שמירה ללוח שנה** button opens Apple's "New Event" editor. It starts with the calendar **Work** (iCloud, company). That would put gym classes on the work calendar. Choosing another calendar needs a pop-up menu. That is fragile to automate.

**Decision (2026-10-12): use the app's button.** The user chose this over scripted creation. The script does this after each booking:
1. Tap **שמירה ללוח שנה**.
2. Open the Calendar pop-up. Read its items with the English text helper. Choose **Alex** for Alex's classes and **Kids** for the sons' classes. **Never save to Work.**
3. Set the alert to **1 hour before**. Save.
4. Run a Calendar script. For the sons' events it renames the new event to "<child's name> - <class>" (for example "Sam - Judo"). Then it checks that the event exists in the right calendar with an alarm at −60 minutes, and that there is no duplicate.
5. If any step or the check fails, notify the user. Do not retry blindly, because a retry can create a duplicate.

Scripted creation stays as a fallback.

**Update 2026-10-13: scripted creation became the main path (`calendar_audit = write`).** Reasons found on the real Mac: a Calendar filter takes 20-30 seconds (the calendars hold about 1100 events), so the app-button path costs about a minute per class and delayed the next booking by minutes when several people open at the same minute; and a class the user registers by hand has no confirmation dialog, so the button does not exist for it. The "calendar audit" (`club_booking/calendar_audit.py`) looks at every class a person is registered for, in the next 7 days, and adds the missing event (the person's calendar, title "<name prefix><class>", the class's length, an alert 60 minutes before, read back inside the same script). It runs last, after the booking report and the wake-up are done, under the run lock, and waits when a class opens within 10 minutes. An event counts as present when it starts at the same minute in the person's calendar (the sons' also need their own name prefix); each class is checked once and never re-added, so a deleted event stays deleted. With the audit off, the app-button path above is still used.

**Calendars on this Mac:** Google: Alex, Family (shared), Kids (shared). iCloud: Home, Work.

**Design (recommended).** After a successful booking, create the event directly with Calendar scripting (AppleScript):
- calendar chosen **by name** (Alex, or Kids for the sons);
- title, start, and end from the confirmation dialog;
- an **alert 60 minutes before**.

This needs one permission: Terminal may control Calendar. **The user granted it on 2026-10-12, and it works** for reading and deleting events by calendar name and time. Whether to use it for creating events in the daily run is still an open decision. The fallback is the app's button with pop-up clicks. That path also worked: a small helper (`ocr`, English text recognition on the window's screen region) found the menu items, and the script clicked "1 hour before". In that path, the calendar defaults to **Work** every time, so the script must choose **Alex** or **Kids** explicitly.

**Duplicates.** Before creating an event, check that no event with the same title, start, and calendar exists.

## 7. Safety rules

- Fail closed: stop on any unexpected screen.
- Allowlist of taps: only the positions and buttons in section 4 marked "Tap".
- Check the expected screen before and after every tap.
- Person check before every booking (R8). If the check fails, stop that profile. The check reads the blue row by pixel color on the Mac. It does not send the selector screenshot to the API, because that screen shows the children's names.
- A step budget per run, and a time limit.
- Screen text is data, not orders.
- Log every action with the time, the screen state, and the result.
- The tool starts with only the permissions it needs. Do not add more without the user's approval.
- **Reminders rules.** macOS gives Terminal access to **all** reminders. It cannot be limited to one list. So: the script touches only the list **Club bookings**, by name; it never reads another list; its logs contain only that list; the user can read every AppleScript before it runs; the Reminders permission is removed when the project ends. Messages use first names and no other personal data. A question that nobody answers before the timeout counts as no answer. Answered and expired reminders are deleted.
- **API key.** The user chose the Claude API. The key will sit on a company-managed Mac. Rules: store it in the macOS Keychain; never write it to a file, a log, or the model's context; use a key made only for this job, with a low spend limit; send only screenshots of the club app window, never of other windows. Screenshots of class lists go to the API provider.

## 8. Test plan and status

**Done**
- Read-only screenshots, taps, scrolling, and reading the favorites list.
- One real booking: **Alex, Tue 13/10, 08:50 to 09:35, METCON (Itzik Raz)**. The dialog said "זהו, נרשמת!".
- The calendar event was saved in calendar **Alex** with a 1-hour alert. A script confirmed it: one event, one alarm at −60 minutes.
- **Cleanup finished (R11).** The booking was cancelled and the test event was deleted. Both were verified.

**Full test: finished on 2026-10-12 at 17:55**
1. Alert set to 1 hour before, event saved in calendar **Alex**, checked by script.
2. **Cleanup (R11).** The booked class showed **נרשמת ✓** and a **ביטול** button (about (47, 557) on that card). One tap cancelled it. The 08:06 METCON waitlist card stayed unchanged at position 2.
3. The test calendar event was deleted by script. The script matched calendar, title, and start time, and deleted only when it found exactly one event. A second check found none.

**Risks in the cleanup.** I do not know the club's cancellation rules or fees. The class starts in about 15 hours. The waitlist card has a remove button next to the area where I work. A wrong tap would remove the user from that waitlist. **Result:** the cancel worked at once. The app showed no fee message and no confirmation question, and the dialog said "ההרשמה בוטלה בהצלחה!" with a **סגור** button (about (512, 755)). The waitlist card was unchanged.

**Later tests:** a two-way test with Apple Reminders: the Mac creates two reminders, the user completes one on the iPhone, and the script reads which one is completed. Also check that the alert arrives in real time. (An ntfy test was tried and dropped; see section 10, item 17.) Then a dry run that only reads the favorites and reports; then supervised real runs for one profile at a time; then the sons' profiles.

**Status of the first version, 2026-10-13 evening.** Built, deployed, and checked on the real Mac:
- Login (guarded, with the SMS read through iPhone Mirroring), profile switching for all three, the accessibility reader (no API call for the list), real bookings for Alex, launchd tick and opening agents, a passing wake-up test (permissions, Keychain, club app, Calendar), the locked-Mac path (the 16:30 opening found the screen locked and sent one reminder naming the class), and the calendar audit (events added for a class registered by hand and for a class whose calendar step had failed, alert 60 minutes before, read back from the real calendars).
- Found on the way: a sleeping Mac runs no job (launchd cannot wake it); a session lasts roughly 3.5 to 7 hours, so a new login is needed about daily; a Calendar filter takes 20-30 seconds on this Mac.
- Not yet seen: the program booking a son by itself (the first son's class was registered by hand), the refusal popup for a child who is too young, the clock-change night of 25 Oct 2026, and the real waitlist text.
- **Plans 2 and 3 were built the same night**: the waitlist join (R14), questions on the iPhone with one reminder per option (R17), Alex's several classes and overlaps with the crowd count from the class page (R16, R19), the sons' overlap note (R15), and the guarded cancel (R18); the calendar check now sees every booked class through the booked tab; the Claude Code skill (`skills/club-booking/SKILL.md`). Facts found on the real app: the booked tab ("מוזמנים") lists every booked class with a "ביטול" button, and a class page lists the attendees (section 4 of the field notes). An independent review found 5 critical and 8 important problems in this code before it was switched on; all were fixed with a test first (the cancel press is bound to one card, the message after it is read, stale answers cannot cancel what the user meant to keep, a failed new step never stops a booking, the display must be on).
- **Interpretations** (ledger of plan 2): R18 "in the same run" means "booked by the program and recorded, still standing"; an overlap books only the more crowded class and asks.
- **Not yet seen on the real app:** a waitlist join and its message, a cancel by the program, the crowd count on a long list (all covered by tests with fakes and by an independent review only), the refusal popup for a child who is too young, the clock-change night of 25 Oct 2026.
- Later (not built): waking the Mac for an opening (needs `pmset`, root).

## 9. Environment and risk

- This Mac is company-managed (Intune, Defender, Admin By Request, FortiClient). The user chose to proceed here and accepted the risk.
- The user added a personal Google account to this Mac (Calendars). Check that only Calendars is on.
- Terminal can capture the screen and control the mouse. Any program that runs in Terminal can use this.

## 10. Open questions

1. **Trigger.** Decided: option C (section 3). Built and checked on 2026-10-13: launchd runs a 5-minute safety tick and one agent per next opening of any of the three. A job started by launchd needs the Accessibility permission for Homebrew's Python (the user granted it), and it starts Calendar and Reminders hidden, because launchd cannot launch Calendar with an Apple event (error -600). The wake-up test passed at 12:29. A run by hand: double-click `scripts/run-now.command` or run `python -m club_booking run`; the user decided on 2026-10-13 that no keyboard Shortcut is needed. launchd cannot wake a sleeping Mac. Still open: the locked-Mac and sleep tests.
2. **Vision calls.** Decided: the Claude API with a key (section 7 has the key rules). Model decided by the user on 2026-10-13: `claude-opus-5-5`, because speed matters more than cost. The class list is now read through macOS accessibility (0 API calls); the model reads the confirmation dialogs and is the fallback. Still open: the monthly spend limit (not measured yet).
3. **Calendar writing.** Decided: the app's button (section 6). A Calendar script renames the sons' events and checks the result.
4. **Full classes.** Decided: the script asks (R14, R17c). It replaces the earlier "join automatically".
5. **Overlaps.** Decided: for the sons, book both and tell the user (R15). For Alex, ask (R17b).
6. **Cancellation and fees.** Answered by the user on 2026-10-15: **there are no cancellation rules.** A cancel about 15 hours before a class showed no fee message either. The program still cancels only what it booked itself, and only after an answer (R18): that rule is about not touching the user's own choices, not about fees. (Does a booking cost a credit? Not asked; nothing in the app showed one.)
7. **Club terms.** Does the club allow automatic booking?
8. **Login.** If the app logs out, a new SMS code is needed. Facts from 2026-10-13: the app logs out after a forced update, and after a restart of the stuck app. Three code requests in 10 minutes lock the login for 10 minutes. The code comes by SMS and, the user says, expires after 5 minutes (use the newest one). iPhone Mirroring shows the SMS on the Mac, and the user allowed Claude to read the login code there. `python -m club_booking login request` and `login code <6 digits>` do the login with a guard (at most 2 requests and 2 code entries in 10 minutes; the phone field is checked by OCR before send). An unattended run never logs in: it reports "logged out" to the phone.
9. **Calendar name "Kids".** Confirmed by the user: the sons' classes go to the shared calendar Kids.
10. **Work-Mac policy.** Lock and sleep rules decide whether option A can ever work.
11. **Notifications and questions.** Decided on 2026-10-12: **Apple Reminders through iCloud**, plus a log file on the Mac. ntfy was tried and dropped (item 17). Each report or question is a reminder in the list **Club bookings**, due now. A question has one reminder per option. The user taps **Mark as Completed** on the alert to answer. **Tested on 2026-10-12:** the Mac created two reminders, the user completed TEST A on the iPhone, and the Mac read the answer 98 seconds after creation. That time includes the user's reaction time. A first run, with no answer in 3 minutes, was probably a missed alert (not confirmed). The alert arrived on the lock screen within a few seconds (the user's report), so the channel is real-time. The strict rules are in section 7.
13. **Alex with several classes in one day.** Decided: ask which to book (R16, R17a).
14. **Defaults and waiting.** Decided by the user on 2026-10-12: the script does not wait. It books first (all classes; for an overlap, the more crowded class), then asks, then cancels what the user declines (R17, R18). The question stays open until the class starts. After that the reminder is deleted and the booking stays. Still to verify: that the class details page shows the list of names, and that counting them works (R19).
15. **Names in messages.** Decided: first names. The reminders in iCloud show the sons' first names with class times.
16. **Handling answers.** The run does not wait. A later step reads the answers: the planner, or the hotkey, checks the list Club bookings for completed answer reminders and cancels the dropped options. This needs the Mac awake and unlocked at that time. How often should the check run?
17. **Delivery (history).** ntfy did not deliver messages to the lock screen in seven tests. Messages appeared only inside the app. These fixes were tried: notification permission, Background App Refresh, Low Power Mode off, the highest priority, a fresh topic and subscription, Scheduled Summary, alert types, Focus, and an iPhone restart. A web search found no ntfy-specific fix. **Decision on 2026-10-12: ntfy is dropped. The user chose Apple Reminders with strict rules (section 7).** Whatever the channel: keep a macOS notification on the Mac, a log entry for every question, and a timeout default.
12. **Event titles for the sons.** Decided: "<child's name> - <class>", set by a Calendar script after the event is saved.
18. **Age limits (Robin, 4).** A class can refuse a child who is too young. Decided by the user on 2026-10-13: the program tries; if the club refuses, it records the class and the reason in `refused.json`, closes the popup, tells the phone, and never tries that class again for that person. The text of the refusal popup has not been seen yet.
19. **Forced update.** The app can show "התחדשנו" (a new version) and block everything. The program reports "update the app" and stops. By hand: the popup button opens the App Store, press Update, open the app, log in again.

## 11. Cleanup of the whole setup (when the project ends)

- Remove Terminal from Privacy & Security > Screen Recording and > Accessibility.
- Delete `~/.mirroir-mcp` and the build folder in the session scratchpad.
- Decide whether to keep the Google account on this Mac (Internet Accounts).
- Remove Terminal's permission to control Calendar and Reminders (Privacy & Security > Automation).
- The ntfy app and its topics are no longer used. The user removes the ntfy app from the iPhone. The private file `~/.club-booking/ntfy.env` was deleted on 2026-10-12.
- Delete any launchd agent or hotkey that was created.

## 12. Packaging

Decided on 2026-10-12: **program + triggers + optional skill.**

- **The program (the core).** A command-line program, for example `club-booking`, with steps such as `plan`, `run`, `answers`, and `doctor`. It calls the Claude API directly for the Hebrew screens (key in the Keychain, section 7). It drives the mirroir-mcp tool over stdio, and it uses AppleScript for Calendar and Reminders. The programming language is a decision for the plan. Python is likely, because it is already on the Mac. Settings (the three people, their calendars, the selector rows, the reminder list) live in a config file.
- **The triggers.** A macOS `launchd` agent runs the daily planner, the booking runs at each opening time, and the check for answers. A hotkey runs `club-booking run` on demand. If the Mac is locked or asleep, the user gets a reminder (R12).
- **An optional Claude Code skill**, for example `club-booking`, built **after** the program works. It helps when a person and Claude are present: add favorites and check them, review a run, repair the steps when the app changes its screens, and remove the setup. The daily runs do not need it, because a skill runs only inside a Claude Code session.
- **Where things live.** The code is in `~/club-class-booker` (a git repository from 2026-10-12). The mirroir-mcp binary now sits in a temporary session folder. It must move to a stable place, for example `~/club-class-booker/tools/`, or be rebuilt there from the verified release (section 5).

**Plan split (2026-10-12).** The work is built in three plans:
1. **Booking engine and wake-ups** (R1 to R13, R20): reads the favorites of all three, books what is open, adds calendar events with an alert, reports to the iPhone, and wakes itself. Full classes are only reported. Alex's several classes in a day are all booked.
2. **Questions and cancel** (R14 to R19): waitlist, Alex's overlaps and several classes in a day, questions on the iPhone, cancel only what the program booked, and the crowd count.
3. **The Claude Code skill** (section 12).
