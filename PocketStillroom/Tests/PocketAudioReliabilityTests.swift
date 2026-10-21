import XCTest
import Foundation
import AVFoundation
import CryptoKit
#if canImport(PocketStillroom)
@testable import PocketStillroom
#elseif canImport(PocketCore)
import PocketCore
import PocketAudio
#endif

/// Generated zero-amplitude fixtures only. These checks exercise native
/// transport state; the separate nonzero offline DSP tests establish sound.
/// Owned PCM assertions exclude the OS decoder, Audio Units and process RSS.
final class PocketAudioReliabilityTests: XCTestCase {
    private struct ReliabilityTimeout: Error, CustomStringConvertible { let description: String }
    private static var now: UInt64 { DispatchTime.now().uptimeNanoseconds }
    private static func deadline(_ seconds: Double) -> UInt64 { now + UInt64(seconds * 1_000_000_000) }
    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketAudioReliability-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        return root
    }
    private func appendLittle<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
        var value = value.littleEndian; withUnsafeBytes(of: &value) { data.append(contentsOf: $0) }
    }
    private func waveHeader(frames: Int) -> Data {
        var data = Data("RIFF".utf8)
        appendLittle(UInt32(36 + frames * 4), to: &data); data.append(Data("WAVEfmt ".utf8))
        appendLittle(UInt32(16), to: &data); appendLittle(UInt16(1), to: &data); appendLittle(UInt16(2), to: &data)
        appendLittle(UInt32(48_000), to: &data); appendLittle(UInt32(192_000), to: &data)
        appendLittle(UInt16(4), to: &data); appendLittle(UInt16(16), to: &data)
        data.append(Data("data".utf8)); appendLittle(UInt32(frames * 4), to: &data)
        return data
    }
    private func writeSilence(_ url: URL, seconds: Int, sparse: Bool = false) throws {
        let frames = seconds * 48_000
        var data = waveHeader(frames: frames)
        if !sparse { data.append(Data(repeating: 0, count: frames * 4)) }
        try data.write(to: url)
        if sparse {
            let file = try FileHandle(forWritingTo: url)
            defer { try? file.close() }
            try file.truncate(atOffset: UInt64(44 + frames * 4))
        }
    }
    private func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    @MainActor private func wait(_ message: String, until end: UInt64, condition: () -> Bool) async throws {
        let phaseEnd = min(end, Self.deadline(3))
        while !condition(), Self.now < phaseEnd { try await Task.sleep(nanoseconds: 15_000_000) }
        guard condition(), Self.now < end else {
            XCTFail(message)
            throw ReliabilityTimeout(description: message)
        }
    }
    @MainActor private func effects(_ player: PocketAudioPlayer, amount: Double, until end: UInt64) async throws -> PocketAudioEffectSnapshot {
        let phaseEnd = min(end, Self.deadline(3))
        repeat {
            let result = await player.effectSnapshot()
            if result.hasGraph && result.reverb == amount && result.wetDryMix == Float(amount) * PocketAudioGraph.maximumWetPercent && result.bypassed == (amount == 0) { return result }
            try await Task.sleep(nanoseconds: 20_000_000)
        } while Self.now < phaseEnd
        XCTFail("Applied effect failed to settle at \(amount)")
        throw ReliabilityTimeout(description: "Applied effect did not settle")
    }
#if os(iOS)
    private struct SessionState: Sendable { let category: String; let mode: String; let options: UInt }
