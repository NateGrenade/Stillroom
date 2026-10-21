import Foundation
import CryptoKit
#if canImport(Darwin)
import Darwin
#endif

private struct RecoveryFailure: Error, CustomStringConvertible { let description: String }

/// Storage-only Mac-host checks. All roots, bundles and failure fixtures are
/// generated under one disposable temporary directory; no real library is read.
@main struct RecoveryTests {
    static let fm = FileManager.default
    static let root = fm.temporaryDirectory.appendingPathComponent("PocketRecoveryTests-" + UUID().uuidString, isDirectory: true)
    static var names = [String]()
    static var failures = [String]()

    static func check(_ value: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !value() { throw RecoveryFailure(description: message) }
    }
    static func rejected(_ action: () throws -> Void) throws {
        do { try action() }
        catch is RecoveryFailure { throw RecoveryFailure(description: "A test assertion failed inside the expected rejection") }
        catch { return }
        throw RecoveryFailure(description: "The operation unexpectedly succeeded")
    }
    static func test(_ name: String, _ action: () throws -> Void) {
        do { try action(); names.append(name); print("PASS \(name)") }
        catch { failures.append(name + ": " + String(describing: error)); print("FAIL \(name): \(error)") }
    }
    static func hash(_ bytes: Data) -> String { SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined() }
    static func path(_ name: String) -> URL { root.appendingPathComponent(name) }
    static func fixture(_ id: Character = "1", payload: Data? = nil) -> (PocketManifest, Data) {
        let bytes = payload ?? Data((0..<600_000).map { UInt8($0 % 251) })
        let identifier = String(repeating: String(id), count: 32)
        let file = "audio/" + identifier + ".wav"
        return (.init(tracks: [.init(id: identifier, title: "Generated recovery sample", artist: "Test fixture", album: "Disposable", kind: "loop", duration: 1.5, fileDuration: 2, loopStart: 0.25, loopEnd: 1.75, tags: ["generated"], audioFile: file)],
                      playlists: [.init(id: String(repeating: String(id == "1" ? "2" : "4"), count: 32), name: "Saved moments", trackIds: [identifier])],
                      files: [.init(path: file, byteCount: Int64(bytes.count), sha256: hash(bytes))],
                      settings: .init(playbackRate: 1.37, pitchChanges: false, reverb: 0.42)), bytes)
    }
    static func bundle(_ manifest: PocketManifest, _ payload: Data, name: String = UUID().uuidString) throws -> URL {
        let json = try JSONEncoder().encode(manifest)
        var size = UInt32(json.count).bigEndian
        var bytes = Data("STLRMB01".utf8)
        withUnsafeBytes(of: &size) { bytes.append(contentsOf: $0) }
        bytes.append(json); bytes.append(payload)
        let url = path(name + ".stillroomlibrary"); try bytes.write(to: url)
        return url
    }
    static func snapshot(_ directory: URL) throws -> [String: Data] {
        var result = [String: Data]()
        guard let enumerator = fm.enumerator(at: directory, includingPropertiesForKeys: [.isRegularFileKey], options: []) else { return result }
        for case let url as URL in enumerator {
            if try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true {
                result[String(url.path.dropFirst(directory.path.count + 1))] = try Data(contentsOf: url)
            }
        }
        return result
    }
    static func noPartialExports(_ directory: URL) throws -> Bool {
        try !fm.contentsOfDirectory(atPath: directory.path).contains { $0.hasPrefix(".pocket-export-") }
    }

    static func main() throws {
        setbuf(stdout, nil)
        try fm.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? fm.removeItem(at: root) }
        let (manifest, payload) = fixture()
        let source = try bundle(manifest, payload, name: "source")
        let originalSource = try Data(contentsOf: source)

