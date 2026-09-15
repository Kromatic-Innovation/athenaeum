# C4 retirement preconditions — re-examination after the 2026-09-15 shadow-parity run (athenaeum#1663)

- generated: 2026-09-15 (analysis lane, not a `measure shadow-parity` run — no live corpus, no code touched)
- base: `origin/develop` at `633e31ef`
- inputs: `measurements/shadow-parity-2026-09-04.md` (lines 260-425), `measurements/shadow-parity-2026-09-15.md` (both unedited by this lane), athenaeum#1663, athenaeum#1256, athenaeum#1244, athenaeum#1258, athenaeum#1483, athenaeum#1484, athenaeum#1257, athenaeum#715, athenaeum#1615, athenaeum#1626, athenaeum#1667

## Operator decisions on record

These are recorded verbatim as directed; they are not this lane's judgment.

1. **`retire.py` approach: "Use the comparator's verdicts."** A cluster is move-eligible when the comparator has a trustworthy recorded verdict for its pairs and none is `contradiction`. Degraded/underdetermined handling is specified below (see "Chosen `retire.py` approach"). This lane lists alternatives per the operator's request; the comparator-verdicts option is CHOSEN.
2. **Coordinate coverage gate: athenaeum#1244 (live backfill of `subject`/`claimed_scope`) runs before any retirement.** athenaeum#1244 is already a native `blocked_by` of athenaeum#1256 (confirmed on GitHub — athenaeum#1256's `blocked_by` list includes athenaeum#1244), and athenaeum#1244 is itself `blocked_by` athenaeum#1615, athenaeum#1624 and athenaeum#1626 (confirmed), with athenaeum#1626 `blocked_by` athenaeum#1667 (confirmed).
3. **Go/no-go on releasing athenaeum#1256: NO-GO for now.**
4. **Accepted-loss sign-off, 2026-09-15 (received mid-task, supersedes the brief's "NOT yet decided" framing for two items only):** the operator signed off on treating §3.1's **residual** pairwise-cost delta and §3.9 (cross-scope pooling / raw→wiki sweep) as `accepted loss, operator signed off (2026-09-15)`, once §3.1's memoisation wiring (below) lands. §3.2, §3.3, §3.4, §3.5, §3.7 and §3.10 are to be ported. §3.6 (resolver actions) remains **undecided** — this document gives a recommendation only, and does not carry a sign-off line for it.

## §3.x verdict table

Verdicts use athenaeum#1663's three-way vocabulary (`served by the comparator today` / `still must be ported` / `candidate for accepted loss`), not the 09-04 report's four-way one.

| # | Item | Verdict (2026-09-15, this lane) | Recommended disposition |
| --- | --- | --- | --- |
| §3.1 | N-ary → pairwise cost | still must be ported (memoisation wiring) | Wire `cluster_comparator.py` to `record_comparison`/`get_verdict_status` (below); once wired, accept the residual 1.543× cost delta — **operator signed off 2026-09-15** |
| §3.2 | `conflict_type` lost | still must be ported | Add a `ConflictType`-shaped field to `CompareOutcome`/`EscalationItem` write path; cheap, well-scoped |
| §3.3 | `contradiction-flagged` status + recall header | still must be ported | Comparator writes no page flag anywhere; port must cover both trigger fields the header now reads |
| §3.4 | `retire.py` hard-depends on a contradiction verdict | still must be ported | Discharged by the chosen `retire.py` approach below; fold into athenaeum#1256's AC (already owns this rewrite per athenaeum#1663's Out of scope) |
| §3.5 | `members_involved` / source-file identity | still must be ported | Identity collision in `cluster_comparator.py`'s call site is real and currently untracked (see dedicated section below) — safety issue, not a dropped field |
| §3.6 | Resolver lane unreplaced | still must be ported (recommendation only — undecided) | Port the three ledger *actions* (`not_a_conflict`/`propose_merge`/`attribute_both`) narrowly; do **not** implement an auto-finalizing merge-proposal path — banned by athenaeum#715 |
| §3.7 | `not_a_conflict` ledger + TTL decay | still must be ported | `verdicts.py` has the durable pair store; wire it, add fingerprint-keyed adapter, no TTL yet |
| §3.8 | Deterministic pre-LLM short-circuits | still must be ported (1 of 4 already served) | Validity-disjointness confirmed still served at zero spend; port the other three or accept as a smaller residual loss — operator has not been asked, left as `must be ported` |
| §3.9 | Cross-scope pooling + raw→wiki sweep | candidate for accepted loss | **Accepted loss — operator signed off 2026-09-15** (obsoleted by pipeline shape; not independently re-verified against develop this run — see caveat below) |
| §3.10 | Run-summary / ledger row shape | still must be ported | `cluster_comparator.py`'s `to_row()` still explicitly not wired anywhere; counters live only in the C4 loop |

## Detail, with fresh file:line evidence on `633e31ef`

### §3.1 — N-ary → pairwise cost blow-up

Confirmed unchanged from 09-04: `cluster_comparator.py` still calls `compare_pages` **directly** at what is now `cluster_comparator.py:344` (`outcome = compare_pages(page_a, page_b, client=client, config=config, usage=usage)`), never `record_comparison`. `record_comparison` (`comparator.py:937-1000`) is the function that checks `get_verdict_status` **before** any LLM call and returns early on `skipped="fresh"` — that memoisation is available but unused by the cluster lane. This is unchanged from the 09-04 finding; nothing in this run's new code (the athenaeum#1257 T1 screen, described under §3.6) touches it.

New since 09-04: `record_comparison` requires a `lock: RunLock` keyword-only argument (`comparator.py:947`), the same single-appender contract every `verdicts.py` mutator enforces. Wiring §3.1's memoisation is therefore not just "call `record_comparison` instead of `compare_pages`" — the cluster-domain caller (today dark, unwired in `librarian.py` per `cluster_comparator.py`'s own module docstring) must acquire and thread a `RunLock` the same way `merge_clusters_to_wiki` does for the C4 lane.

