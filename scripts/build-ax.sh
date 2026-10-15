#!/bin/bash
# Compiles the accessibility helper (reads and presses the club app's controls) with the Swift compiler of the Command Line Tools.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p tools/bin
swiftc -O tools/ax.swift -o tools/bin/ax
echo "built tools/bin/ax"
