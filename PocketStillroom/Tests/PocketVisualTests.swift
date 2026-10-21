import XCTest
import SwiftUI
import UIKit
@testable import PocketStillroom

/// Actual native view rendering in disposable stores. Images are review artifacts,
/// not pixel-golden tests; no device library or audio session is opened.
final class PocketVisualTests: XCTestCase {
    @MainActor func testLibraryExpandedPlayerAndPanelsRenderAtCompactLandscapeAndLargestType() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketVisual-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let model = PocketAppModel(libraryRoot: root)
        model.catalog = sampleCatalog()
        let cases: [(String, CGSize, DynamicTypeSize, Bool)] = [
            ("library-pro", CGSize(width: 402, height: 874), .large, false),
            ("library-compact", CGSize(width: 320, height: 568), .large, false),
            ("speed-compact", CGSize(width: 320, height: 568), .large, true),
            ("speed-landscape", CGSize(width: 874, height: 402), .large, true),
            ("library-largest-type", CGSize(width: 402, height: 874), .accessibility5, false),
            ("speed-largest-type", CGSize(width: 402, height: 874), .accessibility5, true)
        ]
        for (name, size, type, expanded) in cases {
            try await capture(name, size: size, type: type,
                              view: PocketLibraryView(model: model, audio: model.audio, initiallyExpandedSpeed: expanded))
        }
        try await capture("advanced-pro", size: CGSize(width: 402, height: 874), type: .large,
                          view: PocketAdvancedPanel(model: model, audio: model.audio, loopA: .constant(nil), loopB: .constant(nil)))
        try await capture("advanced-largest-type", size: CGSize(width: 402, height: 874), type: .accessibility5,
                          view: PocketAdvancedPanel(model: model, audio: model.audio, loopA: .constant(nil), loopB: .constant(nil)))
        try await capture("help-pro", size: CGSize(width: 402, height: 874), type: .large, view: PocketHelpView())
        model.preview = PocketImportPreview(manifest: model.catalog.manifest, tracksToAdd: 5, playlistsToAdd: 1, duplicateTracks: 0, requiredBytes: 8_400_000, audioBytes: 6_364)
        try await capture("import-review-compact", size: CGSize(width: 320, height: 568), type: .large, view: PocketImportReview(model: model))
        try await capture("import-review-largest-type", size: CGSize(width: 402, height: 874), type: .accessibility5, view: PocketImportReview(model: model))
        model.preview = nil
        model.catalog = PocketCatalog()
        try await capture("empty-compact", size: CGSize(width: 320, height: 568), type: .large,
                          view: PocketLibraryView(model: model, audio: model.audio))
        let recoveryParent = root.appendingPathComponent("recovery-app-parent")
        let damagedRoot = recoveryParent.appendingPathComponent("PocketStillroom")
        try FileManager.default.createDirectory(at: damagedRoot, withIntermediateDirectories: true)
        try Data("generated damaged catalogue".utf8).write(to: damagedRoot.appendingPathComponent("catalog.json"))
        let recoveryModel = PocketAppModel(libraryParent: recoveryParent)
        XCTAssertTrue(recoveryModel.recoveryRequired)
        recoveryModel.message = nil
        try await capture("recovery-compact", size: CGSize(width: 320, height: 568), type: .large,
                          view: PocketLibraryView(model: recoveryModel, audio: recoveryModel.audio))
        XCTAssertFalse(model.audio.isPlaying)
        XCTAssertNil(model.audio.currentTrack)
        XCTAssertNil(model.message)
    }

    @MainActor func testReceivedLibraryPickerRendersEmptyReceivedAndRecoveryStatesAtCompactAndLargestType() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketReceivedVisual-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
        let model = PocketAppModel(libraryRoot: root.appendingPathComponent("library"), documentsDirectory: documents)
        await model.refreshReceivedLibraries()
        XCTAssertTrue(model.receivedLibraries.isEmpty)
        try await capture("received-picker-empty-compact", size: CGSize(width: 320, height: 568), type: .large,
                          view: PocketImportPicker(model: model, chooseReceived: { _ in XCTFail("Rendering must not select a file") }, browseFiles: { XCTFail("Rendering must not open Files") }))
        var bytes = Data("STLRMB01".utf8), length = UInt32(2).bigEndian
        withUnsafeBytes(of: &length) { bytes.append(contentsOf: $0) }; bytes.append(Data("{}".utf8))
        for name in ["Stillroom-2026-10-21-01234567-89AB-CDEF-0123-456789ABCDEF.stillroomlibrary", "My saved library (1).stillroomlibrary"] {
            let url = documents.appendingPathComponent(name)
            try bytes.write(to: url)
            try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: 1_792_540_800)], ofItemAtPath: url.path)
        }
        try bytes.write(to: documents.appendingPathComponent(".not-finished.partial"))
        await model.refreshReceivedLibraries()
        XCTAssertEqual(model.receivedLibraries.count, 2)
        for (name, size, type) in [("received-picker-compact", CGSize(width: 320, height: 568), DynamicTypeSize.large),
                                   ("received-picker-largest-type", CGSize(width: 402, height: 874), .accessibility5)] {
            try await capture(name, size: size, type: type,
                              view: PocketImportPicker(model: model, chooseReceived: { _ in XCTFail("Rendering must not import") }, browseFiles: {}))
        }
        let parent = root.appendingPathComponent("recovery-parent")
        let damaged = parent.appendingPathComponent("PocketStillroom")
        try FileManager.default.createDirectory(at: damaged, withIntermediateDirectories: true)
        try Data("generated damaged catalogue".utf8).write(to: damaged.appendingPathComponent("catalog.json"))
        let recovery = PocketAppModel(libraryParent: parent, documentsDirectory: documents)
        recovery.message = nil
        await recovery.refreshReceivedLibraries()
        XCTAssertTrue(recovery.recoveryRequired)
        try await capture("received-picker-recovery-compact", size: CGSize(width: 320, height: 568), type: .large,
                          view: PocketImportPicker(model: recovery, chooseReceived: { _ in XCTFail("Rendering must not restore") }, browseFiles: {}))
        XCTAssertNil(model.preview); XCTAssertTrue(model.catalog.tracks.isEmpty)
    }

    @MainActor func testPlaylistPresentationKeepsImportedOrderAndSettingsRemainIndependentOfViews() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PocketVisualOrder-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let model = PocketAppModel(libraryRoot: root)
        let catalog = sampleCatalog()
        model.catalog = catalog
        model.selectedPlaylist = catalog.playlists[0].id
        XCTAssertEqual(model.visibleTracks.map(\.id), catalog.playlists[0].trackIds)
        model.selectedPlaylist = ""
        XCTAssertEqual(model.visibleTracks.map(\.id), catalog.tracks.map(\.id))
        model.audio.setPlaybackRate(1.37)
        model.audio.setPitchFollowsSpeed(false)
        model.audio.setReverb(0.63)
        model.savePlaybackSettings()
        let reopened = PocketAppModel(libraryRoot: root)
        XCTAssertEqual(reopened.audio.playbackRate, 1.37)
        XCTAssertFalse(reopened.audio.pitchFollowsSpeed)
        XCTAssertEqual(reopened.audio.reverb, 0.63)
        XCTAssertFalse(reopened.audio.isPlaying)
        XCTAssertNil(reopened.message)
    }

    @MainActor func testReadableThemeContrastOnEveryTextSurface() throws {
        for surface in [PocketTheme.background, PocketTheme.surface, PocketTheme.elevated, PocketTheme.player] {
            for foreground in [PocketTheme.text, PocketTheme.secondary, PocketTheme.accent] {
                XCTAssertGreaterThanOrEqual(try contrast(foreground, surface), 4.5)
            }
        }
        XCTAssertGreaterThanOrEqual(try contrast(PocketTheme.onAccent, PocketTheme.accent), 4.5)
    }

    func testBundledHelpAndUnrecordedVideosAlwaysHaveWrittenFallbacks() throws {
        for name in ["README", "Setup", "UseAndImport", "RefreshAndRecovery", "Troubleshooting", "Sources"] {
            let url = try XCTUnwrap(Bundle.main.url(forResource: name, withExtension: "md"))
            XCTAssertFalse(try String(contentsOf: url, encoding: .utf8).isEmpty)
        }
        let url = try XCTUnwrap(Bundle.main.url(forResource: "Videos", withExtension: "json"))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let videos = try XCTUnwrap(json["videos"] as? [[String: Any]])
        XCTAssertEqual(videos.count, 5)
        for video in videos {
            XCTAssertEqual(video["status"] as? String, "not_recorded")
            XCTAssertTrue(video["url"] is NSNull)
            let fallback = try XCTUnwrap(video["writtenFallback"] as? String)
            XCTAssertNotNil(Bundle.main.url(forResource: (fallback as NSString).deletingPathExtension, withExtension: "md"))
        }
    }

    @MainActor private func capture<V: View>(_ name: String, size: CGSize, type: DynamicTypeSize, view: V) async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(origin: .zero, size: size)
        let controller = UIHostingController(rootView: view.environment(\.dynamicTypeSize, type))
        window.rootViewController = controller
        window.isHidden = false
        defer { window.isHidden = true }
        controller.view.frame = window.bounds
        controller.view.setNeedsLayout(); controller.view.layoutIfNeeded()
        // SwiftUI preference-driven dock sizing and native navigation settle on
        // subsequent main-run-loop passes, as on screen.
        try await Task.sleep(nanoseconds: 120_000_000)
        controller.view.layoutIfNeeded()
        XCTAssertEqual(controller.view.bounds.width, size.width, accuracy: 1)
        XCTAssertEqual(controller.view.bounds.height, size.height, accuracy: 1)
        for scroll in descendants(controller.view).compactMap({ $0 as? UIScrollView }) where scroll.bounds.width > 0 && scroll.bounds.height > 0 {
            if scroll.alwaysBounceVertical || scroll.contentSize.height > scroll.bounds.height + 1 {
                XCTAssertLessThanOrEqual(scroll.contentSize.width, scroll.bounds.width + 1, "Vertical content must not require sideways scrolling: \(name)")
            }
        }
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            XCTAssertTrue(window.drawHierarchy(in: window.bounds, afterScreenUpdates: true), "Native view must render: \(name)")
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name; attachment.lifetime = .keepAlways
        add(attachment)
        let scrolls = descendants(controller.view).compactMap { $0 as? UIScrollView }.filter {
            $0.bounds.height > 0 && $0.contentSize.height > $0.bounds.height + 1
        }
        if !scrolls.isEmpty {
            for scroll in scrolls {
                XCTAssertTrue(scroll.isScrollEnabled, "Overflowing content must remain reachable: \(name)")
                let bottom = max(-scroll.adjustedContentInset.top, scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)
                scroll.setContentOffset(CGPoint(x: scroll.contentOffset.x, y: bottom), animated: false)
            }
            try await Task.sleep(nanoseconds: 80_000_000)
            let bottomImage = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
                XCTAssertTrue(window.drawHierarchy(in: window.bounds, afterScreenUpdates: true))
            }
            let bottom = XCTAttachment(image: bottomImage)
            bottom.name = name + "-scrolled"; bottom.lifetime = .keepAlways
            add(bottom)
        }
    }

    private func descendants(_ view: UIView) -> [UIView] { view.subviews.flatMap { [$0] + descendants($0) } }
    private func sampleCatalog() -> PocketCatalog {
        let tracks = (0..<5).map { index in
            PocketTrack(id: String(repeating: "\(index + 1)", count: 32),
                        title: ["Windowlight", "A long title that should remain readable without colliding with the duration", "Northbound quiet", "The last soft hour", "Windowlight · favorite moment"][index],
                        artist: index == 1 ? "A fictional artist with a longer name" : "Generated preview",
                        kind: index == 4 ? "loop" : "original", duration: index == 4 ? 4 : 8, fileDuration: 8,
                        loopStart: index == 4 ? 2 : 0, loopEnd: index == 4 ? 6 : nil,
                        audioFile: "audio/" + String(repeating: "a", count: 32) + ".m4a")
        }
        return PocketCatalog(tracks: tracks, playlists: [.init(id: String(repeating: "b", count: 32), name: "Slow mornings", trackIds: [tracks[2].id, tracks[0].id, tracks[4].id])])
    }
    @MainActor private func contrast(_ lhs: Color, _ rhs: Color) throws -> Double {
        func luminance(_ color: Color) throws -> Double {
            var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            XCTAssertTrue(UIColor(color).resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark)).getRed(&r, green: &g, blue: &b, alpha: &a))
            func linear(_ v: CGFloat) -> Double { let d = Double(v); return d <= 0.04045 ? d / 12.92 : pow((d + 0.055) / 1.055, 2.4) }
            return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
        }
        let l = try luminance(lhs), r = try luminance(rhs)
        return (max(l, r) + 0.05) / (min(l, r) + 0.05)
    }
}
