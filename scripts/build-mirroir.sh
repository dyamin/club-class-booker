#!/bin/bash
# Builds the pinned release of mirroir-mcp (open source, Apache-2.0) from source.
# The Homebrew formula needs full Xcode; this needs only the Command Line Tools.
set -euo pipefail
VERSION="0.40.1"
SHA256="d08c71aa4a972c65c9cfab4b28e4ebaa72296ffa3a2dfcc50e330fa262826e49"
cd "$(dirname "$0")/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/dl" "$WORK/src" tools/bin
curl -fsSL -o "$WORK/dl/mirroir.tar.gz" "https://github.com/jfarcand/mirroir-mcp/archive/refs/tags/v${VERSION}.tar.gz"
echo "${SHA256}  $WORK/dl/mirroir.tar.gz" | shasum -a 256 -c -
tar -xzf "$WORK/dl/mirroir.tar.gz" -C "$WORK/src" --strip-components=1
swift build -c release --package-path "$WORK/src"
cp "$WORK/src/.build/release/mirroir-mcp" tools/bin/mirroir-mcp
echo "built tools/bin/mirroir-mcp (v${VERSION})"
