// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "observability-swift-app",
    platforms: [
        .macOS(.v13)
    ],
    dependencies: [],
    targets: [
        .executableTarget(
            name: "observability-swift-app",
            dependencies: [],
            path: "Sources"
        )
    ]
)
