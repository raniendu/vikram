// swift-tools-version: 6.0
// Vikram's native macOS app. `scripts/build_desktop.py` builds the release
// executable and assembles Vikram.app around it with the Python service.
import PackageDescription

let package = Package(
    name: "Vikram",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "vikram-desktop", targets: ["Vikram"]),
    ],
    targets: [
        .target(name: "VikramKit"),
        .executableTarget(name: "Vikram", dependencies: ["VikramKit"]),
        .testTarget(name: "VikramKitTests", dependencies: ["VikramKit"]),
    ],
    swiftLanguageModes: [.v6]
)
