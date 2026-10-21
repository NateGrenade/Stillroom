import Foundation
import Combine
import AVFoundation
import AudioToolbox
#if os(iOS)
import MediaPlayer
#endif

public struct PocketAudioTrack: Identifiable, Equatable {
    public var id: String
    public var url: URL
    public var title: String
    public var artist: String
    public var loopStart: Double?
    public var loopEnd: Double?
    public init(id: String, url: URL, title: String, artist: String = "", loopStart: Double? = nil, loopEnd: Double? = nil) {
        self.id = id; self.url = url; self.title = title; self.artist = artist
        self.loopStart = loopStart; self.loopEnd = loopEnd
    }
}

public enum PocketRepeatMode: String, CaseIterable, Codable { case off, all, one }

/// Native audio transport. All AVAudioEngine and reader state belongs to `work`.
/// Main-actor published state is a snapshot; render callbacks never touch UI.
@MainActor public final class PocketAudioPlayer: ObservableObject {
    @Published public private(set) var currentTrack: PocketAudioTrack?
    @Published public private(set) var isPlaying = false
    @Published public private(set) var isLoading = false
    @Published public private(set) var position = 0.0
    @Published public private(set) var duration = 0.0
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var playbackRate = 1.0
    @Published public private(set) var pitchFollowsSpeed = true
    @Published public private(set) var reverb = 0.0
    @Published public private(set) var volume = 1.0
    @Published public private(set) var repeatMode: PocketRepeatMode = .off
    @Published public private(set) var queue: [PocketAudioTrack] = []
    @Published public private(set) var queueIndex: Int?
    @Published public private(set) var ownedPCMBytes = 0
    private let transport = NativeAudioTransport()
    private var activeRequest = UUID()
    private var observers: [NSObjectProtocol] = []
#if os(iOS)
    private var remoteTargets: [(MPRemoteCommand, Any)] = []
    private var interruptedPlayback = false
#endif

    public init() {
        transport.onSnapshot = { [weak self] value in
            Task { @MainActor in
                guard let self else { return }
                guard value.request == self.activeRequest else { return }
                self.isPlaying = value.playing; self.isLoading = value.loading
                self.position = value.position; self.duration = value.duration
                self.errorMessage = value.error; self.ownedPCMBytes = value.pcmBytes
#if os(iOS)
                self.updateNowPlaying()
#endif
            }
        }
        transport.onFinished = { [weak self] request in
            Task { @MainActor in
                guard let self, self.activeRequest == request else { return }
                self.finished()
            }
        }
#if os(iOS)
        installSystemControls()
#endif
    }

