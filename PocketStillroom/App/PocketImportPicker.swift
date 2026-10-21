import SwiftUI
#if canImport(PocketCore)
import PocketCore
#endif

struct PocketImportPicker: View {
    @ObservedObject var model: PocketAppModel
    var chooseReceived: (PocketReceivedLibrary) -> Void
    var browseFiles: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    private var canChoose: Bool { model.busy == nil && (!model.recoveryRequired || model.recoveryAvailable) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Label("On this iPhone", systemImage: "iphone.and.arrow.forward")
                                .font(.headline).accessibilityAddTraits(.isHeader)
                            Spacer()
                            Button { Task { await model.refreshReceivedLibraries() } } label: { Image(systemName: "arrow.clockwise") }
                                .buttonStyle(PocketIconStyle()).accessibilityLabel("Refresh received libraries")
                                .accessibilityIdentifier("refresh-received-libraries").disabled(model.refreshingReceivedLibraries)
                        }
                        Text("Libraries sent from Stillroom on your desktop appear here. Choose one to review before importing.")
                            .font(.subheadline).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
                        if model.recoveryRequired {
                            Text("You will review the backup before restoring a separate copy. Your original library stays in place.")
                                .font(.caption).foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if model.refreshingReceivedLibraries && model.receivedLibraries.isEmpty {
                        ProgressView("Looking for libraries…").font(.subheadline).tint(PocketTheme.accent)
                    } else if let message = model.receivedLibrariesMessage {
                        Text(message).font(.subheadline).foregroundStyle(PocketTheme.secondary)
                            .accessibilityIdentifier("received-libraries-unavailable")
                    } else if model.receivedLibraries.isEmpty {
                        Text("Nothing here yet. Send a library from your desktop, or choose one in Files.")
                            .font(.subheadline).foregroundStyle(PocketTheme.secondary)
                            .accessibilityIdentifier("received-libraries-empty")
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(model.receivedLibraries.enumerated()), id: \.element.id) { index, library in
                                receivedRow(library)
                                if index < model.receivedLibraries.count - 1 {
                                    Rectangle().fill(PocketTheme.border.opacity(0.45)).frame(height: 1).padding(.horizontal, 14)
                                }
                            }
                        }.pocketCard().accessibilityIdentifier("received-library-list")
                        if model.receivedLibraries.count == PocketReceivedLibraries.maximumFiles {
                            Text("Showing up to \(PocketReceivedLibraries.maximumFiles) recent libraries. Browse Files to find another.")
                                .font(.caption).foregroundStyle(PocketTheme.secondary)
                        }
                    }
                    Button(action: browseFiles) { Label("Browse Files", systemImage: "folder") }
                        .buttonStyle(PocketActionStyle(prominent: true)).disabled(!canChoose)
                        .accessibilityIdentifier("browse-library-files")
                }.padding(20).frame(maxWidth: 640).frame(maxWidth: .infinity)
            }
            .background(PocketTheme.background)
            .navigationTitle("Import library").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Close") { dismiss() }.accessibilityIdentifier("close-import-picker") } }
            .toolbarBackground(PocketTheme.background, for: .navigationBar).toolbarBackground(.visible, for: .navigationBar)
        }
        .tint(PocketTheme.accent).foregroundStyle(PocketTheme.text).preferredColorScheme(.dark)
        .presentationDetents(typeSize.isAccessibilitySize ? [.large] : [.medium, .large])
        .presentationDragIndicator(.visible)
        .accessibilityIdentifier("import-library-picker")
    }

    private func receivedRow(_ library: PocketReceivedLibrary) -> some View {
        Button { chooseReceived(library) } label: {
            HStack(spacing: 12) {
                Image(systemName: "doc.zipper").font(.title3).foregroundStyle(PocketTheme.accent).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text(library.filename).font(.subheadline.weight(.medium)).foregroundStyle(PocketTheme.text)
                        .lineLimit(typeSize.isAccessibilitySize ? nil : 2).multilineTextAlignment(.leading)
                    Text(library.receivedAt.formatted(date: .abbreviated, time: .shortened))
                        .font(.caption).foregroundStyle(PocketTheme.secondary)
                    Text(ByteCountFormatter.string(fromByteCount: library.byteCount, countStyle: .file))
                        .font(.caption).foregroundStyle(PocketTheme.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(PocketTheme.secondary).accessibilityHidden(true)
            }.padding(14).frame(minHeight: 68)
        }.buttonStyle(.plain).disabled(!canChoose)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("received-library-\(library.id)")
    }
}
