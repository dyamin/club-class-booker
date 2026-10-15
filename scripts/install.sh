#!/bin/bash
# One command to install club-booking on a Mac: virtual environment, the three helper tools, the screen-tool settings.
# Run it from the repo folder:  bash scripts/install.sh
# It stops with a clear message when something is missing. It does not need an administrator password.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "STOP: $1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this program needs macOS."
[ "$(uname -m)" = "arm64" ] || fail "this program needs a Mac with Apple silicon (the club app is an iPhone app that runs on it)."
xcode-select -p >/dev/null 2>&1 || fail "the Command Line Tools are missing. Run: xcode-select --install   then run this script again."

PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
[ -n "$PYTHON" ] || fail "Python 3.10 or newer is missing. Install it with Homebrew: brew install python"

echo "1/4 virtual environment ($PYTHON)"
"$PYTHON" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "2/4 helper tools (accessibility reader, text reader, screen tool) - a few minutes"
bash scripts/build-ax.sh
bash scripts/build-ocr.sh
bash scripts/build-mirroir.sh

echo "3/4 screen tool settings"
.venv/bin/python -m club_booking init-mirroir

echo "4/4 done."
echo
echo "Next, in this order (the guide is docs/INSTALL.md):"
echo "  1. .venv/bin/python -m club_booking setup    (people, calendars, phone number, key)"
echo "  2. give the permissions the guide lists      (Screen Recording, Accessibility, Automation)"
echo "  3. .venv/bin/python -m club_booking doctor   (every line must say OK)"
echo "  4. .venv/bin/python -m club_booking login request   then   login code <6 digits>"
echo "  5. .venv/bin/python -m club_booking install  (the program then runs by itself)"
