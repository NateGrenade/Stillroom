import SwiftUI
import UIKit

/// Equal distances mean equal ratios: half speed, normal, double speed.
/// The displayed rate remains continuous; snapping happens on commit.
enum PocketSpeedScale {
    static let presets: [Double] = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]
    static func position(for rate: Double) -> Float {
        Float(log2(min(2, max(0.5, rate.isFinite ? rate : 1))))
    }
    static func rate(at position: Float) -> Double {
        pow(2, Double(min(1, max(-1, position.isFinite ? position : 0))))
    }
    static func text(_ rate: Double) -> String {
        var value = String(format: "%.2f", rate)
        while value.last == "0" { value.removeLast() }
        if value.last == "." { value.removeLast() }
        return value + "×"
    }
    static func snapped(_ rate: Double) -> Double {
        guard let nearest = presets.min(by: { abs($0 - rate) < abs($1 - rate) }), abs(nearest - rate) <= 0.025 else { return rate }
        return nearest
    }
}

final class PocketRateSlider: UISlider {
    var rate: Double {
        get { PocketSpeedScale.rate(at: value) }
        set { value = PocketSpeedScale.position(for: newValue); updateSpokenRate() }
    }
    func updateSpokenRate() { accessibilityValue = PocketSpeedScale.text(rate) }
    override func accessibilityIncrement() { adjust(by: 0.05) }
    override func accessibilityDecrement() { adjust(by: -0.05) }
    private func adjust(by amount: Double) {
        guard isEnabled else { return }
        rate = min(2, max(0.5, (rate * 100).rounded() / 100 + amount))
        sendActions(for: .valueChanged)
        sendActions(for: .editingDidEnd)
    }
}

/// Native thumb geometry places both ticks and labels. No guessed thumb inset
/// or separate SwiftUI layout can drift away from the real touch control.
final class PocketSpeedSliderView: UIView {
    let slider = PocketRateSlider()
    let tickRates = PocketSpeedScale.presets
    private(set) var ticks: [UIView] = []
    private(set) var labels: [(rate: Double, view: UILabel)] = []

