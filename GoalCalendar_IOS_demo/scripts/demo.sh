#!/usr/bin/env bash
# Prints a terminal walkthrough of intent parsing and scheduling.
# Pass --llm to also call the real model and compare against the rule engine.
set -euo pipefail
cd "$(dirname "$0")/../GoalCalendarKit"

# Config lives one level up, shared by every script.
CONFIG="$(cd .. && pwd)/Config/env.local"
if [ -f "$CONFIG" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$CONFIG"
    set +a
fi

export CLANG_MODULE_CACHE_PATH="$PWD/.build/modulecache"
exec swift run --disable-sandbox --scratch-path .build --cache-path .build/cache goaldemo "$@"
