# Import music and listen offline

Begin with a small sample library. Keep the original export until the imported sample and an independent backup have been checked. A completed cable copy does not itself prove import, every Files/iCloud/AirDrop route or phone backup recovery.

## Copy from the Mac desktop app

1. Connect the iPhone by USB and unlock it. Review any **Trust This Computer** request personally. The exporter uses existing trust; it does not set up credentials or change pairing for you.
2. In desktop Stillroom, choose **Export to iPhone**. Select all songs, one playlist in saved order, or individual songs.
3. Choose **Review selection**, check the count and bundle size, then select the exact **iPhone and installed app**. Use **Refresh devices** if needed.
4. Choose **Copy to iPhone**. Keep Stillroom running and the cable connected until the result is confirmed. Closing the dialog does not cancel a transfer; use **Cancel transfer** and wait for its result.

Then, in the cable-enabled iPhone build:

1. Tap **+** (**Import library**).
2. Under **On this iPhone**, choose the received bundle; the refresh button checks for newly copied files.
3. Review its contents, then choose **Import into offline library**. Copying the file alone does not add playable songs to the library.

If a desktop copy or cancellation ends with an unclear result, use **Check transfer** before **New export** or retrying. The completed file may already be on the phone. Keep the desktop's local transfer receipt and any incomplete staging copies while investigating; do not delete unknown files to force a retry.

The cable copy checks for bundle size **plus 32 MiB** of free space. Import needs more room for its managed offline audio and performs its own check. A successful copy does not guarantee enough space for import. The Mac exporter reads its source audio and library database without modifying them. This runtime path needs no Xcode but uses a private macOS interface, so compatibility can change.

Direct Windows cable export is not implemented. The separate [Apple Devices file-sharing route](https://support.apple.com/en-us/120402) is manual: **Files → Pocket Stillroom → Add File → Add**. [Finder file sharing](https://support.apple.com/en-us/119585) is also available as a manual Mac fallback.

## Files and iCloud Drive alternatives

1. On the Mac, create a **`.stillroomlibrary`** transfer file with the standalone desktop exporter. This file contains copies of audio and library information. It is different from **Download M4A copy**, which shares a single rendered song and does not carry a complete playlist or editable loop settings.
2. Keep an independent copy on the Mac. Transfer another copy by [AirDrop](https://support.apple.com/guide/mac-help/use-airdrop-to-send-items-to-nearby-devices-mh35868/mac), iCloud Drive or Finder file sharing when the selected prototype supports that route. Each complete phone route needs its own check.
3. If the file is in iCloud Drive, open **Files → Browse → iCloud Drive**, then touch and hold the file → **Keep Downloaded**. Wait for it to finish before import. Apple says this option is absent if the file is already local; a visible filename alone does not prove all its bytes are on the phone. See [Apple's download instructions](https://support.apple.com/guide/iphone/transfer-files-iphone-a-storage-device-iphe9aff429a/ios).
4. Tap **+** (**Import library**) → **Browse Files**, then select the transfer file. An empty collection also offers **Import your library**. A file copied into the app's Documents folder can appear under **On this iPhone** instead.
5. Review the song and playlist counts, any duplicate or unsupported-file notice, and required free space. If they do not match the sample export, cancel and keep the current library.
6. In **Review library**, choose **Import into offline library** and wait for the result. Cancelling should leave the working library unchanged. A damaged file should produce a clear failure, not a partially visible collection.

**Success looks like:** the sample songs and ordered playlist appear, their loop bounds match, and the app reports the finished import. A repeated identical import should not create another copy of each track. A conflicting track ID should be explained rather than silently overwriting a saved song.

Importing into a healthy library adds compatible items and keeps matching IDs. It is not an in-place replacement or rollback: conflicting IDs are refused. Keep the source file if you need to investigate a difference.

## Check that the songs are really offline

1. Pause playback. Turn on airplane mode for the listening test.
2. Open a newly imported song, play it, seek forward and switch to another song.
3. Open the sample playlist and check its order. If it contains a saved loop, listen across several repeat boundaries.
4. Lock the screen and try the displayed playback controls. Unlock and confirm the queue position remains correct.

**Success looks like:** the imported copies play without the Mac or network. This first version manages local copies; it does not sync live edits back through iCloud. Airplane-mode launch, restart persistence and the exact transfer route should be recorded as distinct tests rather than inferred from locked playback.

## Save a backup outside the app

1. Choose **Prepare backup**, then **Share backup** when it appears. While preparation is running, use **Cancel operation** if needed and wait for the cancelled result. Do not treat temporary output as a saved backup.
2. Use **Save to Files** or another chosen destination to keep a copy outside Pocket Stillroom's own folder. A file left only in the app's local folder can disappear if the app is removed.
3. Keep another copy on the Mac. Record the expected song/playlist counts without exposing private titles in a public report.
4. Prove restoration with a small export in an empty disposable test library. Do not delete the working phone app or library to create that test. A desktop or simulator test is safe preparation; phone recovery remains a separate gate.

**Success looks like:** the restored sample has matching audio hashes, IDs, playlist order, loop bounds and supported settings. A Share sheet opening alone is not evidence the backup was saved externally or restored.

## If the app opens but the library cannot open

Use this flow only when the app reports that its library cannot open. Prove restoration separately with a disposable sample; do not damage a working library to expose the recovery screen.

1. Keep the original library and an independent backup. Do not reinstall or delete data to make the recovery card appear for a test.
2. On the recovery card, choose **Choose backup**, then review the selected file in **Review backup**. Check the counts and notices. Cancel if it is not the intended backup.
3. Choose **Restore into a new library**. Recovery creates a separate local library and checks its audio and file hashes before switching the app to that copy. The original library and the previous library selection are retained.
4. Wait for a clear result. If validation fails or you cancel before completion, keep the backup and original files; do not assume there is a usable new library.

Recovery needs additional free space for the new copy. The damaged library and failed recovery copies are retained, so retries may require more space. No cleanup of those copies is implied. This flow differs from ordinary import into a healthy library; it does not overwrite the damaged library in place.

**Success looks like:** after the validated switch, the sample library opens with the expected audio, IDs, playlist order, loops and settings. Prove this first with a disposable fixture, then separately on the phone when an agreed test is available.

## Speed, pitch and reverb

On compact screens or at larger text sizes, scroll the page for all playback controls. Tap the current speed to expand its slider. It moves continuously from 0.5× to 2× and gently snaps near familiar rates. **Pitch follows speed** starts on: speeding up also raises pitch. Turn it off to keep the original pitch while changing speed. Time stretching can sound different, especially near the extremes.

Open **Advanced** for added reverb. At zero the signal should be dry. Compare zero, a small amount and the maximum on both an ordinary song and a saved loop. Keep headphone volume comfortable while testing.

Saved A–B loops repeat the region between their start and end. Changing playback speed should preserve those musical boundaries. A song that contains one previously exported loop pass is not automatically an editable saved loop.

The first useful version must work without recommendations. Direct YouTube downloading and automatic cloud synchronization are deferred.