        test("exports refuse managed catalog, media, transaction and symlink-alias destinations") {
            let storeRoot = path("managed-export")
            let store = try PocketLibraryStore(root: storeRoot)
            let alias = path("managed-alias"); try fm.createSymbolicLink(at: alias, withDestinationURL: storeRoot)
            let forbidden = [storeRoot.appendingPathComponent("catalog.json"), storeRoot.appendingPathComponent("Media/backup.stillroomlibrary"), storeRoot.appendingPathComponent("Transactions/backup.stillroomlibrary"), alias.appendingPathComponent("catalog.json")]
            let before = try snapshot(storeRoot)
            for destination in forbidden { try rejected { try store.exportBundle(to: destination) } }
            try check(try snapshot(storeRoot) == before, "Managed-path export modified the library")
            try check(try PocketLibraryStore(root: storeRoot).catalog.tracks.isEmpty, "An empty library cannot reopen")
        }

        for directoryReplacement in [false, true] {
            test("rollback preserves foreign replacement \(directoryReplacement ? "directory" : "file") and existing library") {
                let storeRoot = path(directoryReplacement ? "foreign-directory" : "foreign-file")
                let existing = try PocketLibraryStore(root: storeRoot)
                _ = try existing.importBundle(source)
                let before = existing.catalog
                let (incoming, incomingPayload) = fixture("3", payload: Data(repeating: 0xA7, count: 4096))
                let input = try bundle(incoming, incomingPayload)
                let destination = storeRoot.appendingPathComponent("Media/" + incoming.files[0].sha256 + ".wav")
                let keptOwnedLink = path(UUID().uuidString + ".owned")
                let sentinel = Data("Unrelated bytes must survive rollback".utf8)
                let failing = try PocketLibraryStore(root: storeRoot, beforeCatalogCommit: {
                    try fm.moveItem(at: destination, to: keptOwnedLink)
                    if directoryReplacement {
                        try fm.createDirectory(at: destination, withIntermediateDirectories: false)
                        try sentinel.write(to: destination.appendingPathComponent("keep.txt"))
                    } else { try sentinel.write(to: destination) }
                    throw RecoveryFailure(description: "Injected commit failure after path replacement")
                })
                do { _ = try failing.importBundle(input); throw RecoveryFailure(description: "Commit failure was ignored") }
                catch let error as RecoveryFailure { try check(error.description.contains("Injected commit failure"), "Unexpected test error") }
                let surviving = directoryReplacement ? destination.appendingPathComponent("keep.txt") : destination
                try check(fm.fileExists(atPath: surviving.path), "Rollback removed unrelated replacement data")
                try check(try Data(contentsOf: surviving) == sentinel, "Rollback changed unrelated bytes")
                let reopened = try PocketLibraryStore(root: storeRoot)
                try check(reopened.catalog == before, "A failed new import altered the existing catalog")
                try check(try Data(contentsOf: reopened.audioURL(for: before.tracks[0])) == payload, "Existing audio changed")
            }
        }

        test("backup low-space preflight creates no output or partial file") {
            var capacity: Int64 = Int64.max
            let store = try PocketLibraryStore(root: path("backup-space"), availableBytes: { capacity })
            _ = try store.importBundle(source); let before = store.catalog
            capacity = 0
            let output = path("no-space.stillroomlibrary")
            do { try store.exportBundle(to: output); throw RecoveryFailure(description: "Expected a storage rejection") }
            catch PocketTransferError.insufficientSpace { }
            try check(!fm.fileExists(atPath: output.path), "Low-space backup became visible")
            try check(try noPartialExports(root), "Low-space backup left a partial file")
            try check(store.catalog == before, "Low-space export modified the library")
        }

        test("cancelled streamed backup removes only its partial output") {
            let store = try PocketLibraryStore(root: path("backup-cancel")); _ = try store.importBundle(source)
            var calls = 0; let output = path("cancelled-backup.stillroomlibrary")
            do { try store.exportBundle(to: output, cancellation: { calls += 1; return calls >= 4 }); throw RecoveryFailure(description: "Cancellation was ignored") }
            catch PocketTransferError.cancelled { }
            try check(!fm.fileExists(atPath: output.path) && (try noPartialExports(root)), "Cancelled export left published or partial data")
            try check(try Data(contentsOf: store.audioURL(for: manifest.tracks[0])) == payload, "Cancellation changed stored audio")
        }

