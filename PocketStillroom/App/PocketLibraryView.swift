import SwiftUI
import UniformTypeIdentifiers

struct PocketLibraryView: View {
    @ObservedObject var model: PocketAppModel
    @ObservedObject var audio: PocketAudioPlayer
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.scenePhase) private var scenePhase
    @State private var choosingFile = false
    @State private var showingImportPicker = false
    @State private var pendingReceivedURL: URL?
    @State private var browseAfterImportPicker = false
    @State private var showingHelp = false
    @State private var showingAdvanced = false
    @State private var speedExpanded: Bool
    @State private var loopA: Double?
    @State private var loopB: Double?

    init(model: PocketAppModel, audio: PocketAudioPlayer, initiallyExpandedSpeed: Bool = false) {
        self.model = model; self.audio = audio
        _speedExpanded = State(initialValue: initiallyExpandedSpeed)
    }
    private var collectionName: String { model.catalog.playlists.first(where: { $0.id == model.selectedPlaylist })?.name ?? "Your library" }

    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                let inlinePlayer = geometry.size.height < 620 || geometry.size.width > geometry.size.height || typeSize >= .xxLarge
                if inlinePlayer {
                    libraryScroll(inlinePlayer: true)
                } else {
                    libraryScroll(inlinePlayer: false)
                        .safeAreaInset(edge: .bottom, spacing: 0) {
                            player.padding(.horizontal, 12).padding(.bottom, 8)
                                .background(PocketTheme.background.ignoresSafeArea(edges: .bottom))
                        }
                }
            }
            .background(PocketTheme.background)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    PocketBrandMark().accessibilityHidden(true)
                }
                ToolbarItem(placement: .principal) {
                    Text("stillroom").font(.title3.weight(.semibold)).tracking(-0.7).foregroundStyle(PocketTheme.text)
                        .accessibilityLabel("Pocket Stillroom")
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { openImportPicker() } label: { Image(systemName: "plus") }
                        .buttonStyle(PocketIconStyle()).accessibilityLabel("Import library").accessibilityIdentifier("import-library")
                        .disabled(model.busy != nil || (model.recoveryRequired && !model.recoveryAvailable))
                    Button { showingHelp = true } label: { Image(systemName: "questionmark.circle") }
                        .buttonStyle(PocketIconStyle()).accessibilityLabel("Help").accessibilityIdentifier("open-help")
                }
            }
            .toolbarBackground(PocketTheme.background, for: .navigationBar).toolbarBackground(.visible, for: .navigationBar)
        }
        .tint(PocketTheme.accent).foregroundStyle(PocketTheme.text).preferredColorScheme(.dark)
        .sheet(isPresented: $showingImportPicker, onDismiss: finishImportPicker) {
            PocketImportPicker(model: model, chooseReceived: { library in
                pendingReceivedURL = library.url; showingImportPicker = false
            }, browseFiles: {
                browseAfterImportPicker = true; showingImportPicker = false
            })
        }
        .fileImporter(isPresented: $choosingFile, allowedContentTypes: [UTType(exportedAs: "com.nategrenade.stillroom.library")], allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls): if let url = urls.first { model.beginPreview(url) }
            case .failure(let error): model.message = error.localizedDescription
            }
        }
        .sheet(isPresented: Binding(get: { model.preview != nil }, set: { if !$0 && model.busy == nil { model.closePreview() } })) { PocketImportReview(model: model) }
        .sheet(isPresented: $showingAdvanced) { PocketAdvancedPanel(model: model, audio: audio, loopA: $loopA, loopB: $loopB) }
        .sheet(isPresented: $showingHelp) { PocketHelpView() }
        .alert("Stillroom", isPresented: Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })) {
            Button("OK") { model.message = nil }
        } message: { Text(model.message ?? "") }
        .task { await model.refreshReceivedLibraries() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.refreshReceivedLibraries() } }
        }
        .onChange(of: audio.currentTrack?.id) { _, _ in loopA = nil; loopB = nil }
    }

    private func openImportPicker() {
        pendingReceivedURL = nil; browseAfterImportPicker = false
        Task { await model.refreshReceivedLibraries() }
        showingImportPicker = true
    }
    private func finishImportPicker() {
        if let url = pendingReceivedURL {
            pendingReceivedURL = nil
            model.beginPreview(url)
        } else if browseAfterImportPicker {
            browseAfterImportPicker = false
            choosingFile = true
        }
    }

    private var player: some View {
        PocketPlayerDock(model: model, audio: audio, speedExpanded: $speedExpanded, showingAdvanced: $showingAdvanced)
    }
    private func libraryScroll(inlinePlayer: Bool) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                libraryHeading
                if model.recoveryRequired { recoveryLibrary }
                else if model.catalog.tracks.isEmpty { emptyLibrary }
                else {
                    playlistChooser
                    if inlinePlayer { player }
                    trackCollection
                }
                if inlinePlayer && model.catalog.tracks.isEmpty { player }
                if let busy = model.busy { busyCard(busy) }
                if let error = audio.errorMessage {
                    Label(error, systemImage: "exclamationmark.circle").font(.subheadline).foregroundStyle(PocketTheme.text)
                        .padding(16).pocketCard().accessibilityIdentifier("playback-error")
                }
                if !model.recoveryRequired { backupCard }
                Label("Just here, just yours.", systemImage: "checkmark.shield")
                    .font(.caption).foregroundStyle(PocketTheme.secondary).frame(maxWidth: .infinity)
            }.padding(.horizontal, 20).padding(.top, 16).padding(.bottom, 24)
                .frame(maxWidth: 720).frame(maxWidth: .infinity)
        }.background(PocketTheme.background)
    }

    private var libraryHeading: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 9) {
                if !typeSize.isAccessibilitySize { PocketEyebrow("YOUR COLLECTION") }
                (Text(collectionName) + Text(".").foregroundColor(PocketTheme.accent))
                    .font(.largeTitle.weight(.medium)).tracking(-1.1).fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                Text(model.catalog.tracks.isEmpty ? "A little room for your music." : "Something for the moment. Always close by.")
                    .font(.subheadline).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
            }.frame(maxWidth: .infinity, alignment: .leading)
            if !typeSize.isAccessibilitySize {
                PocketLandscape().frame(width: 76, height: 94).clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(PocketTheme.border, lineWidth: 1))
            }
        }
    }

    private var playlistChooser: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                collectionChip("All songs", id: "", count: model.catalog.tracks.count, icon: "square.stack")
                ForEach(model.catalog.playlists) { playlist in
                    collectionChip(playlist.name, id: playlist.id, count: playlist.trackIds.count, icon: "music.note.list")
                }
            }.padding(.vertical, 2)
        }.accessibilityLabel("Collections").accessibilityIdentifier("collection-picker")
    }
    private func collectionChip(_ name: String, id: String, count: Int, icon: String) -> some View {
        let selected = model.selectedPlaylist == id
        return Button { model.selectedPlaylist = id } label: {
            HStack(spacing: 7) {
                Image(systemName: icon)
                Text(name).fixedSize()
                Text("\(count)").monospacedDigit().foregroundStyle(selected ? PocketTheme.accent : PocketTheme.secondary)
            }.font(.subheadline.weight(selected ? .semibold : .regular))
                .padding(.horizontal, 13).frame(minHeight: 44)
                .foregroundStyle(selected ? PocketTheme.accent : PocketTheme.secondary)
                .background(selected ? PocketTheme.elevated : PocketTheme.surface, in: Capsule())
                .overlay(Capsule().strokeBorder(selected ? PocketTheme.accent.opacity(0.6) : PocketTheme.border, lineWidth: 1))
        }.buttonStyle(.plain).accessibilityLabel("\(name), \(count) songs")
            .accessibilityAddTraits(selected ? .isSelected : []).disabled(model.busy != nil)
    }

    private var trackCollection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                PocketEyebrow("READY TO LISTEN")
                Spacer()
                Label("\(model.visibleTracks.count) offline", systemImage: "arrow.down.circle")
                    .font(.caption).foregroundStyle(PocketTheme.secondary)
            }.accessibilityElement(children: .combine)
            VStack(spacing: 0) {
                ForEach(Array(model.visibleTracks.enumerated()), id: \.element.id) { index, track in
                    trackRow(track)
                    if index < model.visibleTracks.count - 1 { Rectangle().fill(PocketTheme.border.opacity(0.45)).frame(height: 1).padding(.leading, 72) }
                }
            }.pocketCard()
            if model.visibleTracks.isEmpty {
                Text("A quiet collection. Import songs to fill it.").font(.subheadline).foregroundStyle(PocketTheme.secondary).padding(.vertical, 12)
            }
        }
    }
    private func trackRow(_ track: PocketTrack) -> some View {
        let current = audio.currentTrack?.id == track.id
        return Button {
            loopA = nil; loopB = nil; model.play(track)
        } label: {
            HStack(spacing: 12) {
                PocketTrackTile(id: track.id, loop: track.kind == "loop", playing: current && audio.isPlaying).frame(width: 44, height: 48)
                VStack(alignment: .leading, spacing: 4) {
                    Text(track.title).font(.subheadline.weight(.medium)).foregroundStyle(current ? PocketTheme.accent : PocketTheme.text)
                        .lineLimit(typeSize.isAccessibilitySize ? nil : 2).frame(maxWidth: .infinity, alignment: .leading)
                    Text(track.artist.isEmpty ? "Unknown artist" : track.artist).font(.caption).foregroundStyle(PocketTheme.secondary).lineLimit(typeSize.isAccessibilitySize ? nil : 1)
                    if track.kind == "loop" || typeSize.isAccessibilitySize {
                        HStack(spacing: 8) {
                            if track.kind == "loop" { Label("Saved loop", systemImage: "repeat").foregroundStyle(PocketTheme.accent) }
                            if typeSize.isAccessibilitySize { Text(PocketFormatting.time(track.duration)).foregroundStyle(PocketTheme.secondary).monospacedDigit() }
                        }.font(.caption2)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
                if !typeSize.isAccessibilitySize {
                    Text(PocketFormatting.time(track.duration)).font(.caption.monospacedDigit()).foregroundStyle(PocketTheme.secondary)
                }
            }.padding(.horizontal, 14).padding(.vertical, 14).frame(minHeight: 76)
                .background(current ? PocketTheme.accent.opacity(0.07) : Color.clear)
        }.buttonStyle(.plain).disabled(model.busy != nil)
            .accessibilityLabel("Play \(track.title), \(track.artist.isEmpty ? "Unknown artist" : track.artist), \(PocketFormatting.time(track.duration)), \(track.kind == "loop" ? "saved loop" : "stored offline")")
            .accessibilityIdentifier("track-\(track.id)")
    }

    private var emptyLibrary: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack(spacing: 14) {
                Image(systemName: "music.note.house").font(.title2).foregroundStyle(PocketTheme.accent)
                Text("Make yourself at home.").font(.title3.weight(.medium)).fixedSize(horizontal: false, vertical: true)
            }
            Text("Bring a Stillroom library from your Mac. Your songs and playlists stay on this iPhone for offline listening.")
                .font(.subheadline).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
            Button { openImportPicker() } label: { Label("Import your library", systemImage: "square.and.arrow.down") }
                .buttonStyle(PocketActionStyle(prominent: true)).disabled(model.busy != nil)
            Text("Find a library received from your desktop, or browse Files, iCloud Drive or AirDrop.")
                .font(.caption).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(20).pocketCard()
    }
    private var recoveryLibrary: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Keep your music safe.", systemImage: "externaldrive.badge.exclamationmark")
                .font(.title3.weight(.medium)).fixedSize(horizontal: false, vertical: true)
            Text("This library could not open. Keep the app installed. A known-good backup can be restored into a separate library, keeping the original files for recovery.")
                .font(.subheadline).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
            if model.recoveryAvailable {
                Button { openImportPicker() } label: { Label("Choose a backup to restore", systemImage: "square.and.arrow.down") }
                    .buttonStyle(PocketActionStyle(prominent: true)).disabled(model.busy != nil)
                Text("You will review the backup before switching to the recovered copy. Nothing replaces the original library.")
                    .font(.caption).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Open Help and ask for recovery assistance before changing app files or reinstalling.")
                    .font(.subheadline).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Button("Open recovery help") { showingHelp = true }.buttonStyle(PocketActionStyle())
        }.padding(20).pocketCard().accessibilityIdentifier("library-recovery")
    }
    private func busyCard(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            ProgressView(message).font(.subheadline).tint(PocketTheme.accent)
            if model.canCancel { Button("Cancel operation") { model.cancelWork() }.buttonStyle(PocketActionStyle()) }
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading).pocketCard()
    }
    private var backupCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Keep the good ones close.", systemImage: "externaldrive").font(.subheadline.weight(.medium))
            Text("Save a library backup outside the app. Removing the app can remove its music.")
                .font(.caption).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { backupActions }
                VStack(alignment: .leading, spacing: 8) { backupActions }
            }
        }.padding(18).pocketCard()
    }
    @ViewBuilder private var backupActions: some View {
        Button("Prepare backup") { model.exportBackup() }.buttonStyle(PocketActionStyle()).disabled(model.busy != nil || model.catalog.tracks.isEmpty)
        if let url = model.backupURL { ShareLink("Share backup", item: url).buttonStyle(PocketActionStyle(prominent: true)) }
    }
}

