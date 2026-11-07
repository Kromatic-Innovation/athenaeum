// SPDX-License-Identifier: Apache-2.0
//
// promptwitness-se: a small Swift helper exposing a non-exportable macOS
// Secure Enclave P-256 signing key to the promptwitness Python adapter
// (src/promptwitness/secure_enclave.py, athenaeum#2153). Built on
// CryptoKit's `SecureEnclave.P256.Signing.PrivateKey`, which has no Python
// (or even C) binding -- this binary is the only way to reach it.
//
// See tools/promptwitness-se/README.md for build instructions.
// Codesigning, notarization, the real-device signature check, the
// second-Mac uselessness check and latency measurement are athenaeum#2239,
// NOT this tool.
//
// Design notes:
//
// - Key storage: the key's own opaque `dataRepresentation` blob, in a file
//   (mode 0600), never the macOS Keychain -- no entitlement is needed,
//   matching the issue's design.
// - No user-presence (Touch ID) gating: `SecureEnclave.P256.Signing.
//   PrivateKey()` is created with no access-control object at all, per
//   operator ruling 1 (docs/design/witness-ledger.md). That bare
//   initializer call, in `cmdCreate` below, is the one line in this file
//   that decides that question.
// - `sign` reads the payload from stdin to EOF and writes a DER-encoded
//   signature to stdout; `create` and `pubkey` write the base64 X9.63
//   uncompressed public-key point (`.x963Representation`) to stdout,
//   byte-identical
//   encodings either way. Both must stay byte-identical to what
//   `src/promptwitness/secure_enclave.py` expects on its side of the
//   pipe -- a mismatch would silently change every `key_id` the Python
//   adapter derives, with no error to signal it.
// - Every failure path prints one line to stderr and exits non-zero;
//   nothing here ever crashes or exits 0 on failure.
//   `SecureEnclave.isAvailable == false` is itself one such failure, not a
//   crash. MEASURED, because the first revision of this file assumed
//   otherwise: a GitHub-hosted `macos-latest` runner DOES have a usable
//   Secure Enclave -- `create` exits 0 there and returns a real 65-byte
//   X9.63 point -- so .github/workflows/promptwitness-se-macos.yml
//   exercises the real path on CI and keeps the degrade assertions for a
//   runner image that later drops enclave access. Either way nothing is
//   signed or notarized (athenaeum#2239).

import CryptoKit
import Foundation

// Exit codes, small and documented. The Python adapter
// (`secure_enclave.py`'s `_EXIT_NO_KEY`) matches `.noKey` exactly. A
// DISTINCT code for "no key file yet" vs. "something is wrong" mirrors the
// `signing.KeystoreItemAbsent` vs. plain `ledger.KeyUnavailable` split
// documented in src/promptwitness/signing.py: only the former is safe for
// `create` (without `--force`) to treat as "nothing to lose" -- collapsing
// the two is how a transient failure would get read as "the key is empty"
// and overwritten.
enum ExitCode: Int32 {
    case ok = 0
    case failure = 1 // Secure Enclave unavailable, or a tool/crypto failure.
    case noKey = 2 // No key file found where one was expected.
    case usage = 64 // Bad arguments or an unknown subcommand.
}

func fail(_ message: String, code: ExitCode = .failure) -> Never {
    FileHandle.standardError.write(("promptwitness-se: " + message + "\n").data(using: .utf8)!)
    exit(code.rawValue)
}

func printUsage() {
    print("""
    promptwitness-se - Secure Enclave key helper for promptwitness (athenaeum#2153)

    USAGE:
      promptwitness-se create --key-file <path> [--force]
      promptwitness-se sign   --key-file <path>   (payload on stdin, DER signature on stdout)
      promptwitness-se pubkey --key-file <path>   (base64 raw X9.63 public-key point on stdout)
      promptwitness-se --help

    Codesigning and notarization are athenaeum#2239, not this tool.
    """)
}

// MARK: - Argument parsing (plain CommandLine.arguments; no dependency)

struct Args {
    var keyFile: String?
    var force: Bool = false
}

func parseFlags(_ args: [String]) -> Args {
    var result = Args()
    var i = 0
    while i < args.count {
        switch args[i] {
        case "--key-file":
            i += 1
            guard i < args.count else { fail("--key-file requires a value", code: .usage) }
            result.keyFile = args[i]
        case "--force":
            result.force = true
        default:
            fail("unrecognized argument: \(args[i])", code: .usage)
        }
        i += 1
    }
    return result
}

func requireKeyFile(_ args: Args) -> String {
    guard let path = args.keyFile else {
        fail("--key-file is required", code: .usage)
    }
    return path
}

// MARK: - Key file I/O

// Reads the key's opaque dataRepresentation blob from `path` and rebuilds
// the Secure Enclave private key handle. A missing file is `.noKey` --
// there is nothing to sign with, not a malfunction; any other read
// failure, or a blob CryptoKit rejects, is `.failure`. Collapsing the two
// is exactly the mistake the `signing.KeystoreItemAbsent` vs.
// `ledger.KeyUnavailable` distinction (src/promptwitness/signing.py) warns
// against: it would let a `create` without `--force` overwrite a live key
// during a merely transient read failure.
func loadPrivateKey(path: String) -> SecureEnclave.P256.Signing.PrivateKey {
    let fm = FileManager.default
    guard fm.fileExists(atPath: path) else {
        fail("no key file at \(path)", code: .noKey)
    }
    guard let data = fm.contents(atPath: path) else {
        fail("key file at \(path) could not be read", code: .failure)
    }
    do {
        return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: data)
    } catch {
        fail("key file at \(path) is not a valid Secure Enclave key blob: \(error)", code: .failure)
    }
}

