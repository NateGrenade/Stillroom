import Foundation
import AVFoundation

public enum PocketAudioError: LocalizedError {
    case invalidFile, invalidLoop, unsupportedChannels, decodeFailed
    public var errorDescription: String? {
        switch self {
        case .invalidFile: return "This recording has no playable audio."
        case .invalidLoop: return "Choose loop boundaries inside the recording, with the end after the start."
        case .unsupportedChannels: return "This prototype supports mono and stereo recordings. Multichannel files are kept unchanged but cannot play here yet."
        case .decodeFailed: return "The recording could not be decoded."
        }
    }
}

/// Owns one fixed scratch buffer. Call from one serial queue only.
/// Decoding never allocates a song-sized or loop-sized PCM array.
public final class BoundedAudioReader {
    public static let frameCapacity: AVAudioFrameCount = 16_384
    public let file: AVAudioFile
    public let format: AVAudioFormat
    public let firstFrame: AVAudioFramePosition
    public let endFrame: AVAudioFramePosition
    public private(set) var cursor: AVAudioFramePosition
    private let scratch: AVAudioPCMBuffer
    private let cachedRegion: Bool
    public var frameCount: AVAudioFramePosition { endFrame - firstFrame }
    public var duration: Double { Double(frameCount) / format.sampleRate }
    public var position: Double { Double(cursor - firstFrame) / format.sampleRate }
    public var ownedScratchBytes: Int { Int(Self.frameCapacity) * Int(format.channelCount) * MemoryLayout<Float>.size }

    public init(url: URL, loopStart: Double? = nil, loopEnd: Double? = nil) throws {
        file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
        format = file.processingFormat
        guard file.length > 0, format.sampleRate.isFinite, format.sampleRate > 0 else { throw PocketAudioError.invalidFile }
        // Keep the initial effect graph to the mono/stereo formats proved below.
        // Reject multichannel rather than risk an Audio Unit format assertion.
        guard (1...2).contains(format.channelCount) else { throw PocketAudioError.unsupportedChannels }
        let duration = Double(file.length) / format.sampleRate
        let start = loopStart ?? 0, end = loopEnd ?? duration
        guard start.isFinite, end.isFinite, start >= 0, end > start,
              end <= duration + 0.000_001 else { throw PocketAudioError.invalidLoop }
        firstFrame = AVAudioFramePosition((start * format.sampleRate).rounded())
        endFrame = min(file.length, AVAudioFramePosition((end * format.sampleRate).rounded()))
        guard firstFrame >= 0, firstFrame < endFrame else { throw PocketAudioError.invalidLoop }
        cursor = firstFrame
        guard let scratch = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: Self.frameCapacity) else { throw PocketAudioError.decodeFailed }
        self.scratch = scratch
        cachedRegion = endFrame - firstFrame <= AVAudioFramePosition(Self.frameCapacity)
        if cachedRegion {
            file.framePosition = firstFrame
            try file.read(into: scratch, frameCount: AVAudioFrameCount(endFrame - firstFrame))
            guard scratch.frameLength == AVAudioFrameCount(endFrame - firstFrame) else { throw PocketAudioError.decodeFailed }
        }
    }

    public func seek(to seconds: Double) {
        let value = seconds.isFinite ? min(duration, max(0, seconds)) : 0
        cursor = min(endFrame, firstFrame + AVAudioFramePosition((value * format.sampleRate).rounded()))
    }

    public func makeBuffer() throws -> AVAudioPCMBuffer {
        guard let value = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: Self.frameCapacity) else { throw PocketAudioError.decodeFailed }
        return value
    }

    /// Appends the exact region's samples, wrapping within this buffer if needed.
    /// This avoids a special silence-producing boundary buffer at A/B.
    @discardableResult public func read(into output: AVAudioPCMBuffer, looping: Bool) throws -> AVAudioFrameCount {
        guard output.format == format, output.frameCapacity <= Self.frameCapacity,
              let destination = output.floatChannelData, let source = scratch.floatChannelData else { throw PocketAudioError.decodeFailed }
        var written: AVAudioFrameCount = 0
        while written < output.frameCapacity {
            if cursor >= endFrame {
                if !looping { break }
                cursor = firstFrame
            }
            let amount = AVAudioFrameCount(min(AVAudioFramePosition(output.frameCapacity - written), endFrame - cursor))
            let sourceOffset: Int
            if cachedRegion { sourceOffset = Int(cursor - firstFrame) }
            else {
                file.framePosition = cursor
                try file.read(into: scratch, frameCount: amount)
                guard scratch.frameLength == amount else { throw PocketAudioError.decodeFailed }
                sourceOffset = 0
            }
            for channel in 0..<Int(format.channelCount) {
                destination[channel].advanced(by: Int(written)).update(from: source[channel].advanced(by: sourceOffset), count: Int(amount))
            }
            written += amount
            cursor += AVAudioFramePosition(amount)
        }
        output.frameLength = written
        return written
    }
}
