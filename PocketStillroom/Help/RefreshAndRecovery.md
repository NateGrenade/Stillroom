# Keep the app opening and the music safe

## Check the signing route and expiry

Pocket Stillroom does not configure SideStore or perform signing refresh. Follow the chosen tool’s current official guide and check the installed apps’ actual expiry. A successful launch, new build number or offline inspection of a saved app bundle does not prove the phone’s current signing state.

Free Personal Team provisioning expires after seven days. If using SideStore, SideStore itself must also remain valid and uses one of the free account’s active app slots. See [Apple’s account guide](https://developer.apple.com/help/account/basics/about-your-developer-account) and the [SideStore FAQ](https://docs.sidestore.io/docs/faq). These limits are separate from music storage.

## Refresh both apps

Before setup, save an external music backup. Follow the current [SideStore installation guide](https://docs.sidestore.io/docs/installation/install), including its **Stable** choice, rather than inferring stability from a release marked Latest. Review account, certificate, pairing, trust and VPN decisions yourself.

After the selected SideStore setup has passed:

1. Connect Wi-Fi and LocalDevVPN as required by that release’s guide.
2. Open SideStore and select **My Apps**.
3. Refresh **SideStore itself** and **Pocket Stillroom**. Stop and read unexpected account or certificate prompts.
4. Check that each actual expiry indicator moves forward, then reopen both apps.

**Success looks like:** both apps have renewed expiry and open. A shortcut notification is insufficient evidence. The computer need not stay on for ordinary offline listening, but initial setup or recovery can still require it.

## Optional automation

A Refresh All Apps action exists in SideStore [development source](https://github.com/SideStore/SideStore/blob/develop/AltStore/Intents/App%20Intents/RefreshAllAppsIntent.swift). That alone does not prove the installed Stable release includes it or that it runs unattended. The action can require foreground continuation.

Try an available action manually and check both expiries. Only then test a daily automation, reviewing its permissions personally. Exercise several days, reboot, missed Wi-Fi and low-power mode, and keep manual refresh as a fallback. These guides create no automation and do not promise unattended renewal.

## Keep an independent backup

Use **Prepare backup → Share backup** and save the completed `.stillroomlibrary` outside Pocket Stillroom’s own folder. Keep another copy on a computer. Use **Cancel operation** during preparation if needed, then wait for the cancelled result and retain your previous independent backup.

Prove restoration with a small sample in an empty disposable library. Compare audio hashes, counts, playlist order, loops and supported settings. A backup filename or a Share sheet opening is not proof of a completed, restorable external copy. See [Import and offline listening](UseAndImport.md).

## If the app opens but its library cannot open

Keep the original library and backup. The recovery card offers **Choose backup → Review backup → Restore into a new library**. Review the intended file before confirming. The app validates a separate library and switches only after success; it retains the original and previous library selection.

Recovery requires room for another local copy. Damaged libraries and failed recovery copies remain, so repeated attempts can need more storage. Failed or cancelled work must not be treated as a successful switch. Ordinary import into a healthy library merges compatible IDs and refuses conflicts instead of replacing it in place.

Do not delete or reinstall a working app to create a recovery test. Simulator or host checks do not establish physical-device recovery. An iOS application update uses its signing/install route, separately from the desktop ZIP updater; preserve the app identity and verify library contents afterward.

## If signing expires

Do not delete Pocket Stillroom first: deleting an iOS app can remove its local data. Preserve exports already outside the app, note which apps still open, then follow the [current SideStore troubleshooting guide](https://docs.sidestore.io/docs/troubleshooting/common-issues).

If SideStore opens, try its manual refresh. If it cannot open, recovery may require the computer and current installer. An OS update or reset can affect pairing; consult the [pairing guide](https://docs.sidestore.io/docs/advanced/pairing-file). Handle renewed sign-in, pairing, trust and security approvals yourself.

Reinstallation is not guaranteed to preserve data. Understand backup and identity consequences before removing an app or changing its signing arrangement. **Recovery is proven when:** access returns through the selected route and an independent sample backup restores correctly. Multi-week refresh and missed-expiry recovery remain tests to perform, not guarantees supplied by these instructions.
