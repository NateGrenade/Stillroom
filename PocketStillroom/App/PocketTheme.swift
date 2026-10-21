import SwiftUI

/// Stillroom's desktop palette, with brighter secondary text for phone reading.
enum PocketTheme {
    static let background = Color(hex: 0x121116)
    static let surface = Color(hex: 0x1A191F)
    static let elevated = Color(hex: 0x272131)
    static let player = Color(hex: 0x1B1721)
    static let text = Color(hex: 0xEEEBF3)
    static let secondary = Color(hex: 0xB7B2C1)
    static let accent = Color(hex: 0xC6B2F0)
    static let border = Color(hex: 0x4D4359)
    static let track = Color(hex: 0x82758F)
    static let onAccent = Color(hex: 0x21192D)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 255) / 255, green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255, opacity: 1)
    }
}

extension View {
    func pocketCard() -> some View {
        background(PocketTheme.surface, in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(PocketTheme.border.opacity(0.55), lineWidth: 1))
    }
}

struct PocketEyebrow: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).font(.caption2.weight(.semibold)).tracking(1.5)
            .foregroundStyle(PocketTheme.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

struct PocketBrandMark: View {
    var body: some View {
        HStack(spacing: 3) {
            ForEach(Array([16.0, 28.0, 22.0, 11.0].enumerated()), id: \.offset) { _, height in
                Capsule().fill(PocketTheme.accent).frame(width: 3, height: height)
            }
        }.frame(width: 24, height: 30).accessibilityHidden(true)
    }
}

/// A native version of the desktop's quiet orb and ridges, not album artwork.
struct PocketLandscape: View {
    var variant = 0
    private var sky: Color { [Color(hex: 0x635278), Color(hex: 0x4F6468), Color(hex: 0x65535E)][abs(variant % 3)] }
    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width, height = geometry.size.height
            ZStack {
                LinearGradient(colors: [sky, Color(hex: 0x272035)], startPoint: .topLeading, endPoint: .bottomTrailing)
                Circle().fill(Color(hex: 0xD8C6D8)).frame(width: width * 0.25, height: width * 0.25).position(x: width * 0.68, y: height * 0.3)
                Ellipse().fill(Color(hex: 0x4A3B60).opacity(0.8)).frame(width: width * 1.5, height: height * 0.85).rotationEffect(.degrees(22)).position(x: width * 0.28, y: height * 0.94)
                Ellipse().fill(Color(hex: 0x31253F)).frame(width: width * 1.6, height: height * 0.9).rotationEffect(.degrees(-24)).position(x: width * 0.78, y: height * 1.1)
                Ellipse().fill(Color(hex: 0x201A2A)).frame(width: width * 1.7, height: height * 0.7).rotationEffect(.degrees(12)).position(x: width * 0.35, y: height * 1.18)
            }.clipped()
        }.accessibilityHidden(true)
    }
}

struct PocketTrackTile: View {
    let id: String
    var loop = false
    var playing = false
    var body: some View {
        ZStack {
            PocketLandscape(variant: id.utf8.reduce(0) { ($0 + Int($1)) % 3 })
            if playing || loop {
                Image(systemName: playing ? "waveform" : "repeat")
                    .font(.subheadline.weight(.semibold)).foregroundStyle(PocketTheme.text)
                    .padding(6).background(PocketTheme.background.opacity(0.75), in: RoundedRectangle(cornerRadius: 6))
            }
        }.clipShape(RoundedRectangle(cornerRadius: 9)).accessibilityHidden(true)
    }
}

struct PocketActionStyle: ButtonStyle {
    var prominent = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.subheadline.weight(.semibold))
            .foregroundStyle(prominent ? PocketTheme.onAccent : PocketTheme.accent)
            .padding(.horizontal, 16).frame(minHeight: 44)
            .background(prominent ? PocketTheme.accent : PocketTheme.elevated, in: RoundedRectangle(cornerRadius: 10))
            .opacity(enabled ? (configuration.isPressed ? 0.75 : 1) : 0.45)
    }
}

struct PocketIconStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    @ScaledMetric(relativeTo: .body) private var iconSize = 18.0
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: min(iconSize, 26), weight: .medium)).foregroundStyle(PocketTheme.secondary)
            .frame(minWidth: 44, minHeight: 44)
            .background(configuration.isPressed ? PocketTheme.elevated : Color.clear, in: RoundedRectangle(cornerRadius: 10))
            .opacity(enabled ? 1 : 0.4)
    }
}

enum PocketFormatting {
    static func time(_ value: Double) -> String {
        let seconds = Int(max(0, value.isFinite ? value : 0))
        return "\(seconds / 60):\(String(format: "%02d", seconds % 60))"
    }
}
