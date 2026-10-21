import SwiftUI
import AVFoundation
import UniformTypeIdentifiers
#if canImport(PocketCore)
import PocketCore
import PocketAudio
#endif

private final class ImportCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func cancel() { lock.lock(); value = true; lock.unlock() }
    var cancelled: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

private func validateImportedAudio(_ url: URL, manifest: PocketManifest) throws {
    let file = try AVAudioFile(forReading: url)
    guard file.length > 0, (1...2).contains(Int(file.processingFormat.channelCount)),
          file.processingFormat.sampleRate.isFinite, (8_000...192_000).contains(file.processingFormat.sampleRate) else {
        throw PocketTransferError.invalid("This audio format is not supported by this prototype.")
    }
    let fingerprint = url.deletingPathExtension().lastPathComponent
    let paths = Set(manifest.files.filter { $0.sha256 == fingerprint }.map(\.path))
    let actualDuration = Double(file.length) / file.processingFormat.sampleRate
    for track in manifest.tracks where paths.contains(track.audioFile) {
        guard abs(track.fileDuration - actualDuration) <= 0.25,
              track.loopStart < actualDuration,
              (track.loopEnd ?? actualDuration) <= actualDuration + 0.005 else {
            throw PocketTransferError.invalid("A song's duration or loop boundaries do not match its audio. Export the library again.")
        }
    }
}

@MainActor final class PocketAppModel: ObservableObject {
    @Published var catalog = PocketCatalog()
    @Published var selectedPlaylist = ""
    @Published var preview: PocketImportPreview?
    @Published var busy: String?
    @Published var canCancel = false
    @Published var message: String?
    @Published var backupURL: URL?
    @Published private(set) var receivedLibraries: [PocketReceivedLibrary] = []
    @Published private(set) var receivedLibrariesMessage: String?
    @Published private(set) var refreshingReceivedLibraries = false
    @Published private(set) var recoveryRequired = false
    var recoveryAvailable: Bool { location != nil }
    let audio = PocketAudioPlayer()
    private var store: PocketLibraryStore?
    private var location: PocketLibraryLocation?
    private var recoveryDestination: URL?
    private let backupDirectory: URL?
    private let documentsDirectory: URL?
    private var discoveryGeneration = UUID()
    private var selectedURL: URL?
    private var scopedAccess = false
    private var cancellation: ImportCancellation?
    private var settingsSave: Task<Void, Never>?

    init(libraryRoot: URL? = nil, libraryParent: URL? = nil, backupDirectory: URL? = nil, documentsDirectory: URL? = nil) {
        self.backupDirectory = backupDirectory
        self.documentsDirectory = documentsDirectory ?? (try? FileManager.default.url(for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: false))
        do {
            let root: URL
            if let libraryRoot { root = libraryRoot }
            else {
                let support = try libraryParent ?? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
                let selection = try PocketLibraryLocation(parent: support)
                location = selection
                root = try selection.selectedRoot()
            }
            store = try PocketLibraryStore(root: root)
            reload()
            audio.setPlaybackRate(catalog.settings.playbackRate)
            audio.setPitchFollowsSpeed(catalog.settings.pitchChanges)
            audio.setReverb(catalog.settings.reverb)
        } catch {
            recoveryRequired = true
            message = "The local library could not open. Keep the app installed. You can restore a known-good backup into a separate library; the original will be kept. \(error.localizedDescription)"
        }
    }

    var visibleTracks: [PocketTrack] {
        guard let playlist = catalog.playlists.first(where: { $0.id == selectedPlaylist }) else { return catalog.tracks }
        let byID = Dictionary(uniqueKeysWithValues: catalog.tracks.map { ($0.id, $0) })
        return playlist.trackIds.compactMap { byID[$0] }
    }

    func reload() { if let store { catalog = store.catalog } }

