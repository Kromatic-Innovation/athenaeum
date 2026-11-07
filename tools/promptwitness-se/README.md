# promptwitness-se

A small Swift helper exposing a non-exportable macOS Secure Enclave P-256
signing key to the `promptwitness` Python package
(`src/promptwitness/secure_enclave.py`), for the `hardware-nonexportable`
signing tier of the prompt witness ledger (athenaeum#2153).

CryptoKit's `SecureEnclave.P256.Signing.PrivateKey` has no Python (or even
C) binding; this binary is the only way to reach it. The key's opaque
`dataRepresentation` blob is stored in a plain file (mode 0600), never the
macOS Keychain, so no extra entitlement is needed.

## Build

```sh
cd tools/promptwitness-se
swift build              # debug build, binary at .build/debug/promptwitness-se
swift build -c release   # release build, binary at .build/release/promptwitness-se
```

No external SwiftPM dependencies — the dependency graph is deliberately
empty, so building needs no network access to resolve one. Requires
macOS 11+ and a Swift toolchain with CryptoKit's `SecureEnclave` API.

## Usage

```sh
promptwitness-se create --key-file <path> [--force]
promptwitness-se sign   --key-file <path>   # payload on stdin, DER signature on stdout
promptwitness-se pubkey --key-file <path>   # base64 raw X9.63 public-key point on stdout
promptwitness-se --help
```

`create` refuses to overwrite an existing key file unless `--force` is
given: replacing a signing key makes every line it ever signed permanently
unverifiable, and there is no copy anywhere to restore one from. The key
is created with **no access-control object at all** — no Touch ID /
user-presence requirement (operator ruling 1,
`docs/design/witness-ledger.md`).

Exit codes: `0` success, `2` no key file found where one was expected (the
Python adapter maps this to its `KeystoreItemAbsent`-equivalent, "you
never created a key"), `64` a usage error (bad arguments or an unknown
subcommand), `1` anything else — the Secure Enclave is unavailable, a key
file is unreadable or not a valid blob, or the signing operation itself
failed.

## What this does NOT do

Codesigning, notarization, the real-device signature check, the
second-Mac uselessness check, and latency measurement are **athenaeum#2239**,
not this tool. This helper builds and runs unsigned; CI
(`.github/workflows/promptwitness-se-macos.yml`) proves only that it
builds and that every subcommand fails cleanly (non-zero exit, one-line
stderr diagnostic, never a crash) on a runner with no Secure Enclave —
which is every GitHub-hosted macOS runner today.
