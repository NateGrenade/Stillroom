import Foundation
import AVFoundation

// Standalone checks runnable with Command Line Tools; no iOS/simulator claim.
@main struct AudioNativeChecks {
    static var assertions = 0
    static func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        assertions += 1
        if try condition() == false { throw NSError(domain: "PocketAudioChecks", code: assertions, userInfo: [NSLocalizedDescriptionKey: message]) }
    }
    static func little<T: FixedWidthInteger>(_ value: T, into data: inout Data) {
        var value = value.littleEndian; withUnsafeBytes(of: &value) { data.append(contentsOf: $0) }
    }
    static func wavHeader(frames: Int, channels: Int = 2) -> Data {
        let bytes = frames * channels * 2
        var data = Data("RIFF".utf8); little(UInt32(36 + bytes), into: &data)
        data.append(Data("WAVEfmt ".utf8)); little(UInt32(16), into: &data)
        little(UInt16(1), into: &data); little(UInt16(channels), into: &data)
        little(UInt32(48_000), into: &data); little(UInt32(48_000 * channels * 2), into: &data)
        little(UInt16(channels * 2), into: &data); little(UInt16(16), into: &data)
        data.append(Data("data".utf8)); little(UInt32(bytes), into: &data)
        return data
    }
    static func makeTone(_ path: URL, seconds: Double = 4, amplitude: Double = 0.4, frequency: Double = 440) throws {
        let frames = Int(seconds * 48_000)
        var data = wavHeader(frames: frames)
        for frame in 0..<frames {
            let value = Int16((sin(2 * .pi * frequency * Double(frame) / 48_000) * amplitude * 32_767).rounded())
            little(value, into: &data); little(value, into: &data)
        }
        try data.write(to: path)
    }
    static func render(_ path: URL, rate: Double, follows: Bool, wet: Double = 0, seconds: Double? = nil, preset: AVAudioUnitReverbPreset = .largeHall) throws -> [Float] {
        let file = try AVAudioFile(forReading: path)
        let format = file.processingFormat
        let graph = PocketAudioGraph(format: format, reverbPreset: preset)
        graph.setRate(rate, pitchFollowsSpeed: follows); graph.setReverb(wet)
        try graph.engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 2_048)
        try graph.start()
        graph.player.scheduleFile(file, at: nil)
        graph.player.play()
        let frames = Int((seconds ?? (Double(file.length) / format.sampleRate / rate + 0.5)) * format.sampleRate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 2_048)!
        var output: [Float] = []; output.reserveCapacity(frames)
        var retries = 0
        while output.count < frames {
            let amount = AVAudioFrameCount(min(2_048, frames - output.count))
            switch try graph.engine.renderOffline(amount, to: buffer) {
            case .success:
                guard let samples = buffer.floatChannelData else { throw PocketAudioError.decodeFailed }
                output.append(contentsOf: UnsafeBufferPointer(start: samples[0], count: Int(buffer.frameLength)))
                retries = 0
            case .cannotDoInCurrentContext, .insufficientDataFromInputNode:
                retries += 1
                if retries > 100 { throw PocketAudioError.decodeFailed }
            case .error: throw PocketAudioError.decodeFailed
            @unknown default: throw PocketAudioError.decodeFailed
            }
        }
        graph.player.stop(); graph.engine.stop()
        return output
    }
    static func frequency(_ samples: [Float]) -> Double {
        let start = 14_400, end = min(samples.count - 1, 48_000)
        let crossings = (start..<end).filter { samples[$0] <= 0 && samples[$0 + 1] > 0 }.count
        return Double(crossings) * 48_000 / Double(end - start)
    }
    static func renderLiveReverb() throws -> (samples: [Float], wetMix: Float, dryMix: Float, dryBypass: Bool) {
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
    @MainActor static func waitForEffects(_ player: PocketAudioPlayer, reverb: Double) async throws -> PocketAudioEffectSnapshot {
        let deadline = Date().addingTimeInterval(3)
        repeat {
            let snapshot = await player.effectSnapshot()
            if snapshot.hasGraph && snapshot.reverb == reverb && snapshot.wetDryMix == Float(reverb) * PocketAudioGraph.maximumWetPercent && snapshot.bypassed == (reverb == 0) { return snapshot }
            try await Task.sleep(nanoseconds: 20_000_000)
        } while Date() < deadline
        throw NSError(domain: "PocketAudioChecks", code: 1000, userInfo: [NSLocalizedDescriptionKey: "Live serial effect timer did not reach requested reverb \(reverb)"])
    }
    @MainActor static func checkLiveTransport(in root: URL) async throws {
        let silence = root.appendingPathComponent("generated-zero-amplitude.wav")
        try makeTone(silence, seconds: 2, amplitude: 0)
        let player = PocketAudioPlayer()
        defer { player.pause(); player.setQueue([]) }
        player.setRepeatMode(.one)
        player.setQueue([.init(id: "silent-loop", url: silence, title: "Generated silence", loopStart: 0.25, loopEnd: 0.75)], autoplay: true)
        let deadline = Date().addingTimeInterval(3)
        while (!player.isPlaying || player.isLoading) && Date() < deadline { try await Task.sleep(nanoseconds: 20_000_000) }
        try check(player.isPlaying && !player.isLoading && player.errorMessage == nil, "Silent native loop enters actual playing state")
        player.setReverb(1)
        let wet = try await waitForEffects(player, reverb: 1)
        try check(wet.wetDryMix == 35 && !wet.bypassed && player.isPlaying, "Live slider reaches the running graph without restarting playback")
        player.seek(to: 0.1)
        player.setReverb(0)
        let dry = try await waitForEffects(player, reverb: 0)
        try check(dry.wetDryMix == 0 && dry.bypassed && player.isPlaying, "Live slider returns to true dry during a native loop/seek")
        try check(player.ownedPCMBytes == 524_288 && player.queue.count == 1 && player.currentTrack?.id == "silent-loop", "Live reverb leaves bounded PCM ownership and queue intact")
        player.pause(); player.setQueue([])
        let clearDeadline = Date().addingTimeInterval(2)
        while (player.isPlaying || player.ownedPCMBytes != 0) && Date() < clearDeadline { try await Task.sleep(nanoseconds: 20_000_000) }
        try check(!player.isPlaying && player.ownedPCMBytes == 0, "Silent transport fixture stops and releases its bounded buffers")
    }
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketAudioChecks-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) } // This task's generated, disposable fixtures only.
        let tone = root.appendingPathComponent("tone.wav")
        try makeTone(tone)
        let reader = try BoundedAudioReader(url: tone)
        let buffer = try reader.makeBuffer()
        try check(try reader.read(into: buffer, looping: false) == BoundedAudioReader.frameCapacity, "Reader decodes a bounded chunk")
        try check(reader.ownedScratchBytes * 4 == 524_288, "Stereo pool plus scratch is exactly512KiB")
        reader.seek(to: 4)
        try check(try reader.read(into: buffer, looping: false) == 0, "End seek does not wrap a non-looping reader")
        reader.seek(to: .nan)
        try check(reader.position == 0, "Non-finite seek safely resets to0")
        do { _ = try BoundedAudioReader(url: tone, loopStart: 2, loopEnd: 1); try check(false, "Invalid reversed range must throw") }
        catch PocketAudioError.invalidLoop { assertions += 1 }
        let multi = root.appendingPathComponent("unsupported-multichannel.wav")
        var multiBytes = wavHeader(frames: 100, channels: 3)
        multiBytes.append(Data(repeating: 0, count: 600)); try multiBytes.write(to: multi)
        do { _ = try BoundedAudioReader(url: multi); try check(false, "Unproved multichannel effect format must be rejected") }
        catch PocketAudioError.unsupportedChannels { assertions += 1 }

        let stairs = root.appendingPathComponent("stairs.wav")
        var staircase = wavHeader(frames: 50, channels: 1)
        for frame in 0..<50 { little(Int16(frame * 500), into: &staircase) }
        try staircase.write(to: stairs)
        let loop = try BoundedAudioReader(url: stairs, loopStart: 10.0 / 48_000, loopEnd: 17.0 / 48_000)
        let repeatBuffer = try loop.makeBuffer()
        let start = DispatchTime.now().uptimeNanoseconds
        try loop.read(into: repeatBuffer, looping: true)
        let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000_000
        for index in [0, 1, 6, 7, 8, 16383] {
            let expected = Float((10 + index % 7) * 500) / 32_768
            try check(abs(repeatBuffer.floatChannelData![0][index] - expected) < 0.00001, "Exact A/B samples wrap without padding at sample\(index)")
        }
        try check(elapsed < 1, "Very short loops use fixed cache instead of thousands of decoder seeks")
        let shortFilePosition = loop.file.framePosition
        try loop.read(into: repeatBuffer, looping: true)
        try check(loop.file.framePosition == shortFilePosition, "Repeated short loops perform no additional file reads")

        let long = root.appendingPathComponent("ten-minute-sparse-silence.wav")
        let longFrames = 600 * 48_000
        try wavHeader(frames: longFrames).write(to: long)
        let handle = try FileHandle(forWritingTo: long)
        try handle.truncate(atOffset: UInt64(44 + longFrames * 4)); try handle.close()
        let longReader = try BoundedAudioReader(url: long, loopStart: 0, loopEnd: 600)
        try check(abs(longReader.duration - 600) < 0.0001, "Ten-minute native decoder fixture opens")
        try check(longReader.ownedScratchBytes == reader.ownedScratchBytes, "Ten-minute loop has the same owned scratch allocation as four seconds")
        let longBuffer = try longReader.makeBuffer()
        longReader.seek(to: 599.95); try longReader.read(into: longBuffer, looping: true)
        try check(longBuffer.frameLength == BoundedAudioReader.frameCapacity, "Long loop crosses end into start inside a bounded buffer")

        var measurements: [[String: Any]] = []
        for rate in [0.5, 1.25, 2.0] {
            for follows in [true, false] {
                let samples = try render(tone, rate: rate, follows: follows)
                let measured = frequency(samples), expected = follows ? 440 * rate : 440
                let last = samples.lastIndex(where: { abs($0) > 0.02 }) ?? 0
                let measuredLength = Double(last) / 48_000
                try check(abs(measured - expected) < 6, "Native rate\(rate) pitchFollows\(follows): frequency\(measured) expected\(expected)")
                try check(abs(measuredLength - 4 / rate) < 0.15, "Native rate\(rate) length\(measuredLength) expected\(4 / rate)")
                try check(samples.allSatisfy(\.isFinite), "Native processed samples stay finite")
                measurements.append(["rate":rate,"pitchFollowsSpeed":follows,"frequencyHz":measured,"audibleSeconds":measuredLength])
            }
        }
        let graph = PocketAudioGraph(format: reader.format)
        graph.setReverb(1)
        try check(graph.reverb.wetDryMix == 35 && !graph.reverb.auAudioUnit.shouldBypassEffect, "Maximum reverb is bounded at35percent")
        graph.setReverb(0)
        try check(graph.reverb.wetDryMix == 0 && graph.reverb.auAudioUnit.shouldBypassEffect, "Reverb0 truly bypasses the effect")
        let burst = root.appendingPathComponent("burst.wav")
        try makeTone(burst, seconds: 0.25, amplitude: 0.95)
        let dry = try render(burst, rate: 1, follows: true, wet: 0, seconds: 2)
        let wet = try render(burst, rate: 1, follows: true, wet: 1, seconds: 2)
        let smallRoom = try render(burst, rate: 1, follows: true, wet: 1, seconds: 2, preset: .smallRoom)
        let smallRoomTail = smallRoom[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        let dryTail = dry[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        let wetTail = wet[24_000...].reduce(0.0) { $0 + Double(abs($1)) }
        try check(dryTail < 0.01, "Dry route adds no reverb tail")
        try check(wetTail > dryTail + 0.1, "Maximum reverb renders an audible finite tail")
        try check(wetTail > smallRoomTail * 5 && wetTail > smallRoomTail + 100, "Prepared hall and small-room presets render distinct tails; engine initialization must not overwrite them")
        let lifecycle = PocketAudioGraph(format: reader.format)
        lifecycle.setReverb(1)
        try lifecycle.engine.enableManualRenderingMode(.offline, format: reader.format, maximumFrameCount: 960)
        try lifecycle.start()
        lifecycle.reverb.wetDryMix = 12.345 // Diagnostic sentinel: a redundant preset reload would overwrite this.
        try lifecycle.start()
        try check(lifecycle.reverb.wetDryMix == 12.345, "Starting an already running graph does not reload its preset")
        lifecycle.engine.pause(); try lifecycle.start()
        try check(lifecycle.reverb.wetDryMix == 12.345, "Pause/resume retains allocated effect state without reloading its preset")
        lifecycle.engine.stop(); try lifecycle.start()
        try check(lifecycle.reverb.wetDryMix == 35, "True resource reallocation reapplies the selected preset and requested wet mix")
        lifecycle.engine.stop()
        try check(wet.allSatisfy(\.isFinite) && wet.map(abs).max()! <= 1, "Maximum reverb with peak limiter stays finite and below clipping")
        var controls = PocketAudioEffectState()
        try check(!controls.isSmoothing && !controls.advance(), "Stable controls avoid redundant Audio Unit writes")
        controls.setTargets(reverb: 1, volume: 0.5); controls.advance()
        try check(abs(controls.reverb - 0.3) < 0.000001 && abs(controls.volume - 0.85) < 0.000001, "Shared live controls ramp smoothly instead of jumping")
        for _ in 0..<40 { controls.advance() }
        try check(controls.reverb == 1 && controls.volume == 0.5 && !controls.isSmoothing, "Shared controls settle exactly at their targets")
        controls.setTargets(reverb: .nan, volume: .infinity)
        try check(!controls.isSmoothing, "Shared controls reject non-finite input")
        controls.setTargets(reverb: -1, volume: 2)
        for _ in 0..<40 { controls.advance() }
        try check(controls.reverb == 0 && controls.volume == 1, "Shared controls clamp and reach exact dry")
        let live = try renderLiveReverb()
        let liveWetTail = live.samples[62_400..<86_400].reduce(0.0) { $0 + Double($1 * $1) }
        let liveDryTail = live.samples[158_400..<182_400].reduce(0.0) { $0 + Double($1 * $1) }
        let wetRMS = sqrt(liveWetTail / 24_000), dryRMS = sqrt(liveDryTail / 24_000)
        try check(live.wetMix == 35 && live.dryMix == 0 && live.dryBypass, "Live rendering applies full wet and exact dry without an engine restart")
        try check(wetRMS > 0.001 && dryRMS < 0.000001, "Live wet has a measurable tail while the later dry burst has none")
        try check(live.samples.allSatisfy(\.isFinite) && live.samples.map(abs).max()! <= 1, "Live wet/dry transitions remain finite and bounded below clipping")
        try await checkLiveTransport(in: root)
        let result: [String: Any] = ["smallRoomTailAbsoluteSum":smallRoomTail,"liveWetTailRMS":wetRMS,"liveDryTailRMS":dryRMS,"liveMaximumPeak":live.samples.map(abs).max()!,"liveControlCadenceMilliseconds":20,"assertions":assertions,"platform":"Mac-host native offline rendering and zero-amplitude live transport only","ownedStereoPCMBytes":524_288,"shortLoopSecondsToFill":elapsed,"measurements":measurements,"dryTailAbsoluteSum":dryTail,"wetTailAbsoluteSum":wetTail,"maximumWetPeak":wet.map(abs).max()!]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted,.sortedKeys]), encoding: .utf8)!)
    }
}