    override init(frame: CGRect) {
        super.init(frame: frame)
        slider.minimumValue = -1; slider.maximumValue = 1; slider.rate = 1
        slider.isContinuous = true
        slider.tintColor = UIColor(PocketTheme.accent)
        slider.minimumTrackTintColor = UIColor(PocketTheme.accent)
        slider.maximumTrackTintColor = UIColor(PocketTheme.track)
        slider.thumbTintColor = UIColor(PocketTheme.text)
        // Rates increase from left to right in every locale, matching playback.
        slider.semanticContentAttribute = .forceLeftToRight
        slider.accessibilityLabel = "Playback speed"
        slider.accessibilityIdentifier = "playback-speed"
        addSubview(slider)
        for _ in tickRates {
            let tick = UIView(); tick.backgroundColor = UIColor(PocketTheme.secondary)
            tick.isUserInteractionEnabled = false; tick.isAccessibilityElement = false
            addSubview(tick); ticks.append(tick)
        }
        for rate in [0.5, 1.0, 2.0] {
            let label = UILabel(); label.text = PocketSpeedScale.text(rate)
            label.textColor = UIColor(PocketTheme.secondary); label.textAlignment = .center
            label.adjustsFontForContentSizeCategory = true
            label.isUserInteractionEnabled = false; label.isAccessibilityElement = false
            addSubview(label); labels.append((rate, label))
        }
        refreshFonts()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    private func refreshFonts() {
        let font = UIFont.preferredFont(forTextStyle: .caption2, compatibleWith: traitCollection)
        labels.forEach { $0.view.font = font; $0.view.sizeToFit() }
        invalidateIntrinsicContentSize(); setNeedsLayout()
    }
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection)
        if previousTraitCollection?.preferredContentSizeCategory != traitCollection.preferredContentSizeCategory { refreshFonts() }
    }
    override var intrinsicContentSize: CGSize {
        CGSize(width: UIView.noIntrinsicMetric, height: geometry(width: bounds.width > 0 ? bounds.width : 280).height)
    }
    override func sizeThatFits(_ size: CGSize) -> CGSize {
        CGSize(width: size.width, height: geometry(width: size.width).height)
    }
    private func geometry(width: CGFloat) -> (slider: CGRect, ticks: [CGFloat], labels: [CGRect], height: CGFloat) {
        let sizes = labels.map { $0.view.intrinsicContentSize }
        let inset = max(4, (sizes.map(\.width).max() ?? 0) / 2 + 2)
        let frame = CGRect(x: inset, y: 0, width: max(0, width - 2 * inset), height: 44)
        let nativeBounds = CGRect(origin: .zero, size: frame.size)
        let track = slider.trackRect(forBounds: nativeBounds)
        func center(_ rate: Double) -> CGFloat {
            frame.minX + slider.thumbRect(forBounds: nativeBounds, trackRect: track, value: PocketSpeedScale.position(for: rate)).midX
        }
        var rectangles = zip(labels, sizes).map { label, size in
            CGRect(x: center(label.rate) - size.width / 2, y: 52, width: size.width, height: size.height)
        }
        // Preserve readable accessibility text on narrow screens instead of
        // shrinking it or moving labels away from their actual rate positions.
        if rectangles[0].maxX + 6 > rectangles[1].minX || rectangles[1].maxX + 6 > rectangles[2].minX {
            rectangles[1].origin.y += (sizes.map(\.height).max() ?? 0) + 4
        }
        return (frame, tickRates.map(center), rectangles, (rectangles.map(\.maxY).max() ?? 66) + 2)
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        // Also resolve fonts during layout: a newly hosted SwiftUI view can
        // receive its content-size override before UIKit sends a trait callback.
        let font = UIFont.preferredFont(forTextStyle: .caption2, compatibleWith: traitCollection)
        if labels.contains(where: { $0.view.font.pointSize != font.pointSize }) { refreshFonts() }
        let metrics = geometry(width: bounds.width)
        slider.frame = metrics.slider
        for (center, tick) in zip(metrics.ticks, ticks) {
            tick.frame = CGRect(x: center - 0.5, y: 43, width: 1, height: 5)
        }
        for (label, frame) in zip(labels, metrics.labels) {
            label.view.frame = frame
        }
    }
}

struct PocketSpeedControl: UIViewRepresentable {
    @Binding var rate: Double
    var onEditingChanged: (Bool) -> Void
    @Environment(\.isEnabled) private var enabled

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> PocketSpeedSliderView {
        let view = PocketSpeedSliderView()
        view.slider.addTarget(context.coordinator, action: #selector(Coordinator.changed(_:)), for: .valueChanged)
        view.slider.addTarget(context.coordinator, action: #selector(Coordinator.began), for: .touchDown)
        view.slider.addTarget(context.coordinator, action: #selector(Coordinator.ended), for: [.touchUpInside, .touchUpOutside, .touchCancel, .editingDidEnd])
        return view
    }
    func updateUIView(_ view: PocketSpeedSliderView, context: Context) {
        context.coordinator.parent = self
        view.slider.rate = rate; view.slider.isEnabled = enabled
        view.alpha = enabled ? 1 : 0.45
    }
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: PocketSpeedSliderView, context: Context) -> CGSize? {
        uiView.sizeThatFits(CGSize(width: proposal.width ?? 280, height: .greatestFiniteMagnitude))
    }
    final class Coordinator: NSObject {
        var parent: PocketSpeedControl
        init(_ parent: PocketSpeedControl) { self.parent = parent }
        @objc func changed(_ slider: PocketRateSlider) {
            slider.updateSpokenRate(); parent.rate = slider.rate
        }
        @objc func began() { parent.onEditingChanged(true) }
        @objc func ended() { parent.onEditingChanged(false) }
    }
}