**Operator disposition:** the residual live-shape cost multiplier (1.543×, from the 09-04 report) is accepted once this wiring lands. The wiring itself is not optional — without it the retire.py approach below has no verdicts to read.

### §3.2 — `conflict_type` lost

Unchanged from 09-04, confirmed at current line numbers: `verdict_effects.py:699` still sets `EscalationItem.conflict_type="principled"` (also `:517`, `:544`, `:654` all set `"ambiguous"`) — the three-value taxonomy `"principled"|"ambiguous"|"classification_failed"` declared at `models.py:1793`. The lost field is `ContradictionResult.conflict_type: ConflictType | None` at `models.py:1835`, where `ConflictType = Literal["factual", "prescriptive", "stance"]` now lives at `models.py:1819` (moved from `contradictions.py` since 09-04's citation — citation drift, not a code change; `contradictions.py:120-121` still documents the same three-value taxonomy for the detector's own JSON contract). `CompareOutcome` still has no such field.

### §3.3 — `contradiction-flagged` status + recall header

Read site has moved since 09-04's citation (`mcp_server.py:620-624` → now `mcp_server.py:736-740`), and the trigger condition has **grown a second field** since 09-04: `mcp_server.py:736` now reads
```
contested = (isinstance(status, str) and status == "contradiction-flagged") or bool(
    fm.get("contradictions_detected")
)
```
— either the `status` frontmatter field **or** the `contradictions_detected` boolean trips the header. A 09-04-shaped port that only reproduces the `status` string would miss the boolean half. Write side is unchanged: `merge.py:209` (`CONTRADICTION_STATUS_FLAGGED`) and `merge.py:1333-1334`; `verdict_effects.py` still emits no page-status write on any route.

### §3.4 — `retire.py` hard-depends on a contradiction verdict

Verified verbatim and unchanged in substance since 09-04 (line numbers drifted 173-179→176-180 on `633e31ef`): `_move_eligibility(entry: MergedWikiEntry)` at `retire.py:164-180` returns `False, "no contradiction verdict available — not safe to retire"` at `retire.py:177` whenever `entry.contradiction is None`. With C4 retired this field is permanently `None`. Confirmed load-bearing detail not in either prior report: `_move_eligibility` takes **only** the `MergedWikiEntry` — no `wiki_root`, no member→page_id mapping — and is called at `retire.py:490` inside `run_retire_pass`'s per-entry loop, which does already have `wiki_root` and `_resolve_members(entry, extra_roots)` in scope at that point. `DEGRADED_RATIONALES` (`retire.py:93-100`) is `{"llm-unavailable", "detector-returned-no-json", "detector-invalid-conflict-type", "detector-malformed-response"}` — all C4-detector-specific strings that must be replaced or mapped, not merely reused.

