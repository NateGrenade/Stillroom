import Foundation
import SwiftUI
#if canImport(PocketAudio)
import PocketAudio
#endif

struct PocketAdvancedPanel: View {
    @ObservedObject var model: PocketAppModel
    @ObservedObject var audio: PocketAudioPlayer
    @Binding var loopA: Double?
    @Binding var loopB: Double?
    @Environment(\.dismiss) private var dismiss

    private var loopIsReady: Bool {
        guard let a = loopA, let b = loopB else { return false }
        return b > a
    }

    private var reverbDescription: String {
        audio.reverb == 0 ? "Off · original dry sound" : "\(Int(audio.reverb * 100))% added room"
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        PocketEyebrow("MAKE IT YOURS")
                        Text("A little room to linger.")
                            .font(.title2.weight(.medium))
                        Text("Shape the space around a song, or keep a favorite moment close.")
                            .font(.subheadline)
                            .foregroundStyle(PocketTheme.secondary)
                    }
                    .padding(.vertical, 8)
                }
                .listRowBackground(Color.clear)

                Section("Added reverb") {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("A little more space around the sound.")
                            .font(.subheadline)
                            .foregroundStyle(PocketTheme.secondary)
                        Slider(
                            value: Binding(
                                get: { audio.reverb },
                                set: {
                                    audio.setReverb($0)
                                    model.schedulePlaybackSettingsSave()
                                }
                            ),
                            in: 0...1,
                            onEditingChanged: { editing in
                                if !editing { model.savePlaybackSettings() }
                            }
                        )
                        .frame(minHeight: 44)
                        .accessibilityLabel("Added reverb")
                        .accessibilityValue(reverbDescription)
                        Text(reverbDescription)
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(PocketTheme.secondary)
                    }
                    .padding(.vertical, 4)
                    Button {
                        audio.setReverb(0)
                        model.savePlaybackSettings()
                    } label: {
                        Label("Turn reverb off", systemImage: "speaker.wave.1")
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .foregroundStyle(PocketTheme.accent)
                }
                .listRowBackground(PocketTheme.surface)

                Section("Save an A–B loop") {
                    Text("Mark the start and end as the song plays. Saving keeps this moment in your offline library.")
                        .font(.subheadline)
                        .foregroundStyle(PocketTheme.secondary)
                    Button {
                        loopA = audio.position
                    } label: {
                        Label(loopA.map { "A: \(time($0)) · mark again" } ?? "Mark A at current position", systemImage: "a.circle")
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .foregroundStyle(PocketTheme.accent)
                    Button {
                        loopB = audio.position
                    } label: {
                        Label(loopB.map { "B: \(time($0)) · mark again" } ?? "Mark B at current position", systemImage: "b.circle")
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .foregroundStyle(PocketTheme.accent)
                    Button {
                        guard model.busy == nil, let a = loopA, let b = loopB else { return }
                        if model.saveLoop(start: a, end: b) {
                            loopA = nil; loopB = nil
                            dismiss()
                        }
                    } label: {
                        Label("Save loop and repeat", systemImage: "repeat")
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                    .foregroundStyle(PocketTheme.accent)
                    .disabled(!loopIsReady)
                }
                .listRowBackground(PocketTheme.surface)
                .disabled(audio.currentTrack == nil)
            }
            .disabled(model.busy != nil)
            .scrollContentBackground(.hidden)
            .background(PocketTheme.background)
            .foregroundStyle(PocketTheme.text)
            .navigationTitle("Advanced options")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        guard model.busy == nil else { return }
                        model.savePlaybackSettings()
                        dismiss()
                    }
                    .frame(minHeight: 44)
                    .disabled(model.busy != nil)
                }
            }
            .toolbarBackground(PocketTheme.background, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
        }
        .tint(PocketTheme.accent)
        .preferredColorScheme(.dark)
        .interactiveDismissDisabled(model.busy != nil)
    }

    private func time(_ value: Double) -> String {
        let seconds = Int(max(0, value.isFinite ? value : 0))
        return "\(seconds / 60):\(String(format: "%02d", seconds % 60))"
    }
}

struct PocketImportReview: View {
    @ObservedObject var model: PocketAppModel

