import XCTest
import Foundation
import CryptoKit
import AVFoundation
import SwiftUI
import UIKit
#if canImport(PocketStillroom)
@testable import PocketStillroom
#else
import PocketCore
import PocketAudio
#endif

/// These checks use generated fixtures in the test runner's temporary directory.
/// They never open desktop libraries, start audible track playback or use signing.
final class PocketSimulatorTests: XCTestCase {
    @MainActor func testSpeedMappingKeepsNormalCenteredAndContinuousWithGentleSnaps() {
        XCTAssertEqual(PocketSpeedScale.position(for: 0.5), -1)
        XCTAssertEqual(PocketSpeedScale.position(for: 1), 0)
        XCTAssertEqual(PocketSpeedScale.position(for: 2), 1)
        var previous: Float = -2
        for index in 0...1_500 {
            let rate = 0.5 + Double(index) / 1_000
            let position = PocketSpeedScale.position(for: rate)
            XCTAssertGreaterThan(position, previous)
            XCTAssertEqual(PocketSpeedScale.rate(at: position), rate, accuracy: 0.000001)
            previous = position
        }
        XCTAssertEqual(PocketSpeedScale.rate(at: PocketSpeedScale.position(for: 1.37)), 1.37, accuracy: 0.000001)
        XCTAssertEqual(PocketSpeedScale.snapped(1.024), 1)
        XCTAssertEqual(PocketSpeedScale.snapped(1.026), 1.026)
        XCTAssertEqual(PocketSpeedScale.snapped(1.37), 1.37)
        for rate in PocketSpeedScale.presets { XCTAssertEqual(PocketSpeedScale.snapped(rate + 0.01), rate) }
    }