#endif
    @MainActor private func registerCleanup(_ player: PocketAudioPlayer, root: URL) async throws {
#if os(iOS)
        let prior = await Task.detached(priority: .userInitiated) {
            let session = AVAudioSession.sharedInstance()
            return SessionState(category: session.category.rawValue, mode: session.mode.rawValue, options: session.categoryOptions.rawValue)
        }.value
#endif
        addTeardownBlock {
            await Self.release(player)
            try? FileManager.default.removeItem(at: root)
#if os(iOS)
            await Task.detached(priority: .userInitiated) {
                let session = AVAudioSession.sharedInstance()
                try? session.setActive(false, options: .notifyOthersOnDeactivation)
                try? session.setCategory(.init(rawValue: prior.category), mode: .init(rawValue: prior.mode), options: .init(rawValue: prior.options))
            }.value
#endif
        }
#if os(iOS)
        try await Task.detached(priority: .userInitiated) {
            let session = AVAudioSession.sharedInstance()
            try session.setActive(false, options: .notifyOthersOnDeactivation)
            try session.setCategory(.ambient, mode: .default, options: [])
        }.value
#endif
    }
    @MainActor private static func release(_ player: PocketAudioPlayer) async {
        player.pause(); player.setQueue([])
        // Serial-queue barrier: the reader/buffers have been released before
        // deleting this test's generated files or restoring its session.
        _ = await player.effectSnapshot()
    }

    @MainActor func testRepeatedSilentPlaybackControlsRemainConsistentWithinSeventyFiveSeconds() async throws {
        executionTimeAllowance = 90
        let started = Self.now, end = Self.deadline(75)
        let root = try temporaryRoot(), player = PocketAudioPlayer()
        try await registerCleanup(player, root: root)
        let normal = root.appendingPathComponent("generated-eight-second-zero.wav")
        let long = root.appendingPathComponent("generated-ten-minute-sparse-zero.wav")
        try writeSilence(normal, seconds: 8); try writeSilence(long, seconds: 600, sparse: true)
        let normalHash = hash(try Data(contentsOf: normal))
        let tracks = [PocketAudioTrack(id: "reliability-original", url: normal, title: "Generated silence"),
                      PocketAudioTrack(id: "reliability-short-loop", url: normal, title: "Generated short loop", loopStart: 0.25, loopEnd: 0.85),
                      PocketAudioTrack(id: "reliability-long-loop", url: long, title: "Generated long loop", loopStart: 0, loopEnd: 600)]
        let durations = [8.0, 0.6, 600.0], rates = [0.5, 0.873, 1, 1.37, 1.75, 2.0], wet = [0.0, 0.38, 1.0]
        let sampleDelays: [UInt64] = [230, 270, 250, 310, 240, 270, 260, 250].map { $0 * 1_000_000 }
        player.setRepeatMode(.one)
        for cycle in 0..<18 {
            guard Self.now < end else { throw ReliabilityTimeout(description: "Reliability budget exceeded before cycle \(cycle)") }
            let index = (cycle + cycle / rates.count) % tracks.count
            let duration = durations[index], rate = rates[cycle % rates.count], follows = (cycle / rates.count).isMultiple(of: 2), amount = wet[cycle % wet.count]
            player.setQueue(tracks, startIndex: index, autoplay: true)
            try await wait("Cycle \(cycle): selected native track did not begin", until: end) { player.isPlaying && !player.isLoading && abs(player.duration - duration) < 0.0001 }
            player.setPlaybackRate(rate); player.setPitchFollowsSpeed(follows); player.setReverb(amount)
            _ = try await effects(player, amount: amount, until: end)
            // Superseded internal stops must not become a new pause intent.
            for step in 0..<6 {
                player.seek(to: min(duration * 0.2, 1.5) + Double(step) * min(0.01, duration * 0.01))
                player.setRepeatMode(step.isMultiple(of: 2) ? .all : .one)
            }
            player.setRepeatMode(.one)
            let target = index == 2 ? duration - 0.03 : min(duration * 0.2, 1.5)
            player.seek(to: target)
            _ = await player.effectSnapshot() // The final seek reached the serial queue before sampling.
            try await wait("Cycle \(cycle): latest seek lost playback", until: end) { player.isPlaying && !player.isLoading }
            XCTAssertEqual(player.currentTrack?.id, tracks[index].id); XCTAssertEqual(player.queueIndex, index)
            XCTAssertEqual(player.queue.map(\.id), tracks.map(\.id)); XCTAssertEqual(player.ownedPCMBytes, 524_288)
            XCTAssertEqual(player.playbackRate, rate); XCTAssertEqual(player.pitchFollowsSpeed, follows)
            XCTAssertNil(player.errorMessage)
            var positions: [Double] = [], sampleTimes: [UInt64] = []
            for delay in sampleDelays {
                try await Task.sleep(nanoseconds: delay)
                guard Self.now < end else { throw ReliabilityTimeout(description: "Reliability budget exceeded while sampling") }
                XCTAssertTrue(player.isPlaying); XCTAssertFalse(player.isLoading)
                XCTAssertTrue(player.position.isFinite)
                XCTAssertGreaterThanOrEqual(player.position, 0); XCTAssertLessThanOrEqual(player.position, duration + 0.0001)
                XCTAssertEqual(player.ownedPCMBytes, 524_288); XCTAssertNil(player.errorMessage)
                positions.append(player.position); sampleTimes.append(Self.now)
            }
            // A short A/B position wraps; variation, rather than monotonicity,
            // detects a stalled render/transport clock without penalizing wraps.
            XCTAssertGreaterThan((positions.max() ?? 0) - (positions.min() ?? 0), 0.02, "Cycle \(cycle): playback clock stalled")
            if index == 2 { XCTAssertTrue(positions.contains { $0 < 6 }, "Long loop did not cross its native EOF into the start") }
            if index != 1, let first = positions.first, let last = positions.last, let firstTime = sampleTimes.first, let lastTime = sampleTimes.last {
                let delta = last >= first ? last - first : duration - first + last
                let observedRate = delta / (Double(lastTime - firstTime) / 1_000_000_000)
                XCTAssertEqual(observedRate, rate, accuracy: max(0.35, rate * 0.3), "Cycle \(cycle): source clock should follow live speed")
            }
            player.pause(); player.seek(to: 0.1)
            try await wait("Cycle \(cycle): explicit pause/seek did not settle", until: end) { !player.isPlaying && !player.isLoading && abs(player.position - 0.1) < 0.0001 }
            try await Task.sleep(nanoseconds: 100_000_000)
            XCTAssertFalse(player.isPlaying); XCTAssertEqual(player.position, 0.1, accuracy: 0.0001)
            player.play(); player.seek(to: 0.15)
            _ = await player.effectSnapshot()
            try await wait("Cycle \(cycle): Play followed by Seek did not resume", until: end) { player.isPlaying && !player.isLoading }
            let retained = try await effects(player, amount: amount, until: end)
            XCTAssertEqual(retained.wetDryMix, Float(amount) * 35)
            if cycle.isMultiple(of: 6) {
                // Reset to index0 to check both actual queue commands, rather
                // than treating a new setQueue as the entire track test.
                player.setQueue(tracks, startIndex: 0, autoplay: true)
                try await wait("Queue reset failed before next/previous", until: end) { player.isPlaying && !player.isLoading && abs(player.duration - 8) < 0.0001 }
                player.next()
                try await wait("Next failed during repeated playback", until: end) { player.currentTrack?.id == tracks[1].id && player.isPlaying && !player.isLoading && abs(player.duration - 0.6) < 0.0001 }
                player.previous()
                try await wait("Previous failed during repeated playback", until: end) { player.currentTrack?.id == tracks[0].id && player.isPlaying && !player.isLoading && abs(player.duration - 8) < 0.0001 }
                player.setLoopRange(start: 1.0, end: 1.7)
                try await wait("Setting A/B did not retain playback", until: end) { player.isPlaying && !player.isLoading && abs(player.duration - 0.7) < 0.0001 }
                player.clearLoopRange()
                try await wait("Clearing A/B did not retain playback", until: end) { player.isPlaying && !player.isLoading && abs(player.duration - 8) < 0.0001 }
            }
            print("Silent reliability cycle \(cycle + 1)/18: track=\(index), rate=\(rate), pitchFollows=\(follows), reverb=\(amount), ownedPCM=\(player.ownedPCMBytes)")
        }
        player.pause(); player.setQueue([])
        try await wait("Final queue clear did not release owned buffers", until: end) { !player.isPlaying && player.ownedPCMBytes == 0 && player.currentTrack == nil }
        XCTAssertEqual(hash(try Data(contentsOf: normal)), normalHash, "Transport must not rewrite generated source bytes")
        XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: long.path)[.size] as? UInt64, UInt64(44 + 600 * 48_000 * 4))
        print("Completed 18 silent native reliability cycles in \(Double(Self.now - started) / 1_000_000_000) seconds")
    }

    func testTenMinuteLoopReusesThreeBuffersAcrossRepeatedBoundaryDecodes() throws {
        executionTimeAllowance = 90
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("generated-long-sparse-zero.wav")
        try writeSilence(source, seconds: 600, sparse: true)
        let reader = try BoundedAudioReader(url: source, loopStart: 0.125, loopEnd: 599.875)
        let pool = try (0..<3).map { _ in try reader.makeBuffer() }
        let identities = pool.map(ObjectIdentifier.init)
        XCTAssertEqual(reader.ownedScratchBytes * (pool.count + 1), 524_288)
        let first = reader.firstFrame, end = reader.endFrame, length = reader.frameCount
        var expected = end - 240
        reader.seek(to: Double(expected - first) / 48_000)
        for turn in 0..<2_048 {
            let buffer = pool[turn % pool.count]
            XCTAssertEqual(ObjectIdentifier(buffer), identities[turn % pool.count])
            XCTAssertEqual(try reader.read(into: buffer, looping: true), BoundedAudioReader.frameCapacity)
            expected = first + (expected - first + Int64(BoundedAudioReader.frameCapacity)) % length
            XCTAssertEqual(reader.cursor, expected)
            XCTAssertEqual(buffer.frameCapacity, BoundedAudioReader.frameCapacity)
            // Verify both edges and the precise first crossing, without storing
            // a reconstructed ten-minute PCM array in the test or player.
            for channel in 0..<2 {
                for offset in [0, 239, 240, 16_383] { XCTAssertEqual(buffer.floatChannelData![channel][offset], 0) }
            }
        }
        XCTAssertEqual(reader.ownedScratchBytes * 4, 524_288)
        XCTAssertEqual(reader.duration, 599.75, accuracy: 0.0001)
    }

    @MainActor func testReopenedLibraryRestoresControlsSavedLoopAndQueueIntoFreshPlayer() async throws {
        executionTimeAllowance = 90
        let end = Self.deadline(12), root = try temporaryRoot(), player = PocketAudioPlayer()
        try await registerCleanup(player, root: root)
        let source = root.appendingPathComponent("generated-persisted-zero.wav")
        try writeSilence(source, seconds: 8)
        let payload = try Data(contentsOf: source), digest = hash(payload)
        let one = String(repeating: "1", count: 32), two = String(repeating: "2", count: 32), file = "audio/" + String(repeating: "a", count: 32) + ".wav"
        let playlist = PocketPlaylist(id: String(repeating: "3", count: 32), name: "Generated reopen order", trackIds: [two, one])
        let manifest = PocketManifest(tracks: [
            .init(id: one, title: "Generated original", duration: 8, audioFile: file),
            .init(id: two, title: "Generated saved loop", kind: "loop", duration: 0.75, fileDuration: 8, loopStart: 1.25, loopEnd: 2, audioFile: file)
        ], playlists: [playlist], files: [.init(path: file, byteCount: Int64(payload.count), sha256: digest)])
        let encoded = try JSONEncoder().encode(manifest)
        var count = UInt32(encoded.count).bigEndian, transfer = Data("STLRMB01".utf8)
        withUnsafeBytes(of: &count) { transfer.append(contentsOf: $0) }; transfer.append(encoded); transfer.append(payload)
        let bundle = root.appendingPathComponent("generated-reopen.stillroomlibrary"); try transfer.write(to: bundle)
        let storeRoot = root.appendingPathComponent("DisposableLibrary")
        let store = try PocketLibraryStore(root: storeRoot)
        _ = try store.importBundle(bundle, validateAudio: { url in
            let audio = try AVAudioFile(forReading: url)
            guard audio.length == 384_000, audio.processingFormat.channelCount == 2 else { throw PocketAudioError.invalidFile }
        })
        let saved = PocketPlaybackSettings(playbackRate: 0.873, pitchChanges: false, reverb: 0.38)
        try store.saveSettings(saved); try store.updateLoop(trackID: two, start: 2.0, end: 3.25)
        let reopened = try PocketLibraryStore(root: storeRoot), catalog = reopened.catalog
        XCTAssertEqual(catalog.settings, saved); XCTAssertEqual(catalog.playlists, [playlist])
        let byID = Dictionary(uniqueKeysWithValues: catalog.tracks.map { ($0.id, $0) })
        XCTAssertEqual(byID[two]?.loopStart, 2); XCTAssertEqual(byID[two]?.loopEnd, 3.25); XCTAssertEqual(byID[two]?.duration, 1.25)
        let queue = try playlist.trackIds.map { id -> PocketAudioTrack in
            let track = try XCTUnwrap(byID[id]), url = try reopened.audioURL(for: track)
            XCTAssertEqual(hash(try Data(contentsOf: url)), digest)
            return .init(id: id, url: url, title: track.title, loopStart: track.kind == "loop" ? track.loopStart : nil, loopEnd: track.kind == "loop" ? track.loopEnd : nil)
        }
        // A newly created player is deliberately restored through the public
        // settings/queue API, matching app startup; process-kill recovery is not
        // claimed by constructing a new store/player in this test.
        player.setPlaybackRate(saved.playbackRate); player.setPitchFollowsSpeed(saved.pitchChanges); player.setReverb(saved.reverb)
        player.setRepeatMode(.one); player.setQueue(queue, autoplay: false)
        try await wait("Fresh player did not load the persisted saved loop paused", until: end) { !player.isLoading && !player.isPlaying && abs(player.duration - 1.25) < 0.0001 }
        _ = try await effects(player, amount: saved.reverb, until: end)
        XCTAssertEqual(player.queue.map(\.id), [two, one]); XCTAssertEqual(player.playbackRate, saved.playbackRate)
        XCTAssertFalse(player.pitchFollowsSpeed); XCTAssertEqual(player.ownedPCMBytes, 524_288)
        player.play(); player.seek(to: 0.1)
        try await wait("Restored settings/loop could not resume silently", until: end) { player.isPlaying && !player.isLoading }
        XCTAssertNil(player.errorMessage)
        XCTAssertEqual(reopened.catalog.settings, saved)
    }
}