    var body: some View {
        NavigationStack {
            Form {
                if let preview = model.preview {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            PocketEyebrow(model.recoveryRequired ? "A SAFE WAY BACK" : "BRING IT HOME")
                            Text(model.recoveryRequired ? "Restore a separate copy." : "A place for these songs.")
                                .font(.title2.weight(.medium))
                            Text(model.recoveryRequired ? "Review the backup before switching to its recovered library. Your original library stays in place." : "Review the library before keeping a copy on this iPhone.")
                                .font(.subheadline)
                                .foregroundStyle(PocketTheme.secondary)
                        }
                        .padding(.vertical, 8)
                    }
                    .listRowBackground(Color.clear)

                    Section("Ready to import") {
                        LabeledContent("New songs", value: "\(preview.tracksToAdd)")
                        LabeledContent("New playlists", value: "\(preview.playlistsToAdd)")
                        LabeledContent("Songs already present", value: "\(preview.duplicateTracks)")
                        LabeledContent("Space needed", value: ByteCountFormatter.string(fromByteCount: preview.requiredBytes, countStyle: .file))
                    }
                    .listRowBackground(PocketTheme.surface)

                    Section {
                        Text(model.recoveryRequired ? "Audio, playlists, loops and playback settings will be checked and copied into a new library. Only a successful restore switches the app to that copy. The original library and any incomplete recovery copy are kept." : "Music will be copied into this app for offline listening. Existing conflicting IDs are refused; a failed or cancelled import keeps the working library intact.")
                            .font(.subheadline)
                            .foregroundStyle(PocketTheme.secondary)
                        Button {
                            model.importPreview()
                        } label: {
                            Label(model.recoveryRequired ? "Restore into a new library" : "Import into offline library", systemImage: "square.and.arrow.down")
                                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        }
                        .foregroundStyle(PocketTheme.accent)
                        .disabled(model.busy != nil)

                        if let busy = model.busy {
                            ProgressView(busy)
                                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                            Button("Cancel operation") { model.cancelWork() }
                                .frame(minHeight: 44)
                                .foregroundStyle(PocketTheme.accent)
                                .disabled(!model.canCancel)
                        }
                    }
                    .listRowBackground(PocketTheme.surface)
                }
            }
            .scrollContentBackground(.hidden)
            .background(PocketTheme.background)
            .foregroundStyle(PocketTheme.text)
            .navigationTitle(model.recoveryRequired ? "Review backup" : "Review library")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") {
                        guard model.busy == nil else { return }
                        model.closePreview()
                    }
                    .frame(minHeight: 44)
                    .disabled(model.busy != nil)
                }
            }
            .toolbarBackground(PocketTheme.background, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
        }
        .tint(PocketTheme.accent)
        .preferredColorScheme(.dark)
        .interactiveDismissDisabled(model.busy != nil)
    }
}

struct PocketHelpView: View {
    @Environment(\.dismiss) private var dismiss
    private let topics = [
        (name: "README", title: "Start here", detail: "A quiet place for your music.", icon: "sparkles"),
        (name: "Setup", title: "Install and open", detail: "Get the Mac and phone ready.", icon: "iphone"),
        (name: "UseAndImport", title: "Import and listen offline", detail: "Bring a library over and make it yours.", icon: "square.and.arrow.down"),
        (name: "RefreshAndRecovery", title: "Refresh, backup and recovery", detail: "Keep a separate copy of your music.", icon: "arrow.clockwise"),
        (name: "Troubleshooting", title: "I’m stuck", detail: "Find the next small step.", icon: "questionmark.circle"),
        (name: "Sources", title: "Sources and video placeholders", detail: "Written guides and reference links.", icon: "book.closed")
    ]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        PocketEyebrow("A LITTLE GUIDANCE")
                        Text("Make yourself at home.")
                            .font(.title2.weight(.medium))
                        Text("The written guides are here whenever you need a hand.")
                            .font(.subheadline)
                            .foregroundStyle(PocketTheme.secondary)
                    }
                    .padding(.vertical, 8)
                }
                .listRowBackground(Color.clear)

                Section {
                    ForEach(topics, id: \.name) { topic in
                        NavigationLink {
                            ScrollView {
                                Text(.init(contents(topic.name)))
                                    .font(.body)
                                    .foregroundStyle(PocketTheme.text)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .textSelection(.enabled)
                                    .padding(20)
                                    .pocketCard()
                                    .padding(16)
                            }
                            .background(PocketTheme.background)
                            .navigationTitle(topic.title)
                            .navigationBarTitleDisplayMode(.inline)
                            .toolbarBackground(PocketTheme.background, for: .navigationBar)
                            .toolbarBackground(.visible, for: .navigationBar)
                        } label: {
                            HStack(alignment: .top, spacing: 14) {
                                Image(systemName: topic.icon)
                                    .font(.title3)
                                    .foregroundStyle(PocketTheme.accent)
                                    .frame(width: 28)
                                    .accessibilityHidden(true)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(topic.title)
                                        .font(.body.weight(.medium))
                                        .foregroundStyle(PocketTheme.text)
                                    Text(topic.detail)
                                        .font(.caption)
                                        .foregroundStyle(PocketTheme.secondary)
                                }
                            }
                            .frame(minHeight: 44, alignment: .leading)
                            .padding(.vertical, 6)
                        }
                    }
                }
                .listRowBackground(PocketTheme.surface)
            }
            .scrollContentBackground(.hidden)
            .background(PocketTheme.background)
            .navigationTitle("Help")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .frame(minHeight: 44)
                }
            }
            .toolbarBackground(PocketTheme.background, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
        }
        .tint(PocketTheme.accent)
        .preferredColorScheme(.dark)
    }

    private func contents(_ name: String) -> String {
        guard let url = Bundle.main.url(forResource: name, withExtension: "md"),
              let value = try? String(contentsOf: url, encoding: .utf8) else {
            return "The written guide is in the Help folder beside the project. Keep a music backup outside this app; removing or reinstalling the app can remove its local library."
        }
        return value
    }
}
