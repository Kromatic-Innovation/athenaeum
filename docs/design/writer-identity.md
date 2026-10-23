<!-- SPDX-License-Identifier: Apache-2.0 -->

# Writer identity — where authority over a fact comes from today, and an opt-in way to stop trusting self-declared identity

**Status:** IMPLEMENTED, enforcement OFF by default. Issue athenaeum#2041.

A writer-registry entry labels a writer; it does not verify one, and it never confers authority.

## 1. Principle and scope

athenaeum#752 established the principle this document generalises: **authority
must come from a record the model did not write.** It applied the principle to
one decision (the `correct_*` delete). This document inventories every other
place the code decides that one writer's claim outranks another's, says where
each decision's authority comes from, and describes the one opt-in switch that
stops self-declared identity from conferring authority.

Three things ship:

1. This inventory, bound to the code by `tests/test_writer_identity_inventory.py`.
2. `librarian.writer_identity_enforced`, default **off**
   (`src/athenaeum/config.py:resolve_writer_identity_enforced`).
3. A label-only writer registry (`src/athenaeum/writer_registry.py`).

Nothing here verifies a writer. Cryptographic verification (an operator-held
signing key) is tracked in athenaeum#2039 and timestamp anchoring in
athenaeum#2040; neither is implemented here and no `Sealer` is added.

## 2. Inventory: every authority decision about a writer

"Source of authority" is one of:

- **derived**: recomputed from a record the model did not write (a harness-written transcript);
- **self-declared**: read from a field the writer of the memory file chose;
- **channel-implied**: inferred from the channel the input arrived on, not from who spoke.

| Decision | Code | Source of authority | With the flag on |
|---|---|---|---|
| `correct_a` / `correct_b` destructive delete | `src/athenaeum/resolutions.py:_transcript_authorizes_correct`, which calls `src/athenaeum/transcript_receipts.py:classify_for_correct_gate` | **derived**: the originating session transcript, or a receipt of it. `source_type` is never read. | Unchanged. It never reads the flag. |
| Auto-supersession, route (b) strictly greater authority | `src/athenaeum/supersession.py:decide_supersession` calls `src/athenaeum/asserter_authority.py:strictly_greater_authority` and `src/athenaeum/asserter_authority.py:compare_authority` | **self-declared**: grants come from `src/athenaeum/asserter_authority.py:declared_grants`, which reads the frontmatter `asserter.grants` list | Grants count as "none declared". The relation is `equal`; `strictly_greater_authority` is never true on grants alone. |
| Auto-supersession, route (a) same asserter, and the route (c) corroboration scan | `src/athenaeum/supersession.py:_compare_asserters` (wrapping `src/athenaeum/models.py:compare_asserters`) and `src/athenaeum/supersession.py:_third_party_signals` | **self-declared**: the identity key is `src/athenaeum/models.py:asserter_identity_key` over the frontmatter `asserter` block, carried through intake verbatim | `_compare_asserters` answers `"unknown"`. Route (a) does not fire; route (c) finds no corroboration. |
| Route (c) equal-or-incomparable test | `src/athenaeum/supersession.py:decide_supersession` calls `src/athenaeum/asserter_authority.py:treated_as_equal_authority` | derived from the two decisions above | Follows them. |
| Escalation answer recorded as human | `src/athenaeum/answers.py:ingest_answers` calls `src/athenaeum/fingerprint.py:record_resolution` with `resolved_by=` | **channel-implied**: `resolve_question` in `src/athenaeum/mcp_server.py` accepts any caller with `caller_audience is None`, which is the owner/default mode of every local MCP session | Records `resolved_by="human-unverified"` instead of `"human"`. The answer still applies to the question it answered. |
| Auto-apply of a prior human verdict | `src/athenaeum/tiers.py:tier4_escalate` (the `record.get("resolved_by") == "human"` branch) over `src/athenaeum/fingerprint.py:load_resolved_records` | channel-implied, via the stored label | `"human-unverified"` is not `"human"`, so it is not auto-applied to a re-surfaced fingerprint and the pair re-escalates. A record already on disk as `"human"` keeps today's treatment. |
| Auto-apply of the resolver's own verdict | `src/athenaeum/tiers.py:_record_auto` calls `src/athenaeum/fingerprint.py:record_resolution` with `resolved_by="auto"` | not an identity claim; the label says "a machine decided" | Unchanged. |
| Decay of suppression records | `src/athenaeum/fingerprint.py:is_stale_auto_suppression` | label-based | `"human-unverified"` decays like `"auto"`. `"human"` never decays. |

