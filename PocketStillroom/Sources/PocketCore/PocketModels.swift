import Foundation

public enum PocketTransferError: Error, LocalizedError {
    case invalid(String)
    case conflict(String)
    case cancelled
    case insufficientSpace(required: Int64, available: Int64)
    case unsupportedVersion(Int)
    public var errorDescription: String? {
        switch self {
        case .invalid(let message): return message
        case .conflict(let message): return message
        case .cancelled: return "Import cancelled. Your existing library is unchanged."
        case .insufficientSpace(let required, let available):
            return "This import needs \(required) bytes of free space; \(available) bytes are available. Free space and try again."
        case .unsupportedVersion(let version): return "This library uses format \(version). Update Pocket Stillroom before importing it."
        }
    }
}

public struct PocketPlaybackSettings: Codable, Equatable, Sendable {
    public var playbackRate: Double
    public var pitchChanges: Bool
    public var reverb: Double
    public init(playbackRate: Double = 1, pitchChanges: Bool = true, reverb: Double = 0) {
        self.playbackRate = playbackRate; self.pitchChanges = pitchChanges; self.reverb = reverb
    }
}

public struct PocketTrack: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var artist: String
    public var album: String
    public var kind: String
    public var duration: Double
    public var fileDuration: Double
    public var loopStart: Double
    public var loopEnd: Double?
    public var tags: [String]
    public var sourceUrl: String?
    public var addedAt: String?
    public var audioFile: String
    public init(id: String, title: String, artist: String = "", album: String = "", kind: String = "original", duration: Double, fileDuration: Double? = nil, loopStart: Double = 0, loopEnd: Double? = nil, tags: [String] = [], sourceUrl: String? = nil, addedAt: String? = nil, audioFile: String) {
        self.id = id; self.title = title; self.artist = artist; self.album = album; self.kind = kind
        self.duration = duration; self.fileDuration = fileDuration ?? duration
        self.loopStart = loopStart; self.loopEnd = loopEnd; self.tags = tags
        self.sourceUrl = sourceUrl; self.addedAt = addedAt; self.audioFile = audioFile
    }
}

public struct PocketPlaylist: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var trackIds: [String]
    public init(id: String, name: String, trackIds: [String]) { self.id = id; self.name = name; self.trackIds = trackIds }
}

public struct PocketAudioFile: Codable, Equatable, Sendable {
    public var path: String
    public var byteCount: Int64
    public var sha256: String
    public init(path: String, byteCount: Int64, sha256: String) { self.path = path; self.byteCount = byteCount; self.sha256 = sha256 }
}

public struct PocketManifest: Codable, Equatable, Sendable {
    public var format: String
    public var version: Int
    public var tracks: [PocketTrack]
    public var playlists: [PocketPlaylist]
    public var files: [PocketAudioFile]
    public var settings: PocketPlaybackSettings?
    public init(tracks: [PocketTrack], playlists: [PocketPlaylist], files: [PocketAudioFile], settings: PocketPlaybackSettings? = nil) {
        self.format = "stillroom-library"; self.version = 1; self.tracks = tracks; self.playlists = playlists; self.files = files; self.settings = settings
    }
}

public struct PocketCatalog: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    public var tracks: [PocketTrack]
    public var playlists: [PocketPlaylist]
    public var files: [PocketAudioFile]
    public var settings: PocketPlaybackSettings
    public init(tracks: [PocketTrack] = [], playlists: [PocketPlaylist] = [], files: [PocketAudioFile] = [], settings: PocketPlaybackSettings = .init()) {
        schemaVersion = 1; self.tracks = tracks; self.playlists = playlists; self.files = files; self.settings = settings
    }
    public var manifest: PocketManifest { .init(tracks: tracks, playlists: playlists, files: files, settings: settings) }
}

public struct PocketImportPreview: Sendable {
    public let manifest: PocketManifest
    public let tracksToAdd: Int
    public let playlistsToAdd: Int
    public let duplicateTracks: Int
    public let requiredBytes: Int64
    public let audioBytes: Int64
}

public struct PocketImportResult: Sendable {
    public let addedTracks: Int
    public let addedPlaylists: Int
    public let skippedTracks: Int
    public let totalTracks: Int
}

enum PocketLimits {
    static let header = Data("STLRMB01".utf8)
    static let manifestBytes = 8 * 1024 * 1024
    static let fileBytes: Int64 = 512 * 1024 * 1024
    static let totalBytes: Int64 = 32 * 1024 * 1024 * 1024
    static let objectCount = 10_000
    static let chunkBytes = 256 * 1024
    static let safetyBytes: Int64 = 8 * 1024 * 1024
}

func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !condition() { throw PocketTransferError.invalid(message) }
}

func validID(_ id: String) -> Bool {
    id.utf8.count == 32 && id.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}

func validText(_ text: String, maximum: Int, required: Bool = false) -> Bool {
    text.count <= maximum && (!required || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) && !text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
}