    public func setQueue(_ tracks: [PocketAudioTrack], startIndex: Int = 0, autoplay: Bool = false) {
        queue = tracks
        guard tracks.indices.contains(startIndex) else {
            activeRequest = UUID(); queueIndex = nil; currentTrack = nil; transport.clear(request: activeRequest); return
        }
        queueIndex = startIndex; load(tracks[startIndex], autoplay: autoplay)
    }
    public func load(_ track: PocketAudioTrack, autoplay: Bool = false) {
        currentTrack = track; position = 0; isLoading = true; errorMessage = nil
        activeRequest = UUID()
        transport.load(track, autoplay: autoplay, request: activeRequest)
    }
    public func play() {
        activeRequest = UUID(); isLoading = true; errorMessage = nil
        transport.play(request: activeRequest)
    }
    public func pause() { activeRequest = UUID(); transport.pause(request: activeRequest) }
    public func toggle() { transport.playbackIntended ? pause() : play() }
    public func seek(to value: Double) { activeRequest = UUID(); transport.seek(to: value, request: activeRequest) }
    public func next() {
        guard !queue.isEmpty else { return }
        let next = (queueIndex ?? -1) + 1
        if queue.indices.contains(next) { queueIndex = next; load(queue[next], autoplay: true) }
        else if repeatMode == .all { queueIndex = 0; load(queue[0], autoplay: true) }
        else { pause() }
    }
    public func previous() {
        if position > 3 { seek(to: 0); return }
        guard let current = queueIndex, current > 0 else { seek(to: 0); return }
        queueIndex = current - 1; load(queue[current - 1], autoplay: true)
    }
    public func setPlaybackRate(_ value: Double) {
        guard value.isFinite else { return }
        playbackRate = min(2, max(0.5, value)); transport.settings(rate: playbackRate, pitch: pitchFollowsSpeed)
    }
    public func setPitchFollowsSpeed(_ value: Bool) {
        pitchFollowsSpeed = value; transport.settings(rate: playbackRate, pitch: value)
    }
    public func setReverb(_ value: Double) {
        guard value.isFinite else { return }; reverb = min(1, max(0, value)); transport.effect(reverb: reverb)
    }
    public func setVolume(_ value: Double) {
        guard value.isFinite else { return }; volume = min(1, max(0, value)); transport.effect(volume: volume)
    }
    public func effectSnapshot() async -> PocketAudioEffectSnapshot { await transport.effectSnapshot() }
    public func setRepeatMode(_ value: PocketRepeatMode) {
        guard repeatMode != value else { return }
        repeatMode = value; activeRequest = UUID(); transport.repeatOne(value == .one, request: activeRequest)
    }
    public func setLoopRange(start: Double, end: Double) {
        guard var track = currentTrack else { return }
        track.loopStart = start; track.loopEnd = end
        if let index = queueIndex, queue.indices.contains(index) { queue[index] = track }
        load(track, autoplay: transport.playbackIntended)
    }
    public func clearLoopRange() {
        guard var track = currentTrack else { return }
        track.loopStart = nil; track.loopEnd = nil
        if let index = queueIndex, queue.indices.contains(index) { queue[index] = track }
        load(track, autoplay: transport.playbackIntended)
    }
    private func finished() {
        // The streaming reader handles repeat-one without an asynchronous gap.
        if repeatMode == .one { return }
        next()
    }
#if os(iOS)
    private func installSystemControls() {
        let center = MPRemoteCommandCenter.shared()
        func bind(_ command: MPRemoteCommand, _ action: @escaping @MainActor (MPRemoteCommandEvent) -> Void) {
            command.isEnabled = true
            let target = command.addTarget { event in Task { @MainActor in action(event) }; return .success }
            remoteTargets.append((command, target))
        }
        bind(center.playCommand) { [weak self] _ in self?.play() }
        bind(center.pauseCommand) { [weak self] _ in self?.pause() }
        bind(center.togglePlayPauseCommand) { [weak self] _ in self?.toggle() }
        bind(center.nextTrackCommand) { [weak self] _ in self?.next() }
        bind(center.previousTrackCommand) { [weak self] _ in self?.previous() }
        bind(center.changePlaybackPositionCommand) { [weak self] event in
            if let event = event as? MPChangePlaybackPositionCommandEvent { self?.seek(to: event.positionTime) }
        }
        observers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] notification in
            Task { @MainActor in
                guard let self, let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                      let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
                if type == .began { self.interruptedPlayback = self.transport.playbackIntended; self.pause() }
                else {
                    let options = AVAudioSession.InterruptionOptions(rawValue: notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
                    if self.interruptedPlayback && options.contains(.shouldResume) { self.play() }
                    self.interruptedPlayback = false
                }
            }
        })
        observers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] notification in
            let reason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            if reason == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue {
                Task { @MainActor in self?.interruptedPlayback = false; self?.pause() }
            }
        })
        observers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.pause(); self.errorMessage = "Audio services restarted. Select the recording again to resume safely."
            }
        })
    }
    private func updateNowPlaying() {
        guard let track = currentTrack else { MPNowPlayingInfoCenter.default().nowPlayingInfo = nil; return }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [MPMediaItemPropertyTitle: track.title, MPMediaItemPropertyArtist: track.artist,
            MPMediaItemPropertyPlaybackDuration: duration, MPNowPlayingInfoPropertyElapsedPlaybackTime: position,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? playbackRate : 0, MPNowPlayingInfoPropertyDefaultPlaybackRate: playbackRate]
        MPNowPlayingInfoCenter.default().playbackState = isPlaying ? .playing : .paused
    }
