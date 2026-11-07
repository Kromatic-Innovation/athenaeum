// swift-tools-version:5.9
// SPDX-License-Identifier: Apache-2.0
import PackageDescription

let package = Package(
    name: "promptwitness-se",
    // CryptoKit's `SecureEnclave` API (`SecureEnclave.isAvailable`,
    // `SecureEnclave.P256.Signing.PrivateKey`) has existed since macOS
    // 10.15, but the `PrivateKey(dataRepresentation:)` initializer this
    // helper relies on to reload a key from its stored blob was added in
    // macOS 11 -- that is the real floor for this tool, not a merely
    // conservative round number.
    platforms: [.macOS(.v11)],
    products: [
        .executable(name: "promptwitness-se", targets: ["promptwitness-se"])
    ],
    // No dependencies, deliberately: the acceptance criterion (athenaeum#2153
    // AC1) is that this builds in CI with no signing identity or secret, and
    // the same "no surprises on a CI runner" discipline extends to needing
    // no network access to resolve a SwiftPM dependency graph either.
    targets: [
        .executableTarget(
            name: "promptwitness-se",
            path: "Sources/promptwitness-se"
        )
    ]
)