Also classified, but **not changed** by enforcement (out of scope for
athenaeum#2041): `src/athenaeum/resolutions.py:_stance_attribution_verdict`
calls `compare_asserters` on opinion attribution. It decides keep-both versus
precedence for evaluative claims, and `"unknown"` is already its safe
fallback. The `asserter_authority.py` module does not itself read config for
the flag: its callers read the flag once per run and pass it down.

How the flag is read: `decide_supersession` and `ingest_answers` each resolve
`resolve_writer_identity_enforced` once at the top of the run and pass the
boolean to the helpers (`compare_authority(..., writer_identity_enforced=)`,
`declared_grants(..., writer_identity_enforced=)`, `_compare_asserters`).
No helper re-reads the environment.

## 3. Failure classes the identity model must survive

1. **A model writing the frontmatter it wants.** `asserter`, `asserter.grants`
   and `source_type` are plain YAML in a file the model can author. Enforcement
   removes their authority; `correct_*` never trusted `source_type`.
2. **A model answering the escalation queue as courier.** Claude relays the
   queue, so its answer arrives on the same wire a human's would. The label
   `human` records the channel, not the speaker. Enforcement downgrades it to
   `human-unverified`.
3. **A filesystem-level forger.** A process with write access can edit a
   transcript, a registry file, or the resolved-contradictions cache. Nothing in
   this issue defends against that; only signing (athenaeum#2039) would.
4. **A rolled-off transcript.** The `correct_*` gate refuses (`unavailable`)
   rather than guessing; receipts (athenaeum#753) capture the claim earlier
   when opted in.
5. **An IdP `sub` that is pairwise per application.** The Entra case.
   `src/athenaeum/models.py:asserter_identity_key` keys on the tenant-scoped
   object id and ignores `sub`; the registry reuses that function and derives no
   key of its own.

## 4. Rejected alternatives

- **Enforce identity by default.** Rejected by the operator ruling of
  2026-10-23: too risky for the base configuration. With enforcement on, the
  worst case is a legitimate writer losing supersession authority or an answer
  not being auto-applied, which is a missed automation. It is still a
  behaviour change nobody opted into, so it ships off.
- **A registry that grants authority to listed writers.** A name anyone with
  filesystem access can add to a YAML file is as self-declared as the
  frontmatter block it labels. The registry therefore labels only, and a test
  pins that a registered writer carrying self-declared grants is still treated
  as "none declared" with the flag on.
- **Sign human approval now.** The goal is that an agent cannot fake human
  approval, and signing is how. It needs a key custodian and an anchoring
  story that do not exist yet; it is sequenced after this issue
  (athenaeum#2039). Labelling and refusing to trust self-declared identity are
  the part that can ship without one.
- **Fold the registry into the authority manifest.** See the boundary below.

## 5. Boundaries

- **`src/athenaeum/authority.py` and `athenaeum authority` (athenaeum#426)** are
  the *live-source* manifest: which skill or code file owns a topic. That
  answers "which source owns this subject", not "which writer owns this fact".
  The two share the word "authority" and nothing else; the writer registry is a
  separate module (`src/athenaeum/writer_registry.py`) with a separate file.
- **`caller_audience` / `resolve_audience` (athenaeum#538)** is the read-scope
  seam: what a caller may *see*. It is the input an egress-refusal policy
  (athenaeum#428) would build on. This document concerns who may *write* a fact
  with authority; it neither widens nor narrows read scope, and outbound refusal
  logic stays in athenaeum#428.

## 6. The writer registry

`src/athenaeum/writer_registry.py` follows the `src/athenaeum/authority.py`
manifest-loader shape: YAML, `version: 1`, a malformed file raising
`WriterRegistryError` that names the defect, a missing file meaning an empty
registry. The path resolves through
`src/athenaeum/config.py:resolve_writer_registry_path` (env
`ATHENAEUM_WRITER_REGISTRY` > yaml `librarian.writer_registry_path` >
`<knowledge_root>/writer-registry.yaml`).

An entry has a `label`, a `type` drawn from `src/athenaeum/models.py:ASSERTER_TYPES`
(`person` / `software_agent` / `organization`) and the identity fields that
`src/athenaeum/models.py:asserter_identity_key` reads. It is not a second key
derivation.

No code path outside the registry module reads it. That is deliberate and is
what "labels, does not verify, never confers authority" means in practice.

## 7. Known limits

Even with enforcement on, no path can *verify* a named writer. The
limits in [Conflict resolution](conflict-resolution.md) (the user typed these
words, which is not the user authorizing this; the in-session forgery window;
the transcript is not immutable) all still stand.
