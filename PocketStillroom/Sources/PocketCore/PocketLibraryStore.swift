import Foundation
import CryptoKit
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

private struct FileIdentity: Equatable {
    let device: UInt64
    let inode: UInt64
    let bytes: Int64
    let seconds: Int64
    let nanoseconds: Int64
    let changedSeconds: Int64
    let changedNanoseconds: Int64
    init(_ info: stat) {
        device = UInt64(info.st_dev); inode = UInt64(info.st_ino); bytes = Int64(info.st_size)
        #if canImport(Darwin)
        seconds = Int64(info.st_mtimespec.tv_sec); nanoseconds = Int64(info.st_mtimespec.tv_nsec)
        changedSeconds = Int64(info.st_ctimespec.tv_sec); changedNanoseconds = Int64(info.st_ctimespec.tv_nsec)
        #else
        seconds = Int64(info.st_mtim.tv_sec); nanoseconds = Int64(info.st_mtim.tv_nsec)
        changedSeconds = Int64(info.st_ctim.tv_sec); changedNanoseconds = Int64(info.st_ctim.tv_nsec)
        #endif
    }
}

private func regularFile(_ url: URL) throws -> FileHandle {
    try require(url.isFileURL, "Choose a local downloaded library file.")
    let descriptor = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
    guard descriptor >= 0 else { throw PocketTransferError.invalid("The selected file could not be opened safely. Download it in Files and try again.") }
    var info = stat()
    guard fstat(descriptor, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG else {
        close(descriptor); throw PocketTransferError.invalid("Choose a regular file, not a link or folder.")
    }
    return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
}

private func identity(_ handle: FileHandle) throws -> FileIdentity {
    var info = stat()
    try require(fstat(handle.fileDescriptor, &info) == 0, "The selected file is no longer available.")
    return FileIdentity(info)
}

/// Rollback may remove only the inode this operation created. Never recursively
/// remove a path that another actor could have replaced with unrelated data.
private func unlinkOwnedFile(_ url: URL, owner: FileIdentity) {
    var info = stat()
    guard lstat(url.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
          UInt64(info.st_dev) == owner.device, UInt64(info.st_ino) == owner.inode else { return }
    _ = unlink(url.path)
}

private func availableStorageBytes(at directory: URL, override: (() throws -> Int64)?) throws -> Int64 {
    if let override { return try override() }
    let values = try directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
    if let amount = values.volumeAvailableCapacityForImportantUsage, amount > 0 { return amount }
    let attributes = try FileManager.default.attributesOfFileSystem(forPath: directory.path)
    guard let number = attributes[.systemFreeSize] as? NSNumber else {
        throw PocketTransferError.invalid("Available storage could not be checked.")
    }
    return number.int64Value
}

private func checkedCancellation(_ cancelled: () -> Bool) throws {
    if cancelled() { throw PocketTransferError.cancelled }
}

private func readExactly(_ handle: FileHandle, count: Int) throws -> Data {
    var bytes = Data(); bytes.reserveCapacity(count)
    while bytes.count < count {
        let chunk = try handle.read(upToCount: count - bytes.count) ?? Data()
        try require(!chunk.isEmpty, "The bundle is incomplete. Transfer the entire file and try again.")
        bytes.append(chunk)
    }
    return bytes
}

private func createPrivateFile(_ url: URL) throws -> FileHandle {
    let descriptor = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
    guard descriptor >= 0 else { throw PocketTransferError.invalid("A staging file could not be created safely. Your existing library is unchanged.") }
    #if os(iOS)
    try? FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: url.path)
    #endif
    return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
}

private struct TransactionJournal: Codable {
    let format: String
    let version: Int
    let transactionID: String
    let operation: String
    let ownedFiles: [String]
}

private func validMediaName(_ name: String) -> Bool {
    let parts = name.split(separator: ".", omittingEmptySubsequences: false)
    let extensions: Set<String> = ["m4a", "aac", "mp3", "wav", "flac", "opus", "ogg", "webm"]
    return parts.count == 2 && parts[0].utf8.count == 64 && parts[0].utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } && extensions.contains(String(parts[1]))
}