        test("cancellation at the final export commit point publishes no backup") {
            var cancel = false
            let store = try PocketLibraryStore(root: path("backup-final-cancel"), beforeExportPublish: { cancel = true })
            _ = try store.importBundle(source)
            let output = path("final-cancel.stillroomlibrary")
            do { try store.exportBundle(to: output, cancellation: { cancel }); throw RecoveryFailure(description: "Late cancellation was ignored") }
            catch PocketTransferError.cancelled { }
            try check(!fm.fileExists(atPath: output.path) && (try noPartialExports(root)), "Late cancellation leaked a backup")
        }

        test("exclusive export publication preserves a filename created during copying") {
            let output = path("contended-export.stillroomlibrary")
            let sentinel = Data("An existing user file".utf8)
            let store = try PocketLibraryStore(root: path("backup-contention"), beforeExportPublish: { try sentinel.write(to: output) })
            _ = try store.importBundle(source)
            try rejected { try store.exportBundle(to: output) }
            try check(try Data(contentsOf: output) == sentinel, "Export overwrote the contending file")
            try check(try noPartialExports(root), "Failed publication left a partial file")
        }

        test("export cleanup preserves a foreign replacement of its own temporary path") {
            let storeRoot = path("backup-foreign-temp")
            let outputDirectory = path("foreign-temp-output"); try fm.createDirectory(at: outputDirectory, withIntermediateDirectories: false)
            var replacement: URL?
            let sentinel = Data("Unrelated temporary replacement".utf8)
            let store = try PocketLibraryStore(root: storeRoot, beforeExportPublish: {
                let partial = try fm.contentsOfDirectory(at: outputDirectory, includingPropertiesForKeys: nil).first { $0.lastPathComponent.hasPrefix(".pocket-export-") }!
                try fm.moveItem(at: partial, to: path("saved-owned-export"))
                try fm.createDirectory(at: partial, withIntermediateDirectories: false)
                try sentinel.write(to: partial.appendingPathComponent("keep.txt")); replacement = partial
                throw RecoveryFailure(description: "Injected export failure")
            })
            _ = try store.importBundle(source)
            do { try store.exportBundle(to: outputDirectory.appendingPathComponent("not-published.stillroomlibrary")); throw RecoveryFailure(description: "Export failure ignored") }
            catch let error as RecoveryFailure { try check(error.description == "Injected export failure", "Unexpected test error") }
            try check(try Data(contentsOf: replacement!.appendingPathComponent("keep.txt")) == sentinel, "Export cleanup removed unrelated replacement")
        }

