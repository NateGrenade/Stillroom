import Foundation
import AVFoundation

/// Integration check: Python export -> native validation/import -> native
/// backup -> empty native restore. Only the three-song Dev transfer is input.
@main struct DevBundleInterop {
    static func main() throws {
        guard CommandLine.arguments.count == 3 else { fatalError("Usage: DevBundleInterop INPUT REPORT") }
        let input = URL(fileURLWithPath: CommandLine.arguments[1])
        let reportURL = URL(fileURLWithPath: CommandLine.arguments[2])
        let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("pocket-dev-interop-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let first = try PocketLibraryStore(root: temporary.appendingPathComponent("First"))
        let preview = try first.previewBundle(input)
        guard preview.manifest.tracks.count == 3, preview.manifest.playlists.count == 1 else { fatalError("Expected Dev-only sample library") }
        var probed = 0
        var durations: [[String: Double]] = []
        let imported = try first.importBundle(input, expectedManifest: preview.manifest, validateAudio: { url in
            let audio = try AVAudioFile(forReading: url)
            guard audio.length > 0, (1...2).contains(Int(audio.processingFormat.channelCount)) else { throw PocketTransferError.invalid("Native decoding failed") }
            let seconds = Double(audio.length) / audio.processingFormat.sampleRate
            let fingerprint = url.deletingPathExtension().lastPathComponent
            let paths = Set(preview.manifest.files.filter { $0.sha256 == fingerprint }.map(\.path))
            for track in preview.manifest.tracks where paths.contains(track.audioFile) {
                guard abs(track.fileDuration - seconds) <= 0.25, track.loopStart < seconds, (track.loopEnd ?? seconds) <= seconds + 0.005 else { throw PocketTransferError.invalid("Native timing differs from metadata") }
                durations.append(["manifestFileDuration": track.fileDuration, "nativeFileDuration": seconds])
            }
            probed += 1
        })
        guard imported.addedTracks == 3, first.catalog.playlists.first?.trackIds == preview.manifest.playlists.first?.trackIds else { fatalError("Import changed library structure") }
        let backup = temporary.appendingPathComponent("Roundtrip.stillroomlibrary")
        try first.exportBundle(to: backup)
        let second = try PocketLibraryStore(root: temporary.appendingPathComponent("Restored"))
        _ = try second.importBundle(backup)
        guard second.catalog == first.catalog, probed == 3 else { fatalError("Roundtrip failed") }
        let result: [String: Any] = ["outcome": "passed", "tracks": 3, "playlists": 1, "nativeAudioProbes": probed, "originalAudioBytes": preview.audioBytes, "metadataIdsOrderAndLoopFieldsPreserved": true, "backupRestoredIntoEmptyStore": true, "nativeTiming": durations, "platform": "macOS host; not simulator or iPhone", "personalAccessed": false]
        let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: reportURL, options: .atomic)
        print("PASS: three-song desktop/native/backup/restore integration; no re-encoding")
    }
}