func validateSettings(_ settings: PocketPlaybackSettings) throws {
    try require(settings.playbackRate.isFinite && (0.5...2).contains(settings.playbackRate), "Playback speed must be between 0.5× and 2×.")
    try require(settings.reverb.isFinite && (0...1).contains(settings.reverb), "Reverb must be between zero and one.")
}

func validateManifest(_ manifest: PocketManifest) throws -> Int64 {
    guard manifest.version == 1 else { throw PocketTransferError.unsupportedVersion(manifest.version) }
    try require(manifest.format == "stillroom-library", "Choose a Stillroom library bundle.")
    try require(manifest.tracks.count <= PocketLimits.objectCount && manifest.playlists.count <= PocketLimits.objectCount && manifest.files.count <= PocketLimits.objectCount, "This library contains too many items.")
    let extensions: Set<String> = ["m4a", "aac", "mp3", "wav", "flac", "opus", "ogg", "webm"]
    var paths = Set<String>(); var total: Int64 = 0
    for file in manifest.files {
        let parts = file.path.split(separator: "/", omittingEmptySubsequences: false)
        try require(parts.count == 2 && parts[0] == "audio", "The bundle contains an unsafe audio path.")
        let name = String(parts[1]); let pieces = name.split(separator: ".", omittingEmptySubsequences: false)
        try require(pieces.count == 2 && validID(String(pieces[0])) && extensions.contains(String(pieces[1])), "The bundle contains an unsupported audio filename.")
        try require(paths.insert(file.path).inserted, "The bundle repeats an audio filename.")
        try require(file.byteCount > 0 && file.byteCount <= PocketLimits.fileBytes, "An audio file exceeds the 512 MiB limit or is empty.")
        try require(file.sha256.utf8.count == 64 && file.sha256.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }, "The bundle contains an invalid audio fingerprint.")
        let addition = total.addingReportingOverflow(file.byteCount)
        try require(!addition.overflow && addition.partialValue <= PocketLimits.totalBytes, "This bundle exceeds the 32 GiB transfer limit.")
        total = addition.partialValue
    }
    var ids = Set<String>(); var referenced = Set<String>()
    for track in manifest.tracks {
        try require(validID(track.id) && ids.insert(track.id).inserted, "The bundle contains an invalid or repeated song ID.")
        try require(validText(track.title, maximum: 4096, required: true) && validText(track.artist, maximum: 4096) && validText(track.album, maximum: 4096), "The bundle contains invalid song text.")
        try require(track.kind == "original" || track.kind == "loop", "The bundle contains an unknown song type.")
        try require(track.duration.isFinite && track.duration > 0 && track.duration <= 21600 && track.fileDuration.isFinite && track.fileDuration > 0 && track.fileDuration <= 21600, "The bundle contains an invalid song duration.")
        let end = track.loopEnd ?? track.fileDuration
        try require(track.loopStart.isFinite && end.isFinite && track.loopStart >= 0 && end > track.loopStart && end <= track.fileDuration + 0.000_001, "The bundle contains invalid saved loop boundaries.")
        let expectedDuration = track.kind == "loop" ? end - track.loopStart : track.fileDuration
        try require(abs(expectedDuration - track.duration) <= 0.01, "The saved song duration does not match its loop boundaries.")
        try require(track.tags.count <= 32 && Set(track.tags).count == track.tags.count && track.tags.allSatisfy { validText($0, maximum: 100, required: true) }, "The bundle contains invalid song tags.")
        if let source = track.sourceUrl, !source.isEmpty {
            let prefix = "https://www.youtube.com/watch?v="
            let suffix = source.hasPrefix(prefix) ? String(source.dropFirst(prefix.count)) : ""
            try require(suffix.utf8.count == 11 && suffix.utf8.allSatisfy { (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95 }, "Source links must be canonical YouTube video links without account or tracking parameters.")
        }
        if let added = track.addedAt { try require(validText(added, maximum: 100), "The bundle contains an invalid date.") }
        try require(paths.contains(track.audioFile), "A song refers to missing audio.")
        referenced.insert(track.audioFile)
    }
    try require(referenced == paths, "The bundle contains audio that is not referenced by its songs.")
    var playlistIDs = Set<String>()
    for playlist in manifest.playlists {
        try require(validID(playlist.id) && playlistIDs.insert(playlist.id).inserted, "The bundle contains an invalid or repeated playlist ID.")
        try require(validText(playlist.name, maximum: 4096, required: true) && playlist.trackIds.count <= PocketLimits.objectCount, "The bundle contains invalid playlist details.")
        try require(Set(playlist.trackIds).count == playlist.trackIds.count && playlist.trackIds.allSatisfy { ids.contains($0) }, "A playlist contains a missing or repeated song.")
    }
    if let settings = manifest.settings { try validateSettings(settings) }
    return total
}