    func play(_ track: PocketTrack) {
        guard busy == nil, let store else { return }
        do {
            let queue = try visibleTracks.map { item in
                PocketAudioTrack(id: item.id, url: try store.audioURL(for: item), title: item.title, artist: item.artist,
                                 loopStart: item.kind == "loop" ? item.loopStart : nil, loopEnd: item.kind == "loop" ? item.loopEnd : nil)
            }
            guard let index = queue.firstIndex(where: { $0.id == track.id }) else { return }
            audio.setQueue(queue, startIndex: index, autoplay: true)
        } catch { message = error.localizedDescription }
    }

    func savePlaybackSettings() {
        settingsSave?.cancel()
        guard busy == nil else { return }
        do {
            try store?.saveSettings(.init(playbackRate: audio.playbackRate, pitchChanges: audio.pitchFollowsSpeed, reverb: audio.reverb))
            reload()
        } catch { message = "Playback changed, but the setting could not be saved: \(error.localizedDescription)" }
    }

    func schedulePlaybackSettingsSave() {
        settingsSave?.cancel()
        settingsSave = Task {
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            guard !Task.isCancelled else { return }
            savePlaybackSettings()
        }
    }

    func refreshReceivedLibraries() async {
        let generation = UUID(); discoveryGeneration = generation
        refreshingReceivedLibraries = true
        defer { if discoveryGeneration == generation { refreshingReceivedLibraries = false } }
        do {
            guard let documentsDirectory else { throw PocketTransferError.invalid("The local transfer folder is unavailable.") }
            let received = try await Task.detached(priority: .utility) {
                try PocketReceivedLibraries.discover(in: documentsDirectory)
            }.value
            guard !Task.isCancelled, discoveryGeneration == generation else { return }
            receivedLibraries = received; receivedLibrariesMessage = nil
        } catch {
            guard !Task.isCancelled, discoveryGeneration == generation else { return }
            receivedLibraries = []
            receivedLibrariesMessage = "Libraries on this iPhone could not be listed. You can still browse Files."
        }
    }

    func beginPreview(_ url: URL) {
        guard busy == nil, store != nil || (recoveryRequired && recoveryAvailable) else { return }
        savePlaybackSettings()
        closePreview()
        let destination = recoveryRequired ? location?.proposedRecoveryRoot() : nil
        recoveryDestination = destination
        let previewStore = store
        selectedURL = url
        scopedAccess = url.startAccessingSecurityScopedResource()
        let flag = ImportCancellation(); cancellation = flag; canCancel = true; busy = "Checking library and file hashes…"
        Task {
            do {
                let checked = try await Task.detached(priority: .userInitiated) {
                    if let destination {
                        return try PocketLibraryStore.previewBackup(url, forNewRoot: destination, cancellation: { flag.cancelled })
                    }
                    guard let previewStore else { throw PocketTransferError.invalid("The library is unavailable. See Help before changing its files.") }
                    return try previewStore.previewBundle(url, cancellation: { flag.cancelled })
                }.value
                guard !flag.cancelled else { busy = nil; canCancel = false; closePreview(); return }
                preview = checked
            } catch { message = error.localizedDescription; closePreview() }
            busy = nil; canCancel = false
        }
    }

    func importPreview() {
        guard busy == nil, let selectedURL, let reviewed = preview?.manifest else { return }
        if recoveryRequired {
            restoreReviewedBackup(selectedURL, manifest: reviewed)
            return
        }
        guard let store else { return }
        let flag = ImportCancellation(); cancellation = flag; canCancel = true; busy = "Copying verified music into offline storage…"
        Task {
            do {
                let result = try await Task.detached(priority: .userInitiated) {
                    try store.importBundle(selectedURL, expectedManifest: reviewed, cancellation: { flag.cancelled }, validateAudio: { url in
                        try validateImportedAudio(url, manifest: reviewed)
                    })
                }.value
                reload()
                audio.setPlaybackRate(catalog.settings.playbackRate)
                audio.setPitchFollowsSpeed(catalog.settings.pitchChanges)
                audio.setReverb(catalog.settings.reverb)
                message = "Added \(result.addedTracks) songs and \(result.addedPlaylists) playlists. \(result.skippedTracks) existing songs were kept. Music is stored offline."
            } catch { message = "Import stopped. Your working library was kept. \(error.localizedDescription)" }
            busy = nil; canCancel = false; closePreview()
        }
    }

