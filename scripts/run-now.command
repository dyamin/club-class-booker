#!/bin/bash
# Double-click this, or bind it to a keyboard shortcut. It opens in Terminal, so the run uses
# the Screen Recording and Accessibility permissions that Terminal already has.
cd "$(dirname "$0")/.."
.venv/bin/python -m club_booking run --no-countdown
echo
read -n 1 -s -r -p "Press any key to close this window."