    @MainActor func testNativeSpeedTicksAndLabelsFollowThumbAcrossWidthsTypeAndRTL() throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        let host = UIViewController()
        window.rootViewController = host; window.isHidden = false
        defer { window.isHidden = true }
        for width in [220.0, 320.0, 393.0, 600.0] {
            for category in [UIContentSizeCategory.large, .accessibilityExtraExtraExtraLarge] {
                for direction in [UITraitEnvironmentLayoutDirection.leftToRight, .rightToLeft] {
                    let view = PocketSpeedSliderView(frame: CGRect(x: 0, y: 0, width: width, height: 150))
                    host.view.addSubview(view)
                    view.traitOverrides.preferredContentSizeCategory = category
                    view.traitOverrides.layoutDirection = direction
                    view.setNeedsLayout(); view.layoutIfNeeded()
                    XCTAssertEqual(view.traitCollection.preferredContentSizeCategory, category)
                    let ordinaryFont = UIFont.preferredFont(forTextStyle: .caption2, compatibleWith: UITraitCollection(preferredContentSizeCategory: .large))
                    if category.isAccessibilityCategory {
                        XCTAssertGreaterThan(view.labels[1].view.font.pointSize, ordinaryFont.pointSize)
                        XCTAssertGreaterThan(view.intrinsicContentSize.height, 54 + ordinaryFont.lineHeight)
                    }
                    let track = view.slider.trackRect(forBounds: view.slider.bounds)
                    var previousX: CGFloat = -1
                    for (rate, tick) in zip(view.tickRates, view.ticks) {
                        view.slider.rate = rate
                        let thumb = view.slider.thumbRect(forBounds: view.slider.bounds, trackRect: track, value: view.slider.value)
                        let center = view.slider.convert(CGPoint(x: thumb.midX, y: thumb.midY), to: view).x
                        XCTAssertEqual(tick.center.x, center, accuracy: 0.001)
                        XCTAssertGreaterThan(center, previousX); previousX = center
                        XCTAssertFalse(tick.isUserInteractionEnabled)
                    }
                    for (rate, label) in view.labels {
                        view.slider.rate = rate
                        let thumb = view.slider.thumbRect(forBounds: view.slider.bounds, trackRect: track, value: view.slider.value)
                        let center = view.slider.convert(CGPoint(x: thumb.midX, y: thumb.midY), to: view).x
                        XCTAssertEqual(label.center.x, center, accuracy: 0.001)
                        XCTAssertGreaterThanOrEqual(label.frame.minX, 0)
                        XCTAssertLessThanOrEqual(label.frame.maxX, view.bounds.width)
                        XCTAssertFalse(label.isAccessibilityElement)
                    }
                    XCTAssertFalse(view.labels[0].view.frame.intersects(view.labels[1].view.frame))
                    XCTAssertFalse(view.labels[1].view.frame.intersects(view.labels[2].view.frame))
                    // UIKit rounds its native thumb to physical pixels.
                    XCTAssertEqual(view.labels[1].view.center.x, view.bounds.midX, accuracy: 1 / max(1, view.traitCollection.displayScale) + 0.000001)
                    XCTAssertGreaterThanOrEqual(view.intrinsicContentSize.height, view.labels[1].view.frame.maxY)
                    view.removeFromSuperview()
                }
            }
        }
    }

    @MainActor func testSpeedAccessibilityUsesRealRatesAndCommitsWithoutTouchEvents() {
        var value = 1.0, commits = 0
        let control = PocketSpeedControl(rate: Binding(get: { value }, set: { value = $0 }), onEditingChanged: { editing in
            if !editing { value = PocketSpeedScale.snapped(value); commits += 1 }
        })
        let coordinator = control.makeCoordinator()
        let slider = PocketRateSlider()
        slider.minimumValue = -1; slider.maximumValue = 1; slider.rate = value
        slider.addTarget(coordinator, action: #selector(PocketSpeedControl.Coordinator.changed(_:)), for: .valueChanged)
        slider.addTarget(coordinator, action: #selector(PocketSpeedControl.Coordinator.ended), for: .editingDidEnd)
        slider.accessibilityIncrement()
        XCTAssertEqual(value, 1.05, accuracy: 0.000001)
        XCTAssertEqual(slider.accessibilityValue, "1.05×")
        XCTAssertEqual(commits, 1)
        slider.accessibilityDecrement()
        XCTAssertEqual(value, 1, accuracy: 0.000001)
        XCTAssertEqual(slider.accessibilityValue, "1×")
        XCTAssertEqual(commits, 2)
        slider.rate = 2; slider.accessibilityIncrement(); XCTAssertEqual(value, 2)
        slider.isEnabled = false
        let before = commits
        slider.accessibilityDecrement()
        XCTAssertEqual(commits, before); XCTAssertEqual(value, 2)
    }

    private func temporaryRoot() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketSimulatorTests-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        return root
    }
    private func appendLittle<T: FixedWidthInteger>(_ number: T, to data: inout Data) {
        var value = number.littleEndian
        withUnsafeBytes(of: &value) { data.append(contentsOf: $0) }
    }
    private func wavHeader(frames: Int, channels: Int = 2) -> Data {
        let count = frames * channels * 2
        var data = Data("RIFF".utf8); appendLittle(UInt32(36 + count), to: &data)
        data.append(Data("WAVEfmt ".utf8)); appendLittle(UInt32(16), to: &data)
        appendLittle(UInt16(1), to: &data); appendLittle(UInt16(channels), to: &data)
        appendLittle(UInt32(48_000), to: &data); appendLittle(UInt32(48_000 * channels * 2), to: &data)
        appendLittle(UInt16(channels * 2), to: &data); appendLittle(UInt16(16), to: &data)
        data.append(Data("data".utf8)); appendLittle(UInt32(count), to: &data)
        return data
    }
    private func tone(seconds: Double = 2, frequency: Double = 440, amplitude: Double = 0.4) -> Data {
        let frames = Int(seconds * 48_000)
        var data = wavHeader(frames: frames)
        for frame in 0..<frames {
            let sample = Int16((sin(2 * .pi * frequency * Double(frame) / 48_000) * amplitude * 32_767).rounded())
            appendLittle(sample, to: &data); appendLittle(sample, to: &data)
        }
        return data
    }
    private func writeTone(in root: URL, name: String = "tone.wav", seconds: Double = 2) throws -> URL {
        let url = root.appendingPathComponent(name)
        try tone(seconds: seconds).write(to: url)
        return url
    }
    @MainActor private func eventually(_ message: String, timeout: TimeInterval = 6, file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(condition(), message, file: file, line: line)
    }

    @MainActor func testContinuousControlsClampInvalidValuesWithoutStartingPlayback() {
        let player = PocketAudioPlayer()
        XCTAssertEqual(player.playbackRate, 1)
        XCTAssertTrue(player.pitchFollowsSpeed)
        XCTAssertEqual(player.reverb, 0)
        XCTAssertFalse(player.isPlaying)
        player.setPlaybackRate(0.873)
        XCTAssertEqual(player.playbackRate, 0.873, accuracy: 0.000001)
        player.setPlaybackRate(.nan)
        XCTAssertEqual(player.playbackRate, 0.873, accuracy: 0.000001)
        player.setPlaybackRate(4); XCTAssertEqual(player.playbackRate, 2)
        player.setPlaybackRate(0.1); XCTAssertEqual(player.playbackRate, 0.5)
        player.setPitchFollowsSpeed(false); XCTAssertFalse(player.pitchFollowsSpeed)
        player.setReverb(0.38); XCTAssertEqual(player.reverb, 0.38)
        player.setReverb(.infinity); XCTAssertEqual(player.reverb, 0.38)
        player.setReverb(3); XCTAssertEqual(player.reverb, 1)
        player.setReverb(-1); XCTAssertEqual(player.reverb, 0)
        player.setVolume(-1); XCTAssertEqual(player.volume, 0)
        player.setVolume(3); XCTAssertEqual(player.volume, 1)
        XCTAssertFalse(player.isPlaying)
    }

    @MainActor func testIOSPlaybackSessionCanConfigureWithoutQueuedAudio() async throws {
#if os(iOS)
        let previous = await Self.sessionConfiguration()
        addTeardownBlock { await Self.restoreSession(previous) }
        try await Self.prepareInactiveSession()
        let player = PocketAudioPlayer()
        // Actual transport entry point activates its iOS session, but an empty
        // queue has no reader and cannot produce an audio signal.
        player.play()
        await eventually("Serial audio queue completes session activation") { !player.isLoading }
        XCTAssertNil(player.errorMessage)
        let configured = await Self.sessionConfiguration()
        XCTAssertEqual(configured.category, AVAudioSession.Category.playback.rawValue)
        XCTAssertEqual(configured.mode, AVAudioSession.Mode.default.rawValue)
        XCTAssertFalse(player.isPlaying)
        player.pause()
        await eventually("Empty player remains stopped") { !player.isPlaying }
#else
        throw XCTSkip("Audio-session behavior requires the iOS simulator/device target")
#endif
    }

#if os(iOS)
    private struct SessionConfiguration: Sendable {
        let category: String
        let mode: String
        let options: UInt
    }
    private static func sessionConfiguration() async -> SessionConfiguration {
        await Task.detached(priority: .userInitiated) {
            let session = AVAudioSession.sharedInstance()
            return SessionConfiguration(category: session.category.rawValue, mode: session.mode.rawValue, options: session.categoryOptions.rawValue)
        }.value
    }
    private static func prepareInactiveSession() async throws {
        try await Task.detached(priority: .userInitiated) {
            let session = AVAudioSession.sharedInstance()
            try session.setActive(false, options: .notifyOthersOnDeactivation)
            try session.setCategory(.ambient, mode: .default, options: [])
        }.value
    }
    private static func restoreSession(_ previous: SessionConfiguration) async {
        await Task.detached(priority: .userInitiated) {
            let session = AVAudioSession.sharedInstance()
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            try? session.setCategory(.init(rawValue: previous.category), mode: .init(rawValue: previous.mode), options: .init(rawValue: previous.options))
        }.value
    }
#endif

    @MainActor func testEmptyPlayPauseQueueHonorsLatestPauseWithoutAudio() async throws {
#if os(iOS)
        let previous = await Self.sessionConfiguration()
        addTeardownBlock { await Self.restoreSession(previous) }
        try await Self.prepareInactiveSession()
#endif
        let player = PocketAudioPlayer()
        for _ in 0..<12 { player.play(); player.pause() }
        player.pause()
        await eventually("Queued activation cannot leave a later pause loading or playing") { !player.isLoading && !player.isPlaying }
        XCTAssertNil(player.currentTrack)
        XCTAssertTrue(player.queue.isEmpty)
        XCTAssertNil(player.errorMessage)
    }

    @MainActor func testActiveSilentPlaybackSurvivesSupersededSeeksAndRepeatChanges() async throws {
#if os(iOS)
        let previous = await Self.sessionConfiguration()
        addTeardownBlock { await Self.restoreSession(previous) }
        try await Self.prepareInactiveSession()
#endif
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let silence = root.appendingPathComponent("generated-zero-amplitude.wav")
        try tone(seconds: 8, amplitude: 0).write(to: silence)
        let player = PocketAudioPlayer()
        defer { player.pause(); player.setQueue([]) }
        let tracks = [PocketAudioTrack(id: "silent-first", url: silence, title: "Generated silence"), PocketAudioTrack(id: "silent-next", url: silence, title: "Generated silence next")]
        player.setQueue(tracks, autoplay: true)
        await eventually("Zero-amplitude fixture actually enters native playing state") { player.isPlaying && !player.isLoading }
        XCTAssertNil(player.errorMessage)
        // Queue requests on MainActor without yielding so earlier stops cannot
        // change the meaning of a later seek/repeat request.
        for index in 0..<24 {
            player.seek(to: 0.25 + Double(index) * 0.03)
            player.setRepeatMode(index.isMultiple(of: 2) ? .one : .all)
        }
        player.setRepeatMode(.one)
        player.seek(to: 1.5)
        await eventually("Latest active seek reaches its requested position") { !player.isLoading && player.position >= 1.5 && player.position < 2.2 }
        XCTAssertTrue(player.isPlaying, "Superseded seeks must preserve intended playback instead of inheriting a temporary engine stop")
        XCTAssertEqual(player.queueIndex, 0)

        player.pause()
        for index in 0..<12 {
            player.seek(to: 0.5 + Double(index) * 0.04)
            player.setRepeatMode(index.isMultiple(of: 2) ? .off : .one)
        }
        player.seek(to: 2)
        await eventually("An explicit pause remains authoritative through later seeks") { !player.isLoading && !player.isPlaying && abs(player.position - 2) < 0.0001 }

        player.play()
        player.seek(to: 3)
        await eventually("A seek queued immediately after Play retains the new play intent") { !player.isLoading && player.isPlaying && player.position >= 3 && player.position < 3.7 }
        player.setQueue(tracks, autoplay: true)
        player.seek(to: 4)
        await eventually("A seek queued during an autoplay load does not suppress the load's play intent") { !player.isLoading && player.isPlaying && player.position >= 4 && player.position < 4.7 }
        XCTAssertNil(player.errorMessage)
        player.setRepeatMode(.off)
        player.seek(to: 8)
        await eventually("Seeking to a non-repeating end stops without rewinding") { !player.isPlaying && abs(player.position - 8) < 0.0001 }
        player.seek(to: 1)
        await eventually("Seeking backward after an explicit end seek remains paused") { !player.isPlaying && abs(player.position - 1) < 0.0001 }
    }

    @MainActor func testPausedQueueSeekSavedLoopAndClearPreserveLatestIntent() async throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let audio = try writeTone(in: root)
        let tracks = [PocketAudioTrack(id: "first", url: audio, title: "First generated sample"), PocketAudioTrack(id: "second", url: audio, title: "Second generated sample")]
        let player = PocketAudioPlayer()
        player.setQueue(tracks)
        await eventually("Paused native track loads") { !player.isLoading && player.duration > 0 }
        XCTAssertNil(player.errorMessage)
        XCTAssertEqual(player.queueIndex, 0)
        XCTAssertEqual(player.duration, 2, accuracy: 0.0001)
        XCTAssertEqual(player.ownedPCMBytes, 524_288)
        XCTAssertFalse(player.isPlaying)
        player.seek(to: 1.25)
        await eventually("Paused seek uses source-time seconds") { abs(player.position - 1.25) < 0.0001 }
        for value in [0.2, 0.6, 1.0, 0.4, 1.8] { player.seek(to: value); player.pause() }
        player.seek(to: 0.75)
        await eventually("Last seek wins after queued seek/pause commands") { abs(player.position - 0.75) < 0.0001 && !player.isPlaying }
        XCTAssertEqual(player.queueIndex, 0)
        player.seek(to: 2)
        await eventually("End seek stays at end instead of rewinding") { abs(player.position - 2) < 0.0001 && !player.isPlaying }
        player.setLoopRange(start: 0.25, end: 1.25)
        await eventually("Saved A/B range loads as its exact duration") { !player.isLoading && abs(player.duration - 1) < 0.0001 }
        XCTAssertEqual(player.currentTrack?.loopStart, 0.25)
        XCTAssertEqual(player.currentTrack?.loopEnd, 1.25)
        player.setRepeatMode(.one)
        player.seek(to: 0.5)
        await eventually("Repeat and range do not unpause playback") { !player.isPlaying && abs(player.position - 0.5) < 0.0001 }
        XCTAssertEqual(player.repeatMode, .one)
        player.clearLoopRange()
        await eventually("Clear A/B restores full recording") { !player.isLoading && abs(player.duration - 2) < 0.0001 }
        player.setQueue([])
        await eventually("Clear queue releases owned PCM") { player.currentTrack == nil && player.ownedPCMBytes == 0 && player.duration == 0 }
        XCTAssertFalse(player.isPlaying)
    }

    @MainActor func testInvalidLoopAndMissingFileFailThenRecoverOnGoodSelection() async throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let audio = try writeTone(in: root)
        let player = PocketAudioPlayer()
        player.load(.init(id: "bad-loop", url: audio, title: "Invalid bounds", loopStart: 1.5, loopEnd: 0.5))
        await eventually("Invalid loop is reported") { !player.isLoading && player.errorMessage != nil }
        XCTAssertFalse(player.isPlaying)
        player.load(.init(id: "missing", url: root.appendingPathComponent("missing.wav"), title: "Missing"))
        await eventually("Missing local file is reported") { !player.isLoading && player.errorMessage != nil }
        XCTAssertFalse(player.isPlaying)
        player.load(.init(id: "good", url: audio, title: "Generated recovery sample"))
        await eventually("Good selection recovers after failed loads") { !player.isLoading && player.duration > 0 && player.errorMessage == nil }
        XCTAssertEqual(player.duration, 2, accuracy: 0.0001)
        XCTAssertFalse(player.isPlaying)
    }

    private func renderLiveReverb() throws -> (samples: [Float], wetMix: Float, dryMix: Float, dryBypass: Bool) {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
        let graph = PocketAudioGraph(format: format)
        var effects = PocketAudioEffectState()
        let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 192_000)!
        input.frameLength = 192_000
        for channel in 0..<2 {
            for frame in 0..<192_000 {
                let burst = (38_400..<50_400).contains(frame) || (134_400..<146_400).contains(frame)
                input.floatChannelData![channel][frame] = burst ? Float(sin(2 * .pi * 440 * Double(frame) / 48_000) * 0.95) : 0
            }
        }
        try graph.engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 960)
        try graph.start(); graph.player.scheduleBuffer(input); graph.player.play()
        defer { graph.player.stop(); graph.engine.stop() }
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 960)!
        var output: [Float] = [], wetMix: Float = 0, dryMix: Float = -1
        var dryBypass = false, retries = 0
        output.reserveCapacity(192_000)
        while output.count < 192_000 {
            // These are the exact live ramp and 20 ms control cadence, applied
            // after engine start. Wet for burst one; dry again for burst two.
            if output.count == 9_600 { effects.setTargets(reverb: 1) }
            if output.count == 96_000 { effects.setTargets(reverb: 0) }
            if effects.advance() { effects.apply(to: graph) }
            if output.count == 38_400 { wetMix = graph.reverb.wetDryMix }
            if output.count == 134_400 { dryMix = graph.reverb.wetDryMix; dryBypass = graph.reverb.auAudioUnit.shouldBypassEffect }
            switch try graph.engine.renderOffline(960, to: buffer) {
            case .success:
                output.append(contentsOf: UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength))); retries = 0
            case .cannotDoInCurrentContext, .insufficientDataFromInputNode:
                retries += 1; if retries > 100 { throw PocketAudioError.decodeFailed }
            case .error: throw PocketAudioError.decodeFailed
            @unknown default: throw PocketAudioError.decodeFailed
            }
        }
        graph.player.stop(); graph.engine.stop()
        return (output, wetMix, dryMix, dryBypass)
    }

    func testReverbPresetSurvivesNativeResourceInitialization() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let burst = root.appendingPathComponent("preset-regression.wav")
        try tone(seconds: 0.25, amplitude: 0.95).write(to: burst)
        let small = try render(burst, rate: 1, follows: true, wet: 1, seconds: 2, preset: .smallRoom)
        let hall = try render(burst, rate: 1, follows: true, wet: 1, seconds: 2, preset: .largeHall)
        let smallTail = small[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        let hallTail = hall[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        // Before the lifecycle fix, native initialization silently reset both
        // presets to identical output. This compares the same actual signal.
        XCTAssertGreaterThan(hallTail, smallTail * 5)
        XCTAssertGreaterThan(hallTail, smallTail + 100)
        print("Prepared reverb preset tails: small=\(smallTail), hall=\(hallTail)")
    }

    func testLiveReverbRendersMeasurableTailThenReturnsToDryWithoutRestart() throws {
        let live = try renderLiveReverb()
        let wetEnergy = live.samples[62_400..<86_400].reduce(0.0) { $0 + Double($1 * $1) }
        let dryEnergy = live.samples[158_400..<182_400].reduce(0.0) { $0 + Double($1 * $1) }
        let wetRMS = sqrt(wetEnergy / 24_000), dryRMS = sqrt(dryEnergy / 24_000)
        XCTAssertEqual(live.wetMix, PocketAudioGraph.maximumWetPercent)
        XCTAssertEqual(live.dryMix, 0); XCTAssertTrue(live.dryBypass)
        XCTAssertGreaterThan(wetRMS, 0.001)
        XCTAssertLessThan(dryRMS, 0.000001)
        XCTAssertTrue(live.samples.allSatisfy(\.isFinite))
        XCTAssertLessThanOrEqual(live.samples.map(abs).max() ?? 2, 1)
        print("Live reverb tail RMS wet=\(wetRMS), dry=\(dryRMS), peak=\(live.samples.map(abs).max() ?? 0)")
    }

    @MainActor private func waitForEffects(_ player: PocketAudioPlayer, reverb: Double) async throws -> PocketAudioEffectSnapshot {
        let deadline = Date().addingTimeInterval(6)
        repeat {
            let snapshot = await player.effectSnapshot()
            if snapshot.hasGraph && snapshot.reverb == reverb && snapshot.wetDryMix == Float(reverb) * PocketAudioGraph.maximumWetPercent && snapshot.bypassed == (reverb == 0) { return snapshot }
            try await Task.sleep(nanoseconds: 20_000_000)
        } while Date() < deadline
        throw NSError(domain: "PocketSimulatorTests", code: 1000, userInfo: [NSLocalizedDescriptionKey: "Running effect did not reach requested amount \(reverb)"])
    }

    @MainActor func testLiveSliderEffectSurvivesSavedLoopSeekBothPitchModesAndTrackChanges() async throws {
#if os(iOS)
        let previous = await Self.sessionConfiguration()
        addTeardownBlock { await Self.restoreSession(previous) }
        try await Self.prepareInactiveSession()
#endif
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let silence = root.appendingPathComponent("silent-live-reverb.wav")
        try tone(seconds: 8, amplitude: 0).write(to: silence)
        let player = PocketAudioPlayer()
        defer { player.pause(); player.setQueue([]) }
        player.setRepeatMode(.one)
        player.setQueue([
            .init(id: "live-loop", url: silence, title: "Generated silence", loopStart: 0.25, loopEnd: 0.75),
            .init(id: "live-next", url: silence, title: "Generated next silence")
        ], autoplay: true)
        await eventually("Silent saved loop enters actual playing state") { player.isPlaying && !player.isLoading }
        XCTAssertNil(player.errorMessage)
        player.setReverb(1)
        let wet = try await waitForEffects(player, reverb: 1)
        XCTAssertFalse(wet.bypassed); XCTAssertTrue(player.isPlaying)
        for follows in [false, true] {
            player.setPitchFollowsSpeed(follows); player.setPlaybackRate(1.37)
            player.seek(to: 0.1)
            await eventually("Effects do not disturb looping seek or pitch changes") { player.isPlaying && !player.isLoading }
            let kept = try await waitForEffects(player, reverb: 1)
            XCTAssertEqual(kept.wetDryMix, 35)
        }
        player.next()
        await eventually("Next track starts with the same chosen reverb") { player.currentTrack?.id == "live-next" && player.isPlaying && !player.isLoading }
        let nextWet = try await waitForEffects(player, reverb: 1)
        XCTAssertFalse(nextWet.bypassed)
        player.setReverb(0)
        let dry = try await waitForEffects(player, reverb: 0)
        XCTAssertTrue(dry.bypassed); XCTAssertTrue(player.isPlaying)
        XCTAssertEqual(player.ownedPCMBytes, 524_288); XCTAssertEqual(player.queue.count, 2)
        XCTAssertNil(player.errorMessage)
        player.pause(); player.setQueue([])
        await eventually("Silent fixture releases its bounded buffers") { !player.isPlaying && player.ownedPCMBytes == 0 }
    }

    private func render(_ url: URL, rate: Double, follows: Bool, wet: Double = 0, seconds: Double? = nil, preset: AVAudioUnitReverbPreset = .largeHall) throws -> [Float] {
        let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
        let format = file.processingFormat
        let graph = PocketAudioGraph(format: format, reverbPreset: preset)
        graph.setRate(rate, pitchFollowsSpeed: follows); graph.setReverb(wet)
        try graph.engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 2_048)
        try graph.start()
        defer { graph.player.stop(); graph.engine.stop() }
        graph.player.scheduleFile(file, at: nil); graph.player.play()
        let requested = Int((seconds ?? (Double(file.length) / format.sampleRate / rate + 0.4)) * format.sampleRate)
        let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 2_048)!
        var samples: [Float] = []; samples.reserveCapacity(requested)
        var retries = 0
        while samples.count < requested {
            switch try graph.engine.renderOffline(AVAudioFrameCount(min(2_048, requested - samples.count)), to: output) {
            case .success:
                guard output.frameLength > 0, let channel = output.floatChannelData else { throw PocketAudioError.decodeFailed }
                samples.append(contentsOf: UnsafeBufferPointer(start: channel[0], count: Int(output.frameLength)))
                retries = 0
            case .cannotDoInCurrentContext, .insufficientDataFromInputNode:
                retries += 1
                if retries > 100 { throw PocketAudioError.decodeFailed }
            case .error: throw PocketAudioError.decodeFailed
            @unknown default: throw PocketAudioError.decodeFailed
            }
        }
        return samples
    }
    private func measuredFrequency(_ samples: [Float]) -> Double {
        let first = 9_600, end = min(samples.count - 1, 38_400)
        let cycles = (first..<end).filter { samples[$0] <= 0 && samples[$0 + 1] > 0 }.count
        return Double(cycles) * 48_000 / Double(end - first)
    }

    func testNativeIOSOfflineRateAndPitchModesProcessRealSamples() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let audio = try writeTone(in: root)
        for rate in [0.5, 1.25, 2] {
            for follows in [true, false] {
                let samples = try render(audio, rate: rate, follows: follows)
                let frequency = measuredFrequency(samples)
                XCTAssertEqual(frequency, follows ? 440 * rate : 440, accuracy: 6, "Native rate \(rate), pitch follows \(follows)")
                let last = samples.lastIndex(where: { abs($0) > 0.02 }) ?? 0
                XCTAssertEqual(Double(last) / 48_000, 2 / rate, accuracy: 0.15, "Native duration at rate \(rate)")
                XCTAssertTrue(samples.allSatisfy(\.isFinite))
            }
        }
    }

    func testNativeIOSReverbZeroIsDryAndMaximumHasBoundedFiniteTail() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let audio = root.appendingPathComponent("generated-burst.wav")
        try tone(seconds: 0.25, amplitude: 0.95).write(to: audio)
        let reader = try BoundedAudioReader(url: audio)
        let graph = PocketAudioGraph(format: reader.format)
        graph.setReverb(1)
        XCTAssertEqual(graph.reverb.wetDryMix, 35)
        XCTAssertFalse(graph.reverb.auAudioUnit.shouldBypassEffect)
        graph.setReverb(0)
        XCTAssertEqual(graph.reverb.wetDryMix, 0)
        XCTAssertTrue(graph.reverb.auAudioUnit.shouldBypassEffect)
        let dry = try render(audio, rate: 1, follows: true, seconds: 2)
        let wet = try render(audio, rate: 1, follows: true, wet: 1, seconds: 2)
        let dryTail = dry[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        let wetTail = wet[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        XCTAssertLessThan(dryTail, 0.01)
        XCTAssertGreaterThan(wetTail, dryTail + 0.1)
        XCTAssertTrue(wet.allSatisfy(\.isFinite))
        XCTAssertLessThanOrEqual(wet.map(abs).max() ?? 2, 1.001)
    }

    func testExactABSamplesAndTenMinuteLoopHaveFixedPCMAllocation() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let stairs = root.appendingPathComponent("generated-stairs.wav")
        var bytes = wavHeader(frames: 50, channels: 1)
        for frame in 0..<50 { appendLittle(Int16(frame * 500), to: &bytes) }
        try bytes.write(to: stairs)
        let reader = try BoundedAudioReader(url: stairs, loopStart: 10.0 / 48_000, loopEnd: 17.0 / 48_000)
        let buffer = try reader.makeBuffer()
        try reader.read(into: buffer, looping: true)
        for index in [0, 6, 7, 8, 16_383] {
            XCTAssertEqual(buffer.floatChannelData![0][index], Float((10 + index % 7) * 500) / 32_768, accuracy: 0.00001)
        }
        let cachedFilePosition = reader.file.framePosition
        try reader.read(into: buffer, looping: true)
        XCTAssertEqual(reader.file.framePosition, cachedFilePosition, "Short loops reuse only the bounded scratch cache")
        let long = root.appendingPathComponent("generated-ten-minute-sparse-silence.wav")
        let frames = 600 * 48_000
        try wavHeader(frames: frames).write(to: long)
        let handle = try FileHandle(forWritingTo: long)
        try handle.truncate(atOffset: UInt64(44 + frames * 4)); try handle.close()
        let longReader = try BoundedAudioReader(url: long, loopStart: 0, loopEnd: 600)
        let longBuffer = try longReader.makeBuffer()
        XCTAssertEqual(longReader.ownedScratchBytes * 4, 524_288)
        longReader.seek(to: 599.95)
        try longReader.read(into: longBuffer, looping: true)
        XCTAssertEqual(longBuffer.frameLength, BoundedAudioReader.frameCapacity)
        XCTAssertEqual(longReader.duration, 600, accuracy: 0.0001)
    }

    private func sha256(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    private func bundle(in root: URL, manifest: PocketManifest, payload: Data, name: String = UUID().uuidString) throws -> URL {
        let json = try JSONEncoder().encode(manifest)
        var count = UInt32(json.count).bigEndian
        var bytes = Data("STLRMB01".utf8)
        withUnsafeBytes(of: &count) { bytes.append(contentsOf: $0) }
        bytes.append(json); bytes.append(payload)
        let url = root.appendingPathComponent(name + ".stillroomlibrary")
        try bytes.write(to: url)
        return url
    }
    private func transferFixture() -> (PocketManifest, Data, [Data]) {
        let one = String(repeating: "1", count: 32), two = String(repeating: "2", count: 32)
        let samples = [tone(frequency: 440), tone(frequency: 660)]
        let paths = ["audio/\(one).wav", "audio/\(two).wav"]
        let tracks = [PocketTrack(id: one, title: "Generated original", artist: "Simulator fixture", album: "Disposable", duration: 2, tags: ["test"], audioFile: paths[0]),
                      PocketTrack(id: two, title: "Generated saved loop", artist: "Simulator fixture", kind: "loop", duration: 1, fileDuration: 2, loopStart: 0.25, loopEnd: 1.25, audioFile: paths[1])]
        let files = zip(paths, samples).map { PocketAudioFile(path: $0.0, byteCount: Int64($0.1.count), sha256: sha256($0.1)) }
        let manifest = PocketManifest(tracks: tracks, playlists: [.init(id: String(repeating: "3", count: 32), name: "Deliberately reversed", trackIds: [two, one])], files: files, settings: .init(playbackRate: 0.873, pitchChanges: false, reverb: 0.3))
        return (manifest, samples[0] + samples[1], samples)
    }

    func testIOSImportExportRestorePreservesBytesIDsPlaylistOrderLoopsAndSettings() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let (manifest, payload, samples) = transferFixture()
        let source = try bundle(in: root, manifest: manifest, payload: payload)
        let store = try PocketLibraryStore(root: root.appendingPathComponent("Library"))
        let preview = try store.previewBundle(source)
        XCTAssertEqual(preview.tracksToAdd, 2); XCTAssertEqual(preview.playlistsToAdd, 1)
        XCTAssertTrue(store.catalog.tracks.isEmpty, "Preview must not commit")
        _ = try store.importBundle(source, expectedManifest: preview.manifest, validateAudio: { _ = try BoundedAudioReader(url: $0) })
        XCTAssertEqual(store.catalog.manifest, manifest)
        for (index, track) in manifest.tracks.enumerated() { XCTAssertEqual(try Data(contentsOf: store.audioURL(for: track)), samples[index]) }
        let duplicate = try store.importBundle(source)
        XCTAssertEqual(duplicate.addedTracks, 0); XCTAssertEqual(duplicate.skippedTracks, 2)
        let newSettings = PocketPlaybackSettings(playbackRate: 1.37, pitchChanges: true, reverb: 0)
        try store.saveSettings(newSettings)
        _ = try store.importBundle(source)
        XCTAssertEqual(store.catalog.settings, newSettings, "Later imports retain current phone preferences")
        let export = root.appendingPathComponent("roundtrip.stillroomlibrary")
        try store.exportBundle(to: export)
        let restored = try PocketLibraryStore(root: root.appendingPathComponent("Restored"))
        _ = try restored.importBundle(export, validateAudio: { _ = try BoundedAudioReader(url: $0) })
        XCTAssertEqual(restored.catalog, store.catalog)
        XCTAssertEqual(restored.catalog.playlists[0].trackIds, [manifest.tracks[1].id, manifest.tracks[0].id])
        let reopened = try PocketLibraryStore(root: restored.root)
        XCTAssertEqual(reopened.catalog, restored.catalog)
    }

    func testIOSCorruptAndConflictingImportsCannotOverwriteWorkingLibrary() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let (manifest, payload, samples) = transferFixture()
        let source = try bundle(in: root, manifest: manifest, payload: payload)
        let store = try PocketLibraryStore(root: root.appendingPathComponent("Library"))
        _ = try store.importBundle(source)
        let before = store.catalog
        var changed = manifest; changed.tracks[0].title = "Conflicting same ID"
        let conflict = try bundle(in: root, manifest: changed, payload: payload)
        XCTAssertThrowsError(try store.importBundle(conflict))
        var corrupt = payload; corrupt[100] ^= 1
        let bad = try bundle(in: root, manifest: manifest, payload: corrupt)
        XCTAssertThrowsError(try store.previewBundle(bad))
        XCTAssertThrowsError(try store.importBundle(bad))
        var unsafe = manifest
        unsafe.files[0].path = "../outside.wav"; unsafe.tracks[0].audioFile = "../outside.wav"
        XCTAssertThrowsError(try store.importBundle(bundle(in: root, manifest: unsafe, payload: payload)))
        XCTAssertEqual(store.catalog, before)
        for (index, track) in manifest.tracks.enumerated() { XCTAssertEqual(try Data(contentsOf: store.audioURL(for: track)), samples[index]) }
    }

    func testIOSLowSpaceCancellationAndCommitFailureLeaveRecoverableEmptyStores() throws {
        let root = try temporaryRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let (manifest, payload, _) = transferFixture()
        let source = try bundle(in: root, manifest: manifest, payload: payload)
        let original = try Data(contentsOf: source)
        let lowSpace = try PocketLibraryStore(root: root.appendingPathComponent("LowSpace"), availableBytes: { 0 })
        XCTAssertThrowsError(try lowSpace.importBundle(source)) { error in
            guard case PocketTransferError.insufficientSpace = error else { return XCTFail("Expected explicit low-space error, got \(error)") }
        }
        XCTAssertTrue(lowSpace.catalog.tracks.isEmpty)
        var cancelled = false
        let cancelledStore = try PocketLibraryStore(root: root.appendingPathComponent("Cancelled"), beforeCatalogCommit: { cancelled = true })
        XCTAssertThrowsError(try cancelledStore.importBundle(source, cancellation: { cancelled })) { error in
            guard case PocketTransferError.cancelled = error else { return XCTFail("Expected cancellation before catalog commit, got \(error)") }
        }
        let failedRoot = root.appendingPathComponent("FailedCommit")
        let failed = try PocketLibraryStore(root: failedRoot, beforeCatalogCommit: { throw NSError(domain: "InjectedSimulatorCommitFailure", code: 1) })
        XCTAssertThrowsError(try failed.importBundle(source))
        for path in [lowSpace.root, cancelledStore.root, failedRoot] {
            let reopened = try PocketLibraryStore(root: path)
            XCTAssertTrue(reopened.catalog.tracks.isEmpty)
            XCTAssertTrue(try FileManager.default.contentsOfDirectory(at: path.appendingPathComponent("Media"), includingPropertiesForKeys: nil).isEmpty)
        }
        XCTAssertEqual(try Data(contentsOf: source), original, "Failed imports must preserve the independent transfer file")
    }
}
