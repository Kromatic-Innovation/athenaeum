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
call site), :mod:`athenaeum.calibration` (L3 sibling, issue athenaeum#2219 —
the audit sampler + the threshold ledger-override reader's action-id
helper; no cycle either, ``calibration.py`` never imports this module),
:mod:`athenaeum.config` (L2), :mod:`athenaeum.models` (L1), and
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
import importlib.resources
import json
import logging
import os
import re
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from athenaeum import config as config_module
from athenaeum import pii
from athenaeum._retry import with_retry
from athenaeum.atomic_io import atomic_write_text
from athenaeum.calibration import (
    ATTRIBUTION_TIER_NAME,
    attribution_threshold_action_id,
    read_calibration_ledger,
    record_audit_review,
    sample_tier_decision,
)
from athenaeum.calibration import AUDIT_KIND as _CALIBRATION_AUDIT_KIND
from athenaeum.json_utils import extract_json_object
from athenaeum.models import (
    EntityIndex,
    EscalationItem,
    TokenUsage,
    cache_usage_counts,
    parse_frontmatter,
    render_frontmatter,
    resolve_page_type,
)
from athenaeum.prompt_safety import fence_untrusted
from athenaeum.provider import (
    LLMBackend,
    resolve_max_tokens,
    resolve_thinking,
    response_text,
)
from athenaeum.store import append_line_durable, now_iso

if TYPE_CHECKING:
    from anthropic.types import MessageParam, ThinkingConfigParam

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
#: issue athenaeum#2216 (prose-compile scrub): the tier-3 create/merge
#: convergence helpers' writer id — see "Prose-compile scrub" below.
WRITER_PROSE_COMPILE = "prose-compile"

#: issue athenaeum#2212 (model escalation / re-attribution): the placement
#: executor's own writer id. Design §7.2 — "every filing goes back through
#: :func:`check_contact_attribution` with ``writer="re-attribution"`` before
#: it writes" — so the re-check a placement performs is attributable to the
#: placement itself rather than to whichever writer originally proposed the
#: value.
WRITER_RE_ATTRIBUTION = "re-attribution"

#: The four writers issue athenaeum#2210 wires, plus athenaeum#2216's
#: prose-compile scrub and athenaeum#2212's re-attribution placement.
#: Membership, not count, is what the writer-inventory guard test asserts
#: (future children extend this set further).
WRITER_IDS: tuple[str, ...] = (
    WRITER_OBSERVED_CONTACT,
    WRITER_CORRECTION_ADD,
    WRITER_PII_MIGRATION,
    WRITER_CONTACT_FOLD,
    WRITER_PROSE_COMPILE,
    WRITER_RE_ATTRIBUTION,
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
    #: Issue athenaeum#2219 (design §8.3): ``knowledge_root / "wiki"``,
    #: unconditionally (unlike :attr:`entity_index`, which is only built
    #: when that directory already exists) -- the calibration ledger and
    #: the attribution-threshold LEDGER OVERRIDE both live there, and both
    #: tolerate a not-yet-existing path (the sampler call site below checks
    #: ``.is_dir()`` itself; the ledger-override reader already returns
    #: ``None`` for a missing file). ``None`` only when a caller builds an
    #: :class:`AttributionContext` with no ``knowledge_root`` at all, which
    #: no current constructor path does -- kept optional for forward
    #: compatibility rather than widening :func:`build_attribution_context`'s
    #: own required arguments.
    wiki_root: Path | None = None


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
    resolved_wiki_root = root / "wiki"
    ent_idx = entity_index
    if ent_idx is None:
        if resolved_wiki_root.is_dir():
            ent_idx = EntityIndex(resolved_wiki_root)
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
    inline_elsewhere_index, person_name_index = _cached_child_c_indexes(ent_idx, value_key_secret)
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
        wiki_root=resolved_wiki_root,
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


#: Memoizes :func:`_build_child_c_indexes`'s result against the IDENTITY of
#: the :class:`EntityIndex` object a caller handed to
#: :func:`build_attribution_context` (PR review finding, issue
#: athenaeum#2258). Keyed on identity, never on the knowledge-root path or
#: wall-clock time: a caller handing in the SAME ``EntityIndex`` object is
#: vouching that the corpus it describes has not moved since that index was
#: built, so reusing the cached scan is safe; a caller that builds a FRESH
#: ``EntityIndex`` gets a fresh scan, because the corpus genuinely may have
#: changed, and a stale C1 index would produce a silently missed or
#: spurious suspect -- far worse than the scan it would have saved.
#:
#: Every caller in this codebase resolves ``value_key_secret`` from the
#: SAME ``contacts_root`` as the ``entity_index`` it hands in, so identity
#: on the index implies the same secret TODAY -- but that is a convention,
#: not something this cache can take on faith: a violation would silently
#: hand back an index whose ``value_key``s were hashed under a DIFFERENT
#: HMAC key, which no caller could ever detect from the result shape alone
#: (issue athenaeum#2258 PR review, second round). So the secret rides
#: alongside the cached pair and is checked on every hit
#: (:func:`_cached_child_c_indexes`) -- a mismatch is treated as a cache
#: miss and rebuilt, never raised, turning a silent-wrong-answer failure
#: mode into a self-correcting one for the cost of one ``bytes`` comparison.
#:
#: A :class:`weakref.WeakKeyDictionary`, not a plain module-level dict, so
#: this never pins (leaks) an ``EntityIndex`` the rest of the process has
#: already let go of across a long-running process.
_ChildCIndexes = tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]
_CHILD_C_INDEX_CACHE: "weakref.WeakKeyDictionary[EntityIndex, tuple[bytes, _ChildCIndexes]]" = (
    weakref.WeakKeyDictionary()
)


def _cached_child_c_indexes(
    entity_index: EntityIndex | None,
    value_key_secret: bytes,
) -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
    """:func:`_build_child_c_indexes`, memoized per *entity_index* identity.

    The ONLY seam :func:`build_attribution_context` calls — several of this
    seam's own callers construct a context once per record/batch-file
    rather than once per run, passing in an *already-built* ``entity_index``
    specifically so that reuse is cheap (see e.g. ``answers.py``'s
    ``_apply_field_correction`` and ``corrections.py``'s
    ``process_batch_file``); without this memo, every one of those calls
    would re-scan the whole corpus's page bodies, turning a per-record loop
    into an O(corpus)-per-record one (issue athenaeum#2258 PR review). With
    no *entity_index* at all, there is nothing to key a cache on and this
    is simply :func:`_build_child_c_indexes`'s own already-cheap empty-dict
    path -- no cache lookup is attempted.

    A cache hit whose stored *value_key_secret* does not match the one this
    call resolved is treated exactly like a cache miss: rebuilt, and the
    cache entry replaced. The index this seam built the C1 index's keys
    under was wrong under the NEW secret, so returning it would mean a
    hashed value this call can never match, or a hashed value that
    collides with a different plaintext under the new key — either way a
    silently wrong C1 answer. See :data:`_CHILD_C_INDEX_CACHE` for why this
    is checked rather than merely documented as an invariant.
    """
    if entity_index is None:
        return _build_child_c_indexes(entity_index, value_key_secret)
    cached = _CHILD_C_INDEX_CACHE.get(entity_index)
    if cached is not None:
        cached_secret, cached_indexes = cached
        if cached_secret == value_key_secret:
            return cached_indexes
    built = _build_child_c_indexes(entity_index, value_key_secret)
    _CHILD_C_INDEX_CACHE[entity_index] = (value_key_secret, built)
    return built


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
# Prose-compile scrub (athenaeum#2216)
# ---------------------------------------------------------------------------


def scrub_compiled_contact_tokens(
    body: str,
    *,
    subject_uid: str,
    source_ref: str | dict[str, Any] | None,
    context: AttributionContext,
    target_name: str | None = None,
    target_type: str | None = None,
) -> tuple[str, list[str]]:
    """Remove every contact token from tier-3 compiled output (design §10.2).

    Child G of epic athenaeum#2192. The tier-3 create and merge prompts can
    write an address or phone number mentioned in a note into the body of
    whichever page they compile — this is the one place that is undone
    before the body reaches disk, on EVERY caller that converges on
    :func:`athenaeum.tiers.tier3_entity_from_response`,
    :func:`athenaeum.tiers.parse_merge_ops_response` or
    :func:`athenaeum.tiers.tier3_merge_full` (both the synchronous and the
    batch transport). Returns ``(scrubbed_body, removed_tokens)``; a body
    with no email/phone-shaped token comes back byte-identical (same
    string), and ``removed_tokens`` is empty.

    Finds tokens with the existing detectors
    (:func:`athenaeum.pii.find_inline_emails` /
    :func:`athenaeum.pii.find_inline_phones`) and removes every occurrence of
    each one, replacing it with nothing (design §10.2's first option — a
    neutral marker is the design's other sanctioned option, not needed here
    since nothing downstream re-parses the body for sentence structure).

    **The athenaeum#1461 name-field carve-out.** A token that IS *target_name*'s
    own email/phone-shaped value (the athenaeum#502 "name is a raw contact
    address" population) is a durable identifier, most visibly echoed back
    verbatim in a page's own heading — exactly the token
    :func:`athenaeum.storage_migrate.plan_pii_migration` preserves rather
    than redacts. Reused here via
    :func:`athenaeum.pii.name_field_pii_values` rather than re-derived, so
    the two call sites can never drift apart on what counts as the carve-out.

    **Nothing is lost.** Every removed token (the carve-out above means a
    name-field token is never a removal candidate in the first place) is
    sent through :func:`check_contact_attribution` with
    ``writer="prose-compile"``, *source_ref* as the source and *subject_uid*
    as the target, regardless of the verdict's disposition (unlike the other
    four writers, which only park a non-``pass`` verdict, this writer has no
    "file it on the record" path at all in observe mode, so a ``pass`` token
    would otherwise vanish with no trace). It is then parked via
    :func:`park_suspect` as a **shadow** suspect (design §10.2: "In observe
    mode, every token is parked as a shadow entry" — unlike the other
    writers' ``shadow=not is_d1_narrow(verdict)``, this writer removes the
    token from the body unconditionally regardless of disposition, so there
    is no enforce-vs-observe distinction for the shadow flag to encode until
    the enforce child (athenaeum#2220) adds the "file a pass on the
    subject's record" path design §10.2 describes).
    """
    tokens = pii.find_inline_emails(body) + pii.find_inline_phones(body)
    if not tokens:
        return body, []

    name_pii_values = (
        set(pii.name_field_pii_values({"name": target_name})) if target_name else set()
    )

    scrubbed = body
    removed: list[str] = []
    for token in tokens:
        if token in name_pii_values:
            continue
        verdict = check_contact_attribution(
            token,
            target_uid=subject_uid,
            target_name=target_name,
            target_type=target_type,
            source_ref=source_ref,
            writer=WRITER_PROSE_COMPILE,
            context=context,
        )
        park_suspect(
            context,
            token,
            verdict,
            target_uid=subject_uid,
            target_name=target_name,
            writer=WRITER_PROSE_COMPILE,
            source_ref=source_ref,
            # NOT `shadow=not is_d1_narrow(verdict)` (the other four writers'
            # convention) — design §10.2 states shadow=True unconditionally
            # for THIS writer, and prose-compile never files a value onto a
            # record regardless of disposition, so there is no
            # enforce-vs-shadow distinction here to encode yet (athenaeum#2216).
            shadow=True,
        )
        scrubbed = scrubbed.replace(token, "")
        removed.append(token)

    return scrubbed, removed


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
    # Issue athenaeum#2212: keys a LATER actor stamped on this file and this
    # function does not author. Carried forward verbatim, appended after
    # everything above so the pre-athenaeum#2212 key order is untouched.
    # Without this, a routine observe-mode re-park would rebuild `meta` from
    # its own inputs and silently drop the recorded judgement
    # (:func:`record_suspect_judgement`). The idempotency property below is
    # unaffected: an unchanged re-park still produces identical merged bytes.
    for carried in _SUSPECT_CARRIED_FORWARD_KEYS:
        if carried in existing_meta:
            meta[carried] = existing_meta[carried]
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
    "WRITER_PROSE_COMPILE",
    "WRITER_RE_ATTRIBUTION",
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
    "scrub_compiled_contact_tokens",
    "default_attribution_log_path",
    "log_attribution_verdict",
    "park_suspect",
    "value_key_for",
]


# ---------------------------------------------------------------------------
# Escalation: the judge function and the calibration record (athenaeum#2211)
# ---------------------------------------------------------------------------
#
# Child D of epic athenaeum#2192, designed in
# ``docs/design/attribution-verification.md`` §6 ("Escalation contract") and
# §12 ("The calibration eval"). Appended at the very end of the module
# (after :func:`park_suspect` and the module's original ``__all__``) rather
# than interleaved with the checks above: two sibling lanes (issues
# athenaeum#2215, athenaeum#2216) edit this same file concurrently, in the
# regions between :func:`_check_d1` and :func:`check_contact_attribution`,
# and between :func:`check_contact_attribution` and
# :func:`default_attribution_log_path`, respectively. This section never
# touches either region, and the module's new public names are exported via
# a SECOND ``__all__ +=`` below rather than editing the original list.
#
# **EntityBrief** (design §6.2) is not defined anywhere else in the tree:
# issue athenaeum#2210 (child B) only carries bare uid strings in
# :class:`AttributionVerdict.candidates`, and the design cites
# ``EntityBrief`` only in §6.2's ``AttributionQuestion`` shape, with no
# separate section defining its fields beyond the inline comment ("uid,
# name, type, aliases, employer/domain hints"). Issue athenaeum#2215's own
# C1-C3 checks are the other plausible owner (its candidates feed
# ``AttributionQuestion.candidates``), but neither this issue's brief nor
# the design assigns it there explicitly, so it is defined HERE, next to
# its only other consumer in this codebase today. If athenaeum#2215 also
# defines an ``EntityBrief``, the two definitions need reconciling at merge
# time — flagged in this lane's report to the orchestrator.


