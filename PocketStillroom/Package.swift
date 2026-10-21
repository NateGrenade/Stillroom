// swift-tools-version: 6.0
import PackageDescription
let package = Package(
    name: "PocketStillroom",
    platforms: [.macOS(.v13), .iOS(.v17)],
    products: [.library(name: "PocketCore", targets: ["PocketCore"]), .library(name: "PocketAudio", targets: ["PocketAudio"])],
    targets: [.target(name: "PocketCore"), .target(name: "PocketAudio")],
    swiftLanguageModes: [.v5]
)
