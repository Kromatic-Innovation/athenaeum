<!-- SPDX-License-Identifier: Apache-2.0 -->

# The access model: principals, rights, proof and the two team topologies

**Status:** DESIGN, mostly not built. Issue athenaeum#3283, a child of the access
epic athenaeum#3282. Every section below is marked **shipped** or **planned**.
A section marked *planned* describes a child of the epic that has not merged;
nothing in it is behaviour you can rely on yet. This note is rewritten to
describe shipped behaviour only when the epic closes.

## 0. Summary and what exists today

Authorization is decided in about ten places today, in five overlapping
vocabularies: the process-wide `serve.audience` pin and page `access:` and
`audience:` fields, identity and client allowlists and OAuth scopes, tool
profiles, owner-only checks in the MCP server, per-store rights in
`_store/access.yaml`, the vault read gate, and sensitivity `read_policy`. The
epic replaces them with one decider, so that one module says whether a person,
an agent or an automated writer may read, write or decide anything, and records
why.

What exists at this commit, and nothing more:

- `src/athenaeum/access.py`, a single module with one function, `can(principal,
  location, right, *, config, knowledge_root)`. It answers from one store's
  declaration and returns a boolean. It knows four rights (`read`, `propose`,
  `compile`, `answer`), no agents, no conditions and no acts. It is described
  in [federation-store-access.md](federation-store-access.md) section 16.
- The identity provider protocol and the `local` and `oidc` providers
  ([security-posture.md](security-posture.md) section 2.7).
- The per-process audience pin, the owner-only MCP tool checks, the remote
  profile and the vault read gate
  ([security-posture.md](security-posture.md) sections 2.1 and 2.8).

What does **not** exist yet, all of it **planned**: the `src/athenaeum/access/`
package, `src/athenaeum/request_context.py`, the `access:` policy block, the
nine rights, the proof enums, the engine-written source label, installation
keys, arrival checks and the migration command. Wherever this note says
"is", read "will be" unless the section says *shipped*.

### Design reference (access epic athenaeum#3282)

*Planned.* These are the fixed points the rest of the note refers to.

- **One package decides.** `src/athenaeum/access/` decides every read, write
  and act. Code outside it imports only the facade `athenaeum.access` and the
  tier-0 module `athenaeum.request_context`. (Today `access` is a plain module;
  the package replaces it and the old module becomes a shim until the removal
  child lands.)
