#!/bin/sh
# Generated fixtures and Mac-host checks only. No device/signing/tool setup.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd -- "$project_root"
check_directory=$(mktemp -d "${TMPDIR:-/tmp}/pocket-host-checks.XXXXXX")
check_directory=$(CDPATH= cd -- "$check_directory" && pwd -P)
trap 'rm -rf -- "$check_directory"' EXIT HUP INT TERM
module_cache="${TMPDIR:-/tmp}/pocket-stillroom-module-cache"
mkdir -p -- "$module_cache"
python3 -B -W error::ResourceWarning -m unittest discover -s Tests -p test_transfer_export.py
python3 -B Tests/make_transfer_fixture.py "$check_directory/python-fixture.stillroomlibrary"
swiftc -swift-version 5 -module-cache-path "$module_cache" Sources/PocketCore/*.swift Tests/TransferTests.swift -o "$check_directory/transfer-checks"
"$check_directory/transfer-checks" --python-fixture "$check_directory/python-fixture.stillroomlibrary"
swiftc -swift-version 5 -module-cache-path "$module_cache" Sources/PocketCore/*.swift Tests/RecoveryTests.swift -o "$check_directory/recovery-checks"
"$check_directory/recovery-checks"
python3 -B -m unittest discover -s Tools -p 'test_signing_preflight.py'
swiftc -swift-version 5 -module-cache-path "$module_cache" Sources/PocketAudio/*.swift Tests/AudioNativeChecks.swift -o "$check_directory/audio-checks"
"$check_directory/audio-checks"
swiftc -typecheck -swift-version 5 -module-cache-path "$module_cache" Sources/PocketCore/*.swift Sources/PocketAudio/*.swift
python3 -B Project/generate_project.py
plutil -lint App/Info.plist Project/PocketStillroom.xcodeproj/project.pbxproj
echo 'Mac-host checks passed. App UI/model and iOS-only branches are compiled by the separate simulator checks; this run does not validate phone behavior.'