    private func restoreReviewedBackup(_ url: URL, manifest: PocketManifest) {
        guard let location, let destination = recoveryDestination else { return }
        let flag = ImportCancellation(); cancellation = flag
        canCancel = true; busy = "Restoring a separate, verified library…"
        Task {
            do {
                let recovered = try await Task.detached(priority: .userInitiated) {
                    let library = try PocketLibraryStore.restoreBundle(url, toNewRoot: destination, expectedManifest: manifest,
                        cancellation: { flag.cancelled }, validateAudio: { try validateImportedAudio($0, manifest: manifest) })
                    try location.activateRecoveredLibrary(library, cancellation: { flag.cancelled })
                    return library
                }.value
                store = recovered; recoveryRequired = false; selectedPlaylist = ""
                reload()
                audio.setPlaybackRate(catalog.settings.playbackRate)
                audio.setPitchFollowsSpeed(catalog.settings.pitchChanges)
                audio.setReverb(catalog.settings.reverb)
                message = "Recovered \(catalog.tracks.count) songs and \(catalog.playlists.count) playlists into a separate library. The original library was kept. Prepare and share an external backup before any reinstall."
            } catch {
                message = "Recovery did not switch libraries. The original and any separate recovery copy were kept. \(error.localizedDescription)"
            }
            busy = nil; canCancel = false; closePreview()
        }
    }

    func cancelWork() { cancellation?.cancel() }
    func closePreview() {
        if scopedAccess, let selectedURL { selectedURL.stopAccessingSecurityScopedResource() }
        selectedURL = nil; scopedAccess = false; preview = nil; recoveryDestination = nil
    }

    func exportBackup() {
        guard busy == nil, let store else { return }
        savePlaybackSettings()
        let flag = ImportCancellation(); cancellation = flag
        backupURL = nil; canCancel = true; busy = "Preparing a library backup…"
        Task {
            do {
                let exports: URL
                if let backupDirectory { exports = backupDirectory }
                else {
                    let documents = try FileManager.default.url(for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
                    exports = documents.appendingPathComponent("Backups", isDirectory: true)
                }
                try FileManager.default.createDirectory(at: exports, withIntermediateDirectories: true)
                let url = exports.appendingPathComponent("Stillroom-\(UUID().uuidString).stillroomlibrary")
                try await Task.detached(priority: .userInitiated) { try store.exportBundle(to: url, cancellation: { flag.cancelled }) }.value
                backupURL = url
                message = "Backup is ready. Use Share backup to save a copy outside this app before updating, refreshing, or reinstalling."
            } catch { message = "Backup was not completed: \(error.localizedDescription)" }
            busy = nil; canCancel = false
        }
    }

    @discardableResult func saveLoop(start: Double, end: Double) -> Bool {
        guard busy == nil, let store, let id = audio.currentTrack?.id else { return false }
        do {
            // The transport reports position within a prepared loop; stored
            // boundaries always index the original file, including its guards.
            let base = catalog.tracks.first(where: { $0.id == id && $0.kind == "loop" })?.loopStart ?? 0
            try store.updateLoop(trackID: id, start: base + start, end: base + end)
            reload()
            // Update the active queue directly; the collection being browsed
            // may differ. Native loop updates preserve the current play intent.
            audio.setLoopRange(start: base + start, end: base + end)
            audio.setRepeatMode(.one)
            message = "Loop saved in your local library."
            return true
        } catch { message = error.localizedDescription; return false }
    }
}

@main struct PocketStillroomApp: App {
    @StateObject private var model = PocketAppModel()
    var body: some Scene {
        WindowGroup {
            PocketLibraryView(model: model, audio: model.audio)
                .preferredColorScheme(.dark)
                .onOpenURL { model.beginPreview($0) }
        }
    }
}