        test("export refuses a substituted staged file even when the commit hook returns normally") {
            let directory = path("export-substitution"); try fm.createDirectory(at: directory, withIntermediateDirectories: false)
            let sentinel = Data("Unrelated substituted bytes".utf8)
            var substituted: URL?
            let store = try PocketLibraryStore(root: path("export-substitution-store"), beforeExportPublish: {
                let temporary = try fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil).first { $0.lastPathComponent.hasPrefix(".pocket-export-") }!
                try fm.moveItem(at: temporary, to: directory.appendingPathComponent("owned-backup"))
                try sentinel.write(to: temporary); substituted = temporary
            })
            _ = try store.importBundle(source)
            let output = directory.appendingPathComponent("substituted.stillroomlibrary")
            try rejected { try store.exportBundle(to: output) }
            try check(!fm.fileExists(atPath: output.path), "Unverified substituted bytes became a published backup")
            guard let substituted else { throw RecoveryFailure(description: "Export commit hook was not reached") }
            try check(try Data(contentsOf: substituted) == sentinel, "Rejection removed unrelated temporary bytes")
        }

        test("same-size saved-audio corruption cannot be published as a backup") {
            let store = try PocketLibraryStore(root: path("backup-corrupt")); _ = try store.importBundle(source)
            let audio = try store.audioURL(for: manifest.tracks[0]); var changed = payload; changed[0] ^= 1; try changed.write(to: audio)
            let output = path("corrupt-backup.stillroomlibrary")
            try rejected { try store.exportBundle(to: output) }
            try check(!fm.fileExists(atPath: output.path) && (try noPartialExports(root)), "Corrupt backup was published")
        }

        test("fresh-root recovery preserves bytes, IDs, playlist order, loop bounds and settings") {
            let target = path("fresh-restore")
            let restored = try PocketLibraryStore.restoreBundle(source, toNewRoot: target, expectedManifest: manifest)
            try check(restored.catalog.manifest == manifest, "Restored metadata differs")
            try check(try Data(contentsOf: restored.audioURL(for: restored.catalog.tracks[0])) == payload, "Restored audio differs")
            let exported = path("fresh-restored-backup.stillroomlibrary"); try restored.exportBundle(to: exported)
            let twice = try PocketLibraryStore.restoreBundle(exported, toNewRoot: path("fresh-restore-twice"))
            try check(twice.catalog == restored.catalog, "Backup/restore round trip differs")
        }

        test("fresh-root recovery never replaces an existing, empty, damaged or symlink destination") {
            let existing = path("refuse-existing"); let existingStore = try PocketLibraryStore(root: existing); _ = try existingStore.importBundle(source)
            let empty = path("refuse-empty"); try fm.createDirectory(at: empty, withIntermediateDirectories: false)
            let damaged = path("refuse-damaged"); try fm.createDirectory(at: damaged, withIntermediateDirectories: false)
            try Data("Keep this damaged catalog".utf8).write(to: damaged.appendingPathComponent("catalog.json"))
            let alias = path("refuse-alias"); try fm.createSymbolicLink(at: alias, withDestinationURL: existing)
            let file = path("refuse-file"); try Data("Keep this file".utf8).write(to: file)
            let before = try snapshot(existing), damagedBefore = try snapshot(damaged)
            for destination in [existing, empty, damaged, alias, file] {
                try rejected { _ = try PocketLibraryStore.restoreBundle(source, toNewRoot: destination) }
            }
            try check(try snapshot(existing) == before && snapshot(damaged) == damagedBefore, "Recovery changed an existing destination")
            try check(try fm.contentsOfDirectory(atPath: empty.path).isEmpty, "Recovery wrote into an existing empty folder")
            try check(try Data(contentsOf: file) == Data("Keep this file".utf8), "Recovery changed an existing file")
        }

        test("known-good backup recovers beside an unreadable library without altering it") {
            let damaged = path("broken-library"); let store = try PocketLibraryStore(root: damaged); _ = try store.importBundle(source)
            let backup = path("known-good.stillroomlibrary"); try store.exportBundle(to: backup)
            try Data("Truncated damaged catalog".utf8).write(to: damaged.appendingPathComponent("catalog.json"))
            try rejected { _ = try PocketLibraryStore(root: damaged) }
            let before = try snapshot(damaged)
            let recovered = try PocketLibraryStore.restoreBundle(backup, toNewRoot: path("beside-broken-library"))
            try check(recovered.catalog.manifest == manifest, "Recovery changed the known-good snapshot")
            try check(try snapshot(damaged) == before, "Fresh recovery touched the damaged library")
        }

        test("corrupt or changed-after-review backups create no recovery root") {
            var changed = payload; changed[4] ^= 1
            let bad = try bundle(manifest, changed)
            let corruptTarget = path("corrupt-recovery")
            try rejected { _ = try PocketLibraryStore.restoreBundle(bad, toNewRoot: corruptTarget) }
            try check(!fm.fileExists(atPath: corruptTarget.path), "Corrupt input created a recovery folder")
            var different = manifest; different.tracks[0].title = "Changed after review"
            let reviewedTarget = path("changed-review-recovery")
            try rejected { _ = try PocketLibraryStore.restoreBundle(source, toNewRoot: reviewedTarget, expectedManifest: different) }
            try check(!fm.fileExists(atPath: reviewedTarget.path), "Changed review created a recovery folder")
        }

        test("low-space and preflight cancellation create no recovery root") {
            let lowTarget = path("low-space-recovery")
            do { _ = try PocketLibraryStore.restoreBundle(source, toNewRoot: lowTarget, availableBytes: { 0 }); throw RecoveryFailure(description: "Expected low-space rejection") }
            catch PocketTransferError.insufficientSpace { }
            try check(!fm.fileExists(atPath: lowTarget.path), "Low storage created recovery data")
            let cancelled = path("cancelled-recovery"); var calls = 0
            do { _ = try PocketLibraryStore.restoreBundle(source, toNewRoot: cancelled, cancellation: { calls += 1; return calls >= 3 }); throw RecoveryFailure(description: "Expected cancellation") }
            catch PocketTransferError.cancelled { }
            try check(!fm.fileExists(atPath: cancelled.path), "Cancelled preflight created recovery data")
        }

        test("failed recovery commit keeps its isolated destination unselected and empty") {
            let target = path("failed-recovery-commit")
            do {
                _ = try PocketLibraryStore.restoreBundle(source, toNewRoot: target, beforeCatalogCommit: { throw RecoveryFailure(description: "Injected restore failure") })
                throw RecoveryFailure(description: "Recovery failure was ignored")
            } catch let error as RecoveryFailure { try check(error.description == "Injected restore failure", "Unexpected test error") }
            let reopened = try PocketLibraryStore(root: target)
            try check(reopened.catalog.tracks.isEmpty, "Failed recovery committed songs")
            try check(try fm.contentsOfDirectory(atPath: target.appendingPathComponent("Media").path).isEmpty, "Failed recovery leaked media")
            try check(try fm.contentsOfDirectory(atPath: target.appendingPathComponent("Transactions").path).isEmpty, "Failed recovery leaked staging")
        }

        test("recovery preview hashes bytes and reports exact metadata without creating folders") {
            let parent = path("preview-parent"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let target = parent.appendingPathComponent("unused")
            let before = try snapshot(parent)
            let preview = try PocketLibraryStore.previewBackup(source, forNewRoot: target)
            try check(preview.manifest == manifest && preview.tracksToAdd == 1 && preview.playlistsToAdd == 1 && preview.duplicateTracks == 0, "Recovery preview differs from the backup")
            try check(preview.audioBytes == payload.count && preview.requiredBytes > payload.count, "Recovery preview omitted storage overhead")
            try check(!fm.fileExists(atPath: target.path) && (try snapshot(parent)) == before, "Recovery preview created or changed a library")
            var broken = payload; broken[6] ^= 1
            let bad = try bundle(manifest, broken)
            try rejected { _ = try PocketLibraryStore.previewBackup(bad, forNewRoot: target) }
            do { _ = try PocketLibraryStore.previewBackup(source, forNewRoot: target, availableBytes: { 0 }); throw RecoveryFailure(description: "Low storage preview succeeded") }
            catch PocketTransferError.insufficientSpace { }
            do { _ = try PocketLibraryStore.previewBackup(source, forNewRoot: target, cancellation: { true }); throw RecoveryFailure(description: "Cancelled preview succeeded") }
            catch PocketTransferError.cancelled { }
            try check(try snapshot(parent) == before, "Failed preview changed the destination parent")
        }

        test("library location defaults and proposals are read-only and use app-local siblings") {
            let parent = path("location-default"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let location = try PocketLibraryLocation(parent: parent)
            try check(try location.selectedRoot() == location.defaultRoot, "Missing selector did not select the default path")
            let proposal = location.proposedRecoveryRoot()
            try check(proposal.deletingLastPathComponent().path == parent.path && proposal.lastPathComponent.hasPrefix("PocketStillroom-Recovered-"), "Recovery proposal escapes the parent")
            try check(try fm.contentsOfDirectory(atPath: parent.path).isEmpty, "Inspecting a location created persistent files")
        }

        test("invalid bounded selectors reject traversal, absolute paths, versions and missing folders without repair") {
            let parent = path("location-invalid"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let selector = parent.appendingPathComponent("PocketStillroom-location.json")
            let location = try PocketLibraryLocation(parent: parent)
            let validMissing = "PocketStillroom-Recovered-" + UUID().uuidString
            let values: [Data] = [Data("broken JSON".utf8), Data(repeating: 65, count: 1025)] + ["../escape", "/tmp/escape", "PocketStillroom", validMissing].map {
                try! JSONSerialization.data(withJSONObject: ["format": "pocket-stillroom-location", "version": 1, "folder": $0])
            } + [try JSONSerialization.data(withJSONObject: ["format": "pocket-stillroom-location", "version": 2, "folder": validMissing])]
            for bytes in values {
                try bytes.write(to: selector)
                try rejected { _ = try location.selectedRoot() }
                try check(try Data(contentsOf: selector) == bytes, "Invalid selector was overwritten")
            }
            try fm.removeItem(at: selector)
            let outside = path("selector-secret"); let sentinel = Data("Keep local bytes".utf8); try sentinel.write(to: outside)
            try fm.createSymbolicLink(at: selector, withDestinationURL: outside)
            try rejected { _ = try location.selectedRoot() }
            try check(try Data(contentsOf: outside) == sentinel, "Selector followed or modified an unrelated file")
        }

        test("successful explicit activation preserves the damaged default library and persists selection") {
            let parent = path("activation-success"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let location = try PocketLibraryLocation(parent: parent)
            try fm.createDirectory(at: location.defaultRoot, withIntermediateDirectories: false)
            try Data("Damaged old catalogue must survive".utf8).write(to: location.defaultRoot.appendingPathComponent("catalog.json"))
            let before = try snapshot(location.defaultRoot)
            let target = location.proposedRecoveryRoot()
            let preview = try PocketLibraryStore.previewBackup(source, forNewRoot: target)
            let recovered = try PocketLibraryStore.restoreBundle(source, toNewRoot: target, expectedManifest: preview.manifest)
            try check(try location.selectedRoot() == location.defaultRoot, "Restore selected itself before confirmation")
            try location.activateRecoveredLibrary(recovered)
            let reopenedLocation = try PocketLibraryLocation(parent: parent)
            try check(try reopenedLocation.selectedRoot() == target, "Successful recovery selection did not persist")
            try check(try snapshot(location.defaultRoot) == before, "Activation changed the damaged library")
            try check(try PocketLibraryStore(root: reopenedLocation.selectedRoot()).catalog.manifest == manifest, "Activated library differs from the verified backup")
        }

        test("activation preserves a broken regular selector before replacing it") {
            let parent = path("activation-preserve-selector"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let location = try PocketLibraryLocation(parent: parent)
            let selector = parent.appendingPathComponent("PocketStillroom-location.json")
            let bytes = Data("Keep this invalid selector for recovery".utf8); try bytes.write(to: selector)
            try rejected { _ = try location.selectedRoot() }
            let recovered = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
            try location.activateRecoveredLibrary(recovered)
            try check(location.preservedSelectorURL != nil, "Previous selector was not preserved")
            try check(try Data(contentsOf: location.preservedSelectorURL!) == bytes, "Previous selector bytes changed")
            try check(try location.selectedRoot() == recovered.root, "Repaired selection is not active")
        }

        test("activation refuses an ordinary unverified store and recovered folders outside its parent") {
            let parent = path("activation-unverified"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let location = try PocketLibraryLocation(parent: parent)
            let ordinary = try PocketLibraryStore(root: location.proposedRecoveryRoot()); _ = try ordinary.importBundle(source)
            try rejected { try location.activateRecoveredLibrary(ordinary) }
            let otherParent = path("activation-other-parent"); try fm.createDirectory(at: otherParent, withIntermediateDirectories: false)
            let otherLocation = try PocketLibraryLocation(parent: otherParent)
            let other = try PocketLibraryStore.restoreBundle(source, toNewRoot: otherLocation.proposedRecoveryRoot(), expectedManifest: manifest)
            try rejected { try location.activateRecoveredLibrary(other) }
            try check(try location.selectedRoot() == location.defaultRoot, "A rejected activation changed the selected root")
        }

        for changeCatalog in [false, true] {
            test("activation rejects restored \(changeCatalog ? "catalogue" : "audio") changes after validation") {
                let parent = path(changeCatalog ? "activation-mutated-catalog" : "activation-mutated-audio")
                try fm.createDirectory(at: parent, withIntermediateDirectories: false)
                let location = try PocketLibraryLocation(parent: parent)
                let recovered = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
                if changeCatalog { try recovered.saveSettings(.init(playbackRate: 1.25)) }
                else {
                    let audio = try recovered.audioURL(for: manifest.tracks[0]); var bytes = payload; bytes[9] ^= 1
                    try bytes.write(to: audio)
                }
                try rejected { try location.activateRecoveredLibrary(recovered) }
                try check(try location.selectedRoot() == location.defaultRoot, "Changed recovered bytes were activated")
            }
        }

        test("final activation cancellation retains an existing selection and leaves no temporary selector") {
            let parent = path("activation-cancel"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let initial = try PocketLibraryLocation(parent: parent)
            let first = try PocketLibraryStore.restoreBundle(source, toNewRoot: initial.proposedRecoveryRoot(), expectedManifest: manifest)
            try initial.activateRecoveredLibrary(first)
            let selector = parent.appendingPathComponent("PocketStillroom-location.json"); let before = try Data(contentsOf: selector)
            var cancel = false
            let location = try PocketLibraryLocation(parent: parent, beforeSelectionCommit: { cancel = true })
            let second = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
            do { try location.activateRecoveredLibrary(second, cancellation: { cancel }); throw RecoveryFailure(description: "Final cancellation was ignored") }
            catch PocketTransferError.cancelled { }
            try check(try location.selectedRoot() == first.root && Data(contentsOf: selector) == before, "Cancellation changed the previous selection")
            try check(try !fm.contentsOfDirectory(atPath: parent.path).contains { $0.hasPrefix(".pocket-selection-") }, "Cancellation leaked a partial selector")
        }

        test("activation rejects selector replacement and recovered-audio tampering at the commit hook") {
            for tamperAudio in [false, true] {
                let parent = path(tamperAudio ? "activation-hook-audio" : "activation-hook-selector")
                try fm.createDirectory(at: parent, withIntermediateDirectories: false)
                let selector = parent.appendingPathComponent("PocketStillroom-location.json")
                let outside = path(UUID().uuidString); let sentinel = Data("Never overwrite this".utf8); try sentinel.write(to: outside)
                var candidate: PocketLibraryStore?
                let location = try PocketLibraryLocation(parent: parent, beforeSelectionCommit: {
                    if tamperAudio {
                        let audio = try candidate!.audioURL(for: manifest.tracks[0]); var bytes = payload; bytes[15] ^= 1; try bytes.write(to: audio)
                    } else { try fm.createSymbolicLink(at: selector, withDestinationURL: outside) }
                })
                candidate = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
                try rejected { try location.activateRecoveredLibrary(candidate!) }
                try check(try Data(contentsOf: outside) == sentinel, "Activation changed an unrelated selector target")
                if tamperAudio { try check(try location.selectedRoot() == location.defaultRoot, "Tampered audio was activated") }
                else { try rejected { _ = try location.selectedRoot() } }
            }
        }

        test("activation refuses substituted staged selector without changing the active library") {
            let parent = path("activation-substituted-selector"); try fm.createDirectory(at: parent, withIntermediateDirectories: false)
            let sentinel = Data("Unrelated staged selector bytes".utf8)
            var substituted: URL?
            let location = try PocketLibraryLocation(parent: parent, beforeSelectionCommit: {
                let temporary = try fm.contentsOfDirectory(at: parent, includingPropertiesForKeys: nil).first { $0.lastPathComponent.hasPrefix(".pocket-selection-") }!
                try fm.moveItem(at: temporary, to: parent.appendingPathComponent("owned-selector"))
                try sentinel.write(to: temporary); substituted = temporary
            })
            let recovered = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
            try rejected { try location.activateRecoveredLibrary(recovered) }
            try check(try location.selectedRoot() == location.defaultRoot, "Substituted selector became active")
            guard let substituted else { throw RecoveryFailure(description: "Selector commit hook was not reached") }
            try check(try Data(contentsOf: substituted) == sentinel, "Rejection removed unrelated selector bytes")
        }

        test("missing saved catalogue with retained audio refuses empty-library startup and writes") {
            let target = path("missing-catalogue")
            let store = try PocketLibraryStore(root: target); _ = try store.importBundle(source)
            let catalog = target.appendingPathComponent("catalog.json"); try fm.removeItem(at: catalog)
            let media = target.appendingPathComponent("Media"); let before = try snapshot(media)
            try rejected { _ = try PocketLibraryStore(root: target) }
            try rejected { try store.saveSettings(.init()) }
            try check(!fm.fileExists(atPath: catalog.path), "A missing catalogue was silently replaced")
            try check(try snapshot(media) == before, "Missing-catalogue detection changed retained audio")
        }

        test("missing catalogue preserves unknown media files and directories for recovery") {
            let target = path("missing-catalogue-unknown")
            _ = try PocketLibraryStore(root: target)
            let media = target.appendingPathComponent("Media")
            let sentinel = Data("Keep unknown retained material".utf8)
            try sentinel.write(to: media.appendingPathComponent("unknown-original.bin"))
            let directory = media.appendingPathComponent("unknown-folder")
            try fm.createDirectory(at: directory, withIntermediateDirectories: false)
            try sentinel.write(to: directory.appendingPathComponent("keep.txt"))
            let before = try snapshot(media)
            try rejected { _ = try PocketLibraryStore(root: target) }
            try check(try snapshot(media) == before, "Unknown retained media changed")
            try check(!fm.fileExists(atPath: target.appendingPathComponent("catalog.json").path), "Unknown retained files were hidden by an empty catalogue")
        }

        for hasPrevious in [false, true] {
            test("last activation cancellation gate preserves \(hasPrevious ? "existing" : "absent") selector after proof validation") {
                let parent = path(hasPrevious ? "activation-last-existing" : "activation-last-absent")
                try fm.createDirectory(at: parent, withIntermediateDirectories: false)
                let location = try PocketLibraryLocation(parent: parent)
                if hasPrevious {
                    let first = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
                    try location.activateRecoveredLibrary(first)
                }
                let selectedBefore = try location.selectedRoot()
                let selector = parent.appendingPathComponent("PocketStillroom-location.json")
                let previousBytes = hasPrevious ? try Data(contentsOf: selector) : nil
                let recovered = try PocketLibraryStore.restoreBundle(source, toNewRoot: location.proposedRecoveryRoot(), expectedManifest: manifest)
                let recoveredBefore = try snapshot(recovered.root)
                var checks = 0
                do {
                    try location.activateRecoveredLibrary(recovered, cancellation: { checks += 1; return checks == 3 })
                    throw RecoveryFailure(description: "Last-gate cancellation was ignored")
                } catch PocketTransferError.cancelled { }
                try check(checks == 3, "Cancellation did not reach the final post-proof gate")
                try check(try location.selectedRoot() == selectedBefore, "Final cancellation changed the selected library")
                if let previousBytes { try check(try Data(contentsOf: selector) == previousBytes, "Final cancellation changed existing selector bytes") }
                else { try check(!fm.fileExists(atPath: selector.path), "Final cancellation published an initial selector") }
                let names = try fm.contentsOfDirectory(atPath: parent.path)
                try check(!names.contains { $0.hasPrefix(".pocket-selection-") || $0.hasPrefix("PocketStillroom-location.previous-") }, "Final cancellation leaked staging or unnecessarily archived the selector")
                try check(try snapshot(recovered.root) == recoveredBefore, "Final cancellation altered the restored library")
            }
        }

        test("all backup and recovery attempts preserve the source bundle") {
            try check(try Data(contentsOf: source) == originalSource, "The source bundle was modified")
        }

        let report: [String: Any] = ["suite": "PocketCore backup/recovery safeguards", "passed": names.count, "failed": failures.count,
                                    "tests": names, "failures": failures, "generatedFixturesOnly": true,
                                    "physicalDeviceValidated": false, "destructiveInPlaceRestore": false]
        let reports = URL(fileURLWithPath: fm.currentDirectoryPath).appendingPathComponent("Reports")
        try fm.createDirectory(at: reports, withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: reports.appendingPathComponent("transfer-recovery-tests.json"))
        print("\(names.count) recovery checks passed; \(failures.count) failed")
        if !failures.isEmpty { throw RecoveryFailure(description: failures.joined(separator: "\n")) }
    }
}
