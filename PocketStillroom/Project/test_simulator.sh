#!/bin/sh
# Generated fixtures on an already installed simulator only. No tool downloads,
# agreements, signing, phone setup or desktop-library access.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
simulator_id=${1:?Usage: Project/test_simulator.sh simulator-UDID}
if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
  echo 'Finish Xcode first-launch setup personally; this script accepts no agreements.' >&2
  exit 2
fi
xcrun simctl list devices available --json | python3 -c '
import json, sys
devices = json.load(sys.stdin)["devices"]
if not any(d["udid"] == sys.argv[1] for group in devices.values() for d in group):
    sys.exit("Choose an available simulator UDID from: xcrun simctl list devices available")
' "$simulator_id"
python3 -B "$project_root/Project/generate_project.py"
result_path="$project_root/Reports/Simulator-$(date -u '+%Y%m%dT%H%M%SZ').xcresult"
exec xcodebuild -project "$project_root/Project/PocketStillroom.xcodeproj" \
  -scheme PocketStillroom -configuration Debug \
  -destination "platform=iOS Simulator,id=$simulator_id" \
  -parallel-testing-enabled NO -derivedDataPath "$project_root/DerivedData" \
  -resultBundlePath "$result_path" CODE_SIGNING_ALLOWED=NO test