Discharged by the chosen approach (below); folded into athenaeum#1256's AC rather than a separate follow-up, per athenaeum#1663's Out of scope ("Rewriting `retire.py`... athenaeum#1256's AC already requires that change in the same PR").

### §3.5 — `members_involved` / source-file identity — the sharpest finding in this run

This is the one place where current code actively **contradicts** a claim implied by the 09-04 report, and it needed chasing past the first grep.

09-04 cited `verdicts.py:274-282` for `page_id_for_path = slugify(Path(path).stem)`, colliding with C4's `f"{am.origin_scope}/{am.path.name}"` member ref. On `633e31ef` that function has grown — it now runs `verdicts.py:274-332` and takes an optional `root: Path | None = None` keyword, added by athenaeum#1484 ("Corpus-wide uniqueness"). With `root=wiki_root` passed, the id folds the root-relative path in, so two same-stem pages in different directories under one root get different ids — the exact collision §3.5 describes.

But **the fix is scoped away from the call site that matters here.** The docstring (`verdicts.py:281-320`) states explicitly: `record_pair_decision` — a different, already-existing production caller — passes `root=wiki_root` and is fixed. `cluster_comparator.py` and `comparator.py` **"intentionally do not pass `root`"**, preserving the bare-stem id for "cross-domain slug alignment" with a pinned test (`tests/test_cluster_comparator.py::TestPageFromAutoMemoryFile::test_id_matches_verdict_ledger_slug_space`). Confirmed at the actual call site: `cluster_comparator.py`'s `page_from_auto_memory_file` still calls `page_id_for_path(member.path)` with no `root` argument. The collision §3.5 describes is therefore **still live** for exactly the lane athenaeum#1256 would retire files through.

The docstring further states this residual risk "stays gated by the comparator's own default-off flag, per athenaeum#1484's 'Out of scope' section deferring the comparator itself to athenaeum#1483." Checked: **athenaeum#1484 closed `moscow:wont`**, and **athenaeum#1483 closed** as the shadow-parity subject/claimed_scope re-run (the work landed via athenaeum#1662) — it never touched id identity. **No open issue currently owns fixing the cluster-comparator id collision.** A partial fix shipped for one caller; the deleting lane's own call site stayed broken and lost its only stated tracking edge when both referenced issues closed. This should become its own follow-up (see below) rather than ride silently inside a larger port.

### §3.6 — Resolver lane unreplaced

Constants unchanged: `resolutions.py:307` `SUPPRESS_ACTION`, `:310` `PROPOSE_MERGE_ACTION`, `:337` `ATTRIBUTE_BOTH_ACTION`, `DEFAULT_AUTO_APPLY_THRESHOLD = 0.90` at `:153`, destructive `forget_*`/`correct_*` at 0.95 (`:184-187`), `_NEVER_AUTO_APPLY_ACTIONS = frozenset(("propose_merge",))` at `:209` — `propose_merge` never auto-applies at any threshold. Confirmed `verdict_effects.py` has no import of `resolutions` at all (grep returned nothing) — none of the three actions are wired to the comparator subsystem.

What **did** change since 09-04, and matters for the recommendation: athenaeum#1257 landed a **T1 reasoning screen** inside `cluster_comparator.py` (`_t1_rejects_pair`, gated by `ClusterScreenContext` + its own default-off `reasoning_tier_auditing_enabled` knob). This is not new capability the 09-04 report missed — its own "Subsystem answers" section already accounted for T1 exactly ("Its only screen is T1..., double-gated OFF, and T1 only drops pairs — it produces no action"). What's new is that T1 is now actually *wired into this call site* rather than merely existing elsewhere; it still only drops candidate pairs before comparison, producing no resolver action. The module's own docstring (`cluster_comparator.py`, top) is explicit that T2 (`t2_screen_merge_proposal`) is **deliberately not called** from this lane: T2's auto-finalize path needs a `confidence` scalar and a `draft_merged_body` that `run_cluster_comparator` does not produce, and fabricating them "is the anti-pattern athenaeum#658 finding D2 recorded and athenaeum#715 banned." `tests/test_cluster_comparator_t1_screen.py` pins T2's absence.

