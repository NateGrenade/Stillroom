# Offline signing preflight

This local Python 3 helper inspects **one saved iOS `.app` directory** selected explicitly. It reads `Info.plist` and, if present, decodes `embedded.mobileprovision` with macOS `/usr/bin/security cms -D`. No keychain search, signing, private-key export, network, phone or install command is used.

```sh
python3 signing_preflight.py --app '/absolute/path/to/PocketStillroomDev.app'
```

The JSON reports a neutral profile category, bundle/profile match and actual expiry/days remaining. It omits account/team/device identifiers, certificate material, paths and captured CMS diagnostics. A simulator app without a profile returns **no_device_signing_evidence**, not an error. It does not pretend the profile is a free or paid account profile based only on its duration.

Exit status is 0 for an inspected profile or no profile evidence, 1 for an unreadable/invalid input, and 2 for a bundle/profile mismatch (or invalid command-line arguments). Expiry is evidence in the output, not a claim of installability. Metadata size limits and a 15-second CMS timeout keep the inspection bounded.

This is **not** signature, certificate-chain, revocation or install-eligibility validation. A saved build's expiry cannot establish the phone's installed build or SideStore's actual expiry. Nothing is refreshed or repaired. Keep screenshots and reports free of account details.

Run disposable tests from this directory:

```sh
python3 -m unittest -v test_signing_preflight.py
```

The tests use temporary app folders and synthetic property lists; the CMS subprocess is mocked where a profile is needed. They do not sign or inspect real credentials.
