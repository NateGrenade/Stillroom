#!/bin/sh
# Build only. This script never installs tools, accepts agreements or signs apps.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -z "${DEVELOPER_DIR:-}" ] && [ -d /Applications/Xcode.app/Contents/Developer ]; then
  DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
  export DEVELOPER_DIR
fi
if ! xcrun --find xcodebuild >/dev/null 2>&1 || ! xcrun --find simctl >/dev/null 2>&1; then
  echo 'Blocked: full compatible Xcode and iOS simulator tools are required. Read Help/Setup.md. No installation or account change was attempted.' >&2
  exit 2
fi
if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
  echo 'Blocked: Nathan must finish any Xcode agreement/first-launch setup himself. No agreement was accepted by this script.' >&2
  exit 2
fi
python3 -B "$project_root/Project/generate_project.py"
exec xcodebuild -project "$project_root/Project/PocketStillroom.xcodeproj" -scheme PocketStillroom -configuration Debug -destination 'generic/platform=iOS Simulator' -derivedDataPath "$project_root/DerivedData" CODE_SIGNING_ALLOWED=NO build
