// swift-tools-version: 6.0
// Vikram's native macOS app. `scripts/build_desktop.py` builds the release
// executable and assembles Vikram.app around it with the Python service.
import PackageDescription

let package = Package(
    name: "Vikram",
    platforms: [.macOS("27.0")],
    products: [
        .executable(name: "vikram-desktop", targets: ["Vikram"]),
    ],
    dependencies: [
        // The Terminal view's emulator (MIT). It runs the real `vikram` CLI.
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", exact: "1.11.2"),
    ],
    targets: [
        .target(name: "VikramKit"),
        .executableTarget(
            name: "Vikram",
            dependencies: ["VikramKit", .product(name: "SwiftTerm", package: "SwiftTerm")]),
        .testTarget(name: "VikramKitTests", dependencies: ["VikramKit"]),
    ],
    swiftLanguageModes: [.v6]
)
