// swift-tools-version: 6.0
import PackageDescription

// SwiftPM here, plain swiftc for the menu-bar app next door. That is a
// deliberate divergence, not drift.
//
// macos/QuernMenuBar/run-tests.sh explains why that component avoids SwiftPM:
// adopting it would mean restructuring sources that build.sh and
// release-menubar.sh depend on, and those two scripts produce a *signed,
// notarized* artifact. Disturbing a signing pipeline to gain test discovery
// is a poor trade.
//
// Neither half of that applies here. This binary is unsigned, built at setup
// time, and never notarized, and it is new code with no layout to preserve.
// What SwiftPM buys is a test target that runs with no device attached, which
// is the whole point of splitting capture from encoding from transport.
//
// Cost, measured: a cold build is ~7s against ~1.5s for a single swiftc
// invocation. That lands on `quern setup` and on CI, both of which can absorb
// it. There are no dependencies, so builds work offline with nothing to
// resolve or cache.
let package = Package(
    name: "QuernMedia",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "QuernMedia", targets: ["QuernMedia"]),
        .executable(name: "quern-media", targets: ["quern-media"]),
    ],
    targets: [
        .target(
            name: "QuernMedia",
            // The spike this is extracted from was written in Swift 5 mode.
            // Porting it and adopting strict concurrency in one step would
            // conflate two jobs; tighten this once the port is complete.
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        // Deliberately thin: argument parsing, wiring and lifetime, all of
        // which the library provides tested pieces for. Logic that lands here
        // is logic that cannot be tested.
        .executableTarget(
            name: "quern-media",
            dependencies: ["QuernMedia"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(
            name: "QuernMediaTests",
            dependencies: ["QuernMedia"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
    ]
)
