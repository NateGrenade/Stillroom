#!/bin/sh
# Generated temporary transfers only. No app build, simulator, signing or device.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
check_directory=$(mktemp -d "${TMPDIR:-/tmp}/pocket-received-checks.XXXXXX")
trap 'rm -rf -- "$check_directory"' EXIT HUP INT TERM
module_cache="${TMPDIR:-/tmp}/pocket-received-module-cache"
mkdir -p -- "$module_cache"
swiftc -swift-version 5 -module-cache-path "$module_cache" \
  "$project_root/Sources/PocketCore/PocketModels.swift" \
  "$project_root/Sources/PocketCore/PocketReceivedLibraries.swift" \
  "$project_root/Tests/ReceivedLibrariesTests.swift" -o "$check_directory/received-checks"
"$check_directory/received-checks"
