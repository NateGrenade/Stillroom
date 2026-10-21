# Set up Pocket Stillroom

## Choose a supported build and toolchain

The published companion source is a development prototype, not an installable iPhone release. Building it requires full Xcode on a compatible Mac, an installed iOS SDK and a suitable simulator or device. Command Line Tools alone are insufficient. Check Apple’s [Xcode system requirements](https://developer.apple.com/xcode/system-requirements), which distinguish the included SDK from supported device operating systems. Use Apple’s [official downloads](https://developer.apple.com/download/all/?q=Xcode) or Mac App Store.

Build and test on a simulator first, using the source README. A successful simulator run does not prove phone audio routes, background behavior, battery use or signing recovery. Do not repeat working account or trust setup without a specific reason.

## Install a development build through Xcode

Signing permits a particular app build to run on a device. A Personal Team is the free signing option associated with your Apple Account. Use your own account and valid signing configuration; this repository contains no credentials or provisioning profile.

1. Connect the iPhone with a data-capable cable and unlock it.
2. Review **Trust This Computer** and enter the phone passcode personally if requested.
3. Add your intended Apple Account in Xcode and handle sign-in and two-factor authentication yourself. Never put credentials in source files or screenshots.
4. Select your development team and the intended app identity. Review certificate or profile changes before approving them; other development apps can be affected.
5. If iOS requires Developer Mode or developer trust, follow Apple’s current prompt yourself. These changes may require restarting the device.
6. Build and open the app using a small sample library. Check the actual provisioning expiry separately; a new build number does not prove a new expiry.

**Success looks like:** the intended app opens on the phone and plays a sample. Test seek, speed/pitch, reverb, loops, locked playback and an interruption before importing a large collection.

## Optional SideStore route

SideStore is a candidate for renewing free signing away from the computer. It is not installed or configured by Pocket Stillroom. A clean re-signable IPA and a tested setup/refresh/recovery path are separate distribution prerequisites.

Read the current [prerequisites](https://docs.sidestore.io/docs/installation/prerequisites) and [installation guide](https://docs.sidestore.io/docs/installation/install) for your platform and iOS version. Select the guide’s **Stable** choice; a GitHub **Latest**, alpha or nightly label is not a substitute for that instruction.

The guide currently uses iloader for initial setup, an Apple Account, Wi-Fi and LocalDevVPN. You personally handle downloads, sign-in, passcodes, trust, certificate, pairing, Developer Mode and VPN decisions. Read warnings before accepting them, especially certificate replacement. No paid enrollment is implied by this prototype.

Finish SideStore’s first self-refresh before adding Pocket Stillroom. Verify that **both** apps show renewed expiry and reopen. Keep pairing and signing files private. Do not rely on an automation until manual refresh works and repeated tests establish its behavior.