#endif
    deinit {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
#if os(iOS)
        for (command, target) in remoteTargets { command.removeTarget(target) }
#endif
        transport.shutdown()
    }
}

private struct NativeSnapshot {
    var request = UUID()
    var playing = false, loading = false
    var position = 0.0, duration = 0.0
    var error: String?
    var pcmBytes = 0
}

/// The serial queue owns file decoding, scheduling and effect changes. Its
/// bounded three-buffer pool plus scratch uses <= 512 KiB for supported stereo.
private final class NativeAudioTransport {
    var onSnapshot: ((NativeSnapshot) -> Void)?
    var onFinished: ((UUID) -> Void)?
    private let work = DispatchQueue(label: "Stillroom.native-audio", qos: .userInitiated)
    private let intentLock = NSLock()
    private var requestedIntent: UUID?
    private var wantedPlayback = false
    var playbackIntended: Bool {
        intentLock.lock(); defer { intentLock.unlock() }
        return wantedPlayback
    }
    private var engine = AVAudioEngine()
    private var player = AVAudioPlayerNode()
    private var speed = AVAudioUnitVarispeed()
    private var pitch = AVAudioUnitTimePitch()
    private var room = AVAudioUnitReverb()
    private var graph: PocketAudioGraph?
    private var reader: BoundedAudioReader?
    private var buffers: [AVAudioPCMBuffer] = []
    private var generation = 0, pending = 0
    private var timer: DispatchSourceTimer?
    private var timerCadence = 0
    private var lastEmission = DispatchTime.now().uptimeNanoseconds
    private var state = NativeSnapshot()
    private var rate = 1.0, pitchChanges = true, looping = false
    private var effects = PocketAudioEffectState()
    private var offset = 0.0
    private var finishedSent = false

