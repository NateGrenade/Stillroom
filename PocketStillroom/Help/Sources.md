# Official setup and recovery sources

Requirements can change. Check the current guide for the exact tool release and platform you select.

## Apple

- [Xcode compatibility](https://developer.apple.com/xcode/system-requirements): host requirements, included SDKs and supported device operating systems are separate constraints.
- [Official Xcode downloads](https://developer.apple.com/download/all/?q=Xcode): use official Apple tooling and personally review sign-in or agreements.
- [Account basics](https://developer.apple.com/help/account/basics/about-your-developer-account): Personal Team limits, seven-day provisioning and periodic reprovisioning. Inspect actual installed signing state rather than inventing an expiry.
- [Finder file sharing](https://support.apple.com/en-us/119585) and [Apple Devices file sharing](https://support.apple.com/en-us/120402): manual file transfer routes; not evidence that every route works with every app identity.

## SideStore

- [Prerequisites](https://docs.sidestore.io/docs/installation/prerequisites) and [installation](https://docs.sidestore.io/docs/installation/install): initial computer setup, chosen Stable release, Apple Account and current Wi-Fi/LocalDevVPN requirements. Ordinary offline listening is separate.
- [FAQ](https://docs.sidestore.io/docs/faq): free-signing duration and app-slot limits.
- [Common issues](https://docs.sidestore.io/docs/troubleshooting/common-issues) and [pairing](https://docs.sidestore.io/docs/advanced/pairing-file): refresh and recovery, including cases requiring a computer. Preserve independent backups before any app removal.
- [Development RefreshAllAppsIntent](https://github.com/SideStore/SideStore/blob/develop/AltStore/Intents/App%20Intents/RefreshAllAppsIntent.swift): source for a refresh action and foreground continuation. Development code does not prove Stable availability or unattended reliability.

Follow the installation guide’s Stable choice. Do not substitute an alpha or nightly merely because it is labeled Latest or contains a desired feature. Account, passcode, certificate, trust and VPN decisions belong to the user.

These links describe setup possibilities, not validated refresh, recovery or distribution for a particular phone. The source README states the prototype’s validation limits. Keep device identifiers, credentials, pairing files and signing material out of guides and reports.
