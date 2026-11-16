#!/usr/bin/env bash
# Builds the app, boots the simulator, installs and launches it.
#
# Always regenerates the Xcode project: XcodeGen writes an explicit file list, so
# a newly added .swift file is invisible to Xcode until the project is rebuilt.
set -euo pipefail
cd "$(dirname "$0")/.."

DEVICE="${DEVICE:-iPhone 18 Pro}"
BUNDLE_ID="com.goalcalendar.app"

echo "==> Regenerating project from project.yml"
if [ -x ./.tools/bin/xcodegen ]; then
  ./.tools/bin/xcodegen generate --spec project.yml >/dev/null
elif command -v xcodegen >/dev/null 2>&1; then
  xcodegen generate --spec project.yml >/dev/null
else
  echo "    xcodegen not found — using the committed GoalCalendar.xcodeproj" >&2
fi

echo "==> Building for $DEVICE"
xcodebuild \
    -project GoalCalendar.xcodeproj \
    -scheme GoalCalendar \
    -destination "platform=iOS Simulator,name=$DEVICE" \
    -derivedDataPath .build/DerivedData \
    build

echo "==> Booting $DEVICE"
xcrun simctl boot "$DEVICE" 2>/dev/null || true
xcrun simctl bootstatus "$DEVICE" -b >/dev/null 2>&1 || true

echo "==> Installing"
xcrun simctl install booted ".build/DerivedData/Build/Products/Debug-iphonesimulator/GoalCalendar.app"

# Forward LLM credentials into the app process. SIMCTL_CHILD_-prefixed variables
# are passed through to the launched process.
CONFIG="$(pwd)/Config/env.local"
if [ -f "$CONFIG" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$CONFIG"
    set +a
    export SIMCTL_CHILD_LLM_BASE_URL="${LLM_BASE_URL:-}"
    export SIMCTL_CHILD_LLM_API_KEY="${LLM_API_KEY:-}"
    export SIMCTL_CHILD_LLM_MODEL="${LLM_MODEL:-}"
    echo "==> LLM credentials loaded (model: ${LLM_MODEL:-unset})"
else
    echo "==> No Config/env.local; app will run in offline mode"
fi

echo "==> Launching"
xcrun simctl launch booted "$BUNDLE_ID"