    init() {
        let timer = DispatchSource.makeTimerSource(queue: work)
        timer.schedule(deadline: .distantFuture)
        timer.setEventHandler { [weak self] in self?.tick() }; timer.resume(); self.timer = timer
    }
    func load(_ track: PocketAudioTrack, autoplay: Bool, request: UUID) {
        recordIntent(request, wantsPlayback: autoplay)
        work.async { [self] in
            stop(reset: true); state.request = request; state.loading = true; state.error = nil; emit()
            do {
                reader = try BoundedAudioReader(url: track.url, loopStart: track.loopStart, loopEnd: track.loopEnd)
                guard let reader else { throw PocketAudioError.invalidFile }
                state.duration = reader.duration
                buffers = try (0..<3).map { _ in try reader.makeBuffer() }
                state.pcmBytes = reader.ownedScratchBytes * 4
                configure(format: reader.format)
                // begin() owns activation for every resume path, including a
                // seek/repeat queued while this autoplay load was preparing.
                if shouldPlay(request) { begin() }
                else { state.loading = false; emit() }
            } catch { fail(error) }
        }
    }
    func clear(request: UUID) {
        recordIntent(request, wantsPlayback: false)
        work.async { [self] in stop(reset: true); reader = nil; buffers = []; state = NativeSnapshot(); state.request = request; emit() }
    }
    func play(request: UUID) {
        recordIntent(request, wantsPlayback: true)
        work.async { [self] in
            let position = currentPosition()
            stop(reset: false); reader?.seek(to: position); offset = position
            state.request = request; state.loading = true; state.error = nil; emit()
            if reader != nil { begin() }
            else {
                // An empty Play still configures its session without producing
                // audio, then acknowledges that there is nothing to play.
                do {
                    guard shouldPlay(request) else { state.loading = false; return }
                    try activateAudioSession()
                    state.loading = false
                    guard shouldPlay(request) else { return }
                    finishPlaybackIntent(request); emit()
                } catch {
                    if isLatestIntent(request) { fail(error) }
                    else { state.loading = false; state.error = nil }
                }
            }
        }
    }
    func pause(request: UUID) {
        recordIntent(request, wantsPlayback: false)
        work.async { [self] in
            let position = currentPosition()
            stop(reset: false); reader?.seek(to: position); offset = position
            state.request = request; state.position = position; state.loading = false; emit()
        }
    }
    func seek(to value: Double, request: UUID) {
        recordIntent(request)
        work.async { [self] in
            guard let reader else { state.request = request; emit(); return }
            let resume = shouldPlay(request)
            stop(reset: false); reader.seek(to: value); offset = reader.position; state.position = offset
            state.request = request
            if offset >= reader.duration && !looping {
                finishPlaybackIntent(request); finishedSent = true; emit(); return
            }
            if resume { begin() } else { emit() }
        }
    }
    func repeatOne(_ value: Bool, request: UUID) {
        recordIntent(request)
        work.async { [self] in
            let position = currentPosition(), resume = shouldPlay(request)
            stop(reset: false); looping = value; reader?.seek(to: position); offset = position
            state.request = request
            if resume { begin() } else { emit() }
        }
    }
    func settings(rate: Double, pitch: Bool) {
        work.async { [self] in
            self.rate = rate; pitchChanges = pitch
            graph?.setRate(rate, pitchFollowsSpeed: pitch)
        }
    }
    func effect(reverb: Double? = nil, volume: Double? = nil) {
        work.async { [self] in effects.setTargets(reverb: reverb, volume: volume); updateTimer() }
    }
    func effectSnapshot() async -> PocketAudioEffectSnapshot {
        await withCheckedContinuation { continuation in
            work.async { [self] in
                continuation.resume(returning: PocketAudioEffectSnapshot(
                    hasGraph: graph != nil, reverb: effects.reverb, volume: effects.volume,
                    wetDryMix: graph?.reverb.wetDryMix ?? 0,
                    bypassed: graph?.reverb.auAudioUnit.shouldBypassEffect ?? true))
            }
        }
    }
    func shutdown() {
        work.async { [self] in timer?.cancel(); timer = nil; stop(reset: true); onSnapshot = nil; onFinished = nil; reader = nil; buffers = [] }
    }
    // Record the latest UI intent before dispatch. Session activation may take
    // time; a pause/seek arriving during it must suppress obsolete autoplay.
    private func recordIntent(_ request: UUID, wantsPlayback: Bool? = nil) {
        intentLock.lock(); requestedIntent = request
        if let wantsPlayback { wantedPlayback = wantsPlayback }
        intentLock.unlock()
    }
    private func isLatestIntent(_ request: UUID) -> Bool {
        intentLock.lock(); defer { intentLock.unlock() }
        return requestedIntent == request
    }
    private func shouldPlay(_ request: UUID) -> Bool {
        intentLock.lock(); defer { intentLock.unlock() }
        return requestedIntent == request && wantedPlayback
    }
    private func finishPlaybackIntent(_ request: UUID) {
        intentLock.lock(); defer { intentLock.unlock() }
        if requestedIntent == request { wantedPlayback = false }
    }
    private func activateAudioSession() throws {
        dispatchPrecondition(condition: .onQueue(work))
#if os(iOS)
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: [])
            try session.setActive(true)
        } catch {
            throw NSError(domain: "Stillroom.AudioSession", code: (error as NSError).code,
                          userInfo: [NSLocalizedDescriptionKey: "Audio could not start: \(error.localizedDescription)"])
        }
