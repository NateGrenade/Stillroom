import Foundation
import AVFoundation
import AudioToolbox

/// Shared real-time/offline graph: no feedback connections, constant headroom,
/// bounded reverb mix and Apple's peak limiter after the effects.
public final class PocketAudioGraph {
    public static let headroom: Float = 0.7
    public static let maximumWetPercent: Float = 35
    public let engine = AVAudioEngine()
    public let player = AVAudioPlayerNode()
    public let varispeed = AVAudioUnitVarispeed()
    public let timePitch = AVAudioUnitTimePitch()
    public let reverb = AVAudioUnitReverb()
    public let limiter: AVAudioUnitEffect
    private let reverbPreset: AVAudioUnitReverbPreset
    private var reverbAmount = 0.0
    private var presetPrepared = false

    public init(format: AVAudioFormat, reverbPreset: AVAudioUnitReverbPreset = .largeHall) {
        self.reverbPreset = reverbPreset
        limiter = AVAudioUnitEffect(audioComponentDescription: AudioComponentDescription(
            componentType: kAudioUnitType_Effect,
            componentSubType: kAudioUnitSubType_PeakLimiter,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0, componentFlagsMask: 0))
        [player, varispeed, timePitch, reverb, limiter].forEach(engine.attach)
        engine.connect(player, to: varispeed, format: format)
        engine.connect(varispeed, to: timePitch, format: format)
        engine.connect(timePitch, to: reverb, format: format)
        engine.connect(reverb, to: limiter, format: format)
        engine.connect(limiter, to: engine.mainMixerNode, format: format)
        setRate(1, pitchFollowsSpeed: true); setReverb(0); setVolume(1)
    }
    /// Native Audio Units initialize their defaults when render resources are
    /// allocated. Load the hall after prepare(), before any player audio, so
    /// that initialization cannot silently replace the selected preset.
    public func start() throws {
        guard !engine.isRunning else { return }
        let hadResources = reverb.auAudioUnit.renderResourcesAllocated
        engine.prepare()
        if !presetPrepared || !hadResources {
            reverb.loadFactoryPreset(reverbPreset)
            setReverb(reverbAmount)
            presetPrepared = true
        }
        try engine.start()
    }
    public func setRate(_ value: Double, pitchFollowsSpeed: Bool) {
        let rate = Float(min(2, max(0.5, value.isFinite ? value : 1)))
        varispeed.rate = pitchFollowsSpeed ? rate : 1
        timePitch.rate = pitchFollowsSpeed ? 1 : rate
        timePitch.pitch = 0
        // At unity, the pitch processor has no job and adds avoidable latency.
        timePitch.auAudioUnit.shouldBypassEffect = pitchFollowsSpeed || rate == 1
    }
    public func setReverb(_ value: Double) {
        reverbAmount = min(1, max(0, value.isFinite ? value : 0))
        let amount = Float(reverbAmount)
        if amount > 0 { reverb.auAudioUnit.shouldBypassEffect = false }
        reverb.wetDryMix = amount * Self.maximumWetPercent
        if amount == 0 { reverb.auAudioUnit.shouldBypassEffect = true }
    }
    public func setVolume(_ value: Double) {
        engine.mainMixerNode.outputVolume = Float(min(1, max(0, value.isFinite ? value : 1))) * Self.headroom
    }
}

/// The live transport and offline checks share this control ramp. Advance once
/// per 20 ms control tick; no audio-sized allocation or render-thread work.
public struct PocketAudioEffectState: Sendable {
    public private(set) var reverb = 0.0
    public private(set) var volume = 1.0
    private var wantedReverb = 0.0
    private var wantedVolume = 1.0
    public init() {}
    public var isSmoothing: Bool { reverb != wantedReverb || volume != wantedVolume }
    public mutating func setTargets(reverb: Double? = nil, volume: Double? = nil) {
        if let reverb, reverb.isFinite { wantedReverb = min(1, max(0, reverb)) }
        if let volume, volume.isFinite { wantedVolume = min(1, max(0, volume)) }
    }
    @discardableResult public mutating func advance() -> Bool {
        guard isSmoothing else { return false }
        reverb += (wantedReverb - reverb) * 0.3
        volume += (wantedVolume - volume) * 0.3
        if abs(reverb - wantedReverb) < 0.0001 { reverb = wantedReverb }
        if abs(volume - wantedVolume) < 0.0001 { volume = wantedVolume }
        return true
    }
    public func apply(to graph: PocketAudioGraph) {
        graph.setReverb(reverb); graph.setVolume(volume)
    }
}

/// A work-queue snapshot of applied controls for diagnostics and silent native
/// integration checks. Slider values remain the requested user settings.
public struct PocketAudioEffectSnapshot: Sendable {
    public let hasGraph: Bool
    public let reverb: Double
    public let volume: Double
    public let wetDryMix: Float
    public let bypassed: Bool
}
