# SPDX-License-Identifier: Apache-2.0
"""Contact-value attribution seam — checks D1-D4 (issue athenaeum#2210).

Child B of epic athenaeum#2192, designed in
``docs/design/attribution-verification.md`` §4 ("The seam") and §5
("Deterministic check catalogue"). Implements north-star §2.12 ("filing is
verification") for exactly one value class: contact values (email addresses
and phone numbers) on person records.

**What this module owns.** :func:`check_contact_attribution`, the ONE seam
every contact-value writer calls before it would put a value on a record. It
runs four deterministic checks, cheapest first:

- **D1, non-personal identifier** — two tiers. *D1-narrow* (relay/notification
  local-part prefixes, the operator's own addresses and mailer domains on a
  non-owner page, and :func:`athenaeum.pii.is_service_address` hits) is
  enforced in every mode from this issue onward. *D1-domain* (whole-domain
  relay entries) is observe-only until a narrowed list exists — a whole-domain
  entry over-denies that domain's real employees. A role local part
  (``info``/``hello``/``hi``/``team``/``contact``) passes on a non-person
  target, or when its domain matches an address already on the target's own
  record; otherwise it is ``suspect``.
- **D2, held elsewhere** — another uid's contact record already lists the
  value. A record whose uid has no live wiki page does not count.
- **D3, target is not a person** — the target's declared type is not
  ``person``, or its name matches a ``company`` page's name/alias.
- **D4, fan-out** — the value already sits on two or more uids.

A ``non-personal`` verdict stops the run (no further check is consulted). A
``suspect`` verdict does not — every remaining check still runs so later
escalation (out of scope here: checks C1-C3, athenaeum#2215) sees all the
evidence.

**What this module does NOT own.** It never writes a contact record — see
:func:`athenaeum.pii.record_observed_contact_value` and friends for that. It
DOES write two things, because this is also where suspects and verdicts live
(design §4.3): the attribution log (:func:`log_attribution_verdict`, one JSONL
row per verdict, never carrying the value) and the suspect park file
(:func:`park_suspect`, one markdown record per parked (value, target) pair,
idempotent — re-parking merges into the same file). Only D1-narrow is
enforced in this issue (athenaeum#2220 owns enforcement beyond that); every
writer calls this seam in **observe mode**: it logs the verdict and parks a
**shadow** suspect entry, then behaves exactly as it does today. A D1-narrow
verdict parks a **non-shadow** suspect entry (:func:`is_d1_narrow`) and the
writer must not file the value — see each call site's docstring for how it
does that.

Layering: L3. Imports :mod:`athenaeum.pii` (L3 sibling — no cycle: ``pii``
only reaches this module through a function-local import at its own writer
call site), :mod:`athenaeum.config` (L2), :mod:`athenaeum.models` (L1), and
:mod:`athenaeum.atomic_io` / :mod:`athenaeum.store` (L0) at module scope.
Never imported by :mod:`athenaeum.pii` at module scope, which is the whole
reason this is a separate module rather than growing inside ``pii.py``: the
design (§5.2) lifts the owner-address helper out of the L4 ``dedupe`` module
specifically so this L3 seam never needs an L4 import, and a module-scope
``pii -> pii_attribution`` edge would still be a same-layer sibling edge but
is avoided anyway to keep ``pii.py``'s own import set unchanged for its
existing consumers (see ``tests/test_import_budget.py``'s pinned set for
:mod:`athenaeum.search`, which imports ``pii`` transitively).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import config as config_module
from athenaeum import pii
from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import EntityIndex, parse_frontmatter, render_frontmatter
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Writer ids (design §4.2) — the membership list the writer-inventory guard
# (issue athenaeum#2210 AC "Writer inventory guard") checks against. athenaeum#2216
# (prose-compile scrub) and athenaeum#2212 (model escalation / re-attribution)
# extend this tuple; this issue wires exactly these four.
# ---------------------------------------------------------------------------

WRITER_OBSERVED_CONTACT = "observed-contact"
WRITER_CORRECTION_ADD = "correction-add"
WRITER_PII_MIGRATION = "pii-migration"
WRITER_CONTACT_FOLD = "contact-fold"

#: The four writers this issue wires. Membership, not count, is what the
#: writer-inventory guard test asserts (future children extend this set).
WRITER_IDS: tuple[str, ...] = (
    WRITER_OBSERVED_CONTACT,
    WRITER_CORRECTION_ADD,
    WRITER_PII_MIGRATION,
    WRITER_CONTACT_FOLD,
)

# ---------------------------------------------------------------------------
# Dispositions and check ids
# ---------------------------------------------------------------------------

DISPOSITION_PASS = "pass"
DISPOSITION_NON_PERSONAL = "non-personal"
DISPOSITION_SUSPECT = "suspect"

#: D1 fires one of three check ids depending on which D1 branch matched.
#: Only ``D1-narrow`` is enforced (see :func:`is_d1_narrow`) — ``D1-domain``
#: and ``D1-role`` share D1's *disposition* column in the design table but
#: are observe-only, so the check id (not the disposition alone) is what a
#: writer must inspect to decide enforcement.
CHECK_D1_NARROW = "D1-narrow"
CHECK_D1_DOMAIN = "D1-domain"
CHECK_D1_ROLE = "D1-role"
CHECK_D2_HELD_ELSEWHERE = "D2"
CHECK_D3_NOT_A_PERSON = "D3"
CHECK_D4_FAN_OUT = "D4"

#: Built-in relay/notification local-part prefixes (the "noreply family"),
#: extendable through ``librarian.attribution.relay_local_prefixes``
#: (:func:`athenaeum.config.resolve_relay_local_prefixes`). Matched as a
#: local-part PREFIX, case-insensitively, against the normalized local part
#: (design §5.1, D1-narrow).
BUILTIN_RELAY_LOCAL_PREFIXES: frozenset[str] = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "mailer-daemon",
        "postmaster",
        "notifications",
        "notification",
        "automated",
        "auto-confirm",
        "bounce",
        "bounces",
    }
)

#: Role local parts (design §5.1, D1-narrow's third bullet) — never
#: operator-extendable in this design; the issue names exactly these five.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset({"info", "hello", "hi", "team", "contact"})

#: Schema version stamped on every attribution-log row, mirroring
#: :data:`athenaeum.pii.OBSERVATION_LOG_VERSION`'s convention.
ATTRIBUTION_LOG_VERSION = 1

#: Ledger filename, written under the contacts (excluded) surface root
#: (design §4.3).
ATTRIBUTION_LOG_FILENAME = "_attribution_log.jsonl"

#: Per-deployment HMAC key file, minted on first use (design §4.3's
#: ``value_key``) and stored on the excluded surface alongside the log and
#: suspect files — never in the wiki, never derivable from the log alone.
ATTRIBUTION_KEY_FILENAME = "_attribution/_key"

#: Suspect park directory, relative to the contacts root (design §4.3).
ATTRIBUTION_SUSPECTS_DIRNAME = "_attribution/suspects"


def _local_and_domain(value: str) -> tuple[str, str]:
    """Split a normalized email-shaped *value* into ``(local, domain)``.

    Both lowercased. A *value* with no ``@`` (e.g. a phone number) yields
    ``(value, "")`` — every email-shaped check below is then simply inert for
    it, which is the correct behavior: none of D1's email-specific branches
    apply to a phone number.
    """
    normalized = pii.normalize_identifier(value)
    if "@" not in normalized:
        return normalized, ""
    local, _, domain = normalized.rpartition("@")
    return local, domain


@dataclass(frozen=True)
class CheckEvidence:
    """One fired check and its evidence — uids and labels, never the value.

    ``label`` names WHAT matched (a relay prefix, a role local part, a
    domain) — never the contact value itself, so a log or memo built from
    this can never leak the value it is evidence about.
    """

    check_id: str
    uids: tuple[str, ...] = ()
    label: str | None = None


@dataclass(frozen=True)
class AttributionVerdict:
    """The seam's answer for one (value, target) pair (design §4.1).

    ``disposition`` is one of :data:`DISPOSITION_PASS`,
    :data:`DISPOSITION_NON_PERSONAL`, :data:`DISPOSITION_SUSPECT`.
    ``checks`` is the ordered list of fired checks (empty for ``pass``).
    ``candidates`` are uids the evidence points at as possible owners
    (deduped, first-seen order). ``value_key`` is the keyed hash from
    :func:`value_key_for`, provided here so a caller never has to recompute
    it to log or park this verdict.
    """

    disposition: str
    checks: tuple[CheckEvidence, ...] = ()
    candidates: tuple[str, ...] = ()
    value_key: str = ""


def is_d1_narrow(verdict: AttributionVerdict) -> bool:
    """True when *verdict* fired the D1-narrow check — the only enforced check.

    Every call site in this issue uses this exact predicate to decide
    whether to file the value (design §9.1: "D1-narrow values are never
    filed on their target, in every mode, from child B onward").
    """
    return any(c.check_id == CHECK_D1_NARROW for c in verdict.checks)


@dataclass
class AttributionContext:
    """Indexes and config built once per run, passed to every seam call.

    Built by :func:`build_attribution_context`. Never constructed directly by
    a writer — the constructor is the one place the surface layout, the
    owner-address lift, and the non-personal pattern set come together
    (design §5.2).
    """

    contacts_root: Path
    excluded_index: "pii.ExcludedRecordIndex"
    entity_index: EntityIndex | None
    owner_uid: str
    #: Lowercased, trimmed owner aliases/name (issue athenaeum#2210 PR review,
    #: Seer finding): an owner configured WITHOUT a ``uid`` — emails/aliases
    #: only — would otherwise never match :attr:`owner_uid` on any target, so
    #: ``_check_d1``'s "is this the owner's own page" test would be False
    #: for every target including the owner's real one, and the owner's own
    #: address would be flagged D1-narrow non-personal even there. This set
    #: lets that test also match by name, the same ``owner_name``/
    #: ``owner_alias`` signal :func:`athenaeum.dedupe.owner_signal` already
    #: uses (simplified to an exact, case-folded match — this seam has only
    #: ``target_name``, not the full page metadata ``owner_signal`` reads).
    owner_name_aliases: frozenset[str]
    owner_addresses: frozenset[str]
    relay_local_prefixes: frozenset[str]
    operator_mailer_domains: frozenset[str]
    relay_domains: frozenset[str]
    value_key_secret: bytes = field(repr=False)


def _attribution_key_path(contacts_root: Path) -> Path:
    return Path(contacts_root) / ATTRIBUTION_KEY_FILENAME


def _load_or_mint_value_key_secret(contacts_root: Path) -> bytes:
    """Read the per-deployment HMAC key, minting it on first use (design §4.3).

    Stored on the excluded surface, never in the wiki. A 32-byte random key,
    written hex-encoded. Minting is idempotent under a race: if another
    process already wrote the file by the time this one goes to write, the
    file on disk — not this process's freshly generated bytes — wins, so two
    processes started at once still agree on one key (:func:`value_key_for`
    must be STABLE across a deployment's whole lifetime, not just within one
    process).
    """
    path = _attribution_key_path(contacts_root)
    if path.exists():
        try:
            hex_text = path.read_text(encoding="utf-8").strip()
            return bytes.fromhex(hex_text)
        except (OSError, ValueError):
            pass
    minted = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, minted.hex() + "\n")
    # Re-read after the write: another process may have minted and written
    # first, in which case this reads back ITS key rather than trusting the
    # bytes just generated in memory, which is what keeps the key stable
    # across a race rather than merely across a single process.
    try:
        hex_text = path.read_text(encoding="utf-8").strip()
        return bytes.fromhex(hex_text)
    except (OSError, ValueError):
        return minted


def value_key_for(context: AttributionContext, value: str) -> str:
    """The keyed hash of *value* for logs and suspect filenames (design §4.3).

    An HMAC-SHA256 of the normalized value under the per-deployment key
    (:func:`_load_or_mint_value_key_secret`) — never a plain hash, which a
    dictionary attack could reverse for a common email/phone shape. Stable
    for the lifetime of one deployment's key; never derivable from the
    attribution log alone.
    """
    normalized = pii.normalize_identifier(value)
    digest = hmac.new(context.value_key_secret, normalized.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def build_attribution_context(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    *,
    excluded_index: "pii.ExcludedRecordIndex | None" = None,
    entity_index: EntityIndex | None = None,
    contacts_root: Path | None = None,
    owner: dict[str, Any] | None = None,
) -> AttributionContext:
    """Build an :class:`AttributionContext`, once per run (design §5.2).

    *excluded_index* / *entity_index* let a caller that already built one of
    these for its own pass (the librarian compile loop, a corrections batch)
    pass it in rather than pay a second O(corpus) scan — the design's own
    wording ("built once per run, or once per CLI invocation") is satisfied
    either way. A CLI migration with neither builds both itself, matching the
    single-constructor call shape design §5.2 shows:
    ``pii.build_attribution_context(knowledge_root, config)``.

    *contacts_root* / *owner* (issue athenaeum#2210 PR review, Seer finding)
    let a caller that has ALREADY resolved these — :func:`athenaeum.dedupe.
    merge_duplicate_persons` takes a pre-resolved ``owner`` dict and a
    pre-resolved ``contacts_root`` and carries no ``config`` dict to
    re-derive either from — pass them straight through, bypassing
    :func:`athenaeum.config.resolve_owner` / :func:`athenaeum.pii.
    contacts_surface_root`. *config* still supplies the three
    ``librarian.attribution.*`` extras (``None`` there yields the built-ins
    only, same as every other caller).
    """
    root = Path(knowledge_root)
    resolved_contacts_root = (
        Path(contacts_root)
        if contacts_root is not None
        else pii.contacts_surface_root(root, config)
    )
    idx = (
        excluded_index
        if excluded_index is not None
        else pii.ExcludedRecordIndex(resolved_contacts_root)
    )
    ent_idx = entity_index
    if ent_idx is None:
        wiki_root = root / "wiki"
        if wiki_root.is_dir():
            ent_idx = EntityIndex(wiki_root)
    resolved_owner = owner if owner is not None else config_module.resolve_owner(config)
    owner_uid = str((resolved_owner or {}).get("uid") or "")
    owner_addrs = frozenset(config_module.owner_addresses(resolved_owner))
    owner_name_aliases: set[str] = set()
    if resolved_owner:
        for alias in resolved_owner.get("aliases") or []:
            cleaned = str(alias).strip().lower()
            if cleaned:
                owner_name_aliases.add(cleaned)
    return AttributionContext(
        contacts_root=resolved_contacts_root,
        excluded_index=idx,
        entity_index=ent_idx,
        owner_uid=owner_uid,
        owner_name_aliases=frozenset(owner_name_aliases),
        owner_addresses=owner_addrs,
        relay_local_prefixes=BUILTIN_RELAY_LOCAL_PREFIXES
        | config_module.resolve_relay_local_prefixes(config),
        operator_mailer_domains=config_module.resolve_operator_mailer_domains(config),
        relay_domains=config_module.resolve_relay_domains(config),
        value_key_secret=_load_or_mint_value_key_secret(resolved_contacts_root),
    )


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _holder_uids(context: AttributionContext, value: str) -> tuple[str, ...]:
    """Distinct uids whose contact record already lists *value* (design D2/D4).

    Walks :class:`athenaeum.pii.ExcludedRecordIndex` exactly as
    :func:`athenaeum.pii.record_observed_contact_value` already does,
    deduped by uid (several records for one uid are one person described
    twice, not several holders). First-seen order.
    """
    uids: list[str] = []
    for path in context.excluded_index.all_by_identifier(value):
        found = pii.uid_on_record(path)
        if found and found not in uids:
            uids.append(found)
    return tuple(uids)


def _has_live_page(context: AttributionContext, uid: str) -> bool:
    """True when *uid* resolves to a live wiki page (design D2's "no live page
    does not count"). With no :class:`EntityIndex` available, every uid
    counts as live — the conservative default that never manufactures a
    false D2 hit out of a context this issue's callers always supply one
    for, but a bare unit test exercising the checks without one still runs.
    """
    if context.entity_index is None:
        return True
    return context.entity_index.get_by_uid(uid) is not None


def _target_own_domains(context: AttributionContext, target_uid: str) -> frozenset[str]:
    """Domains already on *target_uid*'s own contact record, if it has one.

    The reversible default this issue picks for D1-role's "matches the
    target's own ... domain" (design §5.1): the design also offers
    "employer domain", which this issue does not have a deterministic
    source for (no employer/company-link field is in scope here) — so only
    the target's own already-recorded domains are consulted. Role addresses
    this misses still fall to ``suspect``, never ``non-personal``, so the
    gap costs an observe-only false suspect, not a misfile.
    """
    if not target_uid:
        return frozenset()
    record_path = context.excluded_index.by_uid(target_uid)
    if record_path is None:
        return frozenset()
    meta = pii.read_bounce_record(record_path)
    domains = {_local_and_domain(ident)[1] for ident in pii.identifiers_on_record(meta)}
    domains.discard("")
    return frozenset(domains)


def _check_d1(
    value: str,
    *,
    target_uid: str,
    target_name: str | None,
    target_type: str | None,
    context: AttributionContext,
) -> CheckEvidence | None:
    """D1: non-personal identifier, two tiers plus the role-local-part branch."""
    local, domain = _local_and_domain(value)
    normalized = pii.normalize_identifier(value)
    is_owner_page = (bool(context.owner_uid) and target_uid == context.owner_uid) or (
        target_name is not None
        and target_name.strip().lower() in context.owner_name_aliases
    )

    if pii.is_service_address(normalized):
        return CheckEvidence(check_id=CHECK_D1_NARROW, label="service-address")

    if not is_owner_page:
        for prefix in context.relay_local_prefixes:
            if local.startswith(prefix):
                return CheckEvidence(check_id=CHECK_D1_NARROW, label=f"relay-local-prefix:{prefix}")
        if normalized in context.owner_addresses:
            return CheckEvidence(check_id=CHECK_D1_NARROW, label="operator-address")
        if domain and domain in context.operator_mailer_domains:
            return CheckEvidence(check_id=CHECK_D1_NARROW, label=f"operator-mailer-domain:{domain}")

    if domain and domain in context.relay_domains:
        return CheckEvidence(check_id=CHECK_D1_DOMAIN, label=f"relay-domain:{domain}")

    if local in ROLE_LOCAL_PARTS:
        if target_type is not None and target_type != "person":
            return None  # role addresses on a non-person target pass
        if domain and domain in _target_own_domains(context, target_uid):
            return None  # matches a domain already on the target's own record
        return CheckEvidence(check_id=CHECK_D1_ROLE, label=f"role-local-part:{local}")

    return None


def check_contact_attribution(
    value: str,
    *,
    target_uid: str,
    target_name: str | None,
    target_type: str | None,
    source_ref: str | dict[str, Any] | None,
    writer: str,
    context: AttributionContext,
) -> AttributionVerdict:
    """The seam (design §4.1). Pure apart from appending to the attribution log.

    Runs D1-D4 cheapest first. A ``non-personal`` D1 result stops the run
    (no D2-D4 call). A ``suspect`` result from ANY check does not stop the
    run: every remaining check still runs, so later escalation sees all the
    evidence (design §5.1). Logs exactly one row via
    :func:`log_attribution_verdict` before returning. Never writes a contact
    record and never parks a suspect file — that is the WRITER's job (design
    §4.1), via :func:`park_suspect`, once it has decided how to act on the
    verdict for its own mode.
    """
    key = value_key_for(context, value)
    checks: list[CheckEvidence] = []
    candidates: list[str] = []

    d1 = _check_d1(
        value,
        target_uid=target_uid,
        target_name=target_name,
        target_type=target_type,
        context=context,
    )
    disposition = DISPOSITION_PASS
    if d1 is not None:
        checks.append(d1)
        if d1.check_id in (CHECK_D1_NARROW, CHECK_D1_DOMAIN):
            disposition = DISPOSITION_NON_PERSONAL
            verdict = AttributionVerdict(
                disposition=disposition,
                checks=tuple(checks),
                candidates=(),
                value_key=key,
            )
            log_attribution_verdict(context, verdict, target_uid=target_uid, writer=writer)
            return verdict
        disposition = DISPOSITION_SUSPECT  # D1-role

    holders = tuple(u for u in _holder_uids(context, value) if u != target_uid)

    live_holders = tuple(u for u in holders if _has_live_page(context, u))
    if live_holders:
        checks.append(CheckEvidence(check_id=CHECK_D2_HELD_ELSEWHERE, uids=live_holders))
        disposition = DISPOSITION_SUSPECT
        for u in live_holders:
            if u not in candidates:
                candidates.append(u)

    if target_type is not None and target_type != "person":
        checks.append(CheckEvidence(check_id=CHECK_D3_NOT_A_PERSON, label=target_type))
        disposition = DISPOSITION_SUSPECT
    elif target_name and context.entity_index is not None:
        entry = context.entity_index.lookup(target_name)
        if entry is not None and entry.type == "company":
            checks.append(CheckEvidence(check_id=CHECK_D3_NOT_A_PERSON, label="company"))
            disposition = DISPOSITION_SUSPECT

    if len(holders) >= 2:
        checks.append(CheckEvidence(check_id=CHECK_D4_FAN_OUT, uids=holders))
        disposition = DISPOSITION_SUSPECT
        for u in holders:
            if u not in candidates:
                candidates.append(u)

    verdict = AttributionVerdict(
        disposition=disposition,
        checks=tuple(checks),
        candidates=tuple(candidates),
        value_key=key,
    )
    log_attribution_verdict(context, verdict, target_uid=target_uid, writer=writer)
    return verdict


# ---------------------------------------------------------------------------
# Excluded-surface storage: the attribution log and the suspect park file
# ---------------------------------------------------------------------------


def default_attribution_log_path(contacts_root: Path) -> Path:
    """Default attribution ledger path: ``<contacts_root>/_attribution_log.jsonl``."""
    return Path(contacts_root) / ATTRIBUTION_LOG_FILENAME


def log_attribution_verdict(
    context: AttributionContext,
    verdict: AttributionVerdict,
    *,
    target_uid: str,
    writer: str,
    mode: str = "observe",
) -> None:
    """Append one row to the attribution log. Carries no value (design §4.3).

    One row per verdict: ``value_key``, uids, writer, disposition, check ids
    and mode — never the value itself, so the log is safe to read without
    the per-deployment key.
    """
    row = {
        "v": ATTRIBUTION_LOG_VERSION,
        "value_key": verdict.value_key,
        "target_uid": target_uid,
        "writer": writer,
        "disposition": verdict.disposition,
        "checks": [c.check_id for c in verdict.checks],
        "candidates": list(verdict.candidates),
        "mode": mode,
        "at": now_iso(),
    }
    target = default_attribution_log_path(context.contacts_root)
    append_line_durable(target, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))


def _suspect_path(
    context: AttributionContext, verdict: AttributionVerdict, target_uid: str
) -> Path:
    return (
        Path(context.contacts_root)
        / ATTRIBUTION_SUSPECTS_DIRNAME
        / f"{verdict.value_key}-{target_uid}.md"
    )


def park_suspect(
    context: AttributionContext,
    value: str,
    verdict: AttributionVerdict,
    *,
    target_uid: str,
    target_name: str | None,
    writer: str,
    source_ref: str | dict[str, Any] | None,
    shadow: bool,
) -> Path:
    """Park *value* for *target_uid* on the excluded surface (design §4.3).

    One file per (value, target) pair, at ``<contacts_root>/_attribution/
    suspects/<value_key>-<target_uid>.md``. Idempotent: re-parking the SAME
    (value, target) merges into the existing file rather than duplicating it
    — checks and candidates union (first-seen order), ``shadow`` only ever
    ratchets from ``True`` to ``False`` (once a value has been parked
    non-shadow — i.e. actually enforced, D1-narrow — a later observe-mode
    re-park must never make it look merely observed again). A re-park whose
    merged result is byte-identical to what is already on disk writes
    nothing.
    """
    path = _suspect_path(context, verdict, target_uid)
    existing_meta: dict[str, Any] = {}
    if path.exists():
        existing_meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if not isinstance(existing_meta, dict):
            existing_meta = {}

    existing_checks = [str(c) for c in (existing_meta.get("checks") or [])]
    new_checks = [c.check_id for c in verdict.checks]
    merged_checks = existing_checks + [c for c in new_checks if c not in existing_checks]

    existing_candidates = [str(c) for c in (existing_meta.get("candidates") or [])]
    merged_candidates = existing_candidates + [
        c for c in verdict.candidates if c not in existing_candidates
    ]

    existing_shadow = bool(existing_meta.get("shadow", True))
    merged_shadow = existing_shadow and shadow

    meta: dict[str, object] = {
        "value": value,
        "value_key": verdict.value_key,
        "target_uid": target_uid,
        "target_name": target_name,
        "writer": writer,
        "source": source_ref,
        "disposition": verdict.disposition,
        "checks": merged_checks,
        "candidates": merged_candidates,
        "shadow": merged_shadow,
        "updated_at": now_iso(),
    }
    body = (
        "Attribution suspect entry (issue athenaeum#2210). This record is outside "
        "the corpus: not embedded, recalled, or merge-eligible. Reversible with "
        "`athenaeum attribution place` (design §9.4).\n"
    )
    new_text = render_frontmatter(meta) + "\n" + body
    if path.exists():
        existing_text = path.read_text(encoding="utf-8")
        # Compare on everything except updated_at, so an unchanged re-park
        # (the idempotency property) writes nothing at all.
        existing_compare = dict(existing_meta)
        existing_compare.pop("updated_at", None)
        new_compare = dict(meta)
        new_compare.pop("updated_at", None)
        if existing_compare == new_compare:
            return path
        _ = existing_text  # already decided to rewrite
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, new_text)
    return path


__all__ = [
    "WRITER_OBSERVED_CONTACT",
    "WRITER_CORRECTION_ADD",
    "WRITER_PII_MIGRATION",
    "WRITER_CONTACT_FOLD",
    "WRITER_IDS",
    "DISPOSITION_PASS",
    "DISPOSITION_NON_PERSONAL",
    "DISPOSITION_SUSPECT",
    "CHECK_D1_NARROW",
    "CHECK_D1_DOMAIN",
    "CHECK_D1_ROLE",
    "CHECK_D2_HELD_ELSEWHERE",
    "CHECK_D3_NOT_A_PERSON",
    "CHECK_D4_FAN_OUT",
    "BUILTIN_RELAY_LOCAL_PREFIXES",
    "ROLE_LOCAL_PARTS",
    "ATTRIBUTION_LOG_VERSION",
    "ATTRIBUTION_LOG_FILENAME",
    "CheckEvidence",
    "AttributionVerdict",
    "AttributionContext",
    "is_d1_narrow",
    "build_attribution_context",
    "check_contact_attribution",
    "default_attribution_log_path",
    "log_attribution_verdict",
    "park_suspect",
    "value_key_for",
]