- **Storage never imports access.** The storage facade receives `authorizer=`
  and `stamper=` when a store is opened with `open_store` (athenaeum#3237,
  athenaeum#3239). Access imports storage, config and tier-0 modules only, so
  the dependency runs one way and storage stays usable without a policy.
- **Nine rights, five of them acts.** `read`, `propose`, `write`, `compile`,
  `approve`, `manage-policy`, `manage-identities`, `erase`, `break-glass`. The
  last five are *acts* (section 3).
- **Two closed proof enums** (section 2).
- **Grants before existence.** Grants are evaluated before the resource is
  checked to exist. An unknown resource and a denied one both answer
  `not-permitted`, so a caller cannot probe for what it may not read.
- **Zero-config default.** A single-user install with no `access:` block
  behaves exactly as today, plus the engine-written source label (section 6).

### The two team topologies

*Planned.* The single-user local install stays the zero-config default. Two
topologies serve teams, and they protect different things.

**Served deployment.** One server holds the stores and enforces every call.
Every caller reaches data through that server, so the server's decision is the
only door. What this protects: a person sees only the stores their grants name,
an agent never exceeds the person it acts for, and the decision log on the
server is the record of every call. What it does not protect: anyone with a
shell on the server, or with the server's storage, bypasses the decision
(section 8 states the limit for acts).

**Separately held stores.** There is no server. Each store lives in a place
that enforces access for itself, such as a git host with per-repository
permissions, and each installation fetches only what that place lets it fetch.
The place holding the store is the enforcement point for who may read or push.
Each installation then verifies what arrives (section 9), because the place
that holds the store can tell who pushed a commit but not who authored a
statement. What this protects: a reader without the right never holds the
bytes, so there is nothing to filter. What it does not protect: a member who
may push can still write false statements under their own installation key, and
a stolen or copied installation key writes as its owner (section 9).

## 1. Principals

*Planned* (athenaeum#3284 vocabulary, athenaeum#3286 registry,
athenaeum#3290 installation key).

A principal is the party a decision is about. The closed kinds are:

| Kind | What it is |
|---|---|
| `person` | A human, named by an identity key. |
| `agent` | A `(person, host)` pair: a person's assistant running in a named host such as an editor or a chat client. An agent never holds more than its person. |
| `service` | An automated writer. Every service has an `owner` (a person); a service without one is rejected. |
| `librarian` | The engine's own scheduled routines. The librarian's grants never authorise an answer to a question (section 8). |
| `detector` | A narrow principal for cross-store comparison (section 10). |
| `anonymous` | A caller that carries no identity. |

The **installation** is the device element. Each installation creates an
Ed25519 key on first run, with no configuration; the sha256 fingerprint is the
installation id, written `("install", "SHA256:<base64>")`. The key lives in the
machine-local secret location of the storage local face, never in a store.
Requests carry the installation id as their `device`. Identity keys keep the
existing shapes `(iss, sub)` and the tenant-scoped Entra form, and gain
`("install", fingerprint)` and `("ldap", base, uid)`.

The **principals registry** (`principals.yaml`, version 2) evolves today's
label-only writer registry. It lists `people`, `services` (each with an
`owner`) and `installations` (fingerprint, label, person, who accepted it,
`valid_from`, `revoked_at`). It holds labels and links only. It is never a
grant: appearing in it confers no right.

## 2. The two proof enums, and what each value does not prove

*Planned* (athenaeum#3284). Both enums are closed, string-valued and carry a
`VERSION`.

**Identity proof** says how strongly the engine knows who is calling. In rank
order: `none < declared < local-account < holder-attested < token-verified =
key-signed`. Conditions use the rank through a `min:` threshold.

| Value | Means | Does **not** prove |
|---|---|---|
| `none` | No identity at all. | Anything. |
| `declared` | The caller said who it is, for example a stdio client's self-reported `clientInfo` name. | That the caller is that party; anyone can type any name. |
| `local-account` | The call came through an operating-system account on the machine that holds the store (the CLI, a hook). | Which human sits at that account, or that no other process runs under it. |
| `holder-attested` | The holder of the store attests to the caller. | That the holder checked anything beyond their own say-so. |
| `token-verified` | A bearer token passed signature, issuer, audience and expiry checks. | Which software presented the token, or which model reads the answer. The token proves the identity provider issued it to that client. |
| `key-signed` | The request carries a signature from an installation key. | That the key's owner is the person named; a copied key signs as its owner (section 9). |

**Authorship proof** says how strongly the engine knows who *wrote* a
statement, as distinct from who submitted it. Shipped values: `unknown`,
`declared` (an agent's or caller's own claim) and `local-account` (a CLI or
hook write). The values `key-signed`, `confirmed` and `witnessed` are
**reserved and never produced** in this epic. Proven human authorship needs a
signing witness that does not exist yet and is out of scope. Conditions name
sets of authorship values, not a rank.

What authorship does not prove, today and under this epic: that a human
reviewed or meant the statement. An agent claim stays `declared`.

The other closed vocabularies are `PrincipalKind` (the table above),
`AsserterKind` (`self`, `agent-inference`, `third-party`, `unknown`), `Channel`
(`cli`, `stdio`, `http`, `hand-edit`, `scheduled`, `file-drop`, `arrival`) and
`Reason`, the closed set of decision reason codes.

## 3. Resources and rights

*Planned* (athenaeum#3284 vocabulary, athenaeum#3289 decision function).

**Resources** are named with a prefix: `store:`, `loc:` (a location),
`queue:`, `xqueue:` (a cross-store queue), `intake:`, `policy`, `principals`,
`audit:` and `op:` names.

**Rights.** The first four are exercised on data; the last five are acts.

| Right | Lets the holder | Kind |
|---|---|---|
| `read` | Read a store's content through recall and the read tools. | data |
| `propose` | Submit a record for the store's compiler to consider. | data |
| `write` | Write raw intake to the store. | data |
| `compile` | Run the compile that turns raw intake into pages. | data |
| `approve` | Answer a decision queue (questions, merges, audit items). | act |
| `manage-policy` | Change the policy, including capture rules. | act |
| `manage-identities` | Change the principals registry and accept new writers. | act |
| `erase` | Erase content. | act |
| `break-glass` | Grant an emergency, time-boxed read. | act |

An **act** is accepted only through a local channel (section 8), never over
HTTP, even by a principal that holds the right.

**Decision order.** `decide(ctx, right, resource, attrs)` returns a `Decision`
with `allowed`, `reason`, `grant` and `policy_version`:

1. The policy is valid, else `policy-invalid`. On a single-owner install the
   owner keeps `read` and `manage-policy`, so a broken policy never locks the
   operator out.
2. The principal is known and active.
3. Grants for the principal, its groups, `everyone`, `anyone`, `owner` and
   `operator` are evaluated **before** checking that the resource exists.
4. An agent is intersected with its person.
5. The delegation ceiling (token scopes) applies.
6. Store conditions apply (section 5).
7. Acts need a local channel, else `act-channel`.

The public result collapses `unknown-resource` and `no-grant` into one
`not-permitted`; the internal reason stays in the decision log.

**Single-principal installs.** An install with no `access:` block compiles to a
single-owner policy: the owner holds every right, everyone else holds none.
This is also what shipped `can` does for a store with no declaration.

## 4. The policy file: where it lives per topology, and who can change it

*Planned* (athenaeum#3285).

The policy is the `access:` block, schema `version: 2`. It is read through the
configuration loader from `<knowledge_root>/athenaeum.yaml`, never from inside
a store. That placement matters: today a store's declaration is a file inside
the store (`_store/access.yaml`), so anyone who may push to a shared store can
rewrite it and grant themselves rights. The new policy is not in any store
for the receiver to trust.

The block's keys: `operator`; `identity` (provider, provider settings,
admission); `directory` (source, cache and staleness bounds); `principals`;
`contact_details` (`protect: <location>` or `readable`); `approval`
(`mode: local-channels`, the only accepted value); `hosts`; `stores`
(scope, location, held, conditions, `auto_file_for`, `propose`); `roles` and
`grants`; `break_glass`; `detector`; `cross_queues`; `arrival`.

| Topology | Where the policy lives | Who can change it |
|---|---|---|
| Single user | Optional. No block means a single-owner policy. | The owner, by editing their config. |
| Served deployment | On the server, in its configuration. | A holder of `manage-policy`, through a local channel on the server. Not over HTTP. |
| Separately held stores | In each installation's own configuration. Each installation decides for itself what it honours. | The person at that installation. There is no shared signed policy for separately held stores; that is out of scope for this epic. |

A policy edit is itself an act: an HTTP caller cannot change policy, and the
decision log records who did.

## 5. Conditions

*Planned* (athenaeum#3297).

A store may add conditions to its grants: a host allow-list (`hosts`), a
required client type (`client_type: public | confidential`), a minimum
identity proof (`identity.min`), and the model providers allowed to see its
content (`llm`). When `hosts:` is configured at all, an unregistered client is
refused for every call. A store's `hosts` and `client_type` apply to reads and
writes over HTTP.

**What an OAuth client id proves, honestly.**

- A **public client** id proves nothing about the software. Anyone can present
  any public client id.
- A **confidential client** id proves that the token went to whoever holds the
  registration secret for that client.
- Neither says which model reads the data, or which program on the client's
  side holds the token afterwards.

A host allow-list is therefore a statement about registrations, not about
software. The documentation and the refusal message say so. Over stdio,
`clientInfo` is self-reported, so host conditions there are logged as advisory
only. Restricting which model provider receives a store's content is a
separate condition (`allowed_providers(store)`) enforced by the engine's own
routines at the point they send content out; a comparison that spans two stores
uses the intersection of the two lists.

## 6. The source label: an immutable core plus a receiver envelope

*Planned* (athenaeum#3293 stamper, athenaeum#3239 storage hook). This is the one
part that also reaches the zero-config install.

Every raw write and every adopted drop carries `athenaeum_source`, written by
the engine only. It has two parts.

**Core** (immutable once written): `v`, `request_id`, `signed_at`, `store_id`
(or `intake:<submitter>` before filing), `transport`; `submitter`,
`on_behalf_of`, `asserter{principal, kind, authorship}` and `originator`;
`host`, `model`, `device`; `writer_seq` (separately held stores only) and
`content_digest`.

**Envelope** (written by receivers and by routing only): `filed` and
`arrival`.

Rules:

- A caller-supplied `athenaeum_source` is dropped. The drop is reported in the
  tool response and in the log.
- What a caller used to pass as asserter, on-behalf-of or model is routed into
  the label's declared fields, with authorship `declared` for agent claims and
  `local-account` for CLI and hook writes. Reserved authorship values are never
  produced.
- Drops and extra intake roots are attributed to the owner on single-principal
  installs. On multi-principal installs they are attributed to their mapped
  service, else quarantined.
- Compile copies the core by `request_id`. A test asserts that no routine
  rewrites it.

Today (shipped) a remote `remember` is authenticated but the verified identity
never reaches the raw file, and host and device are recorded nowhere. The label
closes that gap.

## 7. What a statement is worth in single-principal versus multi-principal stores

*Planned* (athenaeum#3294).

Source precedence ranks a user's own statement above an assistant's inference.
Today a served remote `remember` can claim `_source_type: user` and be ranked
as if the owner said it. The effective source token is decided from the label
and the store kind, and `precedence.py` stays a pure primitive that reads no
policy.

- **Single-principal store.** The item's own token, unchanged, for labelled and
  legacy items alike. Only one person can write there, so a `user` claim is as
  good as it ever was.
- **Multi-principal store.** An asserter of kind `self` at authorship
  `declared` ranks as `claude`. An unlabelled legacy item whose token is `user`
  or `witnessed` also ranks as `claude`. Third-party, service and script tokens
  are unchanged. The declared claim stays in the record; it just no longer
  outranks a person's own entry.

This is a **breaking change** for served deployments: remote claims of `user`
stop ranking at the top tier. The changelog and the migration guide say so.
Nothing here proves a person meant a statement; proven authorship stays out of
scope.

## 8. Approvals: local channels only, and what that does not protect against

*Planned* (athenaeum#3295, athenaeum#3298).

An act is accepted when the actor holds the right **and** the channel is `cli`,
`stdio`, `hand-edit` or `scheduled`. The local channels are the command line,
a local stdio MCP call, a hand edit of a queue file on the machine that holds
the store, and scheduled ingestion of such an edit. No act is served by any
HTTP process; that is a breaking change for any deployment that served the
decision tools remotely. `approval: {mode: local-channels}` is the only value
the policy accepts.

Attribution: stdio records the actor as an agent `(owner, client)` on behalf
of the owner at `declared`; the CLI and hand edits record the operator at
`local-account`. Scheduled ingestion runs as the librarian but attributes each
answer to the actor recorded for the edit and checks **that** actor's right;
the librarian's grants never authorise an answer. Queue text returned to a
reader is delivered as quoted, typed data beside its label, never as
instructions (athenaeum#3296).

`erase` and `break-glass` default to the operator. A break-glass grant is a
read-only, expiring grant, created by a holder of `break-glass`, that records
requester and approver at `declared`, keeps the reason in an admin location and
surfaces itself to the store owner in `athenaeum status`.

**What local channels do not protect against.** They keep acts off the network.
They do not stop a shell-capable agent running on the machine that holds the
store: such an agent can run the CLI, edit a queue file or read the stores
directly, and each of those is a local channel. If an assistant with a shell
runs on the machine that holds the store, it can approve. Phase one keeps
today's protection and no more. Stronger protection needs a witness that
signs a human's approval, which is out of scope.

## 9. Separately held stores: installation keys, arrival checks, first-seen writers

*Planned* (athenaeum#3300 arrival, athenaeum#3301 writers).

In this topology the place that holds a store enforces who may fetch and push.
The engine adds what that place cannot: who authored each record.

**Signing.** Each record an installation writes to a separately held store
carries a signed core: `store_id`, `signed_at`, `content_digest`, the full
installation fingerprint, `writer_seq` and `request_id`, signed with the
installation key under a domain-separation prefix.

**Arrival checks** run when records come in from a pull:

- Unsigned: quarantined.
- Unknown signer: `unrecognised`, held out of compile.
- A core for another store, or a repeated `request_id`: refused.
- A citation of another person's statement is verified against that person's
  retained signed raw core (kept under `raw_retention: keep-signed-cores`). If
  it is missing or invalid, the citation is downgraded to the page signer's own
  `agent-inference` at `declared`.
- There are no time windows.

The receiver no longer treats a store's own `_store/access.yaml` as authority,
so a pusher cannot grant themselves rights.

**First-seen writers.** A new fingerprint is announced in status with its full
fingerprint, first-seen date, self-given label (marked unverified) and who
introduced it. It is admitted only by a deliberate act on a local channel,
`writers accept SHA256:<full fingerprint> --as <person>`; prefixes are refused.
Acceptance writes a signed introduction to the shared store, which other
members honour under the store's acceptance rule. Peer answers apply only if
the local policy grants that peer `approve`; otherwise they show as "not
honoured here".

**Clone detection.** Two different digests under the same `(fingerprint,
writer_seq)` flag "key in use in two places", and new items from that key are
held. Out-of-order arrival, which folder sync produces routinely, is never
flagged.

**What this does not protect against.** A copied key signs as its owner until
the clone is noticed. An accepted member can write false statements under their
own key. A member whose installation holds a store can read every record in it.
Signing proves which installation wrote a record, not that a person meant it.

## 10. Cross-store contradictions: visible only to readers of both stores

*Planned* (athenaeum#3302).

Today cross-store page pairs are skipped, so contradictions between stores are
never found. A dedicated `detector` principal compares facts across stores. It
holds `read` on its own detector location, `write` on cross queues and nothing
else, and reads a per-principal store only where executives hold standing read
on it.

A contradiction question lives in a cross queue, `xqueue:<opaque pair id>`,
in its own location outside both stores' indexes and recall.

- **Read is derived:** exactly the current readers of both stores. A person who
  can read only one store never learns the other store holds a conflicting
  fact, not even that a question exists.
- **It inherits the stricter conditions of both:** the intersection of host
  allow-lists, the stricter client type and minimum identity proof, the
  intersection of allowed providers, and reads always audited.
- Readers of both may mark an item handled and may propose corrections in
  either store under their own names. The engine never writes a value across.
- Activity goes to a per-pair log readable only by readers of both.
- Holders of `manage-policy` see the policy-derived list of covered pairs that
  nobody can read together. A count of waiting questions is off unless
  `report_unanswerable: true`.

## 11. Migration

*Planned* (athenaeum#3303 `access migrate`, athenaeum#3304 start-up refusals,
athenaeum#3305 removal).

`athenaeum access migrate` writes the first `access:` block from today's
settings so that nothing becomes readable that was not before. It is a dry run
by default; `--apply` writes. The mappings:

- Each audience role becomes a group grant on a location, and restricted pages
  move to locations per audience set.
- In deployments that use audiences, untagged pages move to an owner-only
  `unclassified` location.
- Email allowlist entries map to principals through the directory and require a
  verified email; unmapped entries are reported.
- `allowed_clients` becomes `hosts:`.
- Roles in a store's `_store/access.yaml` become grants.

A checker proves the before and after read sets are equal or narrower for every
known principal; a widening aborts the migration.

A deployment that can admit more than one principal refuses to start without an
`access:` policy and without an explicit `contact_details:` choice. The
built-in contact-details read policy is removed; `contact_details:
{protect: <location>}` routes that class to a vault and `readable` leaves it in
place.

After the earlier children land, the old mechanisms become shims and are then
removed: the process-wide `serve.audience` pin, page `access:` and `audience:`
handling, the writer-identity enforcement flag, the vault read gate and the
asserter grants. A lint then fails on any authorization decision outside
`src/athenaeum/access/`.

**Breaking changes to expect:** no act over HTTP; remote `user` claims no longer
rank at the top tier in multi-principal stores; `serve.audience` goes away. A
single-user install with no `access:` block needs no migration.

## What is out of scope

Signed human-authorship evidence, witnessed approvals, trusted-assistant
weighting, session unlock, and shared signed store policy for separately held
stores. Sections 2 and 8 say what the absence of each costs.
