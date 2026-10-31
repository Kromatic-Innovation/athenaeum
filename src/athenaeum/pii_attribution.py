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
escalation sees all the evidence.

**Child C (athenaeum#2215) adds three more checks, run after D1-D4 on every
value that reaches them:**

- **C1, inline elsewhere** — the value appears as a raw token in another
  uid's wiki page BODY. An inverted index (``value_key`` to uids) is built
  once per :class:`AttributionContext`, from the same token detectors the
  PII lint uses (:func:`athenaeum.pii.find_inline_emails` /
  :func:`athenaeum.pii.find_inline_phones`), never a per-value search.
- **C2, local part names someone else** — the local part of an email value
  matches another known person's full name, or their ``flast``/
  ``first.last``/``firstlast`` form, and does not ALSO match the target's
  own name or aliases. A bare first name never fires. Built once per
  context from :class:`athenaeum.models.EntityIndex` names and aliases.
- **C3, source context** — when ``source_ref`` resolves to a still-existing
  raw file or page, fires when the value is absent from it, or sits within
  a short configurable window of an attribution label or another known
  person's name. Absence of a source is not evidence (design §17 risk 3):
  this check simply does not fire.

Only D1-narrow is enforced; C1-C3 are observe-only evidence exactly like
D1-role/D2-D4 — no writer in this issue's scope changes what it writes
because of a C-check (design's "Observe mode is preserved").

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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import config as config_module
from athenaeum import pii
from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import (
    EntityIndex,
    parse_frontmatter,
    render_frontmatter,
    resolve_page_type,
)
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

#: Child C (athenaeum#2215) check ids. Unlike D1, none of these has a
#: sub-tier -- the design table (§5.1) lists each as a single row.
CHECK_C1_INLINE_ELSEWHERE = "C1"
CHECK_C2_LOCAL_PART_NAMES_OTHER = "C2"
CHECK_C3_SOURCE_CONTEXT = "C3"

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

#: Built-in C3 attribution-label phrases (design §5.1's C3 row), extendable
#: through ``librarian.attribution.context_labels``
#: (:func:`athenaeum.config.resolve_attribution_context_labels`). Matched as
#: a case-insensitive substring of the window of source text around a value
#: occurrence. ``"from:"`` is deliberately NOT here — see
#: :data:`QUOTED_BLOCK_LABEL`.
BUILTIN_CONTEXT_LABELS: frozenset[str] = frozenset(
    {
        "referred by",
        "introduced by",
        "cc",
        "via",
        "on behalf of",
        "forwarded",
    }
)

#: The one C3 label the design singles out as needing EXTRA context (§5.1:
#: ``"from:"`` in a quoted block): on its own, "from:" is far too common a
#: prose token to be evidence, so unlike :data:`BUILTIN_CONTEXT_LABELS` it
#: only counts when the specific occurrence sits on a line that is itself a
#: quoted-block line (starts with ``>``, the plain-text email quote marker,
#: after stripping leading whitespace) -- the shape a forwarded/replied
#: message's quoted headers take. Never operator-extendable, same reasoning
#: as :data:`ROLE_LOCAL_PARTS`: this is a single named shape, not a list.
QUOTED_BLOCK_LABEL: str = "from:"

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
    #: Child C (athenaeum#2215) fields, appended here per the anchor
    #: discipline this issue's dispatch brief lays out (siblings editing
    #: this same module append their own fields after these, never
    #: reordering what is already here).
    #:
    #: :data:`BUILTIN_CONTEXT_LABELS` plus the operator's
    #: ``librarian.attribution.context_labels`` additions (design §5.2) --
    #: C3's "beside an attribution label" signal.
    context_labels: frozenset[str] = frozenset()
    #: C3's proximity window, in characters each side of a value occurrence
    #: (:func:`athenaeum.config.resolve_attribution_context_window_chars`).
    context_window_chars: int = 200
    #: C1's inverted index: HMAC ``value_key`` (:func:`value_key_for`) to the
    #: uids whose wiki page BODY carries that value as a raw inline token.
    #: Built once per context by :func:`_build_child_c_indexes`.
    inline_elsewhere_index: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: C2/C3's person-name index: a name-derived form (a raw "first last"
    #: string, or one of the ``flast``/``first.last``/``firstlast``
    #: abbreviations) to the uids of every ``type: person`` page whose name
    #: or alias produces that form. Built once per context by
    #: :func:`_build_child_c_indexes`.
    person_name_index: dict[str, frozenset[str]] = field(default_factory=dict)


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
    value_key_secret = _load_or_mint_value_key_secret(resolved_contacts_root)
    inline_elsewhere_index, person_name_index = _build_child_c_indexes(ent_idx, value_key_secret)
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
        value_key_secret=value_key_secret,
        context_labels=BUILTIN_CONTEXT_LABELS
        | config_module.resolve_attribution_context_labels(config),
        context_window_chars=config_module.resolve_attribution_context_window_chars(config),
        inline_elsewhere_index=inline_elsewhere_index,
        person_name_index=person_name_index,
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


# ---------------------------------------------------------------------------
# Context checks C1-C3 (athenaeum#2215)
# ---------------------------------------------------------------------------


def _hmac_value_key(secret: bytes, normalized_value: str) -> str:
    """The same keyed hash :func:`value_key_for` computes, taking the raw
    secret directly.

    Duplicated rather than shared because :func:`_build_child_c_indexes`
    runs DURING :func:`build_attribution_context`, before any
    :class:`AttributionContext` exists for :func:`value_key_for` to take —
    there is no context yet to pass it. Both read the same two lines of
    :mod:`hmac`; kept in sync by inspection, not by a shared call, since
    neither is expected to change independent of the other.
    """
    return hmac.new(secret, normalized_value.encode("utf-8"), hashlib.sha256).hexdigest()


def _person_name_forms(name: str) -> frozenset[str]:
    """Candidate local-part-equivalent forms for *name* (design §5.1, C2).

    Returns the empty set for a single-token name (no first/last split to
    abbreviate — design: "A local part with no name signal passes", and a
    single word can never produce ``flast``/``first.last``/``firstlast``).
    For a multi-token name, the LAST token is the surname and only the
    FIRST and last tokens feed the abbreviations — a middle name is dropped
    from ``flast``/``first.last``/``firstlast`` exactly as a real email
    local part normally would be, but is still part of the raw ``spaced``
    form so a full ``"first middle last"`` value is still recognized
    verbatim. All forms are lowercased; none contains the original
    whitespace-insensitive casing.
    """
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    if len(tokens) < 2:
        return frozenset()
    first, last = tokens[0], tokens[-1]
    spaced = " ".join(tokens).lower()
    collapsed_full = "".join(tokens).lower()
    flast = (first[0] + last).lower()
    first_dot_last = f"{first}.{last}".lower()
    first_last = f"{first}{last}".lower()
    return frozenset({spaced, collapsed_full, flast, first_dot_last, first_last})


def _build_child_c_indexes(
    entity_index: EntityIndex | None,
    value_key_secret: bytes,
) -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
    """Build C1's inline-elsewhere index and C2/C3's person-name index.

    ONE combined pass over every uid'd wiki page (design §5.1: "built once
    per run from the same scan the PII lint performs"), so the two indexes
    the design calls out for child C share the single O(corpus) read rather
    than paying it twice. Reads ``entity_index._by_uid`` directly — the same
    private-attribute access :mod:`athenaeum.mcp_server` already uses to
    walk every indexed page by uid; :class:`athenaeum.models.EntityIndex`
    has no public "every (uid, path)" iterator (:meth:`pages_of_type` is
    type-scoped, :meth:`items` walks name/alias keys with one entry per
    alias, duplicating uids).

    With no *entity_index* (a bare unit test exercising the D-checks without
    one, or a knowledge root with no ``wiki/`` dir yet), returns two empty
    dicts — the same conservative "nothing fires" default
    :func:`_has_live_page` picks for D2.

    The inline-elsewhere index is keyed by the HMAC ``value_key``
    (:func:`_hmac_value_key`), never the raw value, so a token this builder
    reads off a page body never lands in the index as plaintext.

    The person-name index is built ONLY from ``type: person`` pages — a
    company's name is D3's concern, not C2/C3's "another known PERSON"
    signal.
    """
    inline_index: dict[str, list[str]] = {}
    name_index: dict[str, set[str]] = {}
    if entity_index is None:
        return {}, {}

    for uid, path in entity_index._by_uid.items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)

        # C1: every email/phone-shaped token in the page BODY (never
        # frontmatter — a page's own declared contact fields are not an
        # "inline elsewhere" leak).
        for token in pii.find_inline_emails(body) + pii.find_inline_phones(body):
            key = _hmac_value_key(value_key_secret, pii.normalize_identifier(token))
            holders = inline_index.setdefault(key, [])
            if uid not in holders:
                holders.append(uid)

        # C2/C3: person-name forms, person pages only.
        if resolve_page_type(meta) != "person":
            continue
        name_raw = meta.get("name") if isinstance(meta, dict) else None
        display_names = [str(name_raw)] if name_raw else []
        aliases_raw = meta.get("aliases") if isinstance(meta, dict) else None
        if isinstance(aliases_raw, list):
            display_names.extend(str(a) for a in aliases_raw if a)
        for display_name in display_names:
            for form in _person_name_forms(display_name):
                name_index.setdefault(form, set()).add(uid)

    frozen_inline = {key: tuple(uids) for key, uids in inline_index.items()}
    frozen_names = {form: frozenset(uids) for form, uids in name_index.items()}
    return frozen_inline, frozen_names


def _check_c1(
    value_key: str,
    *,
    target_uid: str,
    context: AttributionContext,
) -> CheckEvidence | None:
    """C1: the value is inline on another uid's page body (design §5.1)."""
    holders = tuple(
        u for u in context.inline_elsewhere_index.get(value_key, ()) if u != target_uid
    )
    if not holders:
        return None
    return CheckEvidence(check_id=CHECK_C1_INLINE_ELSEWHERE, uids=holders)


def _check_c2(
    value: str,
    *,
    target_uid: str,
    context: AttributionContext,
) -> CheckEvidence | None:
    """C2: the local part names a different known person (design §5.1).

    Inert for a value with no ``@`` (a phone number): there is no local
    part to carry a name signal. Checks the local part BOTH as extracted
    (so ``jane.smith`` matches the ``first.last`` form literally) and with
    ``.``/``_``/``-`` collapsed out (so it also matches ``flast``/
    ``firstlast``). A first-name-only local part never matches because
    :func:`_person_name_forms` never emits a bare-first-name form at all —
    there is no key for it to hit.

    "Does not match the target's name or aliases" (design) and "a local
    part matching BOTH the target and someone else passes" (issue AC) are
    the SAME rule applied to one matched-uid set: if the target's own uid
    is among the forms matched, this passes outright, regardless of who
    else also matched.
    """
    local, domain = _local_and_domain(value)
    if not domain:
        return None  # not email-shaped; no local-part name signal applies
    local_collapsed = local.replace(".", "").replace("_", "").replace("-", "")
    if not local_collapsed:
        return None

    matched: list[str] = []
    for form in (local, local_collapsed):
        for uid in context.person_name_index.get(form, ()):
            if uid not in matched:
                matched.append(uid)
    if not matched:
        return None
    if target_uid in matched:
        return None  # matches the target's own name/alias (also matching
        # someone else does not change that — issue AC: "both match" passes)
    return CheckEvidence(check_id=CHECK_C2_LOCAL_PART_NAMES_OTHER, uids=tuple(matched))


def _extract_source_candidate_path(
    source_ref: str | dict[str, Any] | None,
) -> Path | None:
    """Pull the path *source_ref* names, without touching the filesystem.

    ``source_ref`` is deliberately loose at this seam's call sites (design
    §4.1): a bare path string (:mod:`athenaeum.storage_migrate`'s
    ``str(page_path)``), or a structured mapping carrying a ``path``/``ref``
    key. This names EXACTLY that path, as given (absolute, or relative to
    the process's current directory, same as any other string path this
    codebase opens directly) -- it does not search ``raw/`` or ``wiki/`` for
    a same-named file under a different root, because :attr:`context.
    contacts_root` is, by design, independently operator-configurable
    (:func:`athenaeum.pii.excluded_surface_root`) and so cannot be walked
    back to a ``knowledge_root`` this function could trust in general.

    Returns ``None`` for no *source_ref* or an unrecognized shape. Split out
    from :func:`_resolve_source_text` (which actually reads the file) so
    :func:`_check_c3`'s self-citation guard can compare the CANDIDATE path
    against the target's own page before deciding whether to read anything.
    """
    candidate: str | None = None
    if isinstance(source_ref, dict):
        for key in ("path", "ref", "file"):
            raw = source_ref.get(key)
            if isinstance(raw, str) and raw.strip():
                candidate = raw.strip()
                break
    elif isinstance(source_ref, str):
        candidate = source_ref.strip()
    if not candidate:
        return None
    return Path(candidate)


def _resolve_source_text(path: Path) -> str | None:
    """Best-effort read of *path* (C3).

    Returns ``None`` for anything that does not resolve to an existing,
    UTF-8-readable file: a path that no longer exists, or one this process
    cannot read as text. This is C3's "source missing" branch (design §17
    risk 3: "absence of a source is not evidence") — never treated as a hit.
    """
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _same_file(a: Path, b: Path) -> bool:
    """True when *a* and *b* name the same file on disk.

    Compares RESOLVED paths (falling back to ``absolute()`` when a path
    does not exist, e.g. a still-uncommitted write), not raw strings, so a
    relative-vs-absolute or symlinked spelling of the same file still
    counts as the same file (athenaeum#2215 self-citation guard,
    :func:`_check_c3`).
    """

    def _norm(p: Path) -> Path:
        try:
            return p.resolve()
        except OSError:  # pragma: no cover - defensive (unresolvable path)
            return p.absolute()

    return _norm(a) == _norm(b)


def _value_occurrences(source_text: str, value: str) -> list[tuple[int, int]]:
    """Every ``(start, end)`` span where *value* appears in *source_text*.

    Reuses :func:`athenaeum.pii.find_inline_emails` /
    :func:`athenaeum.pii.find_inline_phones` to decide WHETHER *value*
    appears at all (the same token detectors the PII lint and C1 use, per
    the issue body), then locates every verbatim occurrence of each
    matching token's own exact text for position. An empty result is C3's
    "value is absent from it" branch.
    """
    normalized_value = pii.normalize_identifier(value)
    tokens = pii.find_inline_emails(source_text) + pii.find_inline_phones(source_text)
    positions: list[tuple[int, int]] = []
    for token in tokens:
        if pii.normalize_identifier(token) != normalized_value:
            continue
        for m in re.finditer(re.escape(token), source_text):
            positions.append((m.start(), m.end()))
    return positions


def _quoted_block_label_hit(source_text: str, window_start: int, window_text: str) -> bool:
    """True when *window_text* carries :data:`QUOTED_BLOCK_LABEL` on a line
    that is itself a quoted-block line (starts with ``>`` after stripping
    leading whitespace) in *source_text* as a whole."""
    for m in re.finditer(re.escape(QUOTED_BLOCK_LABEL), window_text, re.IGNORECASE):
        abs_pos = window_start + m.start()
        line_start = source_text.rfind("\n", 0, abs_pos) + 1
        line_end = source_text.find("\n", abs_pos)
        if line_end == -1:
            line_end = len(source_text)
        if source_text[line_start:line_end].lstrip().startswith(">"):
            return True
    return False


def _name_proximity_hit(
    lowered_window: str, context: AttributionContext, target_uid: str
) -> str | None:
    """A non-target person uid whose raw "first [middle] last" name form
    (the person-name index's only SPACED keys — the abbreviated forms are
    deliberately excluded here, since ``jsmith`` turning up inside ordinary
    prose is not the same signal as a full name turning up) appears as a
    substring of *lowered_window*. ``None`` when no such form appears."""
    for form, uids in context.person_name_index.items():
        if " " not in form:
            continue
        others = tuple(u for u in uids if u != target_uid)
        if not others:
            continue
        if form in lowered_window:
            return others[0]
    return None


def _check_c3(
    value: str,
    *,
    target_uid: str,
    source_ref: str | dict[str, Any] | None,
    context: AttributionContext,
) -> CheckEvidence | None:
    """C3: source context (design §5.1; §17 risk 3 for the missing-source case).

    **Self-citation guard (athenaeum#2215, implementation-time decision —
    reversible, since the design doc does not state it literally).** Does
    NOT fire when *source_ref* resolves to the TARGET's own entity page.
    The reason in one line: a record is not context evidence about itself.
    Concretely: the ``pii-migration`` writer (:mod:`athenaeum.
    storage_migrate`) passes the page being migrated as that very value's
    own ``source_ref``, so without this guard, any OTHER value already on
    that same short page (the target's own address, the target's own
    phone number) gets measured against whatever label or name happens to
    sit elsewhere on that page and gets wrongly flagged -- a false
    positive by construction, since nothing EXTERNAL to the page was ever
    consulted. The guard is deliberately narrow: a *source_ref* that
    resolves to a DIFFERENT uid's page still runs every check below
    normally -- a value sitting next to an attribution label or a name on
    someone ELSE's page is exactly the evidence this check exists to find.
    """
    candidate_path = _extract_source_candidate_path(source_ref)
    if candidate_path is None:
        return None

    if context.entity_index is not None:
        target_page = context.entity_index.get_by_uid(target_uid)
        if target_page is not None and _same_file(candidate_path, target_page):
            return None  # self-citation: not context evidence about itself

    source_text = _resolve_source_text(candidate_path)
    if source_text is None:
        return None  # absence of a source is not evidence

    occurrences = _value_occurrences(source_text, value)
    if not occurrences:
        return CheckEvidence(check_id=CHECK_C3_SOURCE_CONTEXT, label="value-absent")

    window_chars = context.context_window_chars
    for start, end in occurrences:
        window_start = max(0, start - window_chars)
        window_end = min(len(source_text), end + window_chars)
        window_text = source_text[window_start:window_end]
        lowered_window = window_text.lower()

        for label in context.context_labels:
            if label in lowered_window:
                return CheckEvidence(check_id=CHECK_C3_SOURCE_CONTEXT, label=f"label:{label}")

        if QUOTED_BLOCK_LABEL in lowered_window and _quoted_block_label_hit(
            source_text, window_start, window_text
        ):
            return CheckEvidence(
                check_id=CHECK_C3_SOURCE_CONTEXT, label=f"label:{QUOTED_BLOCK_LABEL}"
            )

        other_uid = _name_proximity_hit(lowered_window, context, target_uid)
        if other_uid is not None:
            return CheckEvidence(
                check_id=CHECK_C3_SOURCE_CONTEXT, uids=(other_uid,), label="name-proximity"
            )

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

    # Child C (athenaeum#2215): run after every D-check, on every value that
    # reaches here — a suspect disposition from D1-role/D2-D4 does not stop
    # the run, so the evidence stays complete (design §5.1).
    c1 = _check_c1(key, target_uid=target_uid, context=context)
    if c1 is not None:
        checks.append(c1)
        disposition = DISPOSITION_SUSPECT
        for u in c1.uids:
            if u not in candidates:
                candidates.append(u)

    c2 = _check_c2(value, target_uid=target_uid, context=context)
    if c2 is not None:
        checks.append(c2)
        disposition = DISPOSITION_SUSPECT
        for u in c2.uids:
            if u not in candidates:
                candidates.append(u)

    c3 = _check_c3(value, target_uid=target_uid, source_ref=source_ref, context=context)
    if c3 is not None:
        checks.append(c3)
        disposition = DISPOSITION_SUSPECT
        for u in c3.uids:
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
    "CHECK_C1_INLINE_ELSEWHERE",
    "CHECK_C2_LOCAL_PART_NAMES_OTHER",
    "CHECK_C3_SOURCE_CONTEXT",
    "BUILTIN_RELAY_LOCAL_PREFIXES",
    "ROLE_LOCAL_PARTS",
    "BUILTIN_CONTEXT_LABELS",
    "QUOTED_BLOCK_LABEL",
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
