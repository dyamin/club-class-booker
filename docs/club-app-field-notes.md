# The club app: field notes

Facts checked on the real app on 2026-10-13. Read this before you change the screen code. Positions are in **window points** (the window is 1024 x 800; the top-left corner is 0,0).

## What runs where

- The club app is the Mac app **Runner 2** (process `Runner`, bundle `il.co.offline.hashmonaim`). It is an iPhone app that runs on Apple silicon. Its window title is "חשמונאים".
- The window can move between displays. On a Retina display the screen tool returns **native pixels** (2048 x 1600). Every position in the program is in points, so `MirroirScreen.screenshot()` scales the picture to the window size.
- The screen tool (`mirroir-mcp`) can **tap and swipe**. Its settings allow only `tap`. It cannot type.
- The accessibility helper `tools/ax` (source `tools/ax.swift`, build `scripts/build-ax.sh`) reads and presses the controls of the app through macOS accessibility. A dump takes about 0.7 seconds.

## Screens

| Screen | How to know it | Notes |
|---|---|---|
| Login | text "כניסה עם טלפון", field "מספר הטלפון שלך", button "שלחו לי קוד" | The field shows up as a **button** to accessibility. |
| Code | "אימות כניסה", "הקוד שקיבלת", 6 boxes, "קוד נשלח לטלפון ..." | 6 digits by SMS. |
| Profile selector | "בחירת מועדון", rows "קאנטרי חשמונאים - <name>" | See below. |
| Classes | bottom tab "שיעורים" at (640, 775) | Three sub-tabs. |
| Class details | tap a card | Shows the **first names of the registered people** (useful for plan 2, the crowd count). X at (1001, 70). |
| Update popup | "התחדשנו", "גרסה חדשה של האפליקציה זמינה להורדה. אנא עדכנו גרסה על מנת להמשיך", button "לחצו לעדכון" | Blocks everything. See "Problems". |

**Profile selector.** The same screen shows the three profiles. The selected row is filled blue (about 76,159,213) with a check mark. The rows are at y = 176, 214 and 253. The X at (1001, 70) closes it. A tap on a row switches the profile. When the app has no session, this screen shows a spinner for ever.

**Sub-tabs of Classes** (y = 108): "לו"ז שיעורים" (schedule, x = 860), "מועדפים" (favorites, x = 512), "מוזמנים" (booked, x = 170). The selected sub-tab has a blue line under it at y = 126 (about 90,164,215). The program uses that line to check the tab. The order of the cards in the favorites list is **not** chronological.

## A class card

Accessibility gives the whole text of a card, also when the card is cut off by the edge of the screen:

```
שלישי            weekday
13/10            date
|
שעה: 08:06 עד 08:51
הסר מהמועדפים    (only on some cards)
METCON           title
מאמן <name> / שיעור אולם ייעודי
רשום             status line (not always there)
```

| State | What the card shows |
|---|---|
| open | no status line; a blue button **"הרשמה"** (the center is about x = 512) |
| not open yet | status line **"פתיחת הרשמה: 15/10 07:45"** (accessibility) or "פתיחת הרשמה: 07:45 15/10" (on the screen: time first); a blue button "תזכורת" |
| registered | grey **"רשום"** |
| closed | grey **"סגור להרשמה"** (for example a class that starts soon) |
| full | button "הרשמה להמתנה" (not seen yet on the real app today) |
| on the waitlist | text "בהמתנה" with a place (not seen yet today) |

A booked card showed a **"ביטול"** button at about (47, 557) in the earlier test. After a booking the app shows "זהו, נרשמת!" with two buttons: save to calendar (459, 757) and OK (579, 757). After a cancel it shows "ההרשמה בוטלה בהצלחה!" with "סגור" at (512, 755).

## The booked tab and the class page (verified 2026-10-13)