@dataclass(frozen=True)
class EntityBrief:
    """A lightweight entity reference carried on an escalation question.

    Design §6.2's field list for ``AttributionQuestion.target`` /
    ``.holders`` / ``.candidates``: "uid, name, type, aliases, employer/
    domain hints". Deliberately minimal — just enough identity for the judge
    prompt to reason about ownership, never a full :class:`EntityIndex`
    lookup result.
    """

    uid: str
    name: str
    type: str | None = None
    aliases: tuple[str, ...] = ()
    employer_domain_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttributionQuestion:
    """The escalation's input for one (value, target) pair (design §6.2).

    Built by a caller from a :class:`AttributionVerdict`'s evidence once the
    seam (:func:`check_contact_attribution`) has returned ``suspect`` — the
    escalation itself only ever runs on a ``suspect`` verdict (design §6.1).
    """

    value: str
    target: EntityBrief
    holders: tuple[EntityBrief, ...] = ()
    candidates: tuple[EntityBrief, ...] = ()
    checks: tuple[CheckEvidence, ...] = ()
    source_excerpt: str | None = None


#: Verdict vocabulary (design §6.3). ``unclear`` is the fail-closed verdict:
#: no client, an API error, unparseable output, or an out-of-vocabulary
#: verdict from the model all yield this with ``score=0``.
JUDGEMENT_OWN = "own"
JUDGEMENT_OTHER_KNOWN = "other-known"
JUDGEMENT_OTHER_UNKNOWN = "other-unknown"
JUDGEMENT_ALIAS = "alias"
JUDGEMENT_SAME_ENTITY = "same-entity"
JUDGEMENT_NON_PERSONAL = "non-personal"
JUDGEMENT_UNCLEAR = "unclear"

JUDGEMENT_VERDICTS: tuple[str, ...] = (
    JUDGEMENT_OWN,
    JUDGEMENT_OTHER_KNOWN,
    JUDGEMENT_OTHER_UNKNOWN,
    JUDGEMENT_ALIAS,
    JUDGEMENT_SAME_ENTITY,
    JUDGEMENT_NON_PERSONAL,
    JUDGEMENT_UNCLEAR,
)

#: ``alias_relation`` vocabulary for a :data:`JUDGEMENT_ALIAS` verdict
#: (design §6.3).
ALIAS_RELATION_ROLE_INBOX = "role-inbox"
ALIAS_RELATION_SHARED = "shared"
ALIAS_RELATION_FORMER_NAME = "former-name"
ALIAS_RELATION_PERSONAL_ALIAS = "personal-alias"

ALIAS_RELATIONS: tuple[str, ...] = (
    ALIAS_RELATION_ROLE_INBOX,
    ALIAS_RELATION_SHARED,
    ALIAS_RELATION_FORMER_NAME,
    ALIAS_RELATION_PERSONAL_ALIAS,
)


@dataclass(frozen=True)
class AttributionJudgement:
    """The escalation's answer for one :class:`AttributionQuestion` (design §6.3).

    ``owner_uid`` is set for :data:`JUDGEMENT_OTHER_KNOWN` and
    :data:`JUDGEMENT_SAME_ENTITY`. ``alias_relation`` is set for
    :data:`JUDGEMENT_ALIAS`. ``score`` is the raw confidence signal (design
    §6.5) — ``0.0`` on every fail-closed path. ``model``/``prompt_sha256``
    are always populated, even on a fail-closed answer, so a caller can log
    which model/prompt an ``unclear`` verdict came from.
    """

    verdict: str
    owner_uid: str | None = None
    alias_relation: str | None = None
    quote: str | None = None
    score: float = 0.0
    model: str = ""
    prompt_sha256: str = ""


# ---------------------------------------------------------------------------
# Question classes (design §6.6)
# ---------------------------------------------------------------------------

QUESTION_CLASS_HELD_ELSEWHERE = "held-elsewhere"
QUESTION_CLASS_NAMES_OTHER = "names-other"
QUESTION_CLASS_SOURCE_CONTEXT = "source-context"
QUESTION_CLASS_NON_PERSON_TARGET = "non-person-target"
QUESTION_CLASS_ROLE_UNMATCHED = "role-unmatched"

QUESTION_CLASSES: tuple[str, ...] = (
    QUESTION_CLASS_HELD_ELSEWHERE,
    QUESTION_CLASS_NAMES_OTHER,
    QUESTION_CLASS_SOURCE_CONTEXT,
    QUESTION_CLASS_NON_PERSON_TARGET,
    QUESTION_CLASS_ROLE_UNMATCHED,
)

#: Design §6.6's table, in order (strongest ownership evidence first — the
#: order IS the tie-break when several classes' checks fired). The C1-C3
#: check-id strings are issue athenaeum#2215's (that issue's own C1-C3
#: checks, landing in this same module's ``_check_d1``-to-
#: ``check_contact_attribution`` region) — referenced here as the literal
#: strings design §5's naming convention predicts ("C1", "C2", "C3"), not as
#: imported constants, because athenaeum#2215 may not have landed yet in
#: this checkout. This function needs no edit once it does, only the check
#: ids a :class:`CheckEvidence` actually carries need to match.
_QUESTION_CLASS_TRIGGERS: tuple[tuple[str, frozenset[str]], ...] = (
    (QUESTION_CLASS_HELD_ELSEWHERE, frozenset({CHECK_D2_HELD_ELSEWHERE, CHECK_D4_FAN_OUT, "C1"})),
    (QUESTION_CLASS_NAMES_OTHER, frozenset({"C2"})),
    (QUESTION_CLASS_SOURCE_CONTEXT, frozenset({"C3"})),
    (QUESTION_CLASS_NON_PERSON_TARGET, frozenset({CHECK_D3_NOT_A_PERSON})),
    (QUESTION_CLASS_ROLE_UNMATCHED, frozenset({CHECK_D1_ROLE})),
)


def question_class_for(checks: tuple[CheckEvidence, ...]) -> str:
    """The question class *checks* belongs to (design §6.6).

    Table order is the tie-break: when several classes' checks fired, the
    first one in :data:`_QUESTION_CLASS_TRIGGERS`'s order wins. Falls back
    to :data:`QUESTION_CLASS_ROLE_UNMATCHED` — the weakest-evidence class —
    when nothing in the table matches, rather than raising, so a judge call
    always resolves to some class.
    """
    fired = {c.check_id for c in checks}
    for question_class, trigger_ids in _QUESTION_CLASS_TRIGGERS:
        if fired & trigger_ids:
            return question_class
    return QUESTION_CLASS_ROLE_UNMATCHED


# ---------------------------------------------------------------------------
# The calibration record (design §12.4)
# ---------------------------------------------------------------------------

#: Signal vocabulary (design §6.5).
SIGNAL_SELF_REPORTED = "self-reported"
SIGNAL_AGREEMENT_K3 = "agreement-k3"
SIGNAL_CLASS_LEVEL = "class-level"

SIGNALS: tuple[str, ...] = (SIGNAL_SELF_REPORTED, SIGNAL_AGREEMENT_K3, SIGNAL_CLASS_LEVEL)

#: Packaged location of the shipped calibration record (design §12.4):
#: ``importlib.resources``-loaded, like
#: :func:`athenaeum.erasure.available_retention_packs`'s packaged packs —
#: never a bare :class:`~pathlib.Path` literal, so the loader works
#: identically from an editable install and from the built wheel.
CALIBRATION_RECORD_PACKAGE = "athenaeum.calibration_data"
CALIBRATION_RECORD_FILENAME = "contact_attribution.yaml"

_CALIBRATION_ENTRY_REQUIRED_FIELDS: tuple[str, ...] = (
    "question_class",
    "model",
    "prompt_sha256",
    "signal",
    "bins",
    "threshold",
    "n_fit",
    "n_holdout",
    "holdout_accuracy_above",
    "holdout_wilson_lb",
    "abstain_share",
    "dataset_sha256",
    "measured_at",
    "passes",
)


class CalibrationRecordError(ValueError):
    """The shipped calibration record failed schema validation.

    Raised at LOAD time, never half-swallowed: a corrupt record must fail
    loudly rather than silently degrade a tier decision (design §12.4).
    """


@dataclass(frozen=True)
class CalibrationEntry:
    """One question class's calibration row (design §12.4's YAML shape).

    ``ece``/``auroc`` are ``None`` for a :data:`SIGNAL_CLASS_LEVEL` entry —
    design §12.3: "Signal 3 has no ECE", and by the same reasoning no AUROC
    either, since there is no scalar score to rank.
    """

    question_class: str
    model: str
    prompt_sha256: str
    signal: str
    bins: tuple[tuple[float, float, float], ...]
    threshold: float
    n_fit: int
    n_holdout: int
    holdout_accuracy_above: float
    holdout_wilson_lb: float
    abstain_share: float
    dataset_sha256: str
    measured_at: str
    passes: bool
    ece: float | None = None
    auroc: float | None = None


def _calibration_entry_from_dict(raw: dict[str, Any]) -> CalibrationEntry:
    missing = [f for f in _CALIBRATION_ENTRY_REQUIRED_FIELDS if f not in raw]
    if missing:
        raise CalibrationRecordError(
            f"calibration entry missing required field(s): {missing}"
        )
    question_class = str(raw["question_class"])
    if question_class not in QUESTION_CLASSES:
        raise CalibrationRecordError(f"unknown question_class {question_class!r}")
    signal = str(raw["signal"])
    if signal not in SIGNALS:
        raise CalibrationRecordError(
            f"unknown signal {signal!r} for class {question_class!r}"
        )
    bins_raw = raw.get("bins") or []
    try:
        bins = tuple((float(b[0]), float(b[1]), float(b[2])) for b in bins_raw)
    except (TypeError, ValueError, IndexError) as exc:
        raise CalibrationRecordError(
            f"malformed bins for class {question_class!r}: {bins_raw!r}"
        ) from exc
    try:
        return CalibrationEntry(
            question_class=question_class,
            model=str(raw["model"]),
            prompt_sha256=str(raw["prompt_sha256"]),
            signal=signal,
            bins=bins,
            threshold=float(raw["threshold"]),
            n_fit=int(raw["n_fit"]),
            n_holdout=int(raw["n_holdout"]),
            holdout_accuracy_above=float(raw["holdout_accuracy_above"]),
            holdout_wilson_lb=float(raw["holdout_wilson_lb"]),
            ece=float(raw["ece"]) if raw.get("ece") is not None else None,
            auroc=float(raw["auroc"]) if raw.get("auroc") is not None else None,
            abstain_share=float(raw["abstain_share"]),
            dataset_sha256=str(raw["dataset_sha256"]),
            measured_at=str(raw["measured_at"]),
            passes=bool(raw["passes"]),
        )
    except (TypeError, ValueError) as exc:
        raise CalibrationRecordError(
            f"malformed calibration entry for class {question_class!r}: {exc}"
        ) from exc


def load_calibration_record() -> dict[str, CalibrationEntry]:
    """Load the shipped contact-attribution calibration record, by class.

    Raises :class:`CalibrationRecordError` on any schema violation
    (duplicate class, unknown class/signal, missing/malformed field) —
    never returns a partial record.
    """
    resource = importlib.resources.files(CALIBRATION_RECORD_PACKAGE).joinpath(
        CALIBRATION_RECORD_FILENAME
    )
    raw_text = resource.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw_text) or []
    if not isinstance(loaded, list):
        raise CalibrationRecordError(
            f"{CALIBRATION_RECORD_FILENAME} must parse to a list of entries, "
            f"got {type(loaded).__name__}"
        )
    by_class: dict[str, CalibrationEntry] = {}
    for row in loaded:
        if not isinstance(row, dict):
            raise CalibrationRecordError(f"calibration entry is not a mapping: {row!r}")
        entry = _calibration_entry_from_dict(row)
        if entry.question_class in by_class:
            raise CalibrationRecordError(
                f"duplicate calibration entry for class {entry.question_class!r}"
            )
        by_class[entry.question_class] = entry
    return by_class


def recommended_model_for_class(question_class: str) -> str | None:
    """The calibration record's recommended model for *question_class*.

    ``None`` when the record cannot be loaded, carries no entry for this
    class, or that entry does not :attr:`~CalibrationEntry.passes` — design
    §12.4: "If no tier passes, the class has no recommended tier." Never
    raises: a corrupt or missing shipped record degrades to "no
    recommendation", which is exactly today's shipped state (every entry
    ships ``passes: false`` until a live eval run records real numbers).
    """
    try:
        record = load_calibration_record()
    except (CalibrationRecordError, OSError, FileNotFoundError):
        return None
    entry = record.get(question_class)
    if entry is None or not entry.passes:
        return None
    return entry.model


# ---------------------------------------------------------------------------
# Model resolution (design §6.4)
# ---------------------------------------------------------------------------


def _get_attribution_model(
    question_class: str,
    config: dict[str, Any] | None,
    *,
    calibration_model: str | None = None,
) -> str:
    """Resolve the judge model for *question_class* (design §6.4).

    Precedence: env ``ATHENAEUM_ATTRIBUTION_MODEL`` > yaml
    ``models.attribution_<question_class>`` > yaml ``models.attribution`` >
    *calibration_model* (:func:`recommended_model_for_class`'s result for
    this class) > :data:`athenaeum.config.DEFAULT_CLASSIFY_MODEL`. No path
    here ever escalates to a MORE expensive model on its own initiative —
    if the class's measured tier is not confident enough, the item goes to
    a human (design §8), never to a bigger model.
    """
    env = os.environ.get("ATHENAEUM_ATTRIBUTION_MODEL")
    if env:
        return env
    if isinstance(config, dict):
        models_cfg = config.get("models")
        if isinstance(models_cfg, dict):
            raw = models_cfg.get(f"attribution_{question_class}")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            raw = models_cfg.get("attribution")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    if calibration_model:
        return calibration_model
    return config_module.DEFAULT_CLASSIFY_MODEL


def _get_attribution_model_for_preflight(config: dict[str, Any] | None) -> str:
    """Single-string ``attribution`` knob resolution, for
    :func:`athenaeum.librarian._knob_model_getters`'s preflight (issue
    athenaeum#783/#1174: every ``prompt_registry.KNOBS`` member needs a
    ``Callable[[dict | None], str]`` getter there, to check the resolved
    model is priced before a real run starts).

    The judge itself resolves PER QUESTION CLASS via
    :func:`_get_attribution_model` (design §6.4) — preflight needs only one
    representative model per knob, so this calls that same resolver with
    an empty question class, which never matches a
    ``models.attribution_<class>`` override and falls straight through to
    the knob-wide ``models.attribution`` / default chain every question
    class shares before its own override.
    """
    return _get_attribution_model("", config)