**Recommendation (undecided — operator's call):** port the three ledger actions narrowly — `not_a_conflict` (suppression + TTL, shared machinery with §3.7), `attribute_both`, and `propose_merge` staying non-auto-applying exactly as it is today (`_NEVER_AUTO_APPLY_ACTIONS`) — as new verdict-effect branches keyed on the comparator's verdict/route, the same shape `verdict_effects.py` already uses for `queued`/`fold-proposal`/`refines-written`. Do **not** attempt to auto-finalize a merge from the comparator's `duplicate`/`specialization` verdicts without a human step; that would recreate the athenaeum#658/#715-banned T2 pattern with the comparator as the new source of the fabricated fields. This mirrors the existing T1-only, no-auto-merge posture the cluster lane already has.

### §3.7 — `not_a_conflict` ledger + TTL decay

Unchanged. `verdicts.py` has the durable pair store — `append_verdict` (`:541`, requires `lock: RunLock`), `lookup_pair` (`:585`), `get_verdict_status` (`:598-616`, returns `{"decided", "fresh", "verdict", "at", "stale_reason"}`), `mark_pairs_stale` (`:838-869`, sets `record["stale"] = True` and `record["stale_reason"]` on first mark only, never clears). `cluster_comparator.py` imports only `page_id_for_path` from `verdicts.py` — confirmed, no `append_verdict`/`get_verdict_status`/`mark_pairs_stale` call anywhere in the module. Keys on page-pair identity (`make_pair_key`), not C4's `claim_pair_fingerprint(text_a, text_b, conflict_type)` (`fingerprint.py:89`), and has no TTL decay. The port is a fingerprint-keyed adapter over `verdicts.py`'s existing store, not a ledger from scratch — same conclusion as 09-04.

### §3.8 — Deterministic pre-LLM short-circuits

Not independently re-verified against `633e31ef` beyond confirming `comparator.py:828-935`'s Gate 1 structure (`compare_pages` above) is unchanged in shape: `gate1_separator_relations` runs first, `disjoint_dims` returns `VERDICT_DISTINCT` before any LLM call (`comparator.py:848-854`) — the validity-disjointness short-circuit 09-04 observed firing live is structurally still in place. The other three (declared-pair filter, partial prune, post-detection disjoint downgrade) were not re-checked line-by-line this run; no evidence contradicts 09-04's "3 of 4 unreplaced."

### §3.9 — Cross-scope pooling + raw→wiki sweep

**Caveat:** this item was not independently re-verified against `633e31ef` with fresh evidence beyond a confirming grep — `merge.py:115-119` still imports `cross_scope_similarity_pairs`/`resolve_cross_scope_mode`/`resolve_similarity_threshold` from `cross_scope.py`, and the sole call site is `merge.py:2681`. `cluster_comparator.py` still does not do candidate-set generation — it consumes an already-formed cluster's members. This disposition rests on the pipeline-shape argument the 09-04 report made, not on a fresh line-by-line audit of `cross_scope.py`'s internals this run. The operator's accepted-loss sign-off is on record regardless.

### §3.10 — Run-summary / ledger row shape

Confirmed unchanged in kind; every citation has drifted. Counters now live at `merge.py:1888-1901` (`haiku_calls`, `haiku_calls_succeeded`, `pairs_added_via_similarity`, `chunks_run`), `merge.py:2276-2282` (`resolve_calls`, `resolve_calls_succeeded`), written into `out_stats` at `merge.py:2784-2796` and `merge.py:2823-2832`. `librarian.py` reads them into the run summary as `detector_haiku`/`resolver_opus`/`escalations` at **four** separate sites now (`librarian.py:6549-6553`, `:8476-8480`, `:8502-8506`), not the single site 09-04 implied. `cluster_comparator.py`'s `ClusterComparatorResult.to_row()` (its own docstring: "Not written anywhere by this dark module") emits `cluster_id`/`pair_count`/`gate_enabled`/`outcomes`/`screened_out` — still a different shape, still unwired.

## Citation drift since 2026-09-04 (both existing reports and both open issues cite stale lines)

| Symbol | 09-04 citation | Current (`633e31ef`) |
| --- | --- | --- |
| `EscalationItem.conflict_type` | `models.py:1735` | `models.py:1793` |
| `ContradictionResult.conflict_type` | `models.py:1758` | `models.py:1835` |
| `ConflictType` literal | `contradictions.py` (implied) | `models.py:1819` |
| `verdict_effects.py` conflict_type hardcode | `:697` | `:699` (09-04 already corrected this one) |
| `resolutions.py` action constants | `:303`/`:306`/`:333` | `:307`/`:310`/`:337` |
| `merge.py` `_maybe_propose` | `:2187-2241` | `:2285` onward |
| Counter writes into `out_stats` | `merge.py:2982-2996` | `merge.py:2784-2796` / `:2823-2832` |
| `librarian.py` run-summary read | `:5255-5313` | `:6549-6553`, `:8476-8480`, `:8502-8506` (now three sites) |
| `retire.py` `_move_eligibility` refusal | `:173-179` (09-15 report) / `:176-177` (athenaeum#1256 body) | `:164-180`, refusal string at `:177` |
| `page_id_for_path` | `verdicts.py:274-282` | `verdicts.py:274-332` (grew a `root=` parameter) |
| `mcp_server.py` contested-header read | `:619-624` (athenaeum#1256 body) / `:620-624` (09-04) | `:736-740`, and the trigger condition gained a second field |

athenaeum#1256's own acceptance criteria cite the stale `merge.py` and `librarian.py` ranges above and cannot be followed literally without a re-read at merge time.

## Chosen `retire.py` approach: use the comparator's verdicts

### Alternatives considered

1. **Loosen the rule to "nothing flagged + nothing pending."** Drop the contradiction-verdict requirement entirely; move whenever `contradictions_detected` is false and the pending-confirmation check (`_open_pending_text`) is clear. *Tradeoff:* simplest, zero new wiring — but removes the one signal that currently prevents moving raw that a detector (of any kind) would have flagged. Fails the operator's own framing that §3.4 "breaks a shipping feature," because it silently converts "no verdict" into "safe," which is the exact failure mode athenaeum#1254's characterisation tests exist to catch (09-04 report, line 264).
2. **Deliberately pause the lane with a visible status.** Make `_move_eligibility` return `False` with an explicit "retire lane paused pending comparator wiring" reason for every entry until the port lands, so the freeze is loud rather than silent. *Tradeoff:* honest and safe, but is functionally the no-port status quo of athenaeum#1256 today — except made visible instead of accidental. Strengthens the case for NO-GO but does not unblock anything.
3. **Use the comparator's verdicts (CHOSEN).** Read the comparator's own recorded verdicts for a cluster's pairs from the ledger; move only when every pair has a trustworthy verdict and none is `contradiction`.

### What the chosen option requires in code

`cluster_comparator.py` calls `compare_pages` directly (`:344`) and never `record_comparison`, so nothing is written to the `verdicts.py` ledger for cluster-domain pairs today. The chosen approach requires, in order:

1. **Wire the cluster lane to the ledger (discharges §3.1's required half).** Replace the direct `compare_pages` call in `run_cluster_comparator` with `record_comparison`, threading a `RunLock` (acquired once per run, mirroring `merge_clusters_to_wiki`'s lock lifecycle) and `wiki_root` through `ClusterScreenContext` or an equivalent parameter. This is a precondition for `retire.py` having anything to read — not optional.
2. **Fix the id space at this call site (discharges §3.5).** Pass `root=wiki_root`/`knowledge_root` (or a cluster-domain-appropriate root) into `page_id_for_path` inside `page_from_auto_memory_file`, replacing the currently-intentional bare-stem id. This must be coordinated with the pinned cross-domain-alignment test (`tests/test_cluster_comparator.py::TestPageFromAutoMemoryFile::test_id_matches_verdict_ledger_slug_space`), which currently asserts the *opposite* behaviour and will need updating in the same change — not a silent test break.
3. **Give `_move_eligibility` what it needs to read the ledger.** Its current signature (`retire.py:164`) takes only `entry: MergedWikiEntry`. It needs either `wiki_root` plus a member→page_id mapping added as parameters (both already in scope at its call site, `retire.py:490`), or the mapping pre-resolved into a lookup passed alongside `entries` into `run_retire_pass`. `_resolve_members(entry, extra_roots)` (used just above the call site) is the existing helper that maps an entry back to member paths; page ids for the ledger key come from the same fixed-up `page_id_for_path` as step 2.
4. **Specify the verdict-to-eligibility mapping.** For every pair among an entry's members:
   - `get_verdict_status` returns `decided=False` (no row) → **HOLD**, same posture as today's `c is None`.
   - `decided=True, fresh=False` (a `mark_pairs_stale`-invalidated row) → **HOLD** — a stale verdict cannot authorize retirement any more than it can authorize "a new automatic operation" (`verdicts.py:can_authorize_auto_operation`'s existing rule, reused here rather than re-invented).
   - `verdict="contradiction"` → **HOLD** (the operator's stated rule: "none is `contradiction`").
   - `verdict="underdetermined"` → **HOLD**, explicitly, not treated as clean. An absent answer degrading to "safe to retire" is exactly the athenaeum#1254 failure mode the 09-04 report's threshold framing was built to catch, and the 09-15 live run shows the comparator no longer defaults here on annotated coordinates — but a page short of coordinate coverage still can.
   - `outcome.verdict is None` (Gate 2 unavailable — offline/API error, `comparator.py:860`) → **HOLD**, mapped onto today's `DEGRADED_RATIONALES` branch (`retire.py:93-100`) rather than treated as a clean pass.
   - `verdict` in `{duplicate, distinct, specialization}` → eligible, same as today's real-clean-verdict path.
   - **Vacuous-quantifier edge case, load-bearing.** "None is `contradiction`" is vacuously true both for a genuine singleton (zero pairs — matches today's documented `singleton` move-eligible case, `retire.py:164-172`'s own docstring) and for a cluster where every candidate pair was T1-screened-out (`cluster_comparator.py`'s `screened_out` list) — zero `outcomes`, but **not** a singleton, and with no verdict at all recorded for any pair. The two must not collapse into the same code path: require `len(outcomes) + len(screened_out) == pair_count` as an invariant, and treat any cluster where `screened_out` is non-empty as **HOLD**, not move-eligible — `cluster_comparator.py`'s own docstring already names this exact hazard ("a T1 reject would be indistinguishable from a pair that was never formed").
5. **Note the throughput dependency, not a safety one.** The 2026-09-15 run found the comparator over-fires `contradiction` on `merge`-class clusters (0/2 correct — tracked separately, not in this issue's scope). Under the chosen rule this means merge-shaped clusters will keep landing on HOLD rather than moving, which is safe (conservative) but means the retire lane's *effective throughput* depends on that separate over-fire defect being fixed, not on anything in this document.

## Coverage requirement (tied to athenaeum#1244)

- **What retirement needs.** Gate 1 (`comparator.py:828-854`, `gate1_separator_relations`) only separates a pair — reaching `VERDICT_DISTINCT` at zero model spend — when an enforced separator dimension reads a *known*, non-`unknown` relation on both sides. The two live-corpus-relevant coordinates gated by athenaeum#1244 are `subject` (only meaningfully separating once *ratified* — `subject_ratified: bool = False` is the call-site default at `comparator.py:308`/`:332`, so unratified coverage does not help Gate 1 at all) and `claimed_scope` (one of `COORDINATE_FIELDS = ("valid_from", "valid_until", "claimed_scope")` at `audit.py:88`, filled by the audit pass, not by athenaeum#1244 directly per its 2026-09-15 scope split).
- **What fails below coverage.** When neither side of a pair has a ratified `subject`, `gate1_separator_relations` records that dimension `unknown` rather than absent. If the content relation from Gate 2 comes back `CONFLICTING`, `comparator.py:885-888` returns `VERDICT_UNDERDETERMINED` on any `unknown_dims` — the exact "absent answer silently degrades to no-contradiction" path the retire.py mapping above explicitly holds on, but only if that mapping is implemented. Without step 4 above, an `underdetermined` verdict today has no consumer in `retire.py` at all (the field the retire lane reads, `entry.contradiction`, is C4's, not the comparator's).
- **Why this is tied to athenaeum#1244 specifically.** athenaeum#1244's 2026-09-15 split moved the `subject := uid` refusal to athenaeum#1656 and kept "populate `subject` by meaning" (blocked by athenaeum#1615's entity-resolution wiring) and post-pilot coverage re-measurement (blocked by athenaeum#1626, itself blocked by athenaeum#1667) as this issue's remaining scope. `valid_from`/`valid_until`/`claimed_scope` ride the audit pass (athenaeum#1624 and athenaeum#1627), not athenaeum#1244 directly — this document's brief named only athenaeum#1615, athenaeum#1626 and athenaeum#1667 in the coverage chain; athenaeum#1624 belongs in it too for the `claimed_scope` half.

## Go/no-go on releasing athenaeum#1256

**NO-GO for now**, per the operator decision on record. Three preconditions remain open at the time of this document: (a) the `retire.py` wiring specified above is not implemented — this document specifies it but athenaeum#1663's Out of scope reserves the implementation for athenaeum#1256's own PR; (b) §3.2/§3.3/§3.5/§3.6(pending)/§3.7/§3.10 are not ported; (c) athenaeum#1244's coordinate backfill has not run. None of this document's analysis changes any of those facts — it only makes them concrete enough to act on.

## Proposed follow-up issues

Per this lane's limits, these are proposals only — no issue is filed by this lane; the orchestrator disposes.

1. **Fix the `cluster_comparator.py` page-id collision athenaeum#1484 deferred and lost track of (discharges §3.5).** Scope: pass a root into `page_id_for_path` at `cluster_comparator.py`'s `page_from_auto_memory_file` call site, coordinated with updating `tests/test_cluster_comparator.py::TestPageFromAutoMemoryFile::test_id_matches_verdict_ledger_slug_space`, which currently pins the opposite (bare-stem, collision-prone) behaviour. Should block athenaeum#1256 — a same-named-member collision in a lane that deletes files is a correctness hazard, not a style nit, and no currently-open issue owns it now that athenaeum#1483/#1484 both closed.
2. **Wire `cluster_comparator.py` to the verdict ledger (discharges §3.1's required half and is a precondition for the chosen retire.py approach).** Scope: replace the direct `compare_pages` call with `record_comparison`, threading a `RunLock` and `wiki_root` through the cluster-domain call path. Should block athenaeum#1256 — the chosen retire.py approach has nothing to read without it.
3. **Port §3.2/§3.3/§3.7/§3.10 (status flag, recall header, `conflict_type`, suppression+TTL ledger adapter, run-summary shape) as one grouped port.** Scope: each is independently small; bundling avoids four near-identical small PRs each touching `verdict_effects.py`. Should block athenaeum#1256 per the operator's port decision above.
4. **§3.6 resolver-action port, scoped per this document's recommendation (suppress + attribute_both + non-auto-applying propose_merge only, no T2-shaped auto-finalize).** Should block athenaeum#1256 once the operator disposes on §3.6; not yet actionable as a blocking edge since the disposition is still open.

## Where current code contradicts the 2026-09-04 report or the athenaeum#1663 body

- **§3.5 (above) is the clearest case.** 09-04 described a collision at a since-superseded citation; athenaeum#1484 landed a *partial* fix for a different call site while leaving the cluster-comparator call site — the one that matters for athenaeum#1256 — exactly as broken, and the tracking trail (athenaeum#1483/#1484) closed without addressing it. Neither existing report nor athenaeum#1663's body flags that the fix and the hazard now live in the same function with different behaviour depending on caller.
- **§3.6, minor:** athenaeum#1663's plan text does not mention that a T1 screen is now actually wired into `cluster_comparator.py` (athenaeum#1257, landed since 09-04) — worth noting so a reader doesn't rediscover it expecting §3.6 to be entirely unstarted. It changes nothing about the disposition (T2/resolver actions remain fully unported), only the completeness of "resolver lane unreplaced" as a one-line description.
- **`mcp_server.py`'s contested-header trigger condition (§3.3) grew a second field** (`contradictions_detected` in addition to `status == "contradiction-flagged"`) since 09-04's citation — not contradictory to the classification, but a port written strictly to the 09-04 citation would miss half the current trigger.
- **athenaeum#1256's own acceptance criteria** cite `retire.py:173-179`, `merge.py:2982-2996`, `librarian.py:5255-5313` — all stale on `633e31ef` per the citation-drift table above. Not a contradiction of substance, but the AC cannot be executed literally without a re-read at merge time.
