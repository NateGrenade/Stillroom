import Foundation
import CryptoKit
#if canImport(Darwin)
import Darwin
#endif

private struct TestFailure: Error, CustomStringConvertible { let description: String }
private func check(_ value: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !value() { throw TestFailure(description: message) }
}

@main struct TransferTests {
    static var passed = 0
    static var names = [String]()
    static let fm = FileManager.default
    static var directory = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("PocketTransferTests-" + UUID().uuidString, isDirectory: true)

    static func test(_ name: String, _ work: () throws -> Void) throws {
        try work(); passed += 1; names.append(name); print("PASS \(name)")
    }
    static func sub(_ name: String) -> URL { directory.appendingPathComponent(name, isDirectory: true) }
    static func mustFail(_ work: () throws -> Void) throws {
        do { try work() }
        catch is TestFailure { throw TestFailure(description: "Test assertion failed while checking an expected rejection") }
        catch { return }
        throw TestFailure(description: "Expected operation to fail")
    }
    static func hash(_ bytes: Data) -> String { SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined() }
    static func write(_ manifest: PocketManifest, payload: Data, name: String = UUID().uuidString) throws -> URL {
        let bytes = try JSONEncoder().encode(manifest)
        var length = UInt32(bytes.count).bigEndian; var result = Data("STLRMB01".utf8)
        withUnsafeBytes(of: &length) { result.append(contentsOf: $0) }
        result.append(bytes); result.append(payload)
        let path = directory.appendingPathComponent(name + ".stillroomlibrary")
        try result.write(to: path); return path
    }
    static func fixture() -> (PocketManifest, Data) {
        let payload = Data((0..<2048).map { UInt8($0 % 251) })
        let id = String(repeating: "1", count: 32); let path = "audio/" + id + ".wav"
        let track = PocketTrack(id: id, title: "Synthetic sample", artist: "Fixture", kind: "loop", duration: 1.5, fileDuration: 2, loopStart: 0.25, loopEnd: 1.75, tags: ["test"], sourceUrl: "https://www.youtube.com/watch?v=abcdefghijk", addedAt: "2026-10-20T00:00:00Z", audioFile: path)
        return (.init(tracks: [track], playlists: [.init(id: String(repeating: "2", count: 32), name: "Samples", trackIds: [id])], files: [.init(path: path, byteCount: Int64(payload.count), sha256: hash(payload))], settings: .init(playbackRate: 1.25, pitchChanges: false, reverb: 0.1)), payload)
    }
    static func main() throws {
        if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--crash-import" {
            let root = URL(fileURLWithPath: CommandLine.arguments[3])
            let store = try PocketLibraryStore(root: root, beforeCatalogCommit: { _exit(73) })
            _ = try store.importBundle(URL(fileURLWithPath: CommandLine.arguments[2]))
            _exit(74)
        }
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        let (manifest, payload) = fixture(); let source = try write(manifest, payload: payload)
        let sourceBytes = try Data(contentsOf: source)
        let store = try PocketLibraryStore(root: sub("main"))
        try test("preview checks hashes, preserves span versus file duration and leaves empty catalogue") {
            let preview = try store.previewBundle(source)
            try check(preview.tracksToAdd == 1 && preview.playlistsToAdd == 1 && preview.audioBytes == payload.count, "Preview counts")
            try check(preview.manifest.tracks[0].duration == 1.5 && preview.manifest.tracks[0].fileDuration == 2 && preview.manifest.tracks[0].loopEnd == 1.75, "Loop guards preserved")
            try check(store.catalog.tracks.isEmpty, "Preview wrote catalogue")
        }
        try test("import commits exact audio, IDs, playlists, order and supported settings") {
            let result = try store.importBundle(source, expectedManifest: manifest)
            try check(result.addedTracks == 1 && result.addedPlaylists == 1 && result.totalTracks == 1, "Import counts")
            try check(store.catalog.manifest == manifest, "Metadata changed")
            try check(try Data(contentsOf: store.audioURL(for: manifest.tracks[0])) == payload, "Audio bytes changed")
        }
        try test("reload preserves saved data and settings") {
            let reopened = try PocketLibraryStore(root: sub("main"))
            try check(reopened.catalog == store.catalog, "Reload mismatch")
        }
        try test("identical duplicate import is harmless and does not add a second audio file") {
            let result = try store.importBundle(source)
            try check(result.addedTracks == 0 && result.skippedTracks == 1 && result.totalTracks == 1, "Duplicate count")
            try check(try fm.contentsOfDirectory(atPath: sub("main").appendingPathComponent("Media").path).count == 1, "Duplicate physical file")
        }
        try test("conflicting song ID rejects the whole transaction without changing existing data") {
            let before = store.catalog; var changed = manifest; changed.tracks[0].title = "Conflicting title"
            let url = try write(changed, payload: payload)
            try mustFail { _ = try store.importBundle(url) }; try check(store.catalog == before, "Conflict changed catalogue")
        }
        try test("conflicting playlist ID rejects without overwriting order") {
            var changed = manifest; changed.playlists[0].name = "Changed"
            let url = try write(changed, payload: payload)
            try mustFail { _ = try store.importBundle(url) }; try check(store.catalog.playlists == manifest.playlists, "Playlist conflict changed data")
        }
        try test("hash corruption rejects and leaves no published media or catalogue") {
            let empty = try PocketLibraryStore(root: sub("corrupt")); var bad = payload; bad[0] ^= 1
            let url = try write(manifest, payload: bad)
            try mustFail { _ = try empty.importBundle(url) }
            try check(empty.catalog.tracks.isEmpty, "Corrupt import visible")
            try check(try fm.contentsOfDirectory(atPath: sub("corrupt").appendingPathComponent("Media").path).isEmpty, "Corrupt media published")
        }
        try test("truncated and trailing payloads reject before commit") {
            for data in [payload.dropLast(), payload + Data([0])] {
                let url = try write(manifest, payload: Data(data)); try mustFail { _ = try store.previewBundle(url) }
            }
        }
        try test("unsupported format version rejects") {
            var invalid = manifest; invalid.version = 99; let url = try write(invalid, payload: payload)
            try mustFail { _ = try store.previewBundle(url) }
        }
        try test("archive traversal and absolute filenames reject without filesystem writes") {
            for name in ["../escaped.wav", "/tmp/escaped.wav", "audio/../../escaped.wav", "audio/" + String(repeating: "1", count: 32) + ".WAV"] {
                var invalid = manifest; invalid.files[0].path = name; invalid.tracks[0].audioFile = name
                let url = try write(invalid, payload: payload); try mustFail { _ = try store.importBundle(url) }
            }
        }
        try test("duplicate song IDs, audio paths and playlist memberships reject") {
            var variants = [PocketManifest]()
            var tracks = manifest; tracks.tracks.append(tracks.tracks[0]); variants.append(tracks)
            var files = manifest; files.files.append(files.files[0]); variants.append(files)
            var memberships = manifest; memberships.playlists[0].trackIds.append(manifest.tracks[0].id); variants.append(memberships)
            for item in variants { let url = try write(item, payload: payload); try mustFail { _ = try store.importBundle(url) } }
        }
        try test("missing playlist references and unreferenced assets reject") {
            var missing = manifest; missing.playlists[0].trackIds = [String(repeating: "f", count: 32)]
            let a = try write(missing, payload: payload); try mustFail { _ = try store.previewBundle(a) }
            var orphan = manifest; orphan.tracks = []; orphan.playlists = []
            let b = try write(orphan, payload: payload); try mustFail { _ = try store.previewBundle(b) }
        }
        try test("invalid duration, loop metadata, settings and secret-bearing URL reject") {
            var variants = [PocketManifest]()
            var span = manifest; span.tracks[0].duration = 2; variants.append(span)
            var bounds = manifest; bounds.tracks[0].loopEnd = 2.1; variants.append(bounds)
            var settings = manifest; settings.settings?.playbackRate = 3; variants.append(settings)
            var source = manifest; source.tracks[0].sourceUrl = "https://www.youtube.com/watch?v=abcdefghijk&token=secret"; variants.append(source)
            for item in variants { let url = try write(item, payload: payload); try mustFail { _ = try store.importBundle(url) } }
        }
        try test("file and catalogue limits reject before large allocation") {
            var item = manifest; item.files[0].byteCount = 512 * 1024 * 1024 + 1
            let url = try write(item, payload: payload); try mustFail { _ = try store.importBundle(url) }
            let huge = directory.appendingPathComponent("huge.stillroomlibrary"); var data = Data("STLRMB01".utf8); data.append(contentsOf: [0x7f, 0xff, 0xff, 0xff]); try data.write(to: huge)
            try mustFail { _ = try store.previewBundle(huge) }
        }
        try test("insufficient storage rejects with no visible or staged changes") {
            let limited = try PocketLibraryStore(root: sub("low-space"), availableBytes: { 1 })
            try mustFail { _ = try limited.importBundle(source) }
            try check(limited.catalog.tracks.isEmpty && (try fm.contentsOfDirectory(atPath: sub("low-space").appendingPathComponent("Transactions").path)).isEmpty, "Low storage modified data")
        }
        try test("cancellation during streamed copy cleans staged files") {
            let cancelled = try PocketLibraryStore(root: sub("cancelled")); var calls = 0
            try mustFail { _ = try cancelled.importBundle(source, cancellation: { calls += 1; return calls >= 4 }) }
            try check(cancelled.catalog.tracks.isEmpty && (try fm.contentsOfDirectory(atPath: sub("cancelled").appendingPathComponent("Transactions").path)).isEmpty, "Cancelled transaction leaked")
        }
        try test("unsupported audio decode probe rejects before publication") {
            let decoder = try PocketLibraryStore(root: sub("decode"))
            try mustFail { _ = try decoder.importBundle(source, validateAudio: { _ in throw PocketTransferError.invalid("Unsupported fixture codec") }) }
            try check(decoder.catalog.tracks.isEmpty && (try fm.contentsOfDirectory(atPath: sub("decode").appendingPathComponent("Media").path)).isEmpty, "Decode failure published audio")
        }
        try test("injected catalogue commit failure rolls back published media") {
            let failed = try PocketLibraryStore(root: sub("commit-failure"), beforeCatalogCommit: { throw PocketTransferError.invalid("Injected disk failure") })
            try mustFail { _ = try failed.importBundle(source) }
            try check(failed.catalog.tracks.isEmpty && (try fm.contentsOfDirectory(atPath: sub("commit-failure").appendingPathComponent("Media").path)).isEmpty, "Commit failure left visible media")
        }
        try test("cancellation at final commit boundary leaves prior library intact") {
            var cancelled = false
            let failed = try PocketLibraryStore(root: sub("commit-cancel"), beforeCatalogCommit: { cancelled = true })
            try mustFail { _ = try failed.importBundle(source, cancellation: { cancelled }) }
            try check(failed.catalog.tracks.isEmpty && (try fm.contentsOfDirectory(atPath: sub("commit-cancel").appendingPathComponent("Media").path)).isEmpty, "Commit cancellation published")
        }
        try test("bundle source symlink and storage-directory symlink reject") {
            let link = directory.appendingPathComponent("linked.stillroomlibrary"); try fm.createSymbolicLink(at: link, withDestinationURL: source)
            try mustFail { _ = try store.importBundle(link) }
            let linkedRoot = sub("unsafe"); try fm.createDirectory(at: linkedRoot, withIntermediateDirectories: true)
            try fm.createSymbolicLink(at: linkedRoot.appendingPathComponent("Media"), withDestinationURL: sub("main").appendingPathComponent("Media"))
            try mustFail { _ = try PocketLibraryStore(root: linkedRoot) }
        }
        try test("a FIFO named like a bundle is rejected without blocking") {
            let fifo = directory.appendingPathComponent("pipe.stillroomlibrary")
            try check(mkfifo(fifo.path, 0o600) == 0, "Could not make disposable FIFO")
            let started = Date(); try mustFail { _ = try store.previewBundle(fifo) }
            try check(Date().timeIntervalSince(started) < 1, "FIFO rejection blocked")
        }
        try test("abrupt process exit before catalogue commit recovers only owned staging and unpublished media") {
            let crashedRoot = sub("process-crash")
            let child = Process(); child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
            child.arguments = ["--crash-import", source.path, crashedRoot.path]
            try child.run(); child.waitUntilExit()
            try check(child.terminationStatus == 73, "Crash fixture did not reach pre-commit point")
            try check(try fm.contentsOfDirectory(atPath: crashedRoot.appendingPathComponent("Transactions").path).count >= 2, "No recovery fixture remained")
            let recovered = try PocketLibraryStore(root: crashedRoot)
            try check(recovered.catalog.tracks.isEmpty, "Uncommitted song became visible after crash")
            try check(try fm.contentsOfDirectory(atPath: crashedRoot.appendingPathComponent("Transactions").path).isEmpty, "Owned transaction staging not recovered")
            try check(try fm.contentsOfDirectory(atPath: crashedRoot.appendingPathComponent("Media").path).isEmpty, "Unreferenced published media not recovered")
        }
        try test("recovery retains committed media and preserves unknown files") {
            let url = try store.audioURL(for: manifest.tracks[0]); let name = url.lastPathComponent
            let stage = sub("main").appendingPathComponent("Transactions").appendingPathComponent(UUID().uuidString)
            try fm.createDirectory(at: stage, withIntermediateDirectories: true)
            try fm.linkItem(at: url, to: stage.appendingPathComponent(name))
            let journal: [String: Any] = ["format": "pocket-stillroom-transaction", "version": 1, "transactionID": stage.lastPathComponent, "operation": "import", "ownedFiles": [name]]
            try JSONSerialization.data(withJSONObject: journal).write(to: stage.appendingPathComponent("transaction.json"))
            _ = try PocketLibraryStore(root: sub("main"))
            try check(!fm.fileExists(atPath: stage.path) && (try Data(contentsOf: url)) == payload, "Recovery removed committed audio")
            let unknown = sub("main").appendingPathComponent("Transactions").appendingPathComponent(UUID().uuidString)
            try fm.createDirectory(at: unknown, withIntermediateDirectories: true)
            var unusual = journal; unusual["transactionID"] = unknown.lastPathComponent
            try JSONSerialization.data(withJSONObject: unusual).write(to: unknown.appendingPathComponent("transaction.json"))
            try Data("Retain my file".utf8).write(to: unknown.appendingPathComponent("unexpected.txt"))
            _ = try PocketLibraryStore(root: sub("main"))
            try check(try String(contentsOf: unknown.appendingPathComponent("unexpected.txt"), encoding: .utf8) == "Retain my file", "Recovery removed an unknown file")
        }
        try test("reviewed manifest must match at import time") {
            var changed = manifest; changed.tracks[0].title = "Replaced after preview"
            let url = try write(changed, payload: payload)
            try mustFail { _ = try store.importBundle(url, expectedManifest: manifest) }
        }
        try test("saved A–B loops, settings and playlist edits persist without audio changes") {
            try store.updateLoop(trackID: manifest.tracks[0].id, start: 0.5, end: 1.25)
            try store.saveSettings(.init(playbackRate: 1.73, pitchChanges: true, reverb: 0))
            let playlist = try store.createPlaylist(name: "On phone", trackIds: [manifest.tracks[0].id])
            let reopened = try PocketLibraryStore(root: sub("main"))
            try check(reopened.catalog.tracks[0].loopStart == 0.5 && reopened.catalog.tracks[0].loopEnd == 1.25 && reopened.catalog.tracks[0].duration == 0.75, "Loop save mismatch")
            try check(reopened.catalog.settings.playbackRate == 1.73 && reopened.catalog.playlists.last == playlist, "Settings/playlist save mismatch")
            try check(try Data(contentsOf: store.audioURL(for: manifest.tracks[0])) == payload, "Editing metadata rewrote audio")
        }
        try test("export then restore into empty store reproduces all data and hashes") {
            let exported = directory.appendingPathComponent("restore.stillroomlibrary"); try store.exportBundle(to: exported)
            let restored = try PocketLibraryStore(root: sub("restored")); _ = try restored.importBundle(exported)
            try check(restored.catalog == store.catalog, "Restore metadata differs")
            try check(try Data(contentsOf: restored.audioURL(for: restored.catalog.tracks[0])) == payload, "Restored audio differs")
            try mustFail { try store.exportBundle(to: exported) }
        }
        try test("newer stored catalogue version refuses downgrade without overwriting") {
            let path = sub("newer"); try fm.createDirectory(at: path, withIntermediateDirectories: true)
            var data = try JSONSerialization.jsonObject(with: JSONEncoder().encode(PocketCatalog())) as! [String: Any]; data["schemaVersion"] = 99
            let bytes = try JSONSerialization.data(withJSONObject: data); try bytes.write(to: path.appendingPathComponent("catalog.json"))
            try mustFail { _ = try PocketLibraryStore(root: path) }
            try check(try Data(contentsOf: path.appendingPathComponent("catalog.json")) == bytes, "Newer catalogue was replaced")
        }
        try test("two store instances reload before committing and do not lose edits") {
            let another = try PocketLibraryStore(root: sub("main"))
            try store.saveSettings(.init(playbackRate: 1.11, pitchChanges: false, reverb: 0.2))
            _ = try another.createPlaylist(name: "Concurrent instance", trackIds: [])
            let reopened = try PocketLibraryStore(root: sub("main"))
            try check(reopened.catalog.settings.playbackRate == 1.11 && reopened.catalog.playlists.last?.name == "Concurrent instance", "Stale writer lost data")
        }
        try test("Python-produced WAV bundle imports and retains guarded loop and playlist order") {
            let fixture = CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--python-fixture"
                ? URL(fileURLWithPath: CommandLine.arguments[2])
                : URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("Desktop/Fixtures/example.stillroomlibrary")
            let cross = try PocketLibraryStore(root: sub("python")); let result = try cross.importBundle(fixture)
            try check(result.addedTracks == 2 && result.addedPlaylists == 1, "Python bundle counts")
            try check(cross.catalog.tracks[1].loopStart == 0.2 && cross.catalog.tracks[1].loopEnd == 0.8 && cross.catalog.tracks[1].duration == 0.6, "Python loop metadata changed")
            try check(cross.catalog.playlists[0].trackIds == [String(repeating: "b", count: 32), String(repeating: "a", count: 32)], "Python playlist order changed")
        }
        try test("source bundle remains byte-identical after previews, failures, imports and exports") {
            try check(try Data(contentsOf: source) == sourceBytes, "Source bundle changed")
        }
        let report: [String: Any] = ["suite": "PocketCore transfer/storage", "passed": passed, "failed": 0, "tests": names, "nativePhoneValidated": false, "syntheticFixturesOnly": true]
        let output = URL(fileURLWithPath: fm.currentDirectoryPath).appendingPathComponent("Reports/transfer-tests.json")
        try fm.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: output)
        print("\(passed) transfer/storage tests passed")
    }
}
