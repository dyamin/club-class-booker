# Install and use club-booking (a guide for another club member)

This program books your favorite classes in the country club's app by itself, 24 hours before they start. It adds the
class to your calendar (an alert one hour before). It writes to your phone only when it needs **a decision** (for example two
classes at the same time) or **an action** (for example the Mac is locked). Nothing else.

It was built for the Hasmonaim club app (the Mac app that runs the iPhone app, called "Runner" on the Mac). If your club uses
the same app, it can work for you. **Use it at your own risk:** it presses buttons in your own account, like you do. Read your
club's terms first (the program does not check them for you).

## 1. What you need

| Need | Why |
|---|---|
| A Mac with **Apple silicon**, macOS 14 or newer (tested on macOS 27.2 beta) | The club app is an iPhone app that runs on the Mac. |
| The **club app installed on the Mac** (App Store > your account > "iPhone & iPad apps") and **logged in** | The program uses the app's window. |
| The Mac **on, with a screen, unlocked** when a class opens | A locked Mac cannot be clicked. The program waits 20 minutes and tells your phone. |
| **Command Line Tools** (`xcode-select --install`) and **Python 3.10+** (`brew install python`) | To build the helper tools. |
| An **iPhone with iCloud Reminders** on the same Apple ID | The questions come as reminders. |
| The **Calendar** app with your calendars | The class events go there. |
| An **Anthropic API key** | A picture reader reads the confirmation pop-ups. The cost is a few cents a month. |

## 2. Install (10 minutes)

```
git clone <the repo address> club-booking
cd club-booking
bash scripts/install.sh
```
The script checks your Mac, makes the virtual environment, and builds three small helper tools. It stops with a clear message
when something is missing.

## 3. Setup (5 minutes)

```
.venv/bin/python -m club_booking setup
```
It asks for: the first name of each person, which calendar each person uses, the start of the event title, whether you want a
question when two classes overlap, your **phone number** (for the club app login) and the **API key**. The phone number and
the key go into the **Keychain**, never into a file. It writes `config.json`. If you open the person list of the club app
first, it finds the profile positions by itself.

## 4. Permissions (once)

Open System Settings > Privacy & Security. Give each of these to **Terminal** (and, for the scheduled runs, to the Python app
that `doctor` names):

| Permission | Where | For |
|---|---|---|
| Screen Recording | Privacy & Security > Screen Recording | The screen tool. |
| Accessibility | Privacy & Security > Accessibility | Reading and pressing the club app's buttons. |
| Automation | The first run asks: allow Terminal to control System Events, Calendar, Reminders and the club app | Calendar events, reminders, bringing the app to the front. |
| Full Disk Access | Privacy & Security > Full Disk Access | **Only** for the automatic login (it reads the login SMS). |

After you change a permission, **quit Terminal completely (Cmd+Q) and open it again.** A new window is not enough.
Then run:
```
.venv/bin/python -m club_booking doctor
```
Every line must say OK. A FAIL line says how to fix it.

## 5. First login

The club app needs a login with an SMS code. Two ways:
- **By hand:** `.venv/bin/python -m club_booking login request`, read the 6 digits from the SMS, then
  `.venv/bin/python -m club_booking login code 123456`. Press "send me a code" **once**: three requests lock the login for 10 minutes.
- **By itself** (recommended, because the app logs you out every few hours): turn on **Text Message Forwarding** on the iPhone
  (Settings > Apps > Messages > Text Message Forwarding > your Mac), give **Full Disk Access** to the Python app that `doctor`
  prints, put `"auto_login": true` in `config.json`, and run `.venv/bin/python -m club_booking login auto` once to test it.
  The program then logs in at a class opening when it must, reads only the club's SMS, and tells you only if that fails.

## 6. Start it

```
.venv/bin/python -m club_booking install     # the program now wakes at every class opening, by itself
.venv/bin/python -m club_booking status      # the plan and the next wake-ups
.venv/bin/python -m club_booking arm-test    # a test wake-up; the result is a Mac banner after 2 minutes
```
Add the classes you want in the club app's **Favorites**. The list is read at every wake-up: you can change it any time.

## 7. Daily life

- You do nothing. A booked class is in your calendar. **No message means all is fine.**
- **A question** appears as reminders in the list "Club bookings", one reminder per option. Tap the circle (**Mark as
  Completed**) on **one** option. The program reads it and carries it out (it wakes a few times while a question is open).
- **An action message** says what to do: unlock the Mac, open the club app window, log in (if the automatic login failed).
- Pause everything: create the file `~/Library/Application Support/club-booking/PAUSE` (delete it to go on).

## 8. Update, remove

- Update: `git pull`, then `bash scripts/install.sh` and `.venv/bin/python -m club_booking doctor`.
- Remove: `.venv/bin/python -m club_booking uninstall`, then delete the folder, the Keychain items `club-booking-phone` and
  `club-booking-anthropic`, and the folder `~/Library/Application Support/club-booking`.

## 9. If something is wrong

| You see | Do |
|---|---|
| `Club booking: the Mac is locked` | Unlock the Mac: it books when you do. |
| `the club app is logged out` | Log in (section 5). With `auto_login` the program does it; the message says why it failed. |
| `the club app window is not visible` | Open the club app (it may be closed or on another desktop). |
| `doctor` says FAIL | Read the line: it names the permission or the file. |
| After a macOS or Python update nothing works | Check the permissions again (section 4), run `doctor`. A Python upgrade by brew changes the Python app path: give Full Disk Access again. |
| A class was not booked | The reminder `N to check` names it. The log is `~/Library/Application Support/club-booking/runs.jsonl`. |

More: `docs/lessons.md` lists every problem we met and its rule, `docs/club-app-field-notes.md` describes the club app's screens.

## 10. What leaves your Mac

- **Anthropic API:** pictures of the club app window (the confirmation pop-ups, and the class list only when accessibility
  fails) are sent for reading. They can show class names and the names of the profiles.
- **Nothing else.** The phone number and the key stay in your Keychain. The login SMS is read on your Mac only, for one
  sender, and is never saved or logged. The program never touches a heart (favorite) and cancels only what it booked itself,
  and only after you answered a question.