@discardableResult private func syncDirectory(_ url: URL) -> Bool {
    let descriptor = open(url.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard descriptor >= 0 else { return false }
    defer { close(descriptor) }
    return fsync(descriptor) == 0
}

private final class BundleReader {
    let handle: FileHandle
    let originalIdentity: FileIdentity
    let manifest: PocketManifest
    let manifestData: Data
    let audioBytes: Int64
    init(url: URL) throws {
        handle = try regularFile(url)
        originalIdentity = try identity(handle)
        try require(originalIdentity.bytes >= 12 && originalIdentity.bytes <= PocketLimits.totalBytes + Int64(PocketLimits.manifestBytes) + 12, "This bundle is empty, incomplete or too large.")
        let header = try readExactly(handle, count: 12)
        try require(header.prefix(8) == PocketLimits.header, "Choose a .stillroomlibrary file exported by Stillroom.")
        let length = header.suffix(4).reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        try require(length > 0 && length <= PocketLimits.manifestBytes, "The bundle's catalogue is too large or invalid.")
        manifestData = try readExactly(handle, count: Int(length))
        do { manifest = try JSONDecoder().decode(PocketManifest.self, from: manifestData) }
        catch { throw PocketTransferError.invalid("The bundle catalogue could not be read. Export a fresh copy from Stillroom.") }
        audioBytes = try validateManifest(manifest)
        try require(originalIdentity.bytes == 12 + Int64(length) + audioBytes, "The bundle has missing or unexpected data.")
    }
    deinit { try? handle.close() }

    /// Hash every declared byte, even for songs already present. Optional outputs
    /// are private staged files owned by the current import transaction.
    func consume(outputs: [String: URL] = [:], cancelled: () -> Bool) throws {
        for file in manifest.files {
            try checkedCancellation(cancelled)
            let output = try outputs[file.path].map(createPrivateFile)
            do {
                var remaining = file.byteCount; var hasher = SHA256()
                while remaining > 0 {
                    try checkedCancellation(cancelled)
                    let chunk = try readExactly(handle, count: Int(min(remaining, Int64(PocketLimits.chunkBytes))))
                    hasher.update(data: chunk); try output?.write(contentsOf: chunk)
                    remaining -= Int64(chunk.count)
                }
                let hash = hasher.finalize().map { String(format: "%02x", $0) }.joined()
                try require(hash == file.sha256, "An audio fingerprint does not match. The bundle may be damaged; export it again.")
                try output?.synchronize(); try output?.close()
            } catch {
                try? output?.close(); throw error
            }
        }
        try checkedCancellation(cancelled)
        try require((try handle.read(upToCount: 1) ?? Data()).isEmpty, "The bundle contains unexpected trailing data.")
        try require(try identity(handle) == originalIdentity, "The source bundle changed during import. Try again with a stable downloaded copy.")
    }
}

/// A local, versioned catalogue with immutable content-addressed audio files.
/// The catalogue rename is the commit point: until it succeeds, no imported song
/// or playlist is visible. Failed transactions remove only their own new files.
public final class PocketLibraryStore {
    public let root: URL
    private let lock = NSRecursiveLock()
    private let media: URL
    private let transactions: URL
    private let catalogURL: URL
    private let lockURL: URL
    private var current = PocketCatalog()
    private let availableBytes: (() throws -> Int64)?
    private let beforeCatalogCommit: (() throws -> Void)?
    private let beforeExportPublish: (() throws -> Void)?
    private var recoveryProof: (catalog: FileIdentity, media: [String: FileIdentity])?
    public private(set) var durabilityWarning: String?
    public var catalog: PocketCatalog { locked { current } }

    public init(root: URL, availableBytes: (() throws -> Int64)? = nil, beforeCatalogCommit: (() throws -> Void)? = nil, beforeExportPublish: (() throws -> Void)? = nil) throws {
        try require(root.isFileURL, "The library must be stored locally.")
        self.root = root.standardizedFileURL
        media = self.root.appendingPathComponent("Media", isDirectory: true)
        transactions = self.root.appendingPathComponent("Transactions", isDirectory: true)
        catalogURL = self.root.appendingPathComponent("catalog.json")
        lockURL = self.root.appendingPathComponent(".store.lock")
        self.availableBytes = availableBytes; self.beforeCatalogCommit = beforeCatalogCommit
        self.beforeExportPublish = beforeExportPublish
        try ensureDirectory(self.root); try ensureDirectory(media); try ensureDirectory(transactions)
        try withStoreLock { try reload(); try recoverOwnedTransactions() }
    }

    private static func recoveryDestination(_ destination: URL) throws -> URL {
        try require(destination.isFileURL, "Choose a new local recovery folder.")
        let target = destination.standardizedFileURL
        var existing = stat()
        try require(lstat(target.path, &existing) != 0 && errno == ENOENT, "Recovery needs a new folder. Existing libraries and folders are never replaced.")
        let parent = target.deletingLastPathComponent()
        var parentInfo = stat()
        try require(lstat(parent.path, &parentInfo) == 0 && (parentInfo.st_mode & S_IFMT) == S_IFDIR, "Choose an existing local parent recovery folder, not a link.")
        return target
    }

    private static func recoveryPreview(_ reader: BundleReader, parent: URL, availableBytes: (() throws -> Int64)?) throws -> PocketImportPreview {
        let required = reader.audioBytes + Int64(reader.manifestData.count * 2) + PocketLimits.safetyBytes
        let available = try availableStorageBytes(at: parent, override: availableBytes)
        if available < required { throw PocketTransferError.insufficientSpace(required: required, available: available) }
        return .init(manifest: reader.manifest, tracksToAdd: reader.manifest.tracks.count,
                     playlistsToAdd: reader.manifest.playlists.count, duplicateTracks: 0,
                     requiredBytes: required, audioBytes: reader.audioBytes)
    }

    /// Validate a backup without creating a library or touching a damaged store.
    /// This hashes all bytes and estimates space on the destination volume.
    public static func previewBackup(_ url: URL, forNewRoot destination: URL,
                                     availableBytes: (() throws -> Int64)? = nil,
                                     cancellation: () -> Bool = { false }) throws -> PocketImportPreview {
        let target = try recoveryDestination(destination)
        try checkedCancellation(cancellation)
        let reader = try BundleReader(url: url)
        let preview = try recoveryPreview(reader, parent: target.deletingLastPathComponent(), availableBytes: availableBytes)
        try reader.consume(cancelled: cancellation)
        return preview
    }

    /// Recover into a new, exclusively created library. Existing destinations
    /// (including empty folders and damaged libraries) are always refused.
    /// This never switches the app's active root or modifies the old library.
    /// A failed copy/probe leaves its isolated folder available for inspection;
    /// normal import rollback removes only its own newly created files.
    public static func restoreBundle(_ url: URL, toNewRoot destination: URL, expectedManifest: PocketManifest? = nil,
                                     availableBytes: (() throws -> Int64)? = nil,
                                     beforeCatalogCommit: (() throws -> Void)? = nil,
                                     cancellation: () -> Bool = { false },
                                     validateAudio: (URL) throws -> Void = { _ in }) throws -> PocketLibraryStore {
        let target = try recoveryDestination(destination)
        try checkedCancellation(cancellation)
        let reader = try BundleReader(url: url)
        if let expectedManifest {
            try require(reader.manifest == expectedManifest, "The selected backup changed after review. Review it again before restoring.")
        } else {
            // Standalone recovery validates before creating a destination. After
            // a reviewed preview, import rechecks metadata and hashes as it copies.
            try reader.consume(cancelled: cancellation)
        }
        _ = try recoveryPreview(reader, parent: target.deletingLastPathComponent(), availableBytes: availableBytes)
        try checkedCancellation(cancellation)
        guard mkdir(target.path, S_IRWXU) == 0 else {
            throw PocketTransferError.invalid("The new recovery folder could not be created exclusively. Nothing was replaced.")
        }
        let restored = try PocketLibraryStore(root: target, availableBytes: availableBytes, beforeCatalogCommit: beforeCatalogCommit)
        _ = try restored.importBundle(url, expectedManifest: reader.manifest, cancellation: cancellation, validateAudio: validateAudio)
        try restored.withStoreLock {
            let catalog = try regularFile(restored.catalogURL); defer { try? catalog.close() }
            var mediaProof = [String: FileIdentity]()
            for entry in restored.current.files {
                let url = restored.mediaURL(entry)
                let handle = try regularFile(url); defer { try? handle.close() }
                mediaProof[url.lastPathComponent] = try identity(handle)
            }
            restored.recoveryProof = (try identity(catalog), mediaProof)
        }
        return restored
    }

    fileprivate func validatePreparedRecovery() throws {
        guard let proof = recoveryProof else {
            throw PocketTransferError.invalid("Only a freshly completed backup restore can be activated. Restore the backup into a new folder first.")
        }
        let catalog = try regularFile(catalogURL); defer { try? catalog.close() }
        try require(try identity(catalog) == proof.catalog, "The recovered catalogue changed. Restore the backup again before switching libraries.")
        for (name, expected) in proof.media {
            let handle = try regularFile(media.appendingPathComponent(name)); defer { try? handle.close() }
            try require(try identity(handle) == expected, "Recovered audio changed. Restore the backup again before switching libraries.")
        }
    }

    fileprivate func withPreparedRecovery<T>(_ work: () throws -> T) throws -> T {
        try withStoreLock { try validatePreparedRecovery(); return try work() }
    }

    private func locked<T>(_ work: () throws -> T) rethrows -> T {
        lock.lock(); defer { lock.unlock() }; return try work()
    }

    private func withStoreLock<T>(_ work: () throws -> T) throws -> T {
        try locked {
            try ensureDirectory(root); try ensureDirectory(media); try ensureDirectory(transactions)
            let descriptor = open(lockURL.path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
            guard descriptor >= 0 else { throw PocketTransferError.invalid("The local library lock could not be opened safely.") }
            defer { close(descriptor) }
            var info = stat()
            try require(fstat(descriptor, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG, "The local library lock is unsafe.")
            guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { throw PocketTransferError.invalid("Another library operation is running. Wait for it to finish.") }
            defer { flock(descriptor, LOCK_UN) }
            return try work()
        }
    }

    private func ensureDirectory(_ url: URL) throws {
        var info = stat()
        if lstat(url.path, &info) == 0 {
            try require((info.st_mode & S_IFMT) == S_IFDIR, "A local library folder is not a safe directory.")
        } else {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        }
    }

    private func makeStage(operation: String, names: [String]) throws -> URL {
        let id = UUID().uuidString
        let stage = transactions.appendingPathComponent(id, isDirectory: true)
        try ensureDirectory(stage)
        let journal = TransactionJournal(format: "pocket-stillroom-transaction", version: 1, transactionID: id, operation: operation, ownedFiles: names.sorted())
        let bytes = try JSONEncoder().encode(journal)
        let handle = try createPrivateFile(stage.appendingPathComponent("transaction.json"))
        do { try handle.write(contentsOf: bytes); try handle.synchronize(); try handle.close() }
        catch { try? handle.close(); throw error }
        _ = syncDirectory(stage); _ = syncDirectory(transactions)
        return stage
    }

    /// Only journal-owned files are unlinked. An unexpected child is preserved,
    /// so a user-added file is never swept by recursive directory deletion.
    private func removeStage(_ stage: URL, names: [String]) {
        for name in names + ["transaction.json"] { _ = unlink(stage.appendingPathComponent(name).path) }
        _ = rmdir(stage.path); _ = syncDirectory(transactions)
    }

    private func recoverOwnedTransactions() throws {
        let references = Set(current.files.map { mediaURL($0).lastPathComponent })
        for stage in try FileManager.default.contentsOfDirectory(at: transactions, includingPropertiesForKeys: nil) {
            guard UUID(uuidString: stage.lastPathComponent)?.uuidString == stage.lastPathComponent else { continue }
            var directoryInfo = stat()
            guard lstat(stage.path, &directoryInfo) == 0 && (directoryInfo.st_mode & S_IFMT) == S_IFDIR else { continue }
            let journalURL = stage.appendingPathComponent("transaction.json")
            guard let handle = try? regularFile(journalURL) else { continue }
            let journal: TransactionJournal?
            do {
                let info = try identity(handle)
                if info.bytes > 0 && info.bytes <= PocketLimits.manifestBytes {
                    journal = try JSONDecoder().decode(TransactionJournal.self, from: handle.readToEnd() ?? Data())
                } else { journal = nil }
                try handle.close()
            } catch { try? handle.close(); continue }
            guard let journal, journal.format == "pocket-stillroom-transaction", journal.version == 1,
                  journal.transactionID == stage.lastPathComponent, journal.ownedFiles.count <= PocketLimits.objectCount,
                  Set(journal.ownedFiles).count == journal.ownedFiles.count,
                  ((journal.operation == "import" && journal.ownedFiles.allSatisfy(validMediaName)) || (journal.operation == "catalog" && journal.ownedFiles == ["catalog.pending"])) else { continue }
            let children = try FileManager.default.contentsOfDirectory(at: stage, includingPropertiesForKeys: nil)
            let allowed = Set(journal.ownedFiles + ["transaction.json"])
            guard children.allSatisfy({ allowed.contains($0.lastPathComponent) }) else { continue }
            var safe = true
            for child in children {
                var info = stat()
                if lstat(child.path, &info) != 0 || (info.st_mode & S_IFMT) != S_IFREG { safe = false; break }
            }
            guard safe else { continue }
            if journal.operation == "import" {
                for name in journal.ownedFiles where !references.contains(name) {
                    var staged = stat(); var published = stat()
                    let source = stage.appendingPathComponent(name); let destination = media.appendingPathComponent(name)
                    if lstat(source.path, &staged) == 0 && lstat(destination.path, &published) == 0,
                       (published.st_mode & S_IFMT) == S_IFREG,
                       staged.st_dev == published.st_dev && staged.st_ino == published.st_ino {
                        _ = unlink(destination.path)
                    }
                }
            }
            removeStage(stage, names: journal.ownedFiles)
        }
        _ = syncDirectory(media)
    }

    private func reload() throws {
        var existing = stat()
        if lstat(catalogURL.path, &existing) != 0 {
            try require(errno == ENOENT, "The local catalogue could not be inspected safely.")
            // An interrupted first import may have linked journal-owned audio
            // before its catalogue commit. Recover only that proven ownership.
            // Remaining media means this is not a clean empty initialization.
            try recoverOwnedTransactions()
            try require(try FileManager.default.contentsOfDirectory(atPath: media.path).isEmpty,
                        "The saved catalogue is missing but audio remains. Keep the app installed and recover into a new library from a known-good backup.")
            current = .init(); return
        }
        let file = try regularFile(catalogURL); defer { try? file.close() }
        try require(try identity(file).bytes <= PocketLimits.manifestBytes, "The local catalogue is too large.")
        let bytes = try file.readToEnd() ?? Data()
        let value: PocketCatalog
        do { value = try JSONDecoder().decode(PocketCatalog.self, from: bytes) }
        catch { throw PocketTransferError.invalid("The saved catalogue could not be read. Keep the app installed and restore from a known-good export.") }
        guard value.schemaVersion == 1 else { throw PocketTransferError.unsupportedVersion(value.schemaVersion) }
        _ = try validateManifest(value.manifest)
        for entry in value.files {
            let handle = try regularFile(mediaURL(entry)); defer { try? handle.close() }
            try require(try identity(handle).bytes == entry.byteCount, "Saved audio is missing or incomplete. Restore from your backup bundle.")
        }
        current = value
    }

    private func mediaURL(_ file: PocketAudioFile) -> URL {
        let suffix = (file.path as NSString).pathExtension
        return media.appendingPathComponent(file.sha256 + "." + suffix)
    }

    public func audioURL(for track: PocketTrack) throws -> URL {
        try locked {
            guard let stored = current.tracks.first(where: { $0.id == track.id }),
                  let file = current.files.first(where: { $0.path == stored.audioFile }) else {
                throw PocketTransferError.invalid("This song is not in the local library.")
            }
            let url = mediaURL(file)
            let handle = try regularFile(url); defer { try? handle.close() }
            try require(try identity(handle).bytes == file.byteCount, "This song is not fully available offline.")
            return url
        }
    }

    private func merged(_ manifest: PocketManifest) throws -> (PocketCatalog, Int, Int, Int) {
        var result = current; var tracks = 0; var playlists = 0; var skipped = 0
        let oldTracks = Dictionary(uniqueKeysWithValues: current.tracks.map { ($0.id, $0) })
        let oldFiles = Dictionary(uniqueKeysWithValues: current.files.map { ($0.path, $0) })
        let oldPlaylists = Dictionary(uniqueKeysWithValues: current.playlists.map { ($0.id, $0) })
        for file in manifest.files {
            if let old = oldFiles[file.path] {
                guard old == file else { throw PocketTransferError.conflict("An audio filename already identifies different bytes. No songs were imported.") }
            } else { result.files.append(file) }
        }
        for track in manifest.tracks {
            if let old = oldTracks[track.id] {
                guard old == track else { throw PocketTransferError.conflict("A song ID already has different details or loop settings. Keep both exports and resolve the conflict on the desktop.") }
                skipped += 1
            } else { result.tracks.append(track); tracks += 1 }
        }
        for playlist in manifest.playlists {
            if let old = oldPlaylists[playlist.id] {
                guard old == playlist else { throw PocketTransferError.conflict("A playlist ID already has different contents. No playlists were overwritten.") }
            } else { result.playlists.append(playlist); playlists += 1 }
        }
        // Settings accompany an initial restore. Later imports keep the phone's
        // current playback preferences; the preview exposes incoming settings.
        if current.tracks.isEmpty, let settings = manifest.settings { result.settings = settings }
        _ = try validateManifest(result.manifest)
        return (result, tracks, playlists, skipped)
    }

    private func freeBytes() throws -> Int64 {
        // Some macOS volumes report zero for the "important usage" estimate
        // while the filesystem still has free blocks. The filesystem estimate
        // remains useful; an actual allocation failure still rolls back safely.
        return try availableStorageBytes(at: root, override: availableBytes)
    }

    private func preview(_ reader: BundleReader) throws -> PocketImportPreview {
        let (_, tracks, playlists, skipped) = try merged(reader.manifest)
        var unique = Set<String>(); var needed: Int64 = 0
        for file in reader.manifest.files {
            let destination = mediaURL(file)
            if unique.insert(destination.lastPathComponent).inserted && !FileManager.default.fileExists(atPath: destination.path) { needed += file.byteCount }
        }
        let required = needed + Int64(reader.manifestData.count * 2) + PocketLimits.safetyBytes
        let available = try freeBytes()
        if available < required { throw PocketTransferError.insufficientSpace(required: required, available: available) }
        return .init(manifest: reader.manifest, tracksToAdd: tracks, playlistsToAdd: playlists, duplicateTracks: skipped, requiredBytes: required, audioBytes: reader.audioBytes)
    }

    public func previewBundle(_ url: URL, cancellation: () -> Bool = { false }) throws -> PocketImportPreview {
        try withStoreLock {
            try reload(); try checkedCancellation(cancellation)
            let reader = try BundleReader(url: url); let result = try preview(reader)
            try reader.consume(cancelled: cancellation)
            return result
        }
    }

    public func importBundle(_ url: URL, expectedManifest: PocketManifest? = nil, cancellation: () -> Bool = { false }, validateAudio: (URL) throws -> Void = { _ in }) throws -> PocketImportResult {
        try withStoreLock {
            try reload(); try checkedCancellation(cancellation)
            let reader = try BundleReader(url: url)
            if let expectedManifest { try require(reader.manifest == expectedManifest, "The selected bundle changed after review. Preview it again before importing.") }
            let details = try preview(reader); let (next, _, _, _) = try merged(reader.manifest)
            let candidateNames = Array(Set(reader.manifest.files.map { mediaURL($0).lastPathComponent }))
            let stage = try makeStage(operation: "import", names: candidateNames)
            defer { removeStage(stage, names: candidateNames) }
            var outputs = [String: URL](); var physical = [String: URL]()
            for file in reader.manifest.files {
                let target = mediaURL(file); let name = target.lastPathComponent
                if FileManager.default.fileExists(atPath: target.path) {
                    try verifyAudioFile(target, entry: file, cancellation: cancellation)
                } else if physical[name] == nil {
                    let staged = stage.appendingPathComponent(name)
                    outputs[file.path] = staged; physical[name] = staged
                }
            }
            try reader.consume(outputs: outputs, cancelled: cancellation)
            // Probe every unique audio asset before making any catalogue change.
            var probed = Set<String>()
            for file in reader.manifest.files {
                let target = mediaURL(file)
                guard probed.insert(target.lastPathComponent).inserted else { continue }
                try checkedCancellation(cancellation)
                try validateAudio(physical[target.lastPathComponent] ?? target)
            }
            try checkedCancellation(cancellation)
            var published = [(url: URL, owner: FileIdentity)](); var committed = false
            defer {
                if !committed {
                    for item in published { unlinkOwnedFile(item.url, owner: item.owner) }
                    _ = syncDirectory(media)
                }
            }
            for (_, staged) in physical.sorted(by: { $0.key < $1.key }) {
                let destination = media.appendingPathComponent(staged.lastPathComponent)
                let file = try regularFile(staged)
                let owner = try identity(file); try file.close()
                try FileManager.default.linkItem(at: staged, to: destination) // exclusive; same-volume private staging
                published.append((destination, owner))
            }
            if !syncDirectory(media) { durabilityWarning = "Directory synchronization was unavailable; hard power-loss recovery remains unverified." }
            try checkedCancellation(cancellation)
            try persist(next, cancellation: cancellation)
            committed = true; current = next
            return .init(addedTracks: details.tracksToAdd, addedPlaylists: details.playlistsToAdd, skippedTracks: details.duplicateTracks, totalTracks: next.tracks.count)
        }
    }

    private func verifyAudioFile(_ url: URL, entry: PocketAudioFile, cancellation: () -> Bool) throws {
        let handle = try regularFile(url); defer { try? handle.close() }
        let before = try identity(handle)
        try require(before.bytes == entry.byteCount, "Saved audio does not match its fingerprint. Restore a known-good export.")
        var hasher = SHA256(); var total: Int64 = 0
        while let chunk = try handle.read(upToCount: PocketLimits.chunkBytes), !chunk.isEmpty {
            try checkedCancellation(cancellation); hasher.update(data: chunk); total += Int64(chunk.count)
            try require(total <= entry.byteCount, "Saved audio changed while it was being checked.")
        }
        let hash = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        try require(hash == entry.sha256 && total == entry.byteCount && (try identity(handle)) == before, "Saved audio does not match its fingerprint. Restore a known-good export.")
    }

    private func persist(_ value: PocketCatalog, cancellation: () -> Bool = { false }) throws {
        _ = try validateManifest(value.manifest)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let bytes = try encoder.encode(value)
        try require(bytes.count <= PocketLimits.manifestBytes, "The resulting catalogue exceeds its size limit.")
        let stage = try makeStage(operation: "catalog", names: ["catalog.pending"])
        let temporary = stage.appendingPathComponent("catalog.pending")
        let handle = try createPrivateFile(temporary)
        defer { try? handle.close(); removeStage(stage, names: ["catalog.pending"]) }
        try handle.write(contentsOf: bytes); try handle.synchronize(); try handle.close()
        try beforeCatalogCommit?()
        try checkedCancellation(cancellation)
        var existing = stat()
        if lstat(catalogURL.path, &existing) == 0 { try require((existing.st_mode & S_IFMT) == S_IFREG, "The saved catalogue path is unsafe.") }
        guard rename(temporary.path, catalogURL.path) == 0 else { throw PocketTransferError.invalid("The catalogue could not be saved. Your existing library is unchanged.") }
        if !syncDirectory(root) { durabilityWarning = "Directory synchronization was unavailable; hard power-loss recovery remains unverified." }
    }

    public func saveSettings(_ settings: PocketPlaybackSettings) throws {
        try withStoreLock {
            try reload(); try validateSettings(settings)
            var next = current; next.settings = settings; try persist(next); current = next
        }
    }

    public func updateLoop(trackID: String, start: Double, end: Double?) throws {
        try withStoreLock {
            try reload(); guard let index = current.tracks.firstIndex(where: { $0.id == trackID }) else { throw PocketTransferError.invalid("Choose a saved song first.") }
            var next = current
            next.tracks[index].loopStart = end == nil ? 0 : start
            next.tracks[index].loopEnd = end
            next.tracks[index].kind = end == nil ? "original" : "loop"
            next.tracks[index].duration = end.map { $0 - start } ?? next.tracks[index].fileDuration
            try persist(next); current = next
        }
    }

    @discardableResult public func createPlaylist(name: String, trackIds: [String]) throws -> PocketPlaylist {
        let playlist = PocketPlaylist(id: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(), name: name, trackIds: trackIds)
        try updatePlaylist(playlist); return playlist
    }

    public func updatePlaylist(_ playlist: PocketPlaylist) throws {
        try withStoreLock {
            try reload(); var next = current
            if let index = next.playlists.firstIndex(where: { $0.id == playlist.id }) { next.playlists[index] = playlist }
            else { next.playlists.append(playlist) }
            try persist(next); current = next
        }
    }

    public func exportBundle(to url: URL, cancellation: () -> Bool = { false }) throws {
        try withStoreLock {
            try reload(); try checkedCancellation(cancellation)
            let target = try exportDestination(url)
            try require(!FileManager.default.fileExists(atPath: target.path), "Choose a new export filename. Existing files are never overwritten.")
            let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
            let bytes = try encoder.encode(current.manifest)
            try require(bytes.count <= PocketLimits.manifestBytes, "This library catalogue is too large to export.")
            let required = try validateManifest(current.manifest) + Int64(bytes.count) + 12 + PocketLimits.safetyBytes
            let parent = target.deletingLastPathComponent()
            let available = try availableStorageBytes(at: parent, override: availableBytes)
            if available < required { throw PocketTransferError.insufficientSpace(required: required, available: available) }
            let temporary = parent.appendingPathComponent(".pocket-export-" + UUID().uuidString)
            let handle = try createPrivateFile(temporary)
            let temporaryOwner = try identity(handle)
            defer { try? handle.close(); unlinkOwnedFile(temporary, owner: temporaryOwner) }
            var length = UInt32(bytes.count).bigEndian
            try handle.write(contentsOf: PocketLimits.header)
            try withUnsafeBytes(of: &length) { try handle.write(contentsOf: Data($0)) }
            try handle.write(contentsOf: bytes)
            for file in current.files {
                try checkedCancellation(cancellation)
                let source = try regularFile(mediaURL(file)); defer { try? source.close() }
                let before = try identity(source); var hash = SHA256(); var total: Int64 = 0
                try require(before.bytes == file.byteCount, "A saved song is incomplete. No export was published.")
                while let chunk = try source.read(upToCount: PocketLimits.chunkBytes), !chunk.isEmpty {
                    try checkedCancellation(cancellation); total += Int64(chunk.count)
                    try require(total <= file.byteCount, "A song changed during export.")
                    hash.update(data: chunk); try handle.write(contentsOf: chunk)
                }
                try require(total == file.byteCount && hash.finalize().map { String(format: "%02x", $0) }.joined() == file.sha256 && (try identity(source)) == before, "A saved song failed its fingerprint check. No export was published.")
            }
            try checkedCancellation(cancellation); try handle.synchronize()
            let completed = try identity(handle); try handle.close()
            try beforeExportPublish?()
            try checkedCancellation(cancellation)
            let ready = try regularFile(temporary); defer { try? ready.close() }
            try require(try identity(ready) == completed, "The staged backup changed. No export was published.")
            try FileManager.default.linkItem(at: temporary, to: target)
            if !syncDirectory(parent) { durabilityWarning = "Export directory synchronization was unavailable; hard power-loss recovery remains unverified." }
        }
    }

    private func exportDestination(_ url: URL) throws -> URL {
        try require(url.isFileURL, "Choose a local export filename.")
        let target = url.standardizedFileURL
        let parent = target.deletingLastPathComponent().resolvingSymlinksInPath()
        var rootInfo = stat()
        try require(stat(root.path, &rootInfo) == 0, "The local library folder is unavailable.")
        // Compare directory identities, so symlink aliases and case-insensitive
        // spellings cannot bypass the managed-library destination restriction.
        var ancestor = parent
        while true {
            var info = stat()
            try require(stat(ancestor.path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR, "Choose an existing export folder.")
            try require(info.st_dev != rootInfo.st_dev || info.st_ino != rootInfo.st_ino, "Save the backup outside the managed library folder.")
            if ancestor.path == "/" { break }
            let next = ancestor.deletingLastPathComponent().standardizedFileURL
            if next.path == ancestor.path { break }
            ancestor = next
        }
        return parent.appendingPathComponent(target.lastPathComponent)
    }
}

private struct LibrarySelection: Codable {
    let format: String
    let version: Int
    let folder: String
}

/// App-local selection of a default library or a completed fresh-root recovery.
/// The selector contains a bounded sibling folder name, never an arbitrary path.
/// Constructing or inspecting this helper does not create or move any library.
public final class PocketLibraryLocation {
    private let parent: URL
    private let selector: URL
    private let lockURL: URL
    private let lock = NSRecursiveLock()
    private let beforeSelectionCommit: (() throws -> Void)?
    public let defaultRoot: URL
    public private(set) var preservedSelectorURL: URL?
    public private(set) var durabilityWarning: String?

    public init(parent: URL, beforeSelectionCommit: (() throws -> Void)? = nil) throws {
        try require(parent.isFileURL, "The library location must be local.")
        let canonical = parent.standardizedFileURL.resolvingSymlinksInPath()
        var info = stat()
        try require(lstat(canonical.path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR, "The library parent folder must already exist.")
        self.parent = canonical
        defaultRoot = canonical.appendingPathComponent("PocketStillroom", isDirectory: true)
        selector = canonical.appendingPathComponent("PocketStillroom-location.json")
        lockURL = canonical.appendingPathComponent(".PocketStillroom-location.lock")
        self.beforeSelectionCommit = beforeSelectionCommit
    }

    private func validRecoveryFolder(_ folder: String) -> Bool {
        let prefix = "PocketStillroom-Recovered-"
        guard folder.hasPrefix(prefix) else { return false }
        let suffix = String(folder.dropFirst(prefix.count))
        return UUID(uuidString: suffix)?.uuidString == suffix
    }

    private func requireDirectory(_ url: URL, mayBeMissing: Bool = false) throws {
        var info = stat()
        if lstat(url.path, &info) != 0 {
            try require(mayBeMissing && errno == ENOENT, "The selected library folder is missing. Keep the existing data and recover from a known-good backup.")
        } else {
            try require((info.st_mode & S_IFMT) == S_IFDIR, "The selected library folder is unsafe. Links and files cannot be used as libraries.")
        }
    }

    /// Invalid or missing selections are reported; they never silently replace
    /// a damaged selector with an empty library. The original bytes are kept.
    public func selectedRoot() throws -> URL {
        lock.lock(); defer { lock.unlock() }
        var info = stat()
        if lstat(selector.path, &info) != 0 {
            try require(errno == ENOENT, "The saved library selection could not be inspected.")
            try requireDirectory(defaultRoot, mayBeMissing: true)
            return defaultRoot
        }
        let handle = try regularFile(selector); defer { try? handle.close() }
        let before = try identity(handle)
        try require(before.bytes > 0 && before.bytes <= 1024, "The saved library selection is invalid. Keep it and restore from a known-good backup.")
        let value: LibrarySelection
        do { value = try JSONDecoder().decode(LibrarySelection.self, from: handle.readToEnd() ?? Data()) }
        catch { throw PocketTransferError.invalid("The saved library selection could not be read. Keep it and restore from a known-good backup.") }
        try require(try identity(handle) == before, "The saved library selection changed while it was read.")
        try require(value.format == "pocket-stillroom-location" && value.version == 1 && validRecoveryFolder(value.folder), "The saved library selection is unsupported or unsafe.")
        let selected = parent.appendingPathComponent(value.folder, isDirectory: true)
        try requireDirectory(selected)
        return selected
    }

    public func proposedRecoveryRoot() -> URL {
        parent.appendingPathComponent("PocketStillroom-Recovered-" + UUID().uuidString, isDirectory: true)
    }

    private func withSelectionLock<T>(_ work: () throws -> T) throws -> T {
        lock.lock(); defer { lock.unlock() }
        let descriptor = open(lockURL.path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw PocketTransferError.invalid("The library selection lock could not be opened safely.") }
        defer { close(descriptor) }
        var info = stat()
        try require(fstat(descriptor, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG, "The library selection lock is unsafe.")
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { throw PocketTransferError.invalid("Another library recovery is running. Wait for it to finish.") }
        defer { flock(descriptor, LOCK_UN) }
        return try work()
    }

    /// Explicit activation after successful restore. The old library directory
    /// is never changed. An existing regular selector (even damaged JSON) is
    /// preserved under a unique previous filename before atomic replacement.
    public func activateRecoveredLibrary(_ library: PocketLibraryStore, cancellation: () -> Bool = { false }) throws {
        try withSelectionLock {
            let candidate = library.root.standardizedFileURL
            try require(candidate.deletingLastPathComponent().resolvingSymlinksInPath() == parent && validRecoveryFolder(candidate.lastPathComponent), "Recovery can activate only a new sibling library created for this app.")
            try requireDirectory(candidate)
            try library.withPreparedRecovery {
                try checkedCancellation(cancellation)
                var previousInfo = stat()
                let hadPrevious = lstat(selector.path, &previousInfo) == 0
                try require(hadPrevious ? (previousInfo.st_mode & S_IFMT) == S_IFREG : errno == ENOENT, "The library selector is unsafe. Keep it unchanged and ask for recovery help.")
                let previousIdentity = hadPrevious ? FileIdentity(previousInfo) : nil
                let selection = LibrarySelection(format: "pocket-stillroom-location", version: 1, folder: candidate.lastPathComponent)
                let bytes = try JSONEncoder().encode(selection)
                let temporary = parent.appendingPathComponent(".pocket-selection-" + UUID().uuidString)
                let handle = try createPrivateFile(temporary)
                let owner = try identity(handle)
                defer { try? handle.close(); unlinkOwnedFile(temporary, owner: owner) }
                try handle.write(contentsOf: bytes); try handle.synchronize()
                let completed = try identity(handle); try handle.close()
                try beforeSelectionCommit?()
                try checkedCancellation(cancellation)
                try library.validatePreparedRecovery()
                let ready = try regularFile(temporary); defer { try? ready.close() }
                try require(try identity(ready) == completed, "The staged library selection changed. Nothing was switched.")
                try requireDirectory(candidate)
                var latest = stat()
                let exists = lstat(selector.path, &latest) == 0
                if let previousIdentity {
                    try require(exists && (latest.st_mode & S_IFMT) == S_IFREG && FileIdentity(latest) == previousIdentity, "The library selector changed. Nothing was switched; review recovery again.")
                    let preserved = parent.appendingPathComponent("PocketStillroom-location.previous-" + UUID().uuidString + ".json")
                    try checkedCancellation(cancellation)
                    try FileManager.default.linkItem(at: selector, to: preserved)
                    preservedSelectorURL = preserved
                    guard rename(temporary.path, selector.path) == 0 else {
                        throw PocketTransferError.invalid("The recovered library could not be selected. Your previous selection and library are kept.")
                    }
                } else {
                    try require(!exists && errno == ENOENT, "A library selector appeared during recovery. Nothing was switched.")
                    try checkedCancellation(cancellation)
                    try FileManager.default.linkItem(at: temporary, to: selector)
                }
                if !syncDirectory(parent) { durabilityWarning = "Library selection directory synchronization was unavailable; hard power-loss recovery remains unverified." }
            }
        }
    }
}
