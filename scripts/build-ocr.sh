#!/bin/bash
# Compiles the small OCR helper (English text and positions) with the Swift compiler that ships with the Command Line Tools.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p tools/bin
swiftc -O tools/ocr.swift -o tools/bin/ocr
echo "built tools/bin/ocr"
