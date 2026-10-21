# I’m stuck

Start with the visible symptom. Keep existing music and exports while investigating. Do not put passwords, verification codes, pairing files or signing material in a bug report.

| What you see | First thing to check | What success looks like |
| --- | --- | --- |
| No Xcode or available iOS simulator | Full compatible Xcode and an installed simulator runtime are required; Command Line Tools alone are insufficient. See [Setup](Setup.md). | A sample builds and opens on the chosen simulator. |
| Xcode cannot use the phone | Check the cable, unlocked state and visible trust/setup prompt, then Apple’s compatibility table. Avoid recreating credentials without a specific reason. | The intended device is available to the supported toolchain. |
| Trust, Developer Mode, VPN or account prompt | Personally review the exact purpose and official guide before acting. | The intended official tool recognizes the phone or opens. |
| Pocket Stillroom will not open | Check actual signing expiry. Preserve external backups; do not delete the app first. | It opens after verified refresh or recovery. |
| The app opens but the library does not | Use **Choose backup → Review backup → Restore into a new library** with an independent sample. Keep originals and allow room for a separate copy. | A validated new library opens and the original remains retained. |
| SideStore also will not open | Follow its external recovery guide; the computer may be required. Preserve backups before considering removal. | Both apps reopen with renewed expiry. |
| Refresh reports missing Wi-Fi or VPN | Compare Wi-Fi and LocalDevVPN state with the selected release’s official guide. Do not change DNS, accounts or VPN blindly. | Manual refresh completes and both expiries move forward. |
| Shortcut reports success without renewed expiry | Open SideStore and refresh manually. Record versions for a small reproducible report. | Actual expiry confirms renewal. |
| No phone/app in desktop export | Confirm a trusted USB connection, unlocked phone and supported Pocket Dev identity. Windows automatic transfer is unavailable. | The intended installed app is listed, without selecting an unrelated app. |
| A cable transfer result is unclear | Use **Check transfer** before retrying; the file may already exist. Retain receipts while investigating. | The exact completed, cancelled or uncertain state is established. |
| File visible but import cannot read it | Wait for a complete local download, then use **Browse Files**. A cloud filename alone is insufficient. | The entire bundle is local and review opens. |
| Import rejects a file or conflicting ID | Keep the source bundle and current library. Read the exact notice; check corruption, native codec support and storage. | A valid sample imports without overwriting conflicts. |
| Playback, loop or reverb sounds wrong | Record build, sample, loop times, speed, pitch mode and audio route. Compare 1× speed with dry reverb at comfortable volume. | A small case reproduces and passes the same check after correction. |

Simulator success does not prove phone background audio, Bluetooth behavior, battery use or recovery. Preserve library and signing identity while isolating a failure; reinstalling is not a guaranteed data-preserving repair.
