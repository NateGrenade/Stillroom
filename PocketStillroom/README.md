# Pocket Stillroom

Pocket Stillroom is Stillroom’s native iPhone companion prototype. Its source lives in this folder of the Stillroom repository; it has no separate dependency download, backend service or cloud sync requirement.

SwiftUI provides the library and player interface. AVAudioEngine provides native playback with bounded decoding buffers, saved A–B loops, seeking/repeat, continuous 0.5–2× speed, optional pitch preservation and smooth reverb. Imported recordings become a managed local offline library. The app supports background playback and lock-screen controls.

## Source layout

- `App/`: library, player, import review, recovery, backup and help screens.
- `Sources/PocketAudio/`: native playback and bounded audio reading.
- `Sources/PocketCore/`: validated library bundles and transactional local storage.
- `Desktop/`: read-only, byte-preserving desktop library exporter.
- `Help/`: setup, listening, refresh and recovery guides; video links are optional placeholders.
- `Project/`: Xcode project, dependency-free generator and simulator scripts.
- `Tests/` and `Tools/`: generated-fixture checks and an offline signing preflight.

Private test reports, device backups, recordings, accounts, signing profiles and generated build products are intentionally excluded. The desktop release ZIPs do not contain an iPhone app.

## Build and test

Use a compatible full Xcode installation and installed iOS simulator runtime. Command Line Tools alone cannot build the iOS application. The deployment minimum is iOS 17. Consult Apple’s [Xcode compatibility table](https://developer.apple.com/xcode/system-requirements) for the host, SDK and device versions you actually use.

From this directory:

```sh
python3 -B Project/generate_project.py
Project/build_simulator.sh
xcrun simctl list devices available
Project/test_simulator.sh <available-simulator-UDID>
```

The scripts use existing tools and do not accept agreements, download tooling or create signing credentials. Simulator testing does not require an Apple signing identity. Host checks exercise generated fixtures separately:

```sh
Tests/run_host_checks.sh
Tests/run_received_checks.sh
python3 -B -W error::ResourceWarning -m unittest discover -s Tests -p 'test_*.py'
python3 -B -m unittest discover -s Tools -p 'test_*.py'
```

`Package.swift` also describes the native libraries. The Xcode application target is `Project/PocketStillroom.xcodeproj`, scheme `PocketStillroom`. The source declares **0.1.0 build 4**, development identity `com.nategrenade.stillroom.pocket.dev`, and no development team. Building for a phone requires your own valid signing configuration, trust and any required device setup; source publication does not supply a signed or installable IPA.

## Transfer music

Desktop Stillroom’s **Export to iPhone** interface can choose a song, complete playlist, ordered custom selection or all songs and playlists. Current automatic USB transfer is Mac-only, uses an existing trusted cable connection, and targets the exact Pocket Dev identity above. It does not establish pairing automatically. Windows does not offer automatic cable transfer; file transfer is a separate manual workflow.

On iPhone, open **+ → On this iPhone**, review a completed received bundle and import it. **Browse Files** supports a `.stillroomlibrary` supplied through Files/iCloud Drive/AirDrop or supported file-sharing workflows. If the app is re-signed under a different identity, automatic desktop discovery must be tested rather than assumed.

See [Desktop/README.md](Desktop/README.md) for standalone export and format details. A `.stillroomlibrary` is a bounded versioned manifest followed by unchanged original compressed audio bytes, not a ZIP or live SQLite database. It preserves IDs, supported metadata, playlist membership/order, saved loop bounds and SHA-256 hashes. Accounts, cookies and runtime files are excluded. Unsupported native decoders are reported instead of silently transcoding.

Use **Prepare backup → Share backup** to retain an independent backup outside the app. Recovery validates and switches to a separate new library; it does not overwrite a damaged library in place. Keep original desktop media and prove restoration with a disposable sample first. Deleting or reinstalling a signed iOS app is not a guaranteed data-preserving recovery method.

## Validation and remaining work

Generated-fixture host and simulator checks cover storage/import/recovery, audio behavior and UI flows. Initial iPhone 15 Pro testing confirmed playback, loops, speed/pitch, reverb, locked playback/controls, the revised UI and build-4 cable delivery/import. These are smoke checks, not evidence of every interruption, output route, long-session battery result or multi-week signing recovery.

Normal-user distribution still needs a clean re-signable IPA, identity/discovery validation, a small signing/SideStore pilot, manual refresh and expiry-recovery tests. The seven-day free-signing limit applies to the companion and SideStore itself where used. Current setup guidance requires Wi-Fi and LocalDevVPN; neither SideStore nor unattended refresh is configured by this source. See [Help](Help/README.md).

Direct YouTube extraction, automatic cloud synchronization and an App Store distribution path are outside this prototype.
