<!-- SPDX-License-Identifier: Apache-2.0 -->

# The prompt witness ledger, witnessed provenance, and external anchoring

**Status:** DESIGN OF RECORD. Epic athenaeum#2144. This document is committed by
athenaeum#2147 (the foundational `promptwitness` package slice) from the epic
body and its child issue bodies — the original spec was a local-only file and
is not available to builders, so this page is now the canonical source. Every
builder working on a child issue (athenaeum#2149, athenaeum#2150, athenaeum#2151,
and the rest listed in [§6](#6-children-and-sequencing)) reads this page rather
than re-deriving the design from the epic thread.

Companion to [`docs/design/security-posture.md`](security-posture.md) (athenaeum's
general sensitivity/audience posture — a different boundary from the one this
page states) and [`docs/reference/cli.md`](../reference/cli.md) (the generated
`promptwitness status` reference).

---

## 1. Summary

A harness hook writes a hash-chained, optionally signed, no-plaintext witness
line for every prompt, in a new top-level package `promptwitness`. Athenaeum's
intake joins `user-stated` transcript utterances to those lines and records a
new `witnessed` channel. The chain head is anchored with OpenTimestamps
periodically in-session, at SessionEnd, and nightly, with a mandatory
local-clock fallback. **Nothing gates on the result yet** — every witness miss
is surfaced as an incident, not a blocker.

## 2. Problem

`transcript_verify.classify_backfill_claim` labels a claim `user-stated`
because a `type: user` record in the session transcript JSONL contains it.
Nothing outside the transcript witnesses that a human actually typed it, and
the model's own Bash tool can write that file. "Claude will not deliberately
tamper" is rejected as a design basis: prompt injection or misalignment can
violate that assumption without Claude ever choosing to.

## 3. Security boundary (normative)

The witness ledger is written by the `UserPromptSubmit` hook, which runs as
the same OS user, with the same environment, as the model's Bash tool. **With
a separate OS user excluded, no file and no key on this machine is outside
the model's reach.** Any process running as the user can run the same append
code, recompute a valid chain hash, and ask any key to sign. `chflags uappnd`
/ `chattr +a` can be cleared by the file's owner; only `sappnd` cannot, and it
needs root.

The design does **not** claim that a ledger line proves a human typed the
prompt. It claims only what each mechanism shows:

| Mechanism | What it proves | What it does NOT prove |
|---|---|---|
| Hash chain | Lines were not edited, removed or reordered after a later line was appended, unless the forger rewrote everything after the edit. | Who appended a line. |
| Hook write timing | A genuine line is written before the model sees the prompt. | That a same-session model did not also append a forged line. |
| Entry-channel signals | The harness reported itself as interactive/attended with a TTY. | Spoof-proofing: a same-user process can launch `claude` under a pty with the env set. |
| Signature, `software-file` | Signed by the holder of that key file (readable by the user). | That the signer was the hook. Key theft is undetectable. |
| Signature, `os-keystore` | As `software-file`, and the key is encrypted at rest. | The same. The user's session can unlock it without a prompt. |
| Signature, `hardware-nonexportable` (Secure Enclave, TPM) | Signed on this physical device; the key cannot be copied off it. | That the hook did the signing. Any user process can request a signature. |
| External anchor (OpenTimestamps, RFC 3161) | The chain head, and every line before it, existed by the anchor time. | Anything about lines appended after the last anchor. |
| `local-clock` stamp | Nothing independent; the machine's claimed time. | Anything. It is a fallback marker, not evidence. |

Per-signature user presence (`hardware-presence`) would prove physical
presence, but [ruling 1](#5-operator-rulings-2026-10-28) declines it; the
protocol keeps the seam only.

**Attack windows that stay open** (honest limits; never presented as closed):

- **(a) Live in-session forgery.** A misaligned or prompt-injected model
  appends a forged human-kind line before the next anchor, and edits the
  transcript to match. Periodic in-session anchoring (ruling 2, default every
  15 minutes) bounds the window to the interval; cross-checks (`promptId`,
  `origin.kind`, `entrypoint`, record order, per-session monotonic
  `turn`/`ts`) raise the bar. Only presence-gated signing would close it, and
  ruling 1 declines that, so this window is open by operator choice.
- **(b) Deleting the whole ledger.** Detectable only through anchor records
  copied off-machine (athenaeum#2040 commits them into the pushed knowledge
  repo).
- **(c) Editing the hook configuration** (`settings.json`). Out of scope.
  Every line records `hook_version`, so an un-witnessed gap is visible as
  missing lines.

`witnessed` is defined narrowly: *a witness line written by the harness hook
for an interactive, human-kind prompt (or slash-command arguments) whose
digest matches, with an intact chain.* It does **not** mean "a verified human
identity".

## 4. Architecture summary

**Package boundary.** A new top-level package `src/promptwitness/`, in
athenaeum's wheel alongside `src/athenaeum/`. Zero imports from `athenaeum`
(AST-scan test, `tests/test_promptwitness_boundary.py`); standard library only
at import time. Optional dependencies are lazy: `cryptography` (extra
`witness-sign = ["cryptography>=42,<51"]`), `opentimestamps` (extra
`witness-anchor = ["opentimestamps>=0.4"]`). Dependency direction is one way
only: `athenaeum` → `promptwitness`. Athenaeum adapts promptwitness types onto
its own seams (`Sealer`, `SigningKeyProvider`, config resolvers) — it never
hands its own types the other way. The package lives at the top level (not
`athenaeum.witness`) so a later extraction (athenaeum#2162) is a directory
move, not an import rename. The module contains no operator paths, no
1Password item names, no `~/knowledge` references and no Kromatic
identifiers.

| Module | Responsibility | Owning issue |
|---|---|---|
| `promptwitness.canon` | `normalize` (byte-identical to `transcript_verify._normalize`), `digest -> "sha256:<hex>"`, `canonical_json` (sorted keys, `(",", ":")`, `ensure_ascii=False`, UTF-8) | athenaeum#2147 |
| `promptwitness.ledger` | `WitnessLine` v1, `append_line`, `iter_lines`, `verify_chain`, `LedgerIndex` (by `prompt_id` and `(session, prompt_digest)`) | athenaeum#2147 |
| `promptwitness.cli` | `promptwitness status` (and the later subcommands below) | athenaeum#2147 |
| `promptwitness.harness.claude_code` | Hook stdin/env → `TurnEvent`; wrapper table; channel detection; transcript reader | athenaeum#2149 |
| `promptwitness.signing` | `KeyProvider` protocol and adapters (`none`, `software-file`, …) | athenaeum#2150, athenaeum#2152, athenaeum#2153, athenaeum#2154 |
| `promptwitness.anchor` | `Anchorer` protocol; `LocalClockAnchorer`, `OpenTimestampsAnchorer`, `Rfc3161Anchorer` | athenaeum#2151, athenaeum#2157 |
| `promptwitness.verify` / `.demo` | Per-turn verdicts; static HTML demo | athenaeum#2155 |

### Ledger (schema v1)

Directory `$XDG_STATE_HOME/promptwitness/<harness>/` (fallback
`~/.local/state/promptwitness/<harness>/`, override `PROMPTWITNESS_DIR`),
deliberately **not** in the knowledge repo (prompt digests are
dictionary-recoverable) and not under `~/.claude`. Monthly `<YYYY-MM>.jsonl`
partitions form one continuous chain; `ledger.lock`, `anchors/`, `state/`
(non-authoritative cache) sit alongside the partitions.

One `canonical_json` object per line, UTF-8, `\n`-terminated. Common fields:
`v`, `ledger_id` (`pw-<16 hex>`), `seq`, `kind`, `ts` (UTC ISO, ms), `harness`,
`harness_version`, `hook_version` (e.g. `promptwitness 0.1.0`), `prev`, `key`
(`{id, alg, strength, presence}`), `sig`. Turn-only fields (present only on
`kind == "turn"`): `session`, `turn`, `prompt_id`, `prompt_kind`,
`prompt_digest`, `raw_digest`, `prompt_chars`, `segments` (list of
`{kind: typed|pasted|slash-args, start, end, digest, paste_id?}`), `channel`,
`resumed`, `signals`. Kinds: `turn`, `session-close` (carries `turns`,
`last_seq`, `turns_witnessed`), `anchor`, `key-rotate`, `gap` (carries
`reason`).

**No prompt text, ever.** Only digests, lengths, offsets and kind labels
(operator ruling 7 keeps prompt lengths and paste offsets). `prompt_digest =
canon.digest(canon.normalize(prompt))`; `raw_digest` is the SHA-256 of the
exact UTF-8 bytes. `turn` is per-session monotonic; the authoritative value is
the count of prior `turn` lines for the session in the chain —
`state/<session>.json` is a cache rebuilt from the chain, never the source of
truth.

**Chain.** `line_hash = sha256(canonical_json(line_with_sig))`; `prev` is the
previous line's `line_hash`; genesis `prev = sha256(b"promptwitness-genesis\x00"
+ ledger_id)`; `sig` covers `canonical_json(line_without_sig)`, including
`prev`; with `key.strength: "none"`, `sig` is omitted entirely. The first line
of a new month's partition carries `prev` equal to the hash of the previous
month's last line — the chain is continuous across partition boundaries, not
reset per month. Appends take an exclusive file lock (`fcntl.flock` on POSIX,
`msvcrt.locking` on Windows), write a `gap` line after a torn tail (a file not
ending in `\n`) rather than chaining onto the torn line, then `O_APPEND` +
fsync. Schema `v` bumps are additive-only within major version 1; readers
ignore unknown fields.

A genesis line's `ledger_meta` records the best-effort `chflags uappnd` /
`chattr +a` outcome on the partition file — defence against accidents, not
against the model (§3 already rules out defending against the model this
way).

### Prompt kind and channel (Claude Code)

The hook's stdin has no origin field, so the hook (athenaeum#2149) classifies
each prompt itself:

| Leading text | `prompt_kind` | Human? |
|---|---|---|
| plain text (not `<`) | `human` | yes |
| `<pasted_content` | `human` (typed/pasted segments recorded) | yes |
| `<command-message` / `<command-name` | `slash-command` | the `<command-args>` span only, tagged `slash-args` (ruling 4) |
| `<task-notification` | `task-notification` | no |
| `<local-command-` | `local-command` | no |
| `<system-reminder` | `system` | no |
| any other `<tag` (including unconfirmed `peer`) | `unknown` | no (default-deny) |
| any text, stdin `agent_id` present or `transcript_path` contains `/subagents/` | `subagent` | no (overrides all) |
| any text, first turn, digest equals a `claude` argv positional | `launch-argument` | no (overrides all) |

Channels: `bridge` when `CLAUDE_CODE_BRIDGE_SESSION_ID` is set (ruling 8,
never human, checked first); `interactive` requires
`CLAUDE_CODE_ENTRYPOINT == "cli"`, `CLAUDE_CODE_SESSION_ATTENDED == "1"`, a
controlling tty on the `CLAUDE_PID` process, and `CLAUDE_CODE_CHILD_SESSION`
not `"1"`; `headless` when the entrypoint starts with `sdk` or attended is
`"0"`; anything else is `unknown`. Raw values always go in `signals`.
**Headless and bridge are never human.**

### Signing

`KeyProvider` (`key_id`, `algorithm`, `strength`, `presence_required`,
`sign`, `public_key`). Strength, weakest to strongest: `none` <
`software-file` < `os-keystore` < `hardware-nonexportable` (<
`hardware-presence`, seam only — ruling 1 builds no adapter for it). The hook
never blocks: a failed or slow (2s) `sign` writes the line with
`strength: none` and `key.error`. The athenaeum#2039 1Password receipt
sealer stays as-is and is not reused here — per-turn `op read` latency and a
network dependency are wrong for a per-prompt hook path.

### Anchoring

`Anchorer` (`anchor`, `upgrade`, `verify`); every `Attestation` records its
`source`, and every verification records its `verify_source`. OpenTimestamps
is primary (public calendars, no account) and full verification uses a public
block explorer by default, configurable (ruling 3); RFC 3161 is optional;
`local-clock` is mandatory on every pass and carries no independent weight.
Network failure yields `failed` + `local-clock`, never an exception.

| Subject | Digest | When |
|---|---|---|
| `witness:<ledger_id>:<seq>` | `line_hash` of the chain head (head only; no Merkle batching, no per-turn proofs) | in-session every `anchor_interval_minutes` (default 15) from a detached process the hook spawns, zero added prompt latency; every SessionEnd (10s budget, lock-free); nightly |
| `receipts:<YYYY-MM>:<line_count>` | `receipt_sealers.lines_digest` over the partition (read-only) | every SessionEnd when changed; nightly |

The hook path and SessionEnd hold no `RunLock`, so they record anchors only as
`anchor` lines in the local witness ledger. The nightly run, under its lock,
mirrors them to `wiki/_witness_anchors/<YYYY-MM>.jsonl` (no prompt digests;
pushed off-machine) and to the receipt `_seals/anchors/` store.

### Intake

`src/athenaeum/witness_attestation.py` (athenaeum#2158) loads and verifies the
ledger once per run. For a `user-stated` classification (flag
`librarian.witness_attestation_enabled`), lookup is by `promptId` (fallback
`(session, utterance_digest)`). A hit needs **both** sides to agree: witness
`kind == turn`, `prompt_kind` `human` (or `slash-command` args), `channel ==
interactive`, same session, digest equality, chain intact through that `seq`,
no `signals.agent_id`; transcript `origin.kind == human` when present,
`promptSource` in `{typed, queued}`, `isSidechain` not true. A hit writes
`channel: witnessed` plus `witness_tag`/`witness_ref`/`witness_strength`/
`witness_anchored` (receipt schema v2, additive); a miss stays `user-stated`
with a `witness_miss` reason and is surfaced as a warning/incident (ruling
10). **Nothing gates on the result yet.**

### Precedence

Tier 1 becomes `user:<conversation-ref> / witnessed:<session-ref>`; still 9
tiers, no rank shifts (ruling 9, athenaeum#2159).

### Config

Hook-time env (honoring `config.env`): `ATHENAEUM_WITNESS_LEDGER_ENABLED`
(off), `ATHENAEUM_WITNESS_ANCHOR_INTERVAL_MINUTES` (15), `PROMPTWITNESS_DIR`,
`PROMPTWITNESS_KEY_PROVIDER` (`none`), `PROMPTWITNESS_KEY_PATH`,
`PROMPTWITNESS_OTS_BLOCK_SOURCE`. Nightly yaml:
`librarian.witness_ledger_enabled`, `librarian.witness.dir`,
`librarian.witness.key_provider`, `librarian.witness_attestation_enabled`,
`librarian.receipt_anchor.{adapters, ots_calendars, ots_block_source,
rfc3161_url, timeout_seconds, anchor_interval_minutes}`. Booleans env > yaml >
default; enumerations raise on unknown values; every nested segment is read
inline in the resolver body (parity gate); no seeds in `_DEFAULTS`.

### Migration and rollback

Every behaviour except in-session anchoring cadence is behind flags that
default off, and anchoring only runs once the ledger is enabled; rollback is
turning the flags off. Receipts already written as `witnessed` stay valid
(the gate treats them as `user-stated`), and persisted `witnessed:` sources
rank 1 like `user:`. athenaeum#2146 is the only change that alters existing
decisions, and it ships alone with a dry-run count.

## 5. Operator rulings (2026-10-28)

These replace the open questions the design pass raised.

1. **Per-signature user presence (Touch ID): no.** No presence-gated key
   adapter is built; the `KeyProvider` seam (`presence_required`,
   `hardware-presence`) is kept.
2. **Anchoring cadence: periodic in-session anchoring is on by default**,
   interval `librarian.receipt_anchor.anchor_interval_minutes` (default 15).
   The hook, after appending its line, spawns a detached process when an
   anchor is due and never waits on the network. Chain head only; no Merkle
   batching, no per-turn proofs. Offline, the detached process writes the
   local-clock stamp and exits. SessionEnd and nightly anchors stay.
3. **OpenTimestamps verification: public block explorer by default**,
   configurable; the verification source is recorded per check, like the
   stamp source. No local-node requirement.
4. **Slash-command arguments count as human-typed**, with their own tag
   (`slash-args`).
5. **Channel name is `witnessed`**, not `human-attested`.
6. **Package name is `promptwitness`**, not `turnwitness`.
7. **Prompt lengths and paste offsets in the local ledger: yes.**
8. **claude.ai bridge prompts get their own channel (`bridge`) and are never
   human.**
9. **`witnessed` shares tier 1 with `user:`**, no rank change. The separate-
   tier decision child is dropped and noted on athenaeum#2145.
10. **The 14-day readout gate is removed.** Provenance misses should be zero
    absent an incident, so this is deterministic test territory: a
    real-binary entry-path integration test (athenaeum#2161) replaces the
    readout window, the miss counter stays, and any miss is surfaced as a
    warning/incident (athenaeum#2158, athenaeum#2160), not a tolerated rate.
    athenaeum#2163 is blocked only on athenaeum#2160 and athenaeum#2161.

## 6. Children and sequencing

- athenaeum#2146 — `transcript_verify` counts task-notification, `isMeta` and
  harness-wrapper records as human (ships alone, dry-run count first).
- **athenaeum#2147 — this slice**: `promptwitness` package, canonical digest,
  hash-chained ledger v1, chain verifier, import boundary, this document.
- athenaeum#2149 — Claude Code witness adapter and hook wiring (blocked by
  athenaeum#2147).
- athenaeum#2150 — signing: `KeyProvider`, `software-file`/`none` adapters
  (blocked by athenaeum#2147).
- athenaeum#2152 / athenaeum#2153 / athenaeum#2154 — os-keystore / Secure
  Enclave / TPM key adapters (the latter two ~operator).
- athenaeum#2151 — anchoring: `Anchorer`, local-clock, OpenTimestamps,
  block-explorer verification (blocked by athenaeum#2147).
- athenaeum#2156 — periodic in-session and SessionEnd anchoring, local ledger
  only.
- athenaeum#2040 — nightly anchoring, wiki mirror, receipt `AnchoringSealer`.
- athenaeum#2157 — RFC 3161 anchoring adapter.
- athenaeum#2158 — intake attestation, `witnessed` channel, misses as
  incidents.
- athenaeum#2159 — precedence: `witnessed` in tier 1, persisted source
  rewrite.
- athenaeum#2160 — `athenaeum receipts attest-readout` (incident report).
- athenaeum#2161 — real-binary entry-path integration test, then enable on
  host (~operator).
- athenaeum#2148 — model id and session ref on every `claude:inferred`
  default.
- athenaeum#2155 — `promptwitness verify` and static HTML demo.
- athenaeum#2163 — adjudication provenance (blocked on athenaeum#2160 and
  athenaeum#2161).
- athenaeum#2162 — extract promptwitness to a standalone repo and PyPI
  (~operator).
- Cross-repo: hestia#2820 — record hestia headless runs as machine-authored.

The former "own precedence tier" decision child was dropped (ruling 9) and
folded into athenaeum#2145 as a note. The trust graph (athenaeum#2145) and a
separate OS user or root-owned daemon are explicitly out of scope for this
epic.

## 7. What this slice (athenaeum#2147) actually ships

Everything in the "Ledger (schema v1)" subsection above, plus `promptwitness.canon` and `promptwitness status`. It deliberately leaves the seams the
schema already names rather than building them: a line written with no key
provider carries `key.strength: "none"` and omits `sig` (athenaeum#2150 adds
real adapters); `kind: "anchor"` and `kind: "key-rotate"` lines are
constructible through `append_line`'s `extra` passthrough but nothing in this
slice writes one (athenaeum#2151 and athenaeum#2150 do); the harness adapter,
hook wiring, and the `prompt_kind`/channel classifier tables in
the "Prompt kind and channel (Claude Code)" subsection are athenaeum#2149's, not
implemented here.
