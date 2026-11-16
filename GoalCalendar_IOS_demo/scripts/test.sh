#!/usr/bin/env bash
# Runs the logic-layer test suite on macOS — no simulator required.
#
# The flags are not optional here: SwiftPM wraps manifest compilation in its own
# sandbox-exec, which cannot nest inside this workspace's sandbox. Redirecting the
# cache paths keeps it from touching ~/Library/Caches (also denied).
set -euo pipefail
cd "$(dirname "$0")/../GoalCalendarKit"

export CLANG_MODULE_CACHE_PATH="$PWD/.build/modulecache"
exec swift test \
    --disable-sandbox \
    --scratch-path .build \
    --cache-path .build/cache \
    "$@"