#endif
    }
    private func configure(format: AVAudioFormat) {
        engine.stop()
        let value = PocketAudioGraph(format: format); graph = value
        engine = value.engine; player = value.player; speed = value.varispeed; pitch = value.timePitch; room = value.reverb
        value.setRate(rate, pitchFollowsSpeed: pitchChanges)
        effects.apply(to: value)
        offset = 0; state.position = 0
    }
    private func begin() {
        guard let reader, !state.playing, shouldPlay(state.request) else { state.loading = false; return }
        state.loading = true; emit()
        do {
            // Session activation can block. It belongs here on the serial queue,
            // before scheduling, and must not erase a later seek's play intent.
            try activateAudioSession()
            guard shouldPlay(state.request) else { state.loading = false; return }
            if pending == 0 {
                if reader.position >= reader.duration {
                    generation += 1; player.stop(); reader.seek(to: 0); offset = 0
                }
                finishedSent = false
                for buffer in buffers { try schedule(buffer, token: generation) }
            }
            if !engine.isRunning, let graph { try graph.start() }
            guard shouldPlay(state.request) else { stop(reset: false); return }
            player.play(); state.playing = true; state.loading = false; state.error = nil; updateTimer(); emit()
        } catch {
            if isLatestIntent(state.request) { fail(error) }
            else { stop(reset: false); state.error = nil }
        }
    }
    private func schedule(_ buffer: AVAudioPCMBuffer, token: Int) throws {
        guard let reader, token == generation else { return }
        guard try reader.read(into: buffer, looping: looping) > 0 else { return }
        let finalBuffer = !looping && reader.position >= reader.duration
        pending += 1
        player.scheduleBuffer(buffer, completionCallbackType: finalBuffer ? .dataPlayedBack : .dataConsumed) { [weak self] _ in
            guard let self else { return }
            self.work.async {
                guard self.generation == token else { return }
                self.pending -= 1
                if finalBuffer && !self.finishedSent {
                    self.finishedSent = true; self.state.position = self.state.duration
                    self.finishPlaybackIntent(self.state.request)
                    self.player.pause(); self.engine.pause(); self.state.playing = false
                    self.updateTimer(); self.emit(); self.onFinished?(self.state.request)
                } else {
                    do { try self.schedule(buffer, token: token) } catch { self.fail(error) }
                }
            }
        }
    }
    private func currentPosition() -> Double {
        guard let reader else { return 0 }
        guard let render = player.lastRenderTime, let time = player.playerTime(forNodeTime: render), time.sampleRate > 0 else { return offset }
        let logical = offset + Double(time.sampleTime) / time.sampleRate
        return looping ? logical.truncatingRemainder(dividingBy: reader.duration) : min(reader.duration, max(0, logical))
    }
    private func stop(reset: Bool) {
        generation += 1; player.stop(); pending = 0; state.playing = false; state.loading = false
        if reset { engine.stop(); offset = 0; state.position = 0; state.duration = 0; state.pcmBytes = 0; reader = nil; buffers = [] }
        else { engine.pause() }
        updateTimer()
    }
    private func tick() {
        // Only update Audio Unit parameters while a control is moving. The
        // shared ramp is also exercised during continuous offline rendering.
        if effects.advance(), let graph { effects.apply(to: graph) }
        if state.playing {
            state.position = currentPosition()
        }
        updateTimer()
        if state.playing && DispatchTime.now().uptimeNanoseconds - lastEmission >= 180_000_000 { emit() }
    }
    private func updateTimer() {
        let smoothing = effects.isSmoothing
        let cadence = smoothing ? 20 : state.playing ? 200 : 0
        guard cadence != timerCadence else { return }
        timerCadence = cadence
        if cadence == 0 { timer?.schedule(deadline: .distantFuture) }
        else { timer?.schedule(deadline: .now() + .milliseconds(cadence), repeating: .milliseconds(cadence), leeway: .milliseconds(3)) }
    }
    private func emit() { lastEmission = DispatchTime.now().uptimeNanoseconds; onSnapshot?(state) }
    private func fail(_ error: Error) {
        finishPlaybackIntent(state.request); stop(reset: false)
        state.error = error.localizedDescription; emit()
    }
}