- **The booked tab** is the third sub-tab, **"מוזמנים"** (tap about (170, 108); the blue line under it at y = 126). Its heading is "שיעורים קרובים". It lists **every upcoming class the selected profile is registered for**, not only favorites (the old note that it was empty was wrong, or the list was loading). A card has the status line **"נרשמת"** and a **"ביטול"** button (62 x 37, near x = 15). A waitlisted card should show "בהמתנה" and a "הסרה מהמתנה" button (not seen yet).
- **The class page** opens by pressing a card (accessibility: `AXPress` on the card's `AXButton`). It shows a back button **"חזרה לעמוד קודם"**, the title, the date and time, "תיאור השיעור", "משך השיעור: 40 דקות", and **one text per person who registered**: a first name and an initial ("קרן י."). Counting those texts gives the crowd (spec R19). At the bottom it shows the status ("נרשמת") and the "ביטול" button.
- **Cancel** is `AXPress` on the button "ביטול" **inside the one card** of the class (`tools/bin/ax press-in-card`: the card, found by its exact text, must be on the screen exactly once, and only a button inside its frame is pressed). The program never uses a guessed position or a height hint for it. After the press the screen is read: the only message ever seen is "ההרשמה בוטלה בהצלחה!" with "סגור".
- **A display that is asleep** (or a locked Mac) gives a black screenshot, and the app's window has size 0 for accessibility; the screen tool answers "Failed to capture screenshot" or "window not found". The program treats it like a locked Mac.

## The waitlist (seen on the real app, 2026-10-14 11:23)

- A full class shows the button **"הרשמה להמתנה"**. The program tapped it, and a **bottom sheet** appeared with a clock icon, the title **"נכנסת להמתנה"** (you entered the waitlist), the class (title, trainer, "חמישי 15/10 | 08:45 - 09:30") and **one button "מצויין, תודה"** (note the spelling: "מצויין", not "מצוין") at the bottom centre, about (512, 757). The program closes it through accessibility by that button's text.
- Afterwards the card shows the status line **"בהמתנה"** and **"מיקומך בתור: 2"** (your place in the queue), with a **"הסרה מהמתנה"** button (113 x 31). The card is also listed on the booked tab.
- A class can be full the minute it opens: the 08:45 class of 2026-10-14 was full when the program first looked (the Mac was locked at 08:45).

## A class the club refuses (for example, the child is too young)

Some classes have an age limit; a registration can be refused with a message popup. The program handles it like this: the reader names the popup `refused` and gives its text; the class is written to `refused.json` in the data folder with the reason and the time, so it is **never tried again** for that person; the popup is closed (accessibility: its "סגור" button); the person's other classes go on; the phone gets one line "Refused: <person> - <class>: <reason>". The exact popup text has not been seen yet: add it here when it happens.

## What the app does that surprises you (and what the program does about it)

| What happens | Cause or fact | What the program does |
|---|---|---|
| The tap on a tab is lost | The first click on a window that is **not in front** only brings it to the front | Activates the app before a run, checks the blue line under the tab, taps again (3 tries) |
| The list jumps by many cards | A swipe makes the list **fling**, even for 80 points | Stops with an error when two screens share no class. The list is read with accessibility page scroll (see issue 2) |
| A drag opens a card | A drag that starts on a card can open its details page | Do not start a swipe on a card; close the page with the X |
| Typing does nothing | The text field is not focused, or another app is in front | Focus it with an accessibility press; check the front app right before typing |
| The app shows the selector with a spinner | No session (logged out) | Looks at the screen once; on the login screen it stops and tells the phone. It never restarts the app for a login |
| The app logs out by itself | A forced update (new version) invalidates the session | Same as above; the user updates the app and logs in |
| "ניסית 3 פעמים, מסך הכניסה ננעל למשך 10 דקות" | **3 code requests lock the login for 10 minutes.** Every press on "שלחו לי קוד" counts | The program logs in by itself only when `auto_login` is on (the user changed the old rule "never" on 2026-10-15; see README, "Automatic login"). For a supervised login: press the button **once** and keep the result |
| "יש להזין מספר טלפון נייד תקין" | The phone field holds more than the number (old text is kept; typing appends) | Clear the field, type the number, read the field with OCR, and only then press send |
| "שגיאת כניסה למערכת" | Login failed (often together with an update popup) | Report; the user decides |

## Login by hand (supervised)

1. Bring the app to the front, press the phone field through accessibility, check that the front app is `Runner`, clear the field (select all, delete), and type the number. Read the field with OCR: it must be exactly the number.
2. Press "שלחו לי קוד" **once**. Wait for the code screen.
3. Read the 6-digit code from the SMS (the user can allow the iPhone Mirroring window for this; look only at the newest login SMS, capture only that window, and close iPhone Mirroring after).
4. Type the code in the same way (front app check first).

**Reading the code on the Mac (done on 2026-10-13, the user allowed it).** Open `iPhone Mirroring`; the window is small (about 304 x 671 points) and can be on another display, so read its position with System Events (`position`, `size` of window 1 of the process) and capture only that region with `screencapture -x -R x,y,w,h`. The iPhone shows its home screen: Messages is in the dock (about (120, 612) inside the window). Activate the window first, then click (the first click on an inactive window only activates it). The newest message is from **"Hashmonaim."** and reads "NNNNNN הוא קוד האימות החד פעמי שלך והוא בתוקף ל 5 דקות הקרובות": 6 digits, valid 5 minutes, arrives within a minute. Look only at that message; do not use anything else on the screen. Type the code within the 5 minutes, then quit iPhone Mirroring.

**How long a session lasts.** The login of 2026-10-13 12:00 was still valid at 15:33 and gone by 19:05 (the app showed the login screen again): a session lasts between 3.5 and 7 hours, or it ends when the Mac sleeps for a long time. Plan for a new login about once a day. The program tells the phone when it finds the app logged out, and the 5-minute tick now also looks one hour before an opening (see `docs/lessons.md`, row 10d).

Never put the phone number in the repository. It is in the user's own notes.

## Update by hand

The popup button "לחצו לעדכון" opens the App Store page of the app. The App Store shows an **Update** button (press it through accessibility). The app quits during the update. When the button says **Open**, open the app again. The session is lost: log in again.

## Calendar on this Mac (measured 2026-10-13)

- The calendars **Alex** (1139 events) and **Kids** (1253 events) are Google calendars. Any `whose` filter over one calendar takes **20-30 seconds**, whatever the range. Counting all events takes 1 second; reading all start dates in one request takes 18 seconds; there is no fast query in AppleScript or JXA.
- A filter inside `repeat with e in (every event ... whose ...)` runs again at every property read. One event took 181 seconds (limit 90). Run the filter once into a variable. See `docs/lessons.md`, row 10b.
- Creating an event takes about 3 seconds. Reading the new event's alert inside the same script costs nothing; asking Calendar again costs another pass.
- Events returned for a window can include a repeating event with the date of its first occurrence.
- A faster way is an EventKit helper (a small Swift tool, like `tools/ax`); it needs the Calendars permission for the program that starts it. Not done yet.

## Accessibility facts

- Roles seen: `AXButton` (cards, buttons, the phone field), `AXStaticText` (texts, tab labels, the heading "שיעורים מועדפים"), `AXScrollArea` (the list; actions `AXScrollDownByPage`, `AXScrollUpByPage`, `AXScrollToVisible`).
- A card is one `AXButton` whose description is the card text. The heart ("הסר מהמועדפים") and the action button ("הרשמה", "תזכורת") are separate buttons after it, in document order. Each action button appears **twice** with the same position.
- Elements outside the visible area have height 0 and y = 32. The list is virtual: about one card above and below the screen is present.
- The phone field is an `AXButton` whose text never shows in accessibility (its description stays "מספר הטלפון שלך"). Read what is typed with the OCR helper on a screenshot (the field is at y = 246 to 283).
- `AXPress` works without focus and without coordinates. Typing does not: it needs the front app.
- The helper needs the Accessibility permission for the program that starts it (Terminal now; Python when launchd starts the job, which Task 11 must check).