struct PocketPlayerDock: View {
    @ObservedObject var model: PocketAppModel
    @ObservedObject var audio: PocketAudioPlayer
    @Binding var speedExpanded: Bool
    @Binding var showingAdvanced: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.scenePhase) private var scenePhase
    @ScaledMetric(relativeTo: .title3) private var playIconSize = 20.0
    @State private var scrubbing = false
    @State private var scrubPosition = 0.0

    var body: some View {
        VStack(spacing: 8) {
                nowPlaying
                seekControl
                if typeSize.isAccessibilitySize {
                    HStack { elapsedTime; Spacer(); totalTime }
                    speedButton
                } else {
                    HStack { elapsedTime; Spacer(); speedButton; Spacer(); totalTime }
                }
                if speedExpanded { speedOptions.transition(.opacity.combined(with: .move(edge: .top))) }
                transport
        }.padding(14).fixedSize(horizontal: false, vertical: true)
        .background(PocketTheme.player, in: RoundedRectangle(cornerRadius: 20))
        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(PocketTheme.border, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 20)).accessibilityIdentifier("player-dock")
    }
    private var nowPlaying: some View {
        HStack(spacing: 11) {
            if !typeSize.isAccessibilitySize {
                PocketTrackTile(id: audio.currentTrack?.id ?? "stillroom").frame(width: 44, height: 48)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(audio.currentTrack?.title ?? "A moment of quiet").font(.subheadline.weight(.semibold))
                    .foregroundStyle(PocketTheme.text).lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                Text(audio.currentTrack?.artist.isEmpty == false ? audio.currentTrack!.artist : audio.currentTrack == nil ? "Choose something to play" : "Unknown artist")
                    .font(.caption).foregroundStyle(PocketTheme.secondary).lineLimit(typeSize.isAccessibilitySize ? nil : 1)
            }.frame(maxWidth: .infinity, alignment: .leading)
            Button { showingAdvanced = true } label: { Image(systemName: "slider.horizontal.3") }
                .buttonStyle(PocketIconStyle()).accessibilityLabel("Advanced playback options").accessibilityIdentifier("advanced-options").disabled(model.busy != nil)
        }
    }
    private var elapsedTime: some View {
        Text(PocketFormatting.time(audio.position)).font(.caption.monospacedDigit()).foregroundStyle(PocketTheme.secondary).fixedSize()
    }
    private var totalTime: some View {
        Text(PocketFormatting.time(audio.duration)).font(.caption.monospacedDigit()).foregroundStyle(PocketTheme.secondary).fixedSize()
    }
    private var speedButton: some View {
        Button {
            withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.16)) { speedExpanded.toggle() }
        } label: {
            HStack(spacing: 5) {
                Text(PocketSpeedScale.text(audio.playbackRate)).monospacedDigit().fixedSize()
                Image(systemName: "chevron.down").font(.system(size: 12, weight: .semibold)).rotationEffect(.degrees(speedExpanded ? 180 : 0))
            }.font(.subheadline.weight(.semibold)).foregroundStyle(PocketTheme.accent)
                .padding(.horizontal, 14).frame(minHeight: 44).background(PocketTheme.elevated, in: Capsule())
        }.buttonStyle(.plain).disabled(model.busy != nil)
            .accessibilityLabel("Playback speed \(PocketSpeedScale.text(audio.playbackRate)); adjust speed")
            .accessibilityValue(speedExpanded ? "Expanded" : "Collapsed").accessibilityIdentifier("speed-toggle")
    }
    private var seekControl: some View {
        Slider(value: Binding(get: { scrubbing ? scrubPosition : min(audio.position, max(audio.duration, 0.01)) }, set: {
            scrubPosition = $0
            // Keyboard/accessibility adjustments need not send touch-edit events.
            if !scrubbing { audio.seek(to: $0) }
        }), in: 0...max(audio.duration, 0.01), onEditingChanged: { editing in
            if editing { scrubPosition = audio.position }; scrubbing = editing
            if !editing { audio.seek(to: scrubPosition) }
        }).tint(PocketTheme.accent).frame(minHeight: 44).disabled(audio.currentTrack == nil || model.busy != nil)
            .accessibilityLabel("Playback position").accessibilityValue(PocketFormatting.time(audio.position))
            .accessibilityAdjustableAction { direction in
                guard audio.currentTrack != nil, model.busy == nil else { return }
                switch direction {
                case .increment: audio.seek(to: min(audio.duration, audio.position + 5))
                case .decrement: audio.seek(to: max(0, audio.position - 5))
                @unknown default: break
                }
            }
    }
    private var speedOptions: some View {
        VStack(alignment: .leading, spacing: 8) {
            Rectangle().fill(PocketTheme.border).frame(height: 1)
            HStack {
                PocketEyebrow("FIND YOUR PACE")
                Spacer()
                Button("Done") {
                    model.savePlaybackSettings()
                    withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.16)) { speedExpanded = false }
                }.font(.subheadline.weight(.semibold)).foregroundStyle(PocketTheme.accent).frame(minWidth: 44, minHeight: 44)
            }
            PocketSpeedControl(rate: Binding(get: { audio.playbackRate }, set: { audio.setPlaybackRate($0); model.schedulePlaybackSettingsSave() }), onEditingChanged: { editing in
                if !editing { audio.setPlaybackRate(PocketSpeedScale.snapped(audio.playbackRate)); model.savePlaybackSettings() }
            })
            Toggle("Pitch follows speed", isOn: Binding(get: { audio.pitchFollowsSpeed }, set: { audio.setPitchFollowsSpeed($0); model.savePlaybackSettings() }))
                .font(.subheadline).tint(PocketTheme.accent).frame(minHeight: 44)
            Text("Turn off to keep the original pitch.").font(.caption).foregroundStyle(PocketTheme.secondary)
        }.disabled(model.busy != nil)
    }
    private var transport: some View {
        HStack(spacing: 14) {
            Button { audio.previous() } label: { Image(systemName: "backward.end.fill") }.buttonStyle(PocketIconStyle()).accessibilityLabel("Previous song").disabled(model.busy != nil)
            Button { audio.toggle() } label: {
                Image(systemName: audio.isPlaying ? "pause.fill" : "play.fill").font(.system(size: min(playIconSize, 28), weight: .semibold))
                    .foregroundStyle(PocketTheme.onAccent).frame(width: 52, height: 52).background(PocketTheme.accent, in: Circle())
            }.buttonStyle(.plain).accessibilityLabel(audio.isPlaying ? "Pause" : "Play")
            Button { audio.next() } label: { Image(systemName: "forward.end.fill") }.buttonStyle(PocketIconStyle()).accessibilityLabel("Next song").disabled(model.busy != nil)
            Button { audio.setRepeatMode(audio.repeatMode == .off ? .all : audio.repeatMode == .all ? .one : .off) } label: {
                Image(systemName: audio.repeatMode == .one ? "repeat.1" : "repeat").foregroundStyle(audio.repeatMode == .off ? PocketTheme.secondary : PocketTheme.accent)
            }.buttonStyle(PocketIconStyle()).accessibilityLabel("Repeat \(audio.repeatMode.rawValue)").disabled(model.busy != nil)
        }.frame(maxWidth: .infinity).disabled(audio.currentTrack == nil)
    }
}