// Atomic write at mode 0600: write to a sibling temp file, then rename it
// over the destination. Same discipline `signing.generate_and_write_key`
// uses on the Python side, and for the identical reason -- a half-written
// key blob is a destroyed signing key, and every line it ever signed
// becomes permanently unverifiable with no copy anywhere to restore from.
//
// The last step is POSIX `rename(2)`, not `FileManager.moveItem`. Seer
// review on PR athenaeum#2358 caught the difference and it is not
// cosmetic: `moveItem` FAILS if the destination exists, so the obvious
// shape is to `removeItem` first -- which opens a window where the old key
// is gone and the new one is not yet in place, and a crash inside it
// destroys a non-exportable key with no copy anywhere. `rename(2)`
// replaces the destination atomically on every filesystem macOS supports,
// so no such window exists.
func writeKeyFileAtomically(_ data: Data, to path: String) {
    let directory = (path as NSString).deletingLastPathComponent
    let tmpPath = path + ".new-\(ProcessInfo.processInfo.processIdentifier)"
    do {
        if !directory.isEmpty {
            try FileManager.default.createDirectory(
                atPath: directory, withIntermediateDirectories: true
            )
        }
        try data.write(to: URL(fileURLWithPath: tmpPath), options: [.atomic])
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: tmpPath
        )
    } catch {
        try? FileManager.default.removeItem(atPath: tmpPath)
        fail("could not write key file at \(path): \(error)", code: .failure)
    }
    if rename(tmpPath, path) != 0 {
        let reason = String(cString: strerror(errno))
        try? FileManager.default.removeItem(atPath: tmpPath)
        fail("could not move the new key file into place at \(path): \(reason)", code: .failure)
    }
}

// The X9.63 uncompressed point (`0x04 || X || Y`, 65 bytes for P-256),
// base64-encoded -- must stay byte-identical to what
// `src/promptwitness/secure_enclave.py`'s `_public_key_bytes` expects.
// `create` and `pubkey` print it the same way so the Python adapter never
// needs to know which subcommand produced it.
//
// `x963Representation`, NOT `rawRepresentation`: CryptoKit's
// `rawRepresentation` is the bare 64-byte `X || Y` pair with no leading
// `0x04` tag, and the Python side rebuilds the key with
// `EllipticCurvePublicKey.from_encoded_point`, which accepts only the
// tagged X9.63 form. The 64-byte form would raise there -- every
// signature this helper produced on a real device would fail
// verification, and no test in this repo could see it, because the fake
// helper the Python tests run against emits the 65-byte form. The two
// property names differ by one byte of prefix and nothing else; keep
// them straight.
func printPublicKey(_ key: P256.Signing.PublicKey) {
    print(key.x963Representation.base64EncodedString())
}

// MARK: - Commands

func cmdCreate(_ args: Args) -> Never {
    // Argument validation BEFORE the device probe, in all three
    // subcommands. A malformed command line is a usage error (exit 64)
    // whatever hardware it was typed on, and a caller debugging its own
    // argv should not have the answer depend on whether this machine has
    // an enclave -- which also keeps the exit code stable between a real
    // Mac and the enclave-less CI runner that asserts it.
    let path = requireKeyFile(args)
    guard SecureEnclave.isAvailable else {
        fail("Secure Enclave is not available on this device", code: .failure)
    }
    if FileManager.default.fileExists(atPath: path) && !args.force {
        fail(
            "a key file already exists at \(path); refusing to replace it (pass --force)",
            code: .failure
        )
    }
    do {
        // No access-control object at all: no user-presence (Touch ID)
        // gating, per operator ruling 1 (docs/design/witness-ledger.md).
        let key = try SecureEnclave.P256.Signing.PrivateKey()
        writeKeyFileAtomically(key.dataRepresentation, to: path)
        printPublicKey(key.publicKey)
        exit(ExitCode.ok.rawValue)
    } catch {
        fail("could not create a Secure Enclave key: \(error)", code: .failure)
    }
}

func cmdSign(_ args: Args) -> Never {
    let path = requireKeyFile(args)
    guard SecureEnclave.isAvailable else {
        fail("Secure Enclave is not available on this device", code: .failure)
    }
    let key = loadPrivateKey(path: path)
    let payload = FileHandle.standardInput.readDataToEndOfFile()
    do {
        let signature = try key.signature(for: payload)
        FileHandle.standardOutput.write(signature.derRepresentation)
        exit(ExitCode.ok.rawValue)
    } catch {
        fail("could not sign with the Secure Enclave key: \(error)", code: .failure)
    }
}

func cmdPubkey(_ args: Args) -> Never {
    let path = requireKeyFile(args)
    guard SecureEnclave.isAvailable else {
        fail("Secure Enclave is not available on this device", code: .failure)
    }
    let key = loadPrivateKey(path: path)
    printPublicKey(key.publicKey)
    exit(ExitCode.ok.rawValue)
}

// MARK: - Entry point

let allArgs = Array(CommandLine.arguments.dropFirst())

if allArgs.first == "--help" || allArgs.first == "-h" {
    printUsage()
    exit(ExitCode.ok.rawValue)
}

if allArgs.isEmpty {
    printUsage()
    exit(ExitCode.usage.rawValue)
}

let subcommand = allArgs[0]
let flags = parseFlags(Array(allArgs.dropFirst()))

switch subcommand {
case "create":
    cmdCreate(flags)
case "sign":
    cmdSign(flags)
case "pubkey":
    cmdPubkey(flags)
default:
    fail("unknown subcommand: \(subcommand)", code: .usage)
}