# ---------------------------------------------------------------------------
# The prompt (design §6.4)
# ---------------------------------------------------------------------------


def _load_contact_attribution_judge_prompt() -> str:
    """Read the judge's system prompt from its packaged file (design §6.4).

    Loaded with :mod:`importlib.resources`, the same convention
    :func:`athenaeum.pii_hazard_classifier.load_pii_hazard_question` and
    :mod:`athenaeum.tiers`'s ``_load_name_resolution_confirm_prompt`` already
    established — prompt text is content, not code
    (``policies/prompt-text-is-content.md``).
    """
    resource = importlib.resources.files("athenaeum.prompts").joinpath(
        "contact_attribution_judge.md"
    )
    return resource.read_text(encoding="utf-8")


#: Registered in ``prompt_registry.PROMPT_META`` (design §6.4) as a
#: module-level constant so ``prompt_registry._resolve`` can ``getattr`` it
#: — even though the TEXT lives in the packaged ``.md`` file above and is
#: loaded once at import time, never inlined as a literal here.
CONTACT_ATTRIBUTION_JUDGE_SYSTEM: str = _load_contact_attribution_judge_prompt()


def attribution_call_identity(
    question_class: str, config: dict[str, Any] | None = None
) -> tuple[str, str]:
    """The ``(model, prompt_sha256)`` pair a judge call for *question_class* uses.

    Both halves of the memo key (issue athenaeum#2212, design §4.3) that are
    resolvable BEFORE any call: the model
    (:func:`_get_attribution_model`, seeded with
    :func:`recommended_model_for_class`'s recommendation for this class) and
    the sha256 of the live judge prompt. Factored out of
    :func:`judge_contact_attribution` — which now calls it — rather than
    copied, so a memo LOOKUP (made before the call, which is the whole point
    of a memo) and the judgement a real call stamps cannot disagree about
    which model/prompt pair they are keyed on. Two copies of the same
    expression would silently miss on every lookup the moment one of them
    changed.
    """
    calibration_model = recommended_model_for_class(question_class)
    model = _get_attribution_model(
        question_class, config, calibration_model=calibration_model
    )
    prompt_sha256 = hashlib.sha256(
        CONTACT_ATTRIBUTION_JUDGE_SYSTEM.encode("utf-8")
    ).hexdigest()
    return model, prompt_sha256


#: Output budget: design §13 measures "about 250 output tokens" for one
#: escalation call. 512 gives 2x headroom over that measurement for the
#: quote + reasoning fields, resolved through the same
#: :func:`athenaeum.provider.resolve_max_tokens` seam every other stage uses
#: (so an operator can still override it).
_CONTACT_ATTRIBUTION_JUDGE_MAX_TOKENS = 512

#: Window around the cited value (design §6.2: "a window of at most a few
#: hundred tokens").
_SOURCE_EXCERPT_MAX_CHARS = 1200

#: Budget for the assembled <evidence> fence (athenaeum#2211, Seer finding
#: on PR athenaeum#2259): the value, every entity brief (target/holders/
#: candidates) and the fired-check labels, all together. Generous relative
#: to typical case sizes (a handful of short briefs plus a few check
#: lines) -- this is a safety cap against a pathological number of
#: holders/candidates, not a tuning knob.
_EVIDENCE_MAX_CHARS = 4000


def _format_entity_brief(brief: EntityBrief, *, label: str) -> str:
    lines = [f"{label}: {brief.name} (uid={brief.uid})"]
    if brief.type:
        lines.append(f"  type: {brief.type}")
    if brief.aliases:
        lines.append(f"  aliases: {', '.join(brief.aliases)}")
    if brief.employer_domain_hints:
        lines.append(
            f"  employer/domain hints: {', '.join(brief.employer_domain_hints)}"
        )
    return "\n".join(lines)


def _format_checks(checks: tuple[CheckEvidence, ...]) -> str:
    if not checks:
        return "  (none)"
    lines = []
    for check in checks:
        parts = [check.check_id]
        if check.label:
            parts.append(f"label={check.label}")
        if check.uids:
            parts.append(f"uids={','.join(check.uids)}")
        lines.append("  - " + " ".join(parts))
    return "\n".join(lines)


def _build_question_user_message(question: AttributionQuestion) -> str:
    """Render *question* into the judge call's user message.

    Every piece of attacker-influenced content is fenced with
    :func:`athenaeum.prompt_safety.fence_untrusted` — never interpolated
    bare (athenaeum#2211, Seer finding on PR athenaeum#2259). Two fenced
    regions, not one per field: a single ``<evidence>`` fence wraps the
    value, every :class:`EntityBrief` (``name``/``aliases``/
    ``employer_domain_hints`` all originate from ingested external
    content -- an email display name among them -- so they are
    attacker-influenced by construction, exactly like the value itself),
    and the fired checks' labels (:func:`_format_checks` -- a check label
    such as ``role-local-part:<local>`` embeds a substring of the value,
    so it carries the same risk). Wrapping each short field in its own
    fence would litter the prompt with delimiters and make a name harder
    to read as a name; one fence over the whole assembled region avoids
    that while still drawing the trust boundary around everything that
    did not originate from this module's own deterministic checks. A
    second, separate fence (unchanged from before this finding) wraps the
    source excerpt, which is a different piece of untrusted content (a
    free-text quote, not structured evidence) with its own existing tag.
    """
    evidence_lines = [
        f"Contact value under question: {question.value}",
        "",
        _format_entity_brief(question.target, label="Target"),
    ]
    if question.holders:
        evidence_lines.append("")
        evidence_lines.append("Other uids that already hold or name this value:")
        for holder in question.holders:
            evidence_lines.append(_format_entity_brief(holder, label="Holder"))
    if question.candidates:
        evidence_lines.append("")
        evidence_lines.append("Candidate owners the evidence points at:")
        for candidate in question.candidates:
            evidence_lines.append(_format_entity_brief(candidate, label="Candidate"))
    evidence_lines.append("")
    evidence_lines.append("Checks that fired:")
    evidence_lines.append(_format_checks(question.checks))

    sections = [
        fence_untrusted(
            "\n".join(evidence_lines), tag="evidence", max_chars=_EVIDENCE_MAX_CHARS
        ),
        "",
    ]
    if question.source_excerpt:
        sections.append(
            fence_untrusted(
                question.source_excerpt,
                tag="source_excerpt",
                max_chars=_SOURCE_EXCERPT_MAX_CHARS,
            )
        )
    else:
        sections.append("No source excerpt was available.")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# The judge (design §6.4)
# ---------------------------------------------------------------------------


