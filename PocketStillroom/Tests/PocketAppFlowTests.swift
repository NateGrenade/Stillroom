import XCTest
import Foundation
import CryptoKit
import AVFoundation
@testable import PocketStillroom

/// App-model entry points, generated silent audio and disposable app parents.
/// No physical device, real library, signing or credentials are used.
final class PocketAppFlowTests: XCTestCase {
    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketAppFlow-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        return root
    }
    private func appendLittle<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
        var value = value.littleEndian; withUnsafeBytes(of: &value) { data.append(contentsOf: $0) }
    }
    private func fixture(in parent: URL) throws -> (URL, PocketManifest) {
        let frames = 8 * 48_000
        var audio = Data("RIFF".utf8)
        appendLittle(UInt32(36 + frames * 4), to: &audio); audio.append(Data("WAVEfmt ".utf8))
        appendLittle(UInt32(16), to: &audio); appendLittle(UInt16(1), to: &audio); appendLittle(UInt16(2), to: &audio)
        appendLittle(UInt32(48_000), to: &audio); appendLittle(UInt32(192_000), to: &audio)
        appendLittle(UInt16(4), to: &audio); appendLittle(UInt16(16), to: &audio)
        audio.append(Data("data".utf8)); appendLittle(UInt32(frames * 4), to: &audio)
        audio.append(Data(repeating: 0, count: frames * 4))
        let file = "audio/" + String(repeating: "a", count: 32) + ".wav"
        let tracks = (1...2).map { PocketTrack(id: String(repeating: String($0), count: 32), title: "Generated silent song \($0)", artist: "Test fixture", duration: 8, audioFile: file) }
        let manifest = PocketManifest(tracks: tracks,
            playlists: [.init(id: String(repeating: "b", count: 32), name: "First collection", trackIds: [tracks[0].id]),
                        .init(id: String(repeating: "c", count: 32), name: "Other collection", trackIds: [tracks[1].id])],
            files: [.init(path: file, byteCount: Int64(audio.count), sha256: digest(audio))],
            settings: .init(playbackRate: 0.873, pitchChanges: false, reverb: 0.38))
        let json = try JSONEncoder().encode(manifest)
        var bundle = Data("STLRMB01".utf8), length = UInt32(json.count).bigEndian
        withUnsafeBytes(of: &length) { bundle.append(contentsOf: $0) }
        bundle.append(json); bundle.append(audio)
        let url = parent.appendingPathComponent("generated.stillroomlibrary")
        try bundle.write(to: url)
        return (url, manifest)
    }
    private func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    private func files(in root: URL) throws -> [String: String] {
        let enumerator = try XCTUnwrap(FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey]))
        var result = [String: String]()
        for case let url as URL in enumerator where try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true {
            result[String(url.path.dropFirst(root.path.count))] = digest(try Data(contentsOf: url))
        }
        return result
    }
    @MainActor private func finish(_ model: PocketAppModel) async throws {
        let deadline = DispatchTime.now().uptimeNanoseconds + 8_000_000_000
        while model.busy != nil && DispatchTime.now().uptimeNanoseconds < deadline { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertNil(model.busy)
        guard model.busy == nil else { throw PocketTransferError.invalid("Generated app operation timed out") }
    }

    @MainActor func testReceivedLibraryDiscoveryRefreshesWithoutImportingAndSelectionStillRequiresReview() async throws {
        let parent = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: parent) }
        let documents = parent.appendingPathComponent("Documents", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: false)
        let (bundle, manifest) = try fixture(in: documents)
        let bytes = try Data(contentsOf: bundle)
        let hidden = documents.appendingPathComponent("." + UUID().uuidString + ".partial")
        try bytes.write(to: hidden)
        let model = PocketAppModel(libraryRoot: parent.appendingPathComponent("library"), documentsDirectory: documents)
        await model.refreshReceivedLibraries()
        XCTAssertEqual(model.receivedLibraries.map(\.url), [bundle])
        XCTAssertTrue(model.catalog.tracks.isEmpty); XCTAssertNil(model.preview); XCTAssertNil(model.busy)
        XCTAssertNil(model.receivedLibrariesMessage)
        model.beginPreview(try XCTUnwrap(model.receivedLibraries.first).url)
        try await finish(model)
        XCTAssertEqual(model.preview?.manifest, manifest)
        XCTAssertTrue(model.catalog.tracks.isEmpty, "Selecting a received file must still stop at review")
        model.closePreview()
        XCTAssertEqual(try Data(contentsOf: bundle), bytes)
        XCTAssertTrue(FileManager.default.fileExists(atPath: hidden.path), "Discovery must keep in-progress transfers")
        let collision = documents.appendingPathComponent("generated (1).stillroomlibrary")
        try bytes.write(to: collision)
        await model.refreshReceivedLibraries()
        XCTAssertEqual(Set(model.receivedLibraries.map(\.url)), Set([bundle, collision]))
        XCTAssertNil(model.preview); XCTAssertTrue(model.catalog.tracks.isEmpty)
    }

    @MainActor func testReceivedBackupInRecoveryStillPreviewsWithoutSwitchingOrReplacingTheDamagedLibrary() async throws {
        let parent = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: parent) }
        let documents = parent.appendingPathComponent("Documents", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: false)
        let (bundle, manifest) = try fixture(in: documents)
        let location = try PocketLibraryLocation(parent: parent)
        let source = try PocketLibraryStore(root: location.defaultRoot)
        _ = try source.importBundle(bundle)
        try Data("generated damaged catalogue".utf8).write(to: location.defaultRoot.appendingPathComponent("catalog.json"))
        let before = try files(in: location.defaultRoot)
        let model = PocketAppModel(libraryParent: parent, documentsDirectory: documents)
        XCTAssertTrue(model.recoveryRequired); XCTAssertTrue(model.recoveryAvailable)
        model.message = nil
        await model.refreshReceivedLibraries()
        model.beginPreview(try XCTUnwrap(model.receivedLibraries.first).url)
        try await finish(model)
        XCTAssertEqual(model.preview?.manifest, manifest)
        XCTAssertTrue(model.recoveryRequired)
        XCTAssertEqual(try location.selectedRoot(), location.defaultRoot)
        XCTAssertEqual(try files(in: location.defaultRoot), before)
        model.closePreview()
        XCTAssertTrue(FileManager.default.fileExists(atPath: bundle.path))
    }

    @MainActor func testDamagedLibraryRecoversOnlyAfterReviewedRestoreAndReopensNewCopy() async throws {
        let parent = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: parent) }
        let (bundle, manifest) = try fixture(in: parent)
        let location = try PocketLibraryLocation(parent: parent)
        let source = try PocketLibraryStore(root: location.defaultRoot)
        _ = try source.importBundle(bundle)
        let originalCatalog = location.defaultRoot.appendingPathComponent("catalog.json")
        try Data("damaged catalogue kept for recovery".utf8).write(to: originalCatalog)
        let before = try files(in: location.defaultRoot)
        let model = PocketAppModel(libraryParent: parent)
        XCTAssertTrue(model.recoveryRequired); XCTAssertTrue(model.recoveryAvailable)
        XCTAssertEqual(try location.selectedRoot(), location.defaultRoot)
        model.message = nil; model.beginPreview(bundle)
        try await finish(model)
        XCTAssertEqual(model.preview?.manifest, manifest)
        XCTAssertTrue(model.recoveryRequired)
        XCTAssertEqual(try location.selectedRoot(), location.defaultRoot, "Preview must not switch libraries")
        model.importPreview(); try await finish(model)
        XCTAssertFalse(model.recoveryRequired); XCTAssertNil(model.preview)
        XCTAssertEqual(model.catalog.manifest, manifest)
        XCTAssertNotEqual(try location.selectedRoot(), location.defaultRoot)
        XCTAssertEqual(try files(in: location.defaultRoot), before)
        let reopened = PocketAppModel(libraryParent: parent)
        XCTAssertFalse(reopened.recoveryRequired)
        XCTAssertEqual(reopened.catalog, model.catalog)
        XCTAssertEqual(reopened.audio.playbackRate, manifest.settings!.playbackRate)
        XCTAssertEqual(reopened.audio.pitchFollowsSpeed, manifest.settings!.pitchChanges)
        XCTAssertEqual(reopened.audio.reverb, manifest.settings!.reverb)
        XCTAssertFalse(reopened.audio.isPlaying)
    }

    @MainActor func testCorruptBackupAndCancelledRestoreKeepDamagedOriginalAndSelection() async throws {
        let parent = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: parent) }
        let (bundle, _) = try fixture(in: parent)
        let location = try PocketLibraryLocation(parent: parent)
        let source = try PocketLibraryStore(root: location.defaultRoot)
        _ = try source.importBundle(bundle)
        try Data("original damaged catalogue".utf8).write(to: location.defaultRoot.appendingPathComponent("catalog.json"))
        let before = try files(in: location.defaultRoot)
        let model = PocketAppModel(libraryParent: parent)
        var bad = try Data(contentsOf: bundle); bad[bad.count - 1] ^= 1
        let corrupt = parent.appendingPathComponent("corrupt.stillroomlibrary"); try bad.write(to: corrupt)
        model.message = nil; model.beginPreview(corrupt); try await finish(model)
        XCTAssertNil(model.preview); XCTAssertTrue(model.recoveryRequired)
        XCTAssertEqual(try files(in: location.defaultRoot), before)
        model.message = nil; model.beginPreview(bundle); try await finish(model)
        XCTAssertNotNil(model.preview)
        model.importPreview(); model.cancelWork(); try await finish(model)
        XCTAssertTrue(model.recoveryRequired); XCTAssertNil(model.preview)
        XCTAssertEqual(try location.selectedRoot(), location.defaultRoot)
        XCTAssertEqual(try files(in: location.defaultRoot), before)
        XCTAssertFalse(FileManager.default.fileExists(atPath: parent.appendingPathComponent("PocketStillroom-location.json").path))
    }

    @MainActor func testBackupCancellationCanRetryAndResultRestoresOriginalMetadataAndBytes() async throws {
        let parent = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: parent) }
        let (bundle, manifest) = try fixture(in: parent)
        let libraryRoot = parent.appendingPathComponent("working")
        let source = try PocketLibraryStore(root: libraryRoot); _ = try source.importBundle(bundle)
        let model = PocketAppModel(libraryRoot: libraryRoot, backupDirectory: parent.appendingPathComponent("Backups"))
        model.exportBackup(); model.cancelWork(); try await finish(model)
        XCTAssertNil(model.backupURL)
        model.exportBackup(); try await finish(model)
        let backup = try XCTUnwrap(model.backupURL)
        let restored = try PocketLibraryStore.restoreBundle(backup, toNewRoot: parent.appendingPathComponent("restored"))
        XCTAssertEqual(restored.catalog.manifest, manifest)
        XCTAssertEqual(try Data(contentsOf: restored.audioURL(for: restored.catalog.tracks[0])),
                       try Data(contentsOf: source.audioURL(for: source.catalog.tracks[0])), "Backup preserves original audio bytes")
        XCTAssertFalse(model.audio.isPlaying)
    }

    @MainActor func testSavingLoopWhileBrowsingOtherPlaylistKeepsPlayingQueueAndUsesOriginalBounds() async throws {
        let parent = try temporaryRoot()
        let (bundle, manifest) = try fixture(in: parent)
        let libraryRoot = parent.appendingPathComponent("working")
        let source = try PocketLibraryStore(root: libraryRoot); _ = try source.importBundle(bundle)
        let model = PocketAppModel(libraryRoot: libraryRoot)
#if os(iOS)
        let previous = await Task.detached { () -> (String, String, UInt) in
            let session = AVAudioSession.sharedInstance()
            return (session.category.rawValue, session.mode.rawValue, session.categoryOptions.rawValue)
        }.value
#endif
        addTeardownBlock {
            await MainActor.run { model.audio.pause(); model.audio.setQueue([]) }
            _ = await model.audio.effectSnapshot()
            try? FileManager.default.removeItem(at: parent)
#if os(iOS)
            await Task.detached {
                let session = AVAudioSession.sharedInstance()
                try? session.setActive(false, options: .notifyOthersOnDeactivation)
                try? session.setCategory(.init(rawValue: previous.0), mode: .init(rawValue: previous.1), options: .init(rawValue: previous.2))
            }.value
#endif
        }
        model.selectedPlaylist = manifest.playlists[0].id
        model.play(model.visibleTracks[0])
        _ = await model.audio.effectSnapshot()
        model.selectedPlaylist = manifest.playlists[1].id
        XCTAssertTrue(model.saveLoop(start: 2, end: 4))
        _ = await model.audio.effectSnapshot()
        XCTAssertEqual(model.audio.queue.map(\.id), [manifest.tracks[0].id])
        XCTAssertEqual(model.audio.currentTrack?.loopStart, 2)
        XCTAssertEqual(model.audio.currentTrack?.loopEnd, 4)
        XCTAssertEqual(model.audio.repeatMode, .one)
        XCTAssertEqual(model.catalog.tracks[0].loopStart, 2)
        XCTAssertEqual(model.catalog.tracks[0].loopEnd, 4)
        XCTAssertTrue(model.saveLoop(start: 0.25, end: 1))
        _ = await model.audio.effectSnapshot()
        XCTAssertEqual(model.audio.currentTrack?.loopStart, 2.25)
        XCTAssertEqual(model.audio.currentTrack?.loopEnd, 3)
        XCTAssertEqual(model.audio.queue.map(\.id), [manifest.tracks[0].id])
        XCTAssertEqual(model.selectedPlaylist, manifest.playlists[1].id)
    }
}