def judge_contact_attribution(
    question: AttributionQuestion,
    client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
) -> AttributionJudgement:
    """Ask the model to attribute *question*'s contact value (design §6.4).

    Follows the ``claim_kind.classify_claim_kind`` call pattern: the model
    comes from :func:`_get_attribution_model`, the call is wrapped in
    :func:`athenaeum._retry.with_retry`, and the output is strict JSON
    parsed with :func:`athenaeum.json_utils.extract_json_object`. Unlike
    that pattern, this one FAILS CLOSED (design §6.3): no client, an API
    error, unparseable output, or an out-of-vocabulary verdict all yield
    ``verdict="unclear"``/``score=0`` — never falls back to treating the
    value as settled. Never raises.
    """
    question_class = question_class_for(question.checks)
    # Issue athenaeum#2212: resolved through the SHARED
    # :func:`attribution_call_identity`, so the memo's pre-call lookup key
    # and this judgement's stamped (model, prompt_sha256) agree by
    # construction rather than by two copies of the same expression.
    model, prompt_sha256 = attribution_call_identity(question_class, config)

    def _unclear() -> AttributionJudgement:
        return AttributionJudgement(
            verdict=JUDGEMENT_UNCLEAR,
            score=0.0,
            model=model,
            prompt_sha256=prompt_sha256,
        )

    if client is None:
        return _unclear()

    user_msg = _build_question_user_message(question)
    try:
        response = with_retry(
            lambda: client.messages.create(
                model=model,
                max_tokens=resolve_max_tokens(
                    "attribution",
                    "ATHENAEUM_ATTRIBUTION_MAX_TOKENS",
                    _CONTACT_ATTRIBUTION_JUDGE_MAX_TOKENS,
                    config,
                ),
                thinking=cast(
                    "ThinkingConfigParam",
                    resolve_thinking(
                        "attribution",
                        "ATHENAEUM_ATTRIBUTION_THINKING",
                        "disabled",
                        config,
                    ),
                ),
                system=CONTACT_ATTRIBUTION_JUDGE_SYSTEM,
                messages=cast(
                    "list[MessageParam]", [{"role": "user", "content": user_msg}]
                ),
            ),
            description="contact_attribution_judge",
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed, never raise (design §6.3)
        log.warning("pii_attribution: judge call failed (%s); unclear", exc)
        return _unclear()

    if usage is not None and hasattr(response, "usage"):
        input_toks, output_toks, cache_creation, cache_read = cache_usage_counts(
            response
        )
        usage.add(
            input_toks,
            output_toks,
            cache_creation,
            cache_read,
            model=model,
            knob="attribution",
        )

    try:
        raw_text = response_text(response)
    except (AttributeError, IndexError, ValueError):
        log.warning("pii_attribution: malformed judge response; unclear")
        return _unclear()

    payload = extract_json_object(raw_text)
    if not isinstance(payload, dict):
        log.warning("pii_attribution: no JSON object in judge response; unclear")
        return _unclear()

    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or verdict not in JUDGEMENT_VERDICTS:
        log.warning(
            "pii_attribution: judge returned %r (not a valid verdict); unclear",
            verdict,
        )
        return _unclear()

    owner_uid_raw = payload.get("owner_uid")
    owner_uid = (
        str(owner_uid_raw)
        if isinstance(owner_uid_raw, str) and owner_uid_raw.strip()
        else None
    )

    alias_relation_raw = payload.get("alias_relation")
    alias_relation = (
        alias_relation_raw
        if isinstance(alias_relation_raw, str) and alias_relation_raw in ALIAS_RELATIONS
        else None
    )

    quote_raw = payload.get("quote")
    quote = str(quote_raw) if isinstance(quote_raw, str) and quote_raw.strip() else None

    try:
        score = float(payload.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = min(1.0, max(0.0, score))

    return AttributionJudgement(
        verdict=verdict,
        owner_uid=owner_uid,
        alias_relation=alias_relation,
        quote=quote,
        score=score,
        model=model,
        prompt_sha256=prompt_sha256,
    )


__all__ += [
    "EntityBrief",
    "AttributionQuestion",
    "AttributionJudgement",
    "JUDGEMENT_OWN",
    "JUDGEMENT_OTHER_KNOWN",
    "JUDGEMENT_OTHER_UNKNOWN",
    "JUDGEMENT_ALIAS",
    "JUDGEMENT_SAME_ENTITY",
    "JUDGEMENT_NON_PERSONAL",
    "JUDGEMENT_UNCLEAR",
    "JUDGEMENT_VERDICTS",
    "ALIAS_RELATION_ROLE_INBOX",
    "ALIAS_RELATION_SHARED",
    "ALIAS_RELATION_FORMER_NAME",
    "ALIAS_RELATION_PERSONAL_ALIAS",
    "ALIAS_RELATIONS",
    "QUESTION_CLASS_HELD_ELSEWHERE",
    "QUESTION_CLASS_NAMES_OTHER",
    "QUESTION_CLASS_SOURCE_CONTEXT",
    "QUESTION_CLASS_NON_PERSON_TARGET",
    "QUESTION_CLASS_ROLE_UNMATCHED",
    "QUESTION_CLASSES",
    "question_class_for",
    "SIGNAL_SELF_REPORTED",
    "SIGNAL_AGREEMENT_K3",
    "SIGNAL_CLASS_LEVEL",
    "SIGNALS",
    "CALIBRATION_RECORD_PACKAGE",
    "CALIBRATION_RECORD_FILENAME",
    "CalibrationRecordError",
    "CalibrationEntry",
    "load_calibration_record",
    "recommended_model_for_class",
    "CONTACT_ATTRIBUTION_JUDGE_SYSTEM",
    "judge_contact_attribution",
]


# ---------------------------------------------------------------------------
# Escalation: calibrated thresholds, the verdict memo, and the placement
# entry point (athenaeum#2212)
# ---------------------------------------------------------------------------
#
# Child E of epic athenaeum#2192, designed in
# ``docs/design/attribution-verification.md`` §6 ("Escalation contract"),
# §7 ("Filing the result"), §8 ("The human queue and the threshold") and
# §9 ("Observe, then enforce"). Appended after athenaeum#2211's section for
# the same anchor-discipline reason that section gives: sibling lanes edit
# the regions above, and this one touches none of them. Its public names are
# exported via a THIRD ``__all__ +=`` below.
#
# **This issue is observe (shadow) mode only.** Nothing here files anything
# on its own initiative: :func:`escalate_contact_attribution_suspect` passes
# the caller's ``apply`` straight through to
# :func:`athenaeum.pii.place_contact_value`, and every pipeline caller in
# this issue passes ``apply=False``. Enforcement is athenaeum#2220.
#
# **Layering.** This module is L3 and must not reach L4
# (``tiers``/``pending_merges``/``librarian``/``batch``/``decisions``), in
# any form -- ``tests/test_import_graph_acyclic.py`` counts a
# function-local import as an edge. So:
#
# - the human-queue item is RETURNED as an :class:`athenaeum.models.
#   EscalationItem` (L1) for an L4 caller to hand to ``tiers.tier4_escalate``;
# - the same-entity merge proposal is RETURNED as an
#   :class:`AttributionMergeProposal` for an L4 caller to adapt onto
#   ``pending_merges.write_pending_merge``, and the proposer itself is
#   INJECTED by that caller;
# - the design §7.2 re-check is a callable this module binds to its own
#   :func:`check_contact_attribution`, which is how ``pii`` (an L3 sibling
#   this module imports) performs it without an import back.


# ---------------------------------------------------------------------------
# The calibrated mapping (design §6.5, §12.4)
# ---------------------------------------------------------------------------


def calibrated_probability(entry: CalibrationEntry, score: float) -> float:
    """Map a raw *score* through *entry*'s bins to a calibrated probability.

    Design §6.5: ``score`` is any scalar, and the calibration record maps it
    to an empirical probability of being correct. §12.4's ``bins`` are
    ``(lo, hi, observed_accuracy)`` triples in record order.

    **Edge semantics, stated rather than inferred** (the design gives the
    shape but not the boundary rule):

    - A bin matches when ``lo <= score < hi`` -- lower bound INCLUSIVE,
      upper bound EXCLUSIVE. §12.4's example bins share endpoints
      (``[0.0, 0.42, ...], [0.42, 0.70, ...]``), so this is the only reading
      under which they partition rather than overlap.
    - The LAST bin is closed at both ends (``lo <= score <= hi``). Without
      that, a score exactly at the top of the measured range -- ``1.0`` for
      a self-reported confidence -- would fall off the end of a record that
      demonstrably covers it.
    - A score BELOW the first bin's ``lo``, ABOVE the last bin's ``hi``, or
      in a GAP between two bins is **unmappable** and yields ``0.0``.
    - An EMPTY ``bins`` tuple means no mapping was ever measured, and also
      yields ``0.0``. This is today's shipped state for every class.

    **Fail safe.** Every unmappable case returns ``0.0`` -- a probability
    that cannot clear any threshold in ``[0, 1]`` except ``0.0`` itself, and
    :func:`threshold_for_class` returns ``None`` (not ``0.0``) for exactly
    the entries that have no measured bins, so such an item goes to a human
    (design §8.1). It never returns a default-HIGH probability: a wrongly
    placed address is not recoverable the way a false referral to a human is.

    :data:`SIGNAL_CLASS_LEVEL` is handled explicitly, BEFORE any bin
    arithmetic. Design §6.5 signal 3: the score is a constant, so "the
    threshold degenerates to 'this class auto-files or it does not'". A
    passing class-level entry therefore returns ``1.0`` (the class
    auto-files; its own measured per-class accuracy is what cleared the bar,
    and *score* carries no information to weigh against it) and a
    non-passing one returns ``0.0``. Running such an entry through the bins
    would read its empty ``bins`` as an unmappable score and lose that
    distinction.
    """
    if entry.signal == SIGNAL_CLASS_LEVEL:
        return 1.0 if entry.passes else 0.0
    if not entry.bins:
        return 0.0
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.0
    last_index = len(entry.bins) - 1
    for position, (lo, hi, probability) in enumerate(entry.bins):
        if lo <= value < hi or (position == last_index and lo <= value <= hi):
            return float(probability)
    return 0.0


def _valid_calibration_entry(question_class: str) -> CalibrationEntry | None:
    """The shipped calibration entry for *question_class*, or ``None``.

    ``None`` for a record that cannot be loaded, carries no entry for this
    class, or carries one that does not :attr:`~CalibrationEntry.passes` --
    the same three conditions :func:`recommended_model_for_class` already
    treats as "no recommendation" (design §12.4: "If no tier passes, the
    class has no recommended tier"). Never raises.
    """
    try:
        record = load_calibration_record()
    except (CalibrationRecordError, OSError):
        return None
    entry = record.get(question_class)
    if entry is None or not entry.passes:
        return None
    return entry


def calibrated_probability_for_class(question_class: str, score: float) -> float:
    """:func:`calibrated_probability` against the SHIPPED record's entry.

    ``0.0`` when *question_class* has no valid calibration entry
    (:func:`_valid_calibration_entry`) -- the same fail-safe direction as an
    unmappable score.
    """
    entry = _valid_calibration_entry(question_class)
    if entry is None:
        return 0.0
    return calibrated_probability(entry, score)


def threshold_for_class(
    question_class: str,
    config: dict[str, Any] | None = None,
    *,
    wiki_root: Path | None = None,
    as_of: str | None = None,
) -> float | None:
    """The calibrated-probability threshold for *question_class* (design §8.2).

    Precedence: the operator's ``librarian.attribution.thresholds.<class>``
    override (:func:`athenaeum.config.resolve_attribution_thresholds`) >
    an APPROVED attribution-threshold proposal's ledger override (issue
    athenaeum#2219, design §8.3 — :func:`athenaeum.config.
    auto_apply_threshold_ledger_override_for`, keyed by
    :func:`athenaeum.calibration.attribution_threshold_action_id`) > the
    shipped calibration entry's own :attr:`~CalibrationEntry.threshold` >
    ``None``.

    The explicit-config override wins UNCONDITIONALLY, including over a
    missing or non-passing calibration entry: design §8.2 gives a
    deployment the override precisely so one that has run its own eval is
    not held to the shipped record, and setting it is an explicit operator
    act. The LEDGER override sits BELOW that (issue athenaeum#2219 AC5: "a
    ledger override that the threshold reader honours, BELOW explicit
    config") — a human APPROVING a drafted proposal is a real act too, just
    a lower-precedence one than hand-editing ``athenaeum.yaml``.

    *wiki_root* (default ``None``, meaning "consult no ledger override" —
    every pre-existing caller that omits it keeps today's two-layer
    behavior byte-for-byte) and *as_of* are threaded straight through to
    :func:`athenaeum.config.auto_apply_threshold_ledger_override_for`,
    whose own docstring carries the full ``as_of``-fences-in-flight-work
    contract (an approval dated after the item was already escalated never
    retroactively widens or narrows the floor that gated it).

    An override does NOT substitute for a calibration record. The threshold
    is compared against :func:`calibrated_probability_for_class`, which
    needs a valid entry to map a score at all, so a deployment that
    overrides the threshold without having run its own eval gets ``0.0``
    for every probability and a positive override that nothing can clear --
    every item goes to a human. That is the correct fail-safe direction:
    design §8.2's "a threshold is a calibrated probability, not a raw
    score".

    ``None`` means **"no valid calibration for this class"**, which per
    design §8.1 sends the item to a human. A calibration entry whose
    ``passes`` is ``false`` yields ``None`` rather than its stored
    ``threshold``: that threshold was never measured (today's shipped record
    carries ``threshold: 0.0`` with ``passes: false`` for every class, and a
    ``0.0`` threshold would auto-file EVERYTHING), so a non-passing entry
    must not yield an auto-filing threshold under any reading.
    """
    overrides = config_module.resolve_attribution_thresholds(config)
    override = overrides.get(question_class)
    if override is not None:
        return float(override)
    if wiki_root is not None:
        ledger_override = config_module.auto_apply_threshold_ledger_override_for(
            attribution_threshold_action_id(question_class),
            wiki_root=wiki_root,
            config=config,
            as_of=as_of,
        )
        if ledger_override is not None:
            return ledger_override
    entry = _valid_calibration_entry(question_class)
    if entry is None:
        return None
    return entry.threshold


# ---------------------------------------------------------------------------
# The verdict memo (design §4.3)
# ---------------------------------------------------------------------------

#: Memo filename, on the contacts (excluded) surface beside the attribution
#: log and the suspect files (design §4.3).
ATTRIBUTION_MEMO_FILENAME = "_attribution_memo.jsonl"

#: Row-schema version, mirroring :data:`ATTRIBUTION_LOG_VERSION`.
ATTRIBUTION_MEMO_VERSION = 1


def default_attribution_memo_path(contacts_root: Path) -> Path:
    """Default memo path: ``<contacts_root>/_attribution_memo.jsonl``."""
    return Path(contacts_root) / ATTRIBUTION_MEMO_FILENAME


def _canonical_source_ref(source_ref: str | dict[str, Any] | None) -> str:
    """Deterministic string form of a *source_ref* for the memo key.

    ``source_ref`` is loose at this seam (design §4.1): a bare path string,
    a structured mapping, or ``None``. A canonical JSON dump with sorted
    keys makes all three deterministic and makes two dicts that differ only
    in key ORDER hash to the same memo key -- which is the correct answer,
    since they name the same source. ``default=str`` keeps an exotic value
    (a :class:`~pathlib.Path` a caller passed inside the mapping) from
    raising instead of memoising.
    """
    return json.dumps(source_ref, sort_keys=True, separators=(",", ":"), default=str)


def attribution_memo_key(
    *,
    value_key: str,
    target_uid: str,
    source_ref: str | dict[str, Any] | None,
    prompt_sha256: str,
    model: str,
) -> str:
    """The memo key: a stable digest over design §4.3's exact five components.

    ``(value_key, target_uid, source_ref, prompt_sha256, model)`` -- so a
    repeat of the same question under the same prompt and model is a hit,
    and a CHANGED prompt sha or model re-asks (both are part of the key, and
    both are resolvable before the call via
    :func:`attribution_call_identity`).

    Built from :func:`value_key_for`'s HMAC, never from the raw value, so
    the key is safe to read, diff and log without the excluded surface --
    the same property :func:`log_attribution_verdict`'s rows have.
    """
    payload = json.dumps(
        [
            value_key,
            target_uid,
            _canonical_source_ref(source_ref),
            prompt_sha256,
            model,
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_attribution_memo(memo_path: Path) -> dict[str, AttributionJudgement]:
    """Read the memo at *memo_path* into ``{key: judgement}``.

    Last-write-wins on a duplicate key: the file is append-only, so the LAST
    row for a key is the current answer. A malformed line (not JSON, not an
    object, missing its key, or carrying an out-of-vocabulary verdict) is
    logged at WARNING and SKIPPED -- never fatal. A memo is a cache; a
    corrupt row must cost one re-ask, not a run.

    Returns an empty mapping when the file does not exist.
    """
    resolved = Path(memo_path)
    if not resolved.exists():
        return {}
    entries: dict[str, AttributionJudgement] = {}
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("pii_attribution: memo at %s unreadable (%s); treating as empty", resolved, exc)
        return {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            log.warning("pii_attribution: skipping malformed memo line %d in %s", lineno, resolved)
            continue
        if not isinstance(row, dict):
            log.warning("pii_attribution: skipping non-object memo line %d in %s", lineno, resolved)
            continue
        key = row.get("key")
        verdict = row.get("verdict")
        if not isinstance(key, str) or not key:
            log.warning("pii_attribution: skipping keyless memo line %d in %s", lineno, resolved)
            continue
        if not isinstance(verdict, str) or verdict not in JUDGEMENT_VERDICTS:
            log.warning(
                "pii_attribution: skipping memo line %d in %s (verdict %r not in the vocabulary)",
                lineno,
                resolved,
                verdict,
            )
            continue
        owner_uid = row.get("owner_uid")
        alias_relation = row.get("alias_relation")
        quote = row.get("quote")
        try:
            score = float(row.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        entries[key] = AttributionJudgement(
            verdict=verdict,
            owner_uid=owner_uid if isinstance(owner_uid, str) and owner_uid else None,
            alias_relation=(
                alias_relation
                if isinstance(alias_relation, str) and alias_relation in ALIAS_RELATIONS
                else None
            ),
            quote=quote if isinstance(quote, str) and quote else None,
            score=score,
            model=str(row.get("model") or ""),
            prompt_sha256=str(row.get("prompt_sha256") or ""),
        )
    return entries


def memo_lookup(
    context: AttributionContext,
    *,
    value_key: str,
    target_uid: str,
    source_ref: str | dict[str, Any] | None,
    prompt_sha256: str,
    model: str,
) -> AttributionJudgement | None:
    """The memoised judgement for this question, or ``None`` (design §6.1).

    A hit means **no model call at all** -- the lookup happens BEFORE the
    call, which is why :func:`attribution_call_identity` resolves the model
    and prompt sha up front.
    """
    key = attribution_memo_key(
        value_key=value_key,
        target_uid=target_uid,
        source_ref=source_ref,
        prompt_sha256=prompt_sha256,
        model=model,
    )
    return read_attribution_memo(default_attribution_memo_path(context.contacts_root)).get(key)


def is_memoisable_judgement(judgement: AttributionJudgement) -> bool:
    """Whether *judgement* is an ANSWER worth memoising (issue athenaeum#2212).

    The memo exists so one question is never re-spent (design §4.3). A
    fail-closed :data:`JUDGEMENT_UNCLEAR` is not an answer to that question
    — design §6.3 returns it for an API error, an unparseable response, an
    out-of-vocabulary verdict and a missing client alike. Memoising one
    makes a single transient 429 permanent: the pair is suppressed for its
    whole ``(value_key, target_uid, source_ref, prompt_sha256, model)``
    identity, the item sits in the human queue with no retry, and
    re-running the escalation — the design's own way back — can never
    re-ask. So a fail-closed ``unclear`` is NOT memoised, and the next
    escalation re-asks.

    Telling a fail-closed ``unclear`` from a model that genuinely answered
    "unclear": every fail-closed path in
    :func:`judge_contact_attribution` returns its local ``_unclear()``,
    which is exactly ``verdict="unclear"`` with the dataclass defaults for
    everything the response payload would otherwise fill in — ``score 0.0``
    and no ``owner_uid``/``alias_relation``/``quote``. So ANY of those being
    set proves the payload parsed, i.e. a real model answer, and that
    answer is memoised. A genuine ``unclear`` that happens to carry none of
    them is byte-indistinguishable from a failure, and is treated as a
    failure: one extra call on the next sweep, versus a permanently parked
    item that can never be re-asked. ``judge_contact_attribution`` itself is
    deliberately untouched — the discriminator reads its OUTPUT.
    """
    if judgement.verdict != JUDGEMENT_UNCLEAR:
        return True
    return bool(
        judgement.quote
        or judgement.owner_uid
        or judgement.alias_relation
        or judgement.score > 0.0
    )


def record_attribution_memo(
    context: AttributionContext,
    judgement: AttributionJudgement,
    *,
    value_key: str,
    target_uid: str,
    source_ref: str | dict[str, Any] | None,
) -> str:
    """Append *judgement* to the memo and return its key (design §4.3).

    Keyed on the judgement's OWN ``model``/``prompt_sha256``, so a row is
    always retrievable by the identity the call actually ran under.

    The row carries ``value_key``, never the value: the key's whole purpose
    is that one triple is never re-spent, and an HMAC answers that without
    the value. ``quote`` IS stored verbatim -- it is the model's own
    justification span and may echo the address -- which is in policy
    because this file lives on the excluded surface beside the suspect file,
    which already holds the value in cleartext (design §4.3). The *key* is
    still value-free, so the memo stays diffable without the surface.
    """
    key = attribution_memo_key(
        value_key=value_key,
        target_uid=target_uid,
        source_ref=source_ref,
        prompt_sha256=judgement.prompt_sha256,
        model=judgement.model,
    )
    row = {
        "v": ATTRIBUTION_MEMO_VERSION,
        "key": key,
        "value_key": value_key,
        "target_uid": target_uid,
        "source_ref": _canonical_source_ref(source_ref),
        "prompt_sha256": judgement.prompt_sha256,
        "model": judgement.model,
        "verdict": judgement.verdict,
        "owner_uid": judgement.owner_uid,
        "alias_relation": judgement.alias_relation,
        "quote": judgement.quote,
        "score": judgement.score,
        "at": now_iso(),
    }
    append_line_durable(
        default_attribution_memo_path(context.contacts_root),
        (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    return key


# ---------------------------------------------------------------------------
# Recording the judgement on the parked suspect (design §7.1)
# ---------------------------------------------------------------------------

#: Frontmatter keys :func:`park_suspect` does not itself author but MUST
#: carry forward across a re-park (issue athenaeum#2212). A judgement
#: arrives strictly LATER than the park -- from the escalation, often on a
#: different run -- so without this list a routine observe-mode re-park by
#: any of the five writers would rebuild ``meta`` from its own inputs and
#: silently drop it.
#:
#: Why a separate :func:`record_suspect_judgement` rather than a
#: ``judgement=`` kwarg on :func:`park_suspect`: all five parking writers
#: call that function at the moment they decide NOT to file a value, and
#: none of them has a judgement to pass. Threading an always-``None``
#: argument through five call sites to serve one later caller inverts the
#: dependency. What :func:`park_suspect` genuinely must know is this list of
#: key NAMES -- not the judgement's shape, which stays this section's
#: business.
_SUSPECT_CARRIED_FORWARD_KEYS: tuple[str, ...] = ("judgement",)

#: Discriminator on the attribution-log rows this section appends. A row
#: WITHOUT a ``kind`` key is a :func:`log_attribution_verdict` row -- the
#: pre-athenaeum#2212 shape, left byte-identical so an existing reader (the
#: observe-mode lint, design §9.2) is unaffected.
ATTRIBUTION_LOG_KIND_ESCALATION = "escalation"


def suspect_id_for(value_key: str, target_uid: str) -> str:
    """The suspect id for one (value, target) pair: ``<value_key>-<target_uid>``.

    The stem of :func:`_suspect_path`'s filename, which is what design §8.1
    means by "points to the suspect id" and what ``athenaeum attribution
    show <suspect-id>`` takes.
    """
    return f"{value_key}-{target_uid}"


#: A suspect id is a filename stem this module itself minted
#: (:func:`suspect_id_for`), but it arrives from a CLI argument, so it is
#: validated before it is joined to a path: hex ``value_key``, a ``-``, then
#: a uid of the ordinary slug/uid shape. Anything else is rejected rather
#: than resolved, so no ``..`` or absolute spelling can escape the suspects
#: directory.
_SUSPECT_ID_RE = re.compile(r"^[0-9a-f]{64}-[A-Za-z0-9][A-Za-z0-9._-]*$")


class SuspectIdError(ValueError):
    """A suspect id that is not a well-formed ``<value_key>-<target_uid>`` stem."""


def suspect_path_for_id(contacts_root: Path, suspect_id: str) -> Path:
    """The parked-suspect path for *suspect_id*, validated (design §4.3).

    Raises:
        SuspectIdError: if *suspect_id* is not a well-formed stem
            (:data:`_SUSPECT_ID_RE`). Validated rather than sanitised: the
            suspects directory is on the excluded surface, and a traversal
            attempt is a caller bug worth naming, not something to silently
            repair.
    """
    cleaned = (suspect_id or "").strip()
    if not _SUSPECT_ID_RE.match(cleaned):
        raise SuspectIdError(
            f"{suspect_id!r} is not a suspect id; expected "
            "<value_key>-<target_uid>, where value_key is the 64-character "
            "hex digest in the suspect filename"
        )
    return Path(contacts_root) / ATTRIBUTION_SUSPECTS_DIRNAME / f"{cleaned}.md"


@dataclass(frozen=True)
class ParkedSuspect:
    """One parked suspect, read back off the excluded surface (design §4.3).

    ``checks``/``candidates`` are the check IDS and candidate uids the park
    recorded. The park deliberately stores only check ids, not each check's
    own uids/labels (:func:`park_suspect`), so a suspect rebuilt from disk
    carries the fired-check SET and the UNION of candidates rather than the
    per-check attribution the live verdict had. That is enough for
    :func:`question_class_for` (which reads ids only) and for the escalation
    question's candidate list; see :func:`verdict_from_parked_suspect`.
    """

    suspect_id: str
    path: Path
    value: str
    value_key: str
    target_uid: str
    target_name: str | None
    writer: str
    source_ref: str | dict[str, Any] | None
    disposition: str
    checks: tuple[str, ...]
    candidates: tuple[str, ...]
    shadow: bool
    judgement: dict[str, Any] | None = None


def _parked_suspect_from_path(path: Path) -> ParkedSuspect | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("pii_attribution: suspect at %s unreadable (%s); skipped", path, exc)
        return None
    raw_meta, _ = parse_frontmatter(text)
    if not isinstance(raw_meta, dict):
        log.warning("pii_attribution: suspect at %s has no frontmatter; skipped", path)
        return None
    meta = cast("dict[str, Any]", raw_meta)
    value = str(meta.get("value") or "")
    value_key = str(meta.get("value_key") or "")
    target_uid = str(meta.get("target_uid") or "")
    if not value or not value_key or not target_uid:
        log.warning(
            "pii_attribution: suspect at %s is missing value/value_key/target_uid; skipped",
            path,
        )
        return None
    target_name_raw = meta.get("target_name")
    judgement = meta.get("judgement")
    return ParkedSuspect(
        suspect_id=path.stem,
        path=path,
        value=value,
        value_key=value_key,
        target_uid=target_uid,
        target_name=str(target_name_raw) if isinstance(target_name_raw, str) else None,
        writer=str(meta.get("writer") or ""),
        source_ref=meta.get("source"),
        disposition=str(meta.get("disposition") or ""),
        checks=tuple(str(c) for c in (meta.get("checks") or [])),
        candidates=tuple(str(c) for c in (meta.get("candidates") or [])),
        shadow=bool(meta.get("shadow", True)),
        judgement=judgement if isinstance(judgement, dict) else None,
    )


def read_suspect(contacts_root: Path, suspect_id: str) -> ParkedSuspect | None:
    """Read one parked suspect THROUGH the excluded-surface gate.

    The sanctioned read for a suspect, and the only place the raw value is
    shown (design §8.1: "A human who needs the value reads it through the
    one way out -- ``read_entity`` or ``athenaeum attribution show
    <suspect-id>``"). :func:`athenaeum.pii.read_entity` is the wrong seam
    for it: that reads an ENTITY's page joined to that entity's excluded
    RECORD, and a suspect is neither -- it is a parked (value, target) pair
    that may name an entity with no record and no page at all.

    So the gate, not the reader, is what is shared: this calls
    :func:`athenaeum.pii._require_readable_surface` before it opens
    anything, so an unreachable or unreadable surface raises
    :class:`~athenaeum.pii.ExcludedSurfaceUnavailable` instead of reporting
    a suspect as absent. A false "no such suspect" is exactly the
    fail-closed case that gate exists for.

    Returns ``None`` only when the surface IS readable and no suspect file
    with that id exists.

    Raises:
        SuspectIdError: on a malformed *suspect_id*.
        athenaeum.pii.ExcludedSurfaceUnavailable: on an unreadable surface.
    """
    root = Path(contacts_root)
    path = suspect_path_for_id(root, suspect_id)
    pii._require_readable_surface(root, pii.PII_ENTITY_CLASS)
    if not path.is_file():
        return None
    return _parked_suspect_from_path(path)


def iter_parked_suspects(
    contacts_root: Path, *, shadow_only: bool = False
) -> list[ParkedSuspect]:
    """Every parked suspect under *contacts_root*, in sorted filename order.

    Goes through the same :func:`athenaeum.pii._require_readable_surface`
    gate :func:`read_suspect` does, for the same fail-closed reason: an
    unreadable surface must raise rather than look like an empty backlog.
    A suspect file that is individually unreadable or malformed is logged
    and skipped (one bad file is not a run).

    *shadow_only* keeps only entries whose frontmatter says ``shadow:
    true`` -- design §9.1's observe-mode entries, which are what the
    shadow-escalation sweep acts on. A non-shadow entry was parked because
    something was actually ENFORCED (D1-narrow), and escalation has no
    question to ask about it.

    Returns a list, not a generator: the caller caps and counts, and a
    directory listing of parked suspects is small relative to the corpus
    scans this module already performs.
    """
    root = Path(contacts_root)
    pii._require_readable_surface(root, pii.PII_ENTITY_CLASS)
    suspects_dir = root / ATTRIBUTION_SUSPECTS_DIRNAME
    if not suspects_dir.is_dir():
        return []
    found: list[ParkedSuspect] = []
    for path in sorted(suspects_dir.glob("*.md")):
        suspect = _parked_suspect_from_path(path)
        if suspect is None:
            continue
        if shadow_only and not suspect.shadow:
            continue
        found.append(suspect)
    return found


def verdict_from_parked_suspect(suspect: ParkedSuspect) -> AttributionVerdict:
    """Rebuild an :class:`AttributionVerdict` from a parked suspect.

    The park stores check IDS only, so each :class:`CheckEvidence` comes
    back with no ``uids``/``label`` and the candidate uids come back as the
    verdict's own ``candidates`` union. Deliberately NOT re-running
    :func:`check_contact_attribution`: that would append a fresh log row,
    and a re-check can legitimately answer ``pass`` now (a holder's page was
    deleted since), which is a different question from the one the park
    recorded. Escalation runs on the suspect that was parked (design §9.1).
    """
    return AttributionVerdict(
        disposition=suspect.disposition or DISPOSITION_SUSPECT,
        checks=tuple(CheckEvidence(check_id=c) for c in suspect.checks),
        candidates=suspect.candidates,
        value_key=suspect.value_key,
    )


def record_suspect_judgement(
    context: AttributionContext,
    *,
    suspect_id: str,
    question_class: str,
    judgement: AttributionJudgement,
    calibrated_p: float | None,
    threshold: float | None,
) -> Path | None:
    """Stamp *judgement* onto the parked suspect file (design §7.1).

    Design §7.1's ``other-unknown`` row -- "Recorded as a suspect with
    verdict ``other-unknown``, so a later page for the real owner can claim
    it" -- is the requirement; recording EVERY verdict is the
    generalisation, because the suspect file is then the audit record for
    what was asked and answered about this pair, whatever the answer was.

    Additive by construction: it adds exactly ONE frontmatter key
    (``judgement``), appended after everything :func:`park_suspect` writes,
    and that key is on :data:`_SUSPECT_CARRIED_FORWARD_KEYS` so a later
    re-park preserves it instead of rebuilding ``meta`` without it. Writes
    only when the rendered bytes change, which keeps
    :func:`park_suspect`'s idempotency property intact: re-recording the
    same judgement writes nothing.

    Returns the path written (or already correct), or ``None`` when no
    suspect file exists for *suspect_id* -- the escalation's own callers may
    hold a live verdict that was never parked (a direct unit call), and a
    missing park is not a reason to fail an escalation.
    """
    try:
        path = suspect_path_for_id(context.contacts_root, suspect_id)
    except SuspectIdError as exc:
        log.warning("pii_attribution: not recording a judgement (%s)", exc)
        return None
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    raw_meta, body = parse_frontmatter(text)
    if not isinstance(raw_meta, dict):
        return None
    meta = cast("dict[str, Any]", raw_meta)
    merged: dict[str, Any] = dict(meta)
    judgement_entry: dict[str, Any] = {
        "verdict": judgement.verdict,
        "owner_uid": judgement.owner_uid,
        "alias_relation": judgement.alias_relation,
        "question_class": question_class,
        "calibrated_p": calibrated_p,
        "threshold": threshold,
        "model": judgement.model,
        "prompt_sha256": judgement.prompt_sha256,
        "at": now_iso(),
    }
    merged["judgement"] = judgement_entry
    new_text = render_frontmatter(merged) + "\n" + body
    if new_text == text:
        return path
    # Compare on everything except the judgement's own timestamp, so
    # re-recording an UNCHANGED judgement writes nothing -- the same
    # idempotency property `park_suspect` has, applied to this key.
    existing_judgement = meta.get("judgement")
    if isinstance(existing_judgement, dict):
        a = {k: v for k, v in existing_judgement.items() if k != "at"}
        b = {k: v for k, v in judgement_entry.items() if k != "at"}
        if a == b:
            return path
    atomic_write_text(path, new_text)
    return path


def log_attribution_escalation(
    context: AttributionContext,
    *,
    value_key: str,
    target_uid: str,
    writer: str,
    question_class: str,
    judgement: AttributionJudgement,
    calibrated_p: float | None,
    threshold: float | None,
    above_threshold: bool,
    memo_hit: bool,
    placement: "pii.ContactPlacement | None",
    queued: bool,
    mode: str = "observe",
) -> None:
    """Append one escalation row to the attribution log. Carries no value.

    Design §9.2: observe mode must produce "shadow escalation verdicts and
    their calibrated probabilities". This is that row. Every field is a key,
    a uid, a class name, a model id or a number -- never the value, so the
    log stays safe to read without the excluded surface, exactly like
    :func:`log_attribution_verdict`'s rows.
    """
    row: dict[str, Any] = {
        "v": ATTRIBUTION_LOG_VERSION,
        "kind": ATTRIBUTION_LOG_KIND_ESCALATION,
        "value_key": value_key,
        "target_uid": target_uid,
        "writer": writer,
        "question_class": question_class,
        "verdict": judgement.verdict,
        "owner_uid": judgement.owner_uid,
        "alias_relation": judgement.alias_relation,
        "score": judgement.score,
        "calibrated_p": calibrated_p,
        "threshold": threshold,
        "above_threshold": above_threshold,
        "memo_hit": memo_hit,
        "model": judgement.model,
        "prompt_sha256": judgement.prompt_sha256,
        "placement": placement.outcome if placement is not None else None,
        "placed_uid": placement.placed_uid if placement is not None else None,
        "placed_field": placement.field if placement is not None else None,
        "usage_class": placement.usage_class if placement is not None else None,
        "queued": queued,
        "mode": mode,
        "at": now_iso(),
    }
    append_line_durable(
        default_attribution_log_path(context.contacts_root),
        (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# The escalation entry point (design §6, §7, §8)
# ---------------------------------------------------------------------------

#: Checks whose evidence names a HOLDER of the value (design §6.2: "other
#: uids that hold or name the value (D2, D4, C1)").
_HOLDER_CHECK_IDS: frozenset[str] = frozenset(
    {CHECK_D2_HELD_ELSEWHERE, CHECK_D4_FAN_OUT, CHECK_C1_INLINE_ELSEWHERE}
)

#: Checks whose evidence names a CANDIDATE owner (design §6.2: "uids the
#: C2/C3 evidence points at").
_CANDIDATE_CHECK_IDS: frozenset[str] = frozenset(
    {CHECK_C2_LOCAL_PART_NAMES_OTHER, CHECK_C3_SOURCE_CONTEXT}
)


@dataclass(frozen=True)
class AttributionMergeProposal:
    """The same-entity merge intent an L4 caller adapts onto the merge queue.

    Design §7.1: a ``same-entity`` verdict is a dedupe question, not an
    alias, so it goes to ``pending_merges.write_pending_merge`` and NO
    contact value is written. ``pending_merges`` is L4, so this module can
    neither import it nor call it: the caller injects a proposer and this
    dataclass records, verbatim, the arguments
    :func:`athenaeum.pii.place_contact_value` passed to it.

    ``rationale`` already carries the MASKED value (the executor builds it
    with :func:`athenaeum.pii.mask_contact_address`) and is the only field
    safe to render into ``_pending_merges.md``, which lives in the wiki.
    ``value`` is the RAW value, carried so an adapter can key the proposal
    on the excluded surface -- it must never reach the merge block body.
    """

    target_uid: str
    owner_uid: str
    value: str
    rationale: str


@dataclass(frozen=True)
class EscalationOutcome:
    """What :func:`escalate_contact_attribution_suspect` did for one suspect.

    ``escalated`` is ``False`` only when the verdict was not ``suspect`` --
    design §6.1's "a ``pass`` or ``non-personal`` verdict makes no model
    call". Everything else (including a fail-closed ``unclear``) escalated.

    ``placement`` is the :class:`athenaeum.pii.ContactPlacement` the
    executor returned, or ``None`` when the executor was never reached (the
    item went straight to the human queue). In observe mode its ``outcome``
    is :data:`athenaeum.pii.PLACEMENT_PROPOSED` and it describes the
    placement the apply path WOULD have made.

    ``merge_proposal`` is populated only when the executor actually called
    an injected proposer -- that is, apply mode, a ``same-entity`` verdict
    at or above threshold, and a caller-supplied ``propose_merge``. In
    observe mode the executor calls nothing and builds no rationale, so it
    stays ``None`` and the proposed merge is readable from ``placement``
    (``verdict == "same-entity"``, ``placed_uid`` = the owner).
    """

    escalated: bool
    question_class: str | None = None
    judgement: AttributionJudgement | None = None
    calibrated_p: float | None = None
    threshold: float | None = None
    above_threshold: bool = False
    memo_hit: bool = False
    placement: "pii.ContactPlacement | None" = None
    queue_items: tuple[EscalationItem, ...] = ()
    merge_proposal: AttributionMergeProposal | None = None
    suspect_id: str | None = None
    reason: str = ""


def _entity_brief_for_uid(context: AttributionContext, uid: str) -> EntityBrief:
    """Build an :class:`EntityBrief` for *uid* from the indexes in *context*.

    Falls back to ``name=uid`` when the uid resolves to no wiki page: a
    holder uid can legitimately have no page (design §10.3's orphaned
    records), and the judge is better served by a bare uid than by a brief
    this function refused to build. ``employer_domain_hints`` comes from
    :func:`_target_own_domains` -- the only deterministic domain signal this
    module has, for the reason that function's own docstring gives.
    """
    name = uid
    page_type: str | None = None
    aliases: tuple[str, ...] = ()
    page_path = (
        context.entity_index.get_by_uid(uid) if context.entity_index is not None else None
    )
    if page_path is not None:
        meta = pii.read_bounce_record(page_path)
        raw_name = meta.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        raw_type = meta.get("type")
        if isinstance(raw_type, str) and raw_type.strip():
            page_type = raw_type.strip()
        aliases = tuple(
            str(a).strip()
            for a in (meta.get("aliases") or [])
            if isinstance(a, str) and a.strip()
        )
    return EntityBrief(
        uid=uid,
        name=name,
        type=page_type,
        aliases=aliases,
        employer_domain_hints=tuple(sorted(_target_own_domains(context, uid))),
    )


def _source_excerpt_for(
    context: AttributionContext,
    value: str,
    source_ref: str | dict[str, Any] | None,
) -> str | None:
    """A window of the cited source around each occurrence of *value* (§6.2).

    Reuses the C3 machinery rather than reimplementing it:
    :func:`_extract_source_candidate_path` names the path without touching
    the filesystem, :func:`_resolve_source_text` reads it (``None`` for a
    source that no longer exists or is not UTF-8 text), and
    :func:`_value_occurrences` locates every verbatim occurrence through the
    same token detectors the PII lint uses. The window is
    :attr:`~AttributionContext.context_window_chars` characters each side
    (the existing ``librarian.attribution.context_window_chars`` knob);
    overlapping windows are merged so one span is not emitted twice.

    Returns ``None`` when there is no source, it cannot be read, or the
    value does not appear in it. The last case is C3's own "the value is
    absent from it" branch: there is no window around an occurrence that
    does not exist, and inventing one (the head of the file, say) would
    hand the judge text with no established relationship to the value.

    **Not fenced here.** :func:`_build_question_user_message` already wraps
    ``AttributionQuestion.source_excerpt`` in its own
    :func:`~athenaeum.prompt_safety.fence_untrusted` region with the
    ``source_excerpt`` tag and :data:`_SOURCE_EXCERPT_MAX_CHARS` budget, so
    fencing again here would nest delimiters inside a fence and make the
    boundary harder, not easier, for a model to respect.
    """
    path = _extract_source_candidate_path(source_ref)
    if path is None:
        return None
    text = _resolve_source_text(path)
    if text is None:
        return None
    spans = _value_occurrences(text, value)
    if not spans:
        return None
    window = max(1, int(context.context_window_chars))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        lo = max(0, start - window)
        hi = min(len(text), end + window)
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return "\n...\n".join(text[lo:hi] for lo, hi in merged)


def build_attribution_question(
    context: AttributionContext,
    value: str,
    verdict: AttributionVerdict,
    *,
    target_uid: str,
    target_name: str | None,
    target_type: str | None,
    source_ref: str | dict[str, Any] | None,
) -> AttributionQuestion:
    """Assemble design §6.2's :class:`AttributionQuestion` from a verdict.

    Holders are the uids on D2/D4/C1 evidence; candidates are the uids on
    C2/C3 evidence. Any uid in :attr:`AttributionVerdict.candidates` that
    appears on NEITHER (which is every candidate of a verdict rebuilt from a
    parked suspect, where per-check uids were not stored) is added to the
    candidates, because the design's candidate list is "uids the evidence
    points at as possible owners" and a parked union is still exactly that.
    A uid already named as a holder is never repeated as a candidate.
    """
    holder_uids: list[str] = []
    candidate_uids: list[str] = []
    for check in verdict.checks:
        bucket = (
            holder_uids
            if check.check_id in _HOLDER_CHECK_IDS
            else candidate_uids
            if check.check_id in _CANDIDATE_CHECK_IDS
            else None
        )
        if bucket is None:
            continue
        for uid in check.uids:
            if uid and uid not in holder_uids and uid not in candidate_uids:
                bucket.append(uid)
    for uid in verdict.candidates:
        if uid and uid not in holder_uids and uid not in candidate_uids:
            candidate_uids.append(uid)
    return AttributionQuestion(
        value=value,
        target=EntityBrief(
            uid=target_uid,
            name=target_name or target_uid,
            type=target_type,
            employer_domain_hints=tuple(sorted(_target_own_domains(context, target_uid))),
        ),
        holders=tuple(_entity_brief_for_uid(context, uid) for uid in holder_uids),
        candidates=tuple(_entity_brief_for_uid(context, uid) for uid in candidate_uids),
        checks=verdict.checks,
        source_excerpt=_source_excerpt_for(context, value, source_ref),
    )


def _format_brief_for_queue(brief: EntityBrief) -> str:
    return f"{brief.name} (uid={brief.uid})"


def _build_queue_item(
    *,
    question: AttributionQuestion,
    value: str,
    target_uid: str,
    question_class: str,
    judgement: AttributionJudgement,
    calibrated_p: float | None,
    threshold: float | None,
    suspect_id: str,
    source_ref: str | dict[str, Any] | None,
    origin_writer: str,
    reason: str,
) -> EscalationItem:
    """Render design §8.1's human-queue item for one unsettled suspect.

    ``conflict_type="ambiguous"``, so it reaches a human through the
    existing ``tiers.tier4_escalate`` path and appears in
    ``decisions.list_pending_decisions``.

    The value is MASKED (:func:`athenaeum.pii.mask_contact_address`) in both
    the description and ``entity_name`` -- the same discipline
    ``librarian._record_correspondent_contact`` already applies, and for the
    same reason: ``_pending_questions.md`` lives in the wiki, so the raw
    value must never reach it (design §8.1). The masked form is
    deliberately NOT a syntactically valid address either, so the rendered
    block cannot be re-detected as a fresh inline contact token (see
    :func:`athenaeum.pii.mask_contact_address`'s own docstring).
    """
    masked = pii.mask_contact_address(value)
    holders = ", ".join(_format_brief_for_queue(b) for b in question.holders) or "(none)"
    candidates = ", ".join(_format_brief_for_queue(b) for b in question.candidates) or "(none)"
    fired = ", ".join(c.check_id for c in question.checks) or "(none)"
    probability = "unmeasured" if calibrated_p is None else f"{calibrated_p:.3f}"
    threshold_text = (
        "none (no valid calibration for this class)" if threshold is None else f"{threshold:.3f}"
    )
    source_text = "(none recorded)" if source_ref is None else str(source_ref)
    description = (
        f"Attribution could not be settled for the contact value {masked} "
        f"headed for {_format_brief_for_queue(question.target)}.\n\n"
        f"- Reason it needs a human: {reason}\n"
        f"- Question class: {question_class}\n"
        f"- Checks that fired: {fired}\n"
        f"- Other uids holding or naming the value: {holders}\n"
        f"- Candidate owners the evidence points at: {candidates}\n"
        f"- Model verdict: {judgement.verdict}"
        f"{f' (owner {judgement.owner_uid})' if judgement.owner_uid else ''}"
        f"{f' (relation {judgement.alias_relation})' if judgement.alias_relation else ''}\n"
        f"- Calibrated probability: {probability} (class threshold: {threshold_text})\n"
        f"- Raw confidence signal: {judgement.score:.3f} "
        f"(model {judgement.model or 'unresolved'})\n"
        f"- Original source: {source_text} (writer: {origin_writer or 'unknown'})\n"
        f"- Suspect id: {suspect_id}\n\n"
        f"The value is shown masked because this file lives in the wiki. Read it with "
        f"`athenaeum attribution show {suspect_id}`, and answer with "
        f"`athenaeum attribution place {suspect_id} --as own|other:<uid>|"
        f"alias:<relation>|merge:<uid>`."
    )
    return EscalationItem(
        raw_ref=f"attribution-suspect:{suspect_id}",
        entity_name=f"{question.target.name} / {masked}",
        conflict_type="ambiguous",
        description=description,
    )


def escalate_contact_attribution_suspect(
    context: AttributionContext,
    value: str,
    verdict: AttributionVerdict,
    *,
    target_uid: str,
    target_name: str | None = None,
    target_type: str | None = None,
    source_ref: str | dict[str, Any] | None = None,
    writer: str = WRITER_RE_ATTRIBUTION,
    client: "LLMBackend | None" = None,
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    apply: bool = False,
    suspect_id: str | None = None,
    propose_merge: "Callable[..., Any] | None" = None,
    index: "pii.ExcludedRecordIndex | None" = None,
) -> EscalationOutcome:
    """Escalate one ``suspect`` verdict, and file or queue the answer.

    The join between the deterministic seam (:func:`check_contact_attribution`),
    the measured tier (:func:`judge_contact_attribution`) and the placement
    executor (:func:`athenaeum.pii.place_contact_value`).

    **Suspect only** (design §6.1): a :data:`DISPOSITION_PASS` or
    :data:`DISPOSITION_NON_PERSONAL` verdict returns immediately with
    ``escalated=False`` and makes NO model call -- not even a memo read.

    **Order of operations.** Question class, then the ``(model,
    prompt_sha256)`` identity, then the memo LOOKUP, and only on a miss the
    model call. A hit costs no call at all (design §6.1). The answer is
    memoised only when it IS an answer: a fail-closed ``unclear`` is not
    recorded, so a transient API failure cannot permanently suppress
    re-asking this question (issue athenaeum#2212, see
    :func:`is_memoisable_judgement`).

    **At or above threshold** the judgement goes to the placement executor
    with ``decided_by="model"``, the design §7.2 re-check bound to this
    ``context``, and ``apply`` passed straight through. Every caller in
    issue athenaeum#2212 passes ``apply=False``, so the executor reports the
    placement it WOULD have made and writes nothing; athenaeum#2220 flips
    that.

    **Below threshold, or unsettled, it goes to a human** as an
    :class:`~athenaeum.models.EscalationItem` with
    ``conflict_type="ambiguous"`` on :attr:`EscalationOutcome.queue_items`,
    for an L4 caller to pass to ``tiers.tier4_escalate``. The triggers are
    design §8.1's, plus the two malformed-answer guards below:

    - the calibrated probability is below the class threshold;
    - the judgement is :data:`JUDGEMENT_UNCLEAR` -- which is also what a
      failed call, an unparseable answer and a missing client all yield
      (design §6.3), so "unclear" and "the call failed" are one trigger
      rather than two;
    - the class has no valid calibration, so the threshold is ``None``;
    - the re-check returned ``suspect``
      (:data:`athenaeum.pii.PLACEMENT_QUEUED`) -- which "goes straight to
      the human queue and never back to the model" (design §7.2), a
      guarantee the executor keeps structurally by having no model access;
    - an ``alias`` verdict carrying no valid ``alias_relation``;
    - an ``other-known`` or ``same-entity`` verdict carrying no
      ``owner_uid``.

    The last two are **fail-closed** reads of design §6.3 rather than
    optional hardening. :func:`judge_contact_attribution` nulls an
    out-of-vocabulary ``alias_relation``, and
    :func:`athenaeum.pii.place_contact_value` RAISES ``ValueError`` on an
    ``alias`` verdict with no relation; routing a malformed answer to the
    executor would therefore let one bad model response abort a pipeline
    run. A malformed answer is exactly what a human should see.

    **Never raises.** Any unexpected failure anywhere above is logged and
    degraded to the human queue with a fail-closed ``unclear`` judgement
    (design §6.3) -- the whole point of this seam is that nothing is written
    on an answer the system could not establish.

    Args:
        apply: ``False`` (the default) is observe/shadow mode. Threaded
            verbatim into the executor.
        suspect_id: Defaults to :func:`suspect_id_for`'s
            ``<value_key>-<target_uid>`` stem, which is the parked file's
            own name.
        propose_merge: The L4 adapter onto
            ``pending_merges.write_pending_merge``. ``None`` (the default)
            means a ``same-entity`` verdict in apply mode writes nothing at
            all and reports :data:`athenaeum.pii.PLACEMENT_SKIPPED` --
            never a contact write instead.
        index: An already-built :class:`~athenaeum.pii.ExcludedRecordIndex`
            for the executor's record resolve. Defaults to ``None``
            (resolve fresh), deliberately NOT to
            :attr:`AttributionContext.excluded_index`: a placement WRITES,
            and a run-scoped index built before that write would not see a
            record a previous placement in the same sweep just minted.
    """
    resolved_suspect_id = suspect_id or suspect_id_for(verdict.value_key, target_uid)
    if verdict.disposition != DISPOSITION_SUSPECT:
        return EscalationOutcome(
            escalated=False,
            suspect_id=resolved_suspect_id,
            reason=(
                f"disposition {verdict.disposition!r} is not "
                f"{DISPOSITION_SUSPECT!r}; no model call (design §6.1)"
            ),
        )
    try:
        return _escalate_suspect_inner(
            context,
            value,
            verdict,
            target_uid=target_uid,
            target_name=target_name,
            target_type=target_type,
            source_ref=source_ref,
            writer=writer,
            client=client,
            config=config,
            usage=usage,
            apply=apply,
            suspect_id=resolved_suspect_id,
            propose_merge=propose_merge,
            index=index,
        )
    except Exception as exc:
        # Fail closed, never raise (design §6.3): nothing is written on an
        # answer the system could not establish, and the item goes to a human.
        log.warning(
            "pii_attribution: escalation for suspect %s failed (%s); "
            "sending it to the human queue",
            resolved_suspect_id,
            exc,
            exc_info=True,
        )
        question_class = question_class_for(verdict.checks)
        judgement = AttributionJudgement(verdict=JUDGEMENT_UNCLEAR, score=0.0)
        question = AttributionQuestion(
            value=value,
            target=EntityBrief(uid=target_uid, name=target_name or target_uid, type=target_type),
            checks=verdict.checks,
        )
        item = _build_queue_item(
            question=question,
            value=value,
            target_uid=target_uid,
            question_class=question_class,
            judgement=judgement,
            calibrated_p=None,
            threshold=None,
            suspect_id=resolved_suspect_id,
            source_ref=source_ref,
            origin_writer=writer,
            reason=f"the escalation itself failed ({type(exc).__name__}); fail-closed",
        )
        return EscalationOutcome(
            escalated=True,
            question_class=question_class,
            judgement=judgement,
            queue_items=(item,),
            suspect_id=resolved_suspect_id,
            reason="escalation raised; fail-closed to the human queue (design §6.3)",
        )


def _escalate_suspect_inner(
    context: AttributionContext,
    value: str,
    verdict: AttributionVerdict,
    *,
    target_uid: str,
    target_name: str | None,
    target_type: str | None,
    source_ref: str | dict[str, Any] | None,
    writer: str,
    client: "LLMBackend | None",
    config: dict[str, Any] | None,
    usage: TokenUsage | None,
    apply: bool,
    suspect_id: str,
    propose_merge: "Callable[..., Any] | None",
    index: "pii.ExcludedRecordIndex | None",
) -> EscalationOutcome:
    """The body of :func:`escalate_contact_attribution_suspect`.

    Split out so the public function is a thin, auditable fail-closed
    wrapper: the ``try`` block is the WHOLE body, and no later edit can add
    a step that accidentally sits outside it.
    """
    question_class = question_class_for(verdict.checks)
    model, prompt_sha256 = attribution_call_identity(question_class, config)

    judgement = memo_lookup(
        context,
        value_key=verdict.value_key,
        target_uid=target_uid,
        source_ref=source_ref,
        prompt_sha256=prompt_sha256,
        model=model,
    )
    memo_hit = judgement is not None
    question = build_attribution_question(
        context,
        value,
        verdict,
        target_uid=target_uid,
        target_name=target_name,
        target_type=target_type,
        source_ref=source_ref,
    )
    if judgement is None:
        judgement = judge_contact_attribution(question, client, config, usage)
        # Issue athenaeum#2212: a fail-closed `unclear` is NOT an answer, so
        # it is not memoised -- memoising one makes a single transient 429
        # permanent for this (value_key, target_uid, source_ref,
        # prompt_sha256, model) identity. See
        # :func:`is_memoisable_judgement` for how a failure is told apart
        # from a model that genuinely answered "unclear".
        if is_memoisable_judgement(judgement):
            record_attribution_memo(
                context,
                judgement,
                value_key=verdict.value_key,
                target_uid=target_uid,
                source_ref=source_ref,
            )

    calibrated_p = calibrated_probability_for_class(question_class, judgement.score)
    threshold = threshold_for_class(question_class, config, wiki_root=context.wiki_root)
    above_threshold = threshold is not None and calibrated_p >= threshold

    record_suspect_judgement(
        context,
        suspect_id=suspect_id,
        question_class=question_class,
        judgement=judgement,
        calibrated_p=calibrated_p,
        threshold=threshold,
    )

    queue_reason: str | None = None
    if judgement.verdict == JUDGEMENT_UNCLEAR:
        queue_reason = (
            "the judgement is unclear, or the call failed or was never made "
            "(escalation fails closed, design §6.3)"
        )
    elif threshold is None:
        queue_reason = (
            "this question class has no valid calibration record, so there is "
            "no threshold to clear (design §8.1)"
        )
    elif judgement.verdict == JUDGEMENT_ALIAS and judgement.alias_relation not in ALIAS_RELATIONS:
        queue_reason = (
            "the model answered 'alias' with no valid identifier relation; "
            "filing an alias with no stored relation would lose the only "
            "record of why the address is there (design §6.3)"
        )
    elif (
        judgement.verdict in (JUDGEMENT_OTHER_KNOWN, JUDGEMENT_SAME_ENTITY)
        and not judgement.owner_uid
    ):
        queue_reason = (
            f"the model answered {judgement.verdict!r} without naming an "
            "owner uid, so there is no record to re-attribute to (design §6.3)"
        )
    elif not above_threshold:
        queue_reason = (
            f"the calibrated probability ({calibrated_p:.3f}) is below this "
            f"class's threshold ({threshold:.3f}) (design §8.1)"
        )

    placement: "pii.ContactPlacement | None" = None
    merge_proposal: AttributionMergeProposal | None = None
    if queue_reason is None:
        captured: list[AttributionMergeProposal] = []

        def _recheck(recheck_value: str, **kwargs: Any) -> AttributionVerdict:
            return check_contact_attribution(recheck_value, context=context, **kwargs)

        def _propose(
            *, target_uid: str, owner_uid: str, value: str, rationale: str
        ) -> Any:
            captured.append(
                AttributionMergeProposal(
                    target_uid=target_uid,
                    owner_uid=owner_uid,
                    value=value,
                    rationale=rationale,
                )
            )
            assert propose_merge is not None  # only injected when non-None
            return propose_merge(
                target_uid=target_uid,
                owner_uid=owner_uid,
                value=value,
                rationale=rationale,
            )

        placement = pii.place_contact_value(
            context.contacts_root,
            value,
            verdict=judgement.verdict,
            target_uid=target_uid,
            target_name=target_name,
            owner_uid=judgement.owner_uid,
            owner_name=(
                _entity_brief_for_uid(context, judgement.owner_uid).name
                if judgement.owner_uid
                else None
            ),
            alias_relation=judgement.alias_relation,
            original_source=source_ref,
            writer=WRITER_RE_ATTRIBUTION,
            model=judgement.model,
            prompt_sha256=judgement.prompt_sha256,
            calibrated_p=calibrated_p,
            decided_by="model",
            observed_at=now_iso(),
            apply=apply,
            recheck=_recheck,
            propose_merge=_propose if propose_merge is not None else None,
            index=index,
        )
        merge_proposal = captured[0] if captured else None
        if placement.outcome == pii.PLACEMENT_QUEUED:
            queue_reason = (
                "the design §7.2 re-check returned 'suspect', so the placement "
                "would itself create a fresh conflict; it never goes back to a model"
            )
        elif apply and context.wiki_root is not None and context.wiki_root.is_dir():
            # Issue athenaeum#2219, design §8.3: audit-sample this
            # above-threshold placement into the calibration ledger. Called
            # unconditionally on every verdict that reached the executor
            # (not just the three watched ones) -- sample_tier_decision's
            # own `_WATCHED_VERDICT` gate is what filters `other-unknown` /
            # `same-entity` / `non-personal` out, exactly mirroring how T1's
            # single call site trusts that same gate rather than
            # pre-filtering by verdict itself.
            #
            # ``apply``-gated (issue athenaeum#2212's shadow/observe mode is
            # NOT sampled): the issue's own motivation is calibration
            # against "the live decisions the system actually makes", and
            # in observe mode nothing is actually made -- every current
            # caller passes ``apply=False`` (athenaeum#2220 flips that), so
            # this sampler is correctly inert in production until then,
            # same as `escalate_shadow_suspects`'s own "files nothing,
            # changes nothing" contract already asserts
            # (`tests/test_attribution_escalation.py::
            # TestShadowSweepObserveMode`). *reason* never carries the raw
            # value (design §8.1's masking discipline, extended to this
            # ledger too) -- only the masked address and the measured
            # numbers a human reviewing the item needs.
            sample_tier_decision(
                context.wiki_root,
                tier=ATTRIBUTION_TIER_NAME,
                verdict=judgement.verdict,
                proposal_id=suspect_id,
                reason=(
                    f"{question_class} placement of {pii.mask_contact_address(value)} "
                    f"-> {placement.verdict!r} on {placement.placed_uid or target_uid} "
                    f"(calibrated_p={calibrated_p:.3f}, threshold={threshold:.3f})"
                ),
                config=config,
                applied=placement.outcome == pii.PLACEMENT_FILED,
                question_class=question_class,
            )

    queue_items: tuple[EscalationItem, ...] = ()
    if queue_reason is not None:
        queue_items = (
            _build_queue_item(
                question=question,
                value=value,
                target_uid=target_uid,
                question_class=question_class,
                judgement=judgement,
                calibrated_p=calibrated_p,
                threshold=threshold,
                suspect_id=suspect_id,
                source_ref=source_ref,
                origin_writer=writer,
                reason=queue_reason,
            ),
        )

    log_attribution_escalation(
        context,
        value_key=verdict.value_key,
        target_uid=target_uid,
        writer=writer,
        question_class=question_class,
        judgement=judgement,
        calibrated_p=calibrated_p,
        threshold=threshold,
        above_threshold=above_threshold,
        memo_hit=memo_hit,
        placement=placement,
        queued=bool(queue_items),
        mode="enforce" if apply else "observe",
    )
    return EscalationOutcome(
        escalated=True,
        question_class=question_class,
        judgement=judgement,
        calibrated_p=calibrated_p,
        threshold=threshold,
        above_threshold=above_threshold,
        memo_hit=memo_hit,
        placement=placement,
        queue_items=queue_items,
        merge_proposal=merge_proposal,
        suspect_id=suspect_id,
        reason=queue_reason or "at or above threshold; handed to the placement executor",
    )


# ---------------------------------------------------------------------------
# Attribution audit review (issue athenaeum#2219, design §8.3)
# ---------------------------------------------------------------------------


class AttributionAuditReviewError(ValueError):
    """*audit_id* names no pending ATTRIBUTION audit item."""


@dataclass(frozen=True)
class AttributionAuditReviewOutcome:
    """What one :func:`record_attribution_audit_review` call did.

    ``review`` is always :func:`athenaeum.calibration.record_audit_review`'s
    own return value -- the calibration signal is recorded either way.
    ``placement`` is ``None`` on a CONFIRM (human_verdict matches the
    sampled verdict; nothing else happens) and the
    :class:`athenaeum.pii.ContactPlacement` the re-file produced on an
    OVERTURN.
    """

    review: dict[str, Any]
    placement: "pii.ContactPlacement | None"

    @property
    def overturned(self) -> bool:
        return bool(self.review.get("overturned"))


def record_attribution_audit_review(
    context: AttributionContext,
    *,
    audit_id: str,
    human_verdict: str,
    owner_uid: str | None = None,
    owner_name: str | None = None,
    alias_relation: str | None = None,
    note: str = "",
    propose_merge: "Callable[..., Any] | None" = None,
    index: "pii.ExcludedRecordIndex | None" = None,
) -> AttributionAuditReviewOutcome:
    """Record a human's confirm/overturn of a sampled ATTRIBUTION audit item
    (issue athenaeum#2219, design §8.3).

    **Confirm** (*human_verdict* equals the sampled item's own ``verdict``):
    records the review via :func:`athenaeum.calibration.record_audit_review`
    and does nothing else -- the SAME "a confirm changes nothing" contract
    T1/T2 audits already have.

    **Overturn** (*human_verdict* differs): records the SAME calibration
    signal first, THEN re-files the value through the placement executor
    (:func:`athenaeum.pii.place_contact_value`) with the human's answer,
    ``decided_by="human"`` and ``apply=True`` -- mirroring
    :mod:`athenaeum._cmd_attribution`'s own ``place`` command, which is the
    SAME shape for the SAME reason: the audit record's ``proposal_id`` IS
    the parked suspect's id (see the sampler's call site in
    :func:`_escalate_suspect_inner`), so the raw value is read back through
    the one documented way out (:func:`read_suspect`) rather than ever
    having been stored in the calibration ledger, which lives in the wiki
    (design §8.1's masking discipline).

    *owner_uid* / *owner_name* / *alias_relation* are required exactly when
    *human_verdict* needs them (``other-known``/``same-entity`` need
    ``owner_uid``; ``alias`` needs ``alias_relation``) -- unsupplied, they
    reach :func:`athenaeum.pii.place_contact_value` as ``None`` and that
    function's own existing validation (skip / raise) applies unchanged.

    Raises:
        AttributionAuditReviewError: *audit_id* is not a pending item with
            ``tier`` :data:`athenaeum.calibration.ATTRIBUTION_TIER_NAME` on
            *context*'s wiki (checked BEFORE any write) -- or, on an
            OVERTURN, the re-file step failed AFTER the calibration signal
            was already recorded (the suspect is no longer readable, or
            :func:`athenaeum.pii.place_contact_value` itself raised). In
            that second case the message names the committed signal
            explicitly and points at the real recovery path
            (``athenaeum attribution place <suspect-id> --as ...``) rather
            than letting a retry of this same call fail opaquely -- see
            the try/except around the re-file step for why a retry cannot
            double-record the signal either way.
        ValueError: propagated from
            :func:`athenaeum.calibration.record_audit_review` for an
            unknown or already-reviewed *audit_id* (same vocabulary; the
            lookup above is what turns "unknown" and "not attribution" into
            the SAME clear error before any write happens). This is also
            exactly what a caller sees if it retries this function with an
            *audit_id* whose review already landed -- by design, never a
            duplicate write.
    """
    if context.wiki_root is None:
        raise AttributionAuditReviewError(
            "this AttributionContext has no wiki_root; there is no calibration "
            "ledger to read or write"
        )
    records = read_calibration_ledger(context.wiki_root)
    audit = next(
        (
            r
            for r in records
            if r.get("kind") == _CALIBRATION_AUDIT_KIND and str(r.get("id")) == audit_id
        ),
        None,
    )
    if audit is None or audit.get("tier") != ATTRIBUTION_TIER_NAME:
        raise AttributionAuditReviewError(
            f"not a pending attribution audit item: {audit_id!r}"
        )
    suspect_id = str(audit.get("proposal_id") or "")

    review = record_audit_review(
        context.wiki_root, audit_id=audit_id, human_verdict=human_verdict, note=note
    )
    if not review["overturned"]:
        return AttributionAuditReviewOutcome(review=review, placement=None)

    # AC3's own ordering ("records the calibration signal, THEN re-files")
    # is fixed by design (Seer finding B, PR athenaeum#2269 -- rejected:
    # do not reorder). The ledger write above is already committed and
    # permanent by the time we reach here: `calibration.record_audit_review`
    # allows each audit item to be reviewed AT MOST ONCE (raises
    # ``ValueError`` on any second call for the same `audit_id`), so a
    # caller that retries THIS function with the same `audit_id` after a
    # failure below gets that same ``ValueError`` immediately, before
    # `place_contact_value` is ever reached again -- the calibration
    # signal is therefore idempotent by construction and CANNOT be
    # double-recorded by a retry. But a raw retry also cannot complete the
    # placement either, and a bare exception from `read_suspect` /
    # `place_contact_value` would otherwise leak out unwrapped with no
    # indication that the signal already landed or what to do next. Wrap
    # both in one `AttributionAuditReviewError` that says so explicitly
    # and names the real recovery path -- re-filing directly via
    # `athenaeum attribution place <suspect-id> --as ...`, which the
    # suspect file (untouched by this failure) still supports.
    try:
        suspect = read_suspect(context.contacts_root, suspect_id)
        if suspect is None:
            raise AttributionAuditReviewError(
                f"cannot re-file overturned audit {audit_id!r}: suspect "
                f"{suspect_id!r} is no longer readable under {context.contacts_root}"
            )

        def _recheck(recheck_value: str, **kwargs: Any) -> AttributionVerdict:
            return check_contact_attribution(recheck_value, context=context, **kwargs)

        placement = pii.place_contact_value(
            context.contacts_root,
            suspect.value,
            verdict=human_verdict,
            target_uid=suspect.target_uid,
            target_name=suspect.target_name,
            owner_uid=owner_uid,
            owner_name=owner_name,
            alias_relation=alias_relation,
            original_source=suspect.source_ref,
            writer=WRITER_RE_ATTRIBUTION,
            # Design §8.1: a human answer carries no calibrated probability,
            # model id or prompt sha -- there was no model call to
            # attribute it to, same as `_cmd_attribution._cmd_place`'s own
            # human re-filing.
            model=None,
            prompt_sha256=None,
            calibrated_p=None,
            decided_by="human",
            observed_at=now_iso(),
            apply=True,
            recheck=_recheck,
            propose_merge=propose_merge,
            index=index,
        )
    except AttributionAuditReviewError:
        raise
    except Exception as exc:
        raise AttributionAuditReviewError(
            f"the overturn of audit {audit_id!r} (human_verdict={human_verdict!r}) was "
            "RECORDED, but re-filing the value failed "
            f"({type(exc).__name__}: {exc}). The calibration signal is already "
            "permanent -- do not retry this function with the same audit_id, it will "
            "raise ValueError('audit item already reviewed'). Re-file the value "
            f"directly instead: `athenaeum attribution place {suspect_id} --as "
            "own|other:<uid>|alias:<relation>|merge:<uid>`."
        ) from exc
    return AttributionAuditReviewOutcome(review=review, placement=placement)


# ---------------------------------------------------------------------------
# Observe-mode pipeline hook (design §9.1, §9.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowEscalationSummary:
    """What one :func:`escalate_shadow_suspects` sweep saw (design §9.2).

    ``proposed`` counts placements the apply path WOULD have made
    (:data:`athenaeum.pii.PLACEMENT_PROPOSED`); ``queued`` counts suspects
    that would reach a human. ``placements`` and ``queue_items`` carry the
    objects themselves so an L4 caller can act on them -- in this issue it
    only counts and logs them, because observe mode files nothing.

    **The four skip/do counters partition ``considered`` exactly** (issue
    athenaeum#2212)::

        considered == escalated
                      + skipped_not_suspect
                      + skipped_already_answered
                      + skipped_over_cap

    so an operator can reconcile the row rather than infer a remainder.
    ``skipped_over_cap`` counts only suspects that still NEED escalating and
    did not fit the cap, which is the number that must drain to zero as
    successive sweeps work through a backlog.

    ``memo_hits`` is kept and still counts honestly, but is normally ``0``
    in a sweep now: a memo hit is exactly what ``skipped_already_answered``
    filters out BEFORE the cap is spent on it, so the sweep rarely reaches
    one. It is ``skipped_already_answered`` that carries the "this cost no
    model call" story for this function; ``memo_hits`` still moves for the
    direct :func:`escalate_contact_attribution_suspect` callers.
    """

    considered: int = 0
    escalated: int = 0
    proposed: int = 0
    queued: int = 0
    memo_hits: int = 0
    skipped_not_suspect: int = 0
    skipped_already_answered: int = 0
    skipped_over_cap: int = 0
    placements: tuple["pii.ContactPlacement", ...] = ()
    queue_items: tuple[EscalationItem, ...] = ()
    outcomes: tuple[EscalationOutcome, ...] = ()


def escalate_shadow_suspects(
    context: AttributionContext,
    *,
    client: "LLMBackend | None" = None,
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    limit: int | None = None,
) -> ShadowEscalationSummary:
    """Escalate the parked SHADOW suspects, filing nothing (design §9.1).

    The observe-mode pipeline hook. Every suspect is escalated with
    ``apply=False``, so each placement comes back as
    :data:`athenaeum.pii.PLACEMENT_PROPOSED` and the proposed placement plus
    its calibrated probability is written to the attribution log
    (:func:`log_attribution_escalation`) -- design §9.2's "shadow escalation
    verdicts and their calibrated probabilities". **No contact record is
    written and no writer's output changes.**

    Costs nothing without a client: ``client=None`` returns an empty summary
    before reading the suspects directory at all. A clientless escalation
    would yield ``unclear`` for every suspect (design §6.3) and so flood the
    human queue on every run -- the one failure mode a default-on observe
    hook could cause. This is the second of the two guards; the first is
    ``librarian.attribution.escalate``, which is DEFAULT OFF
    (:func:`athenaeum.config.resolve_attribution_escalate`).

    *limit* caps how many suspects one sweep escalates
    (``librarian.attribution.max_escalations_per_run``, default 25, when the
    caller resolves it). ``None`` means uncapped, and a non-positive value
    means "do nothing" -- the same escape hatch the librarian's own runtime
    knob uses. Suspects are taken in sorted filename order, which is stable
    across runs, so a capped sweep works through a backlog deterministically
    rather than re-rolling the same arbitrary subset.

    **The cap is applied to what still needs escalating, not to the whole
    directory** (issue athenaeum#2212). Sorted order alone is only half of
    "works through a backlog": slicing the full listing re-selects the same
    first *limit* entries on every run, so with a backlog larger than the
    cap the tail is never reached and ``skipped_over_cap`` never drains. The
    memo made the repeat calls free, which is why the stall was silent --
    it is a progress bug, not a spend bug. So two classes are filtered out
    BEFORE the slice, keeping sorted order among what remains:

    - a suspect whose question has already been answered under the CURRENT
      ``(model, prompt_sha256)`` identity, i.e. one the memo already holds a
      row for. The memo -- not the suspect file's ``judgement`` stamp -- is
      the oracle here, deliberately: it is keyed on exactly the five
      components that decide whether a fresh call would ask a NEW question
      (design §4.3), so a bumped model or an edited judge prompt still
      re-asks (the property :func:`attribution_memo_key` exists for), and a
      fail-closed ``unclear`` -- which athenaeum#2212 stopped memoising --
      correctly stays pending so the retry the design promises can happen.
      A ``judgement``-stamp filter would silently defeat both.
    - a suspect whose ``disposition`` is not ``suspect``.
      :func:`escalate_contact_attribution_suspect` returns ``escalated=
      False`` for those without a model call (design §6.1), so letting one
      consume cap budget costs a slot and buys nothing.

    The memo is read ONCE per sweep rather than per suspect. A row appended
    by this sweep's own escalations is therefore not in that snapshot, which
    is harmless: those suspects were selected precisely because no row
    existed, and the next sweep reads a fresh snapshot.
    """
    if client is None:
        return ShadowEscalationSummary()
    if limit is not None and limit <= 0:
        return ShadowEscalationSummary()
    suspects = iter_parked_suspects(context.contacts_root, shadow_only=True)
    considered = len(suspects)
    memo = read_attribution_memo(
        default_attribution_memo_path(context.contacts_root)
    )
    pending: list[ParkedSuspect] = []
    skipped_not_suspect = 0
    skipped_already_answered = 0
    for suspect in suspects:
        parked_verdict = verdict_from_parked_suspect(suspect)
        if parked_verdict.disposition != DISPOSITION_SUSPECT:
            skipped_not_suspect += 1
            continue
        model, prompt_sha256 = attribution_call_identity(
            question_class_for(parked_verdict.checks), config
        )
        if (
            attribution_memo_key(
                value_key=suspect.value_key,
                target_uid=suspect.target_uid,
                source_ref=suspect.source_ref,
                prompt_sha256=prompt_sha256,
                model=model,
            )
            in memo
        ):
            skipped_already_answered += 1
            continue
        pending.append(suspect)
    selected = pending if limit is None else pending[:limit]
    escalated = 0
    proposed = 0
    queued = 0
    memo_hits = 0
    placements: list["pii.ContactPlacement"] = []
    queue_items: list[EscalationItem] = []
    outcomes: list[EscalationOutcome] = []
    for suspect in selected:
        outcome = escalate_contact_attribution_suspect(
            context,
            suspect.value,
            verdict_from_parked_suspect(suspect),
            target_uid=suspect.target_uid,
            target_name=suspect.target_name,
            target_type=None,
            source_ref=suspect.source_ref,
            writer=suspect.writer or WRITER_RE_ATTRIBUTION,
            client=client,
            config=config,
            usage=usage,
            apply=False,
            suspect_id=suspect.suspect_id,
        )
        outcomes.append(outcome)
        if not outcome.escalated:
            continue
        escalated += 1
        if outcome.memo_hit:
            memo_hits += 1
        if outcome.placement is not None:
            placements.append(outcome.placement)
            if outcome.placement.outcome == pii.PLACEMENT_PROPOSED:
                proposed += 1
        if outcome.queue_items:
            queued += 1
            queue_items.extend(outcome.queue_items)
    return ShadowEscalationSummary(
        considered=considered,
        escalated=escalated,
        proposed=proposed,
        queued=queued,
        memo_hits=memo_hits,
        skipped_not_suspect=skipped_not_suspect,
        skipped_already_answered=skipped_already_answered,
        skipped_over_cap=max(0, len(pending) - len(selected)),
        placements=tuple(placements),
        queue_items=tuple(queue_items),
        outcomes=tuple(outcomes),
    )


__all__ += [
    "calibrated_probability",
    "calibrated_probability_for_class",
    "threshold_for_class",
    "attribution_call_identity",
    "ATTRIBUTION_MEMO_FILENAME",
    "ATTRIBUTION_MEMO_VERSION",
    "ATTRIBUTION_LOG_KIND_ESCALATION",
    "default_attribution_memo_path",
    "attribution_memo_key",
    "read_attribution_memo",
    "memo_lookup",
    "record_attribution_memo",
    "is_memoisable_judgement",
    "SuspectIdError",
    "ParkedSuspect",
    "suspect_id_for",
    "suspect_path_for_id",
    "read_suspect",
    "iter_parked_suspects",
    "verdict_from_parked_suspect",
    "record_suspect_judgement",
    "log_attribution_escalation",
    "AttributionMergeProposal",
    "EscalationOutcome",
    "build_attribution_question",
    "escalate_contact_attribution_suspect",
    "AttributionAuditReviewError",
    "AttributionAuditReviewOutcome",
    "record_attribution_audit_review",
    "ShadowEscalationSummary",
    "escalate_shadow_suspects",
]
