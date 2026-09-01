# SPDX-License-Identifier: Apache-2.0
"""Shared data-model hub: frontmatter parsing, claim metadata, and wiki entity shapes.

Contract: this is the single source of truth for what a memory/claim/wiki-page
*looks like* on disk and in memory — frontmatter parse/render
(:func:`parse_frontmatter` / :func:`render_frontmatter`), claim-level metadata
parsers (source type, claim kind, validity window, audience/access, refines/
supersedes, asserter identity), and the core dataclasses (:class:`RawFile`,
:class:`AutoMemoryFile`, :class:`WikiEntity`, :class:`EntityIndex`,
:class:`TokenUsage`, :class:`ProcessingResult`, etc.).

Factoring rule: this module holds DATA SHAPES and pure parse/coerce functions
over them — no I/O beyond trivial frontmatter (de)serialization, no network
calls, no merge/resolution/clustering *policy*. It is the L1 hub every layer
above L1 imports (config, services, pipeline, presentation); nothing here may
import from those higher layers, so a change here can ripple outward but never
loop back. Fail-open is the house style throughout: a malformed or
out-of-vocabulary field value degrades to a safe default (logged at debug/
warning) rather than raising, because a bad frontmatter value must never crash
the nightly compile.

Non-obvious invariant: many parsers here look similar (``parse_x(meta) ->
default-on-anything-wrong``) by design — this is the shared boundary where
every other module's trust in frontmatter shape is established or fails open.
Do not duplicate a parser elsewhere; add a new field's accessor here instead.
"""

from __future__ import annotations

import logging
import re
import uuid
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

import yaml

log = logging.getLogger(__name__)

# --- UID generation ---


def generate_uid() -> str:
    """Generate an 8-character hex UID from uuid4."""
    return uuid.uuid4().hex[:8]


# --- Origin-traced source provenance (issue athenaeum#260, slice A of athenaeum#259) ---

# The legal ``source_type`` values for an origin-traced citation. The
# librarian must cite the ULTIMATE source of a fact — the user, an external
# URL, a permanent document, or (when nothing can be established) an honest
# ``inferred``. It must NEVER cite the raw ``auto-memory/...`` filename as the
# source. See ``policies/auto-memory-citation.md``.
#
# Channel split (issue athenaeum#326): three AI provenance channels were previously
# collapsed under ``inferred``. They are now distinguished:
#
#   ``user-stated``     — human utterance in a session.
#   ``agent-observed``  — AI derived it from in-session artifacts (file
#                         contents, tool output). Verifiable against the
#                         transcript, unlike ``model-prior``.
#   ``inferred``        — AI leap without artifact backing. Stays the
#                         fail-open default via :func:`coerce_source_type`.
#   ``model-prior``     — asserted from training-data knowledge with no
#                         session evidence. Unverifiable and silently stale
#                         past the model cutoff — ranks BELOW ``script:`` in
#                         the resolver's source-precedence taxonomy.
#   ``external``        — externally cited (URL, third-party record).
#   ``document``        — permanent document (PDF, spec, contract).
SOURCE_TYPES: frozenset[str] = frozenset(
    {
        "user-stated",
        "agent-observed",
        "external",
        "document",
        "inferred",
        "model-prior",
    }
)

# The three AI-attributed channels. All three carry a ``model:`` claim
# annotation (per issue athenaeum#326) — a `model-prior` without a model is
# untraceable to a cutoff date; an `agent-observed` without a model
# cannot be cross-checked against a specific model's known reasoning
# quirks. Callers should stamp ``model:`` when writing these channels;
# validation stays fail-open (the field is optional at the schema level).
AI_ATTRIBUTED_SOURCE_TYPES: frozenset[str] = frozenset(
    {"agent-observed", "inferred", "model-prior"}
)

# Default when origin cannot be established. ``inferred`` is the honest
# fallback — an unverifiable agent leap is labeled as such, not promoted to
# ``user-stated``.
DEFAULT_SOURCE_TYPE = "inferred"


def coerce_source_type(value: object) -> str:
    """Return a valid ``source_type``, defaulting unknown input to ``inferred``.

    Backward-compatible: legacy sources written before athenaeum#260 carry no
    ``source_type`` (``None``) and resolve to ``inferred``. A typo'd or
    out-of-vocabulary value is also coerced rather than raising — the
    citation policy is enforced at write time, and a bad value must not
    crash the nightly compile.
    """
    if isinstance(value, str) and value in SOURCE_TYPES:
        return value
    # A non-empty, out-of-vocabulary value is a real downgrade (typo or stale
    # schema) worth a breadcrumb; ``None`` / empty is the ordinary legacy path
    # and stays quiet.
    if value not in (None, ""):
        log.debug(
            "coerce_source_type: downgrading invalid source_type %r to %s",
            value,
            DEFAULT_SOURCE_TYPE,
        )
    return DEFAULT_SOURCE_TYPE


def is_filename_like_ref(ref: object) -> bool:
    """True when a ``source_ref`` looks like a raw ``auto-memory`` filename.

    The load-bearing athenaeum#260 invariant: a citation must point at the ULTIMATE
    source (session+turn / URL / document), never at the transient raw
    ``auto-memory/<scope>/<prefix>_<slug>.md`` view that retires on move
    (athenaeum#259). A ref is filename-shaped when it references the auto-memory tree
    or ends in ``.md``.
    """
    if not isinstance(ref, str) or not ref:
        return False
    lowered = ref.lower()
    return "auto-memory" in lowered or lowered.endswith(".md")


def safe_source_ref(candidate: object, fallback: str) -> str:
    """Return ``candidate`` unless it is filename-shaped, else ``fallback``.

    Enforces the athenaeum#260 invariant on the EXPLICIT path: a producer that stamps
    a raw filename into ``source_ref`` is rejected and replaced with a safe
    session-anchored fallback. Empty candidate also falls back.
    """
    if isinstance(candidate, str) and candidate and not is_filename_like_ref(candidate):
        return candidate
    if is_filename_like_ref(candidate):
        log.debug(
            "safe_source_ref: rejecting filename-shaped source_ref %r; using %r",
            candidate,
            fallback,
        )
    return fallback


# --- Model recording + asserter identity (issue athenaeum#326) ---
#
# AI-attributed claims (`agent-observed`, `inferred`, `model-prior`) carry a
# ``model:`` model-id string so provenance audits can trace a stale claim to a
# specific model cutoff. Human-attributed claims (`user-stated`) carry an
# ``asserter:`` block naming WHICH person made the claim in a way that survives
# email changes and organizational IdP swaps. Both fields are OPTIONAL and
# validated fail-open — a malformed value logs a breadcrumb and returns the
# empty/default form, matching :func:`coerce_source_type`'s "must not crash the
# nightly compile" contract.
#
# Asserter identity keys on the OIDC-guaranteed stable pair (``iss`` + ``sub``).
# Microsoft Entra's ``sub`` claim is PAIRWISE per app (RFC 9068 / OIDC-Core §8.1
# — a client that sees ``sub=X`` for a user in app A gets a DIFFERENT ``sub``
# for the same user in app B). Entra's stable per-tenant identity is
# (``tid``, ``oid``) instead — so an Entra asserter stores those under
# ``provider_ids`` and the identity-key derivation prefers them. Google, Okta,
# and every OIDC provider that respects the spec-recommended stable-``sub`` are
# keyed on (``iss``, ``sub``). ``email`` is a display snapshot — NEVER a key.


def parse_model(meta: dict[str, object] | None) -> str:
    """Return the frontmatter ``model:`` model-id, or ``""`` when absent/malformed.

    A model-id is a free-form string (e.g. ``"claude-opus-4-7"``,
    ``"gpt-5-2026-06-01"``). Fail-open: a non-string value returns ``""``
    rather than raising — the claim is still parseable, the model
    attribution is just missing.
    """
    if not meta:
        return ""
    raw = meta.get("model")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        log.debug(
            "model: expected string model-id, got %r (%s); treating as absent",
            raw,
            type(raw).__name__,
        )
        return ""
    return raw.strip()


def parse_on_behalf_of(meta: dict[str, object] | None) -> str:
    """Return the frontmatter ``on_behalf_of:`` principal name, or ``""``.

    W3C PROV ``actedOnBehalfOf`` — names the responsible human principal
    when a model asserted a claim on their behalf (model asserted, human
    accountable). Fail-open: a non-string value returns ``""``.
    """
    if not meta:
        return ""
    raw = meta.get("on_behalf_of")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        log.debug(
            "on_behalf_of: expected string principal name, got %r (%s); "
            "treating as absent",
            raw,
            type(raw).__name__,
        )
        return ""
    return raw.strip()


# Legal ``asserter.type`` values. Aligns with W3C PROV's Agent taxonomy
# (Person / SoftwareAgent / Organization) so future SCIM (RFC 7643)
# provisioning can correlate directly.
ASSERTER_TYPES: frozenset[str] = frozenset({"person", "software_agent", "organization"})


def parse_asserter(meta: dict[str, object] | None) -> dict[str, object]:
    """Return the frontmatter ``asserter:`` block, or ``{}`` when absent/malformed.

    Shape (issue athenaeum#326):

    .. code-block:: yaml

        asserter:
          type: person                        # person | software_agent | organization
          iss: "https://accounts.google.com"  # OIDC issuer (durable key part 1)
          sub: "1076..."                      # OIDC subject (durable key part 2)
          provider_ids:                       # optional per-provider extras
            entra_oid: "..."
            entra_tid: "..."
          email: user@example.com             # display snapshot; NEVER a key
          name: "Alice Example"               # display snapshot; NEVER a key

    Fail-open: a non-dict value returns ``{}``. Individual sub-fields with
    non-string values are dropped rather than raising — the goal is a
    round-trip guarantee for the identity fields that were correctly
    typed, not a strict schema gate.

    Round-trip fidelity is on the WRITE path (:meth:`WikiEntity.render` /
    :meth:`AutoMemoryFile.is_inactive` etc. carry the dict verbatim) —
    this parser normalizes for READ-side consumers (identity key
    derivation, email-change detection). Unknown keys pass through
    untouched so future extensions round-trip.
    """
    if not meta:
        return {}
    raw = meta.get("asserter")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        log.debug(
            "asserter: expected mapping, got %r (%s); treating as absent",
            raw,
            type(raw).__name__,
        )
        return {}
    normalized: dict[str, object] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            log.debug("asserter: non-string key %r dropped", k)
            continue
        normalized[k] = v
    a_type = normalized.get("type")
    if isinstance(a_type, str) and a_type and a_type not in ASSERTER_TYPES:
        log.debug(
            "asserter: type %r not in %s; kept for round-trip but"
            " identity-key derivation may skip provider-specific fields",
            a_type,
            sorted(ASSERTER_TYPES),
        )
    return normalized


def asserter_identity_key(asserter: dict[str, object] | None) -> tuple[str, ...]:
    """Derive the durable identity key from an ``asserter`` block.

    Returns a tuple of ``(iss, sub)`` — or, when Microsoft Entra
    ``provider_ids`` are present, ``(iss, "entra", tid, oid)``. The Entra
    branch handles the Entra ``sub``-is-pairwise-per-app trap: the OIDC
    ``sub`` is not stable across apps for the same user, so we key on the
    tenant-scoped object id instead.

    Returns ``()`` when no durable identifier can be derived — a caller
    that keys memories by asserter should treat that as "no identity
    declared" and fall back to owner-only defaults.

    Key rules (locked by ``docs/provenance-shape.md`` §10):

    - Empty / non-dict asserter → ``()``.
    - Entra branch: ``iss`` set AND ``provider_ids`` carries a non-empty
      ``entra_tid`` + ``entra_oid`` → ``(iss, "entra", tid, oid)``. The
      ``sub`` value is IGNORED (pairwise per app).
    - Standard branch: non-empty ``iss`` AND non-empty ``sub`` →
      ``(iss, sub)``.
    - Anything else → ``()``.

    ``email`` is NEVER part of the key — a Google/Okta/Entra user who
    changes their email keeps the same identity key. That's the whole
    point of keying on the OIDC-durable pair.
    """
    if not isinstance(asserter, dict) or not asserter:
        return ()
    iss = asserter.get("iss")
    if not isinstance(iss, str) or not iss.strip():
        return ()
    iss = iss.strip()

    provider_ids = asserter.get("provider_ids")
    if isinstance(provider_ids, dict):
        tid = provider_ids.get("entra_tid")
        oid = provider_ids.get("entra_oid")
        if (
            isinstance(tid, str)
            and tid.strip()
            and isinstance(oid, str)
            and oid.strip()
        ):
            return (iss, "entra", tid.strip(), oid.strip())

    sub = asserter.get("sub")
    if isinstance(sub, str) and sub.strip():
        return (iss, sub.strip())
    return ()


# --- Claim kind (issue athenaeum#327) ---
#
# ``claim_kind`` classifies a claim by its EPISTEMIC shape, orthogonal to
# ``source_type`` (which classifies its ORIGIN channel). Classified once at
# intake by a cheap LLM pass (see :mod:`athenaeum.claim_kind`), stored in
# frontmatter, and round-tripped byte-for-byte by tier0 passthrough. It routes
# the resolver: an ``opinion`` pair is EVALUATIVE and must not be resolved by
# source precedence — two people may hold different, both-valid opinions — so
# the resolver keeps both with explicit attribution (``attribute_both``) rather
# than picking a precedence winner. The other kinds keep today's behavior.
#
#   ``fact``        — a verifiable state of the world ("develop tip is SHA abc").
#   ``observation`` — a first-hand report of something seen/measured.
#   ``opinion``     — an evaluative stance / preference / judgment. EVALUATIVE:
#                     different asserters may legitimately disagree.
#   ``decision``    — a timestamped choice with audit value (a pivot, a
#                     deprecation).
#   ``policy``      — a durable prescriptive rule ("always merge green PRs").
#   ``definition``  — a naming/terminology fixing ("X means Y").
#
# Absent / unrecognized => ``""`` (unclassified). Fail-open: an unclassified
# claim behaves exactly as it did before athenaeum#327 (the resolver's stance
# short-circuit does not fire; the LLM path decides as before).
CLAIM_KINDS: frozenset[str] = frozenset(
    {"fact", "observation", "opinion", "decision", "policy", "definition"}
)

# The evaluative claim kind — exported so the resolver / detector can branch on
# it without re-typing the literal. An ``opinion`` pair routes to
# ``attribute_both`` rather than a precedence winner.
OPINION_CLAIM_KIND = "opinion"


def parse_claim_kind(meta: dict[str, object] | None) -> str:
    """Return the frontmatter ``claim_kind:`` value, or ``""`` when absent/invalid.

    Fail-open (issue athenaeum#327): a missing key, a non-string value, or a value
    outside :data:`CLAIM_KINDS` all resolve to ``""`` (unclassified) rather
    than raising — an unrecognized claim_kind must never crash the compile,
    and an unclassified claim keeps pre-athenaeum#327 behavior. An out-of-vocabulary
    non-empty value logs a breadcrumb (typo / stale schema); absent/empty
    stays quiet (the ordinary legacy path).
    """
    if not meta:
        return ""
    raw = meta.get("claim_kind")
    if raw is None or raw == "":
        return ""
    if isinstance(raw, str) and raw in CLAIM_KINDS:
        return raw
    log.debug(
        "claim_kind: ignoring unrecognized value %r (not in %s); "
        "treating as unclassified",
        raw,
        sorted(CLAIM_KINDS),
    )
    return ""


def compare_asserters(
    a: dict[str, object] | None,
    b: dict[str, object] | None,
) -> str:
    """Compare two ``asserter:`` blocks → ``"same"`` / ``"different"`` / ``"unknown"``.

    Issue athenaeum#327. Uses :func:`asserter_identity_key` (the OIDC-durable
    ``(iss, sub)`` / Entra ``(iss, "entra", tid, oid)`` key) so an email
    change never re-classifies two claims as different asserters.

    - ``"unknown"`` when EITHER side yields an empty identity key (no durable
      identifier declared). This is the COMMON case for Claude-session
      intake, which carries no OIDC identity. The resolver's opinion path
      treats ``"unknown"`` as the keep-both fallback — it NEVER supersedes or
      deletes an opinion by precedence when identity is missing (athenaeum#327).
    - ``"same"`` when both keys are non-empty and equal.
    - ``"different"`` when both keys are non-empty and unequal.
    """
    key_a = asserter_identity_key(a)
    key_b = asserter_identity_key(b)
    if not key_a or not key_b:
        return "unknown"
    return "same" if key_a == key_b else "different"


def slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:60]  # cap length


# --- Frontmatter parsing ---

_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)

#: The libyaml-backed safe loader, when this PyYAML build ships one (issue
#: athenaeum#1194). :func:`parse_frontmatter` is the hottest function in the
#: codebase: every corpus-wide surface (``entity_schema``'s class scan,
#: ``athenaeum status``, compile) pays one YAML parse per page, and on the real
#: 23.1k-page corpus the pure-Python scanner alone accounted for 25.8s of a
#: 27.5s scan. ``CSafeLoader`` does the same work in 3.0s (8.5x), verified to
#: return byte-identical results for all 23,111 live frontmatter blocks.
#:
#: ``None`` when PyYAML was built without the libyaml extension (a pure-Python
#: wheel), in which case :func:`parse_frontmatter` behaves exactly as it always
#: has. Correctness never depends on libyaml being present.
_C_SAFE_LOADER = getattr(yaml, "CSafeLoader", None)


def _safe_load_frontmatter(block: str) -> Any:
    """``yaml.safe_load`` *block*, preferring libyaml (issue athenaeum#1194).

    The two loaders agree on every construct the real corpus contains (verified
    across all 23,111 live frontmatter blocks), but they are not identical at
    the edges, and the two directions are NOT symmetric in consequence:

    - A block the pure-Python scanner accepts and the C scanner rejects (a
      lone-surrogate escape such as ``"\\ud83d\\ude00"`` is the known case)
      would be a REGRESSION: :func:`parse_frontmatter` treats an unparseable
      block as "no frontmatter at all", so the page would silently vanish from
      every index. A ``YAMLError`` from the C loader is therefore never final
      — it re-parses with the pure-Python loader and only that verdict counts.
      No page that parsed before this change can stop parsing because of it.
    - A block the C scanner accepts and the pure-Python one rejects (a tab as
      the key/value separator, ``key:<TAB>value``) is admitted. Strictly more
      frontmatter is read, never less. Zero such pages exist in the real
      corpus; the alternative — re-rejecting them to preserve the old verdict
      exactly — would mean deliberately reproducing a scanner quirk.

    Raises :exc:`yaml.YAMLError` only when BOTH loaders reject the block, so
    every caller's existing ``except yaml.YAMLError`` handling is unchanged.
    """
    if _C_SAFE_LOADER is not None:
        try:
            return yaml.load(block, Loader=_C_SAFE_LOADER)
        except yaml.YAMLError:
            pass
    return yaml.safe_load(block)


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split YAML frontmatter from body. Returns ``(metadata, body)``.

    The metadata dict has string keys and arbitrary YAML-scalar/list/dict
    values (hence ``object``). Callers that need narrower types should
    validate the fields they depend on — the schema is intentionally
    open so non-core frontmatter keys round-trip cleanly.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = _safe_load_frontmatter(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    # Coerce identity fields at the YAML boundary. PyYAML loads bare
    # all-decimal hex uids (e.g. ``19052``) and unquoted numeric names
    # as ``int`` — downstream code (schema validation, index lookup,
    # filename rendering) expects ``str``. Fixing it here keeps the
    # on-disk dict consistent with the model and removes the need for
    # int-coercion shims further down.
    if isinstance(meta, dict):
        for _k in ("uid", "type", "name"):
            _v = meta.get(_k)
            if isinstance(_v, int) and not isinstance(_v, bool):
                meta[_k] = str(_v)
    body = text[m.end() :]
    return meta, body


def resolve_page_type(meta: dict[str, object] | None) -> str:
    """Return a page's entity class (``type:``), with a documented precedence.

    Issue athenaeum#964: ``type`` appears both top-level (``type: person``, the
    documented shape) and — on some pages — nested under ``metadata:``
    (``metadata: {type: person}``). Precedence: a non-empty top-level
    ``type`` wins; a non-empty ``metadata.type`` is used only when the
    top-level key is absent/empty; otherwise ``""``. This is the ONE place
    that precedence is decided — the type-filter code path in
    :mod:`athenaeum.search` and the entity-class resolver in
    :mod:`athenaeum.entity_schema` both call this rather than reading
    ``meta.get("type")`` directly, so a page authored either way is found by
    the same filter value.
    """
    if not meta:
        return ""
    top = meta.get("type")
    if isinstance(top, str) and top.strip():
        return top.strip()
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        inner = nested.get("type")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return ""


def parse_refines(meta: dict[str, object] | None) -> list[str]:
    """Coerce a frontmatter ``refines:`` value into a clean list of slugs.

    Accepts:
    - ``None`` / missing key → ``[]``.
    - ``list[str]`` of memory ``name:`` slugs (the documented shape).

    Raises:
        ValueError: when ``refines`` is present but not a list, or any
            entry is not a non-empty string. The frontmatter is a
            durable contract — a typo (``refines: name-x`` rendered as a
            scalar) should be loud, not silent.
    """
    if not meta:
        return []
    raw = meta.get("refines")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"refines must be a list of memory name slugs, got {type(raw).__name__}"
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"refines entries must be non-empty strings, got {entry!r}"
            )
        out.append(entry.strip())
    return out


def parse_supersedes(meta: dict[str, object] | None) -> list[dict[str, str]]:
    """Coerce a frontmatter ``supersedes:`` value into a list of records.

    Accepts:
    - ``None`` / missing key → ``[]``.
    - ``list[dict]`` of ``{name, as_of, reason}`` records. ``name`` is
      required and must be a non-empty string. ``as_of`` and ``reason``
      are optional; missing values are stored as empty strings so
      downstream consumers can rely on the keys existing.

    Raises:
        ValueError: when ``supersedes`` is not a list, an entry is not a
            mapping, or an entry lacks a non-empty ``name`` key.
    """
    if not meta:
        return []
    raw = meta.get("supersedes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"supersedes must be a list of records, got {type(raw).__name__}"
        )
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"supersedes entries must be mappings, got {type(entry).__name__}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("supersedes entries require a non-empty 'name' key")
        as_of = entry.get("as_of", "")
        reason = entry.get("reason", "")
        out.append(
            {
                "name": name.strip(),
                "as_of": str(as_of) if as_of is not None else "",
                "reason": str(reason) if reason is not None else "",
            }
        )
    return out


def parse_superseded_by(meta: Mapping[str, object] | None) -> str:
    """Return the frontmatter ``superseded_by`` pointer (winner name slug), or "".

    Set by the resolver's keep_a/keep_b enactment on the LOSING member to
    mark it as valid-then-replaced history. Non-empty => the member is
    inactive (excluded from recall + C3 compile) but preserved on disk.
    Tolerant: a non-string value coerces to its str form; missing => "".
    """
    if not meta:
        return ""
    raw = meta.get("superseded_by")
    if raw is None:
        return ""
    return str(raw).strip()


def parse_deprecated(meta: Mapping[str, object] | None) -> bool:
    """Return the truthy ``deprecated`` frontmatter flag (deprecate_both, athenaeum#191).

    Accepts a real bool, or a string variant (``true``/``1``/``yes``,
    case-insensitive); any other truthy value coerces via ``bool``.
    Missing / falsey => ``False``.
    """
    if not meta:
        return False
    dep = meta.get("deprecated")
    if isinstance(dep, bool):
        return dep
    if isinstance(dep, str):
        return dep.strip().lower() in ("true", "1", "yes")
    return bool(dep)


# --- Claim-level temporal validity (issue athenaeum#308, slice 1) ---
#
# ``valid_from:`` / ``valid_until:`` are optional ISO-8601 date frontmatter
# fields declaring the real-world window over which a claim is true. They sit
# BESIDE ``source:`` provenance (which answers *where/when ingested*, not *over
# what window valid*) — the bi-temporal split from Zep/Graphiti. Slice 1 makes
# the READER honor a ``valid_until`` set by a human or the resolver; slice 2
# (shipped) has the resolver auto-stamp the interval on a temporal supersession
# (``resolutions.enact_resolution`` — see ``docs/provenance-shape.md`` §8.4).
# Slice 3 (this change) threads the ``as_of`` parameter out to an operator-facing
# ``--as-of DATE`` recall view (``search.build_index`` / ``query`` + the CLI
# ``recall`` / ``rebuild-index`` commands) — a read-only historical rewind
# through the upper bound + athenaeum#191 tombstones. The lower bound (``valid_from``)
# stays ungated (it would collide with athenaeum#324's disjoint detector — see
# :func:`is_inactive_memory`). See ``docs/provenance-shape.md`` §8.


def _coerce_iso_date(value: object) -> date | None:
    """Coerce a frontmatter value to a :class:`datetime.date`, or ``None``.

    Fail-OPEN (issue athenaeum#308): a missing, empty, or UNPARSEABLE value returns
    ``None`` (treated as an open bound / no constraint), mirroring
    :func:`coerce_source_type`'s "must not crash the nightly compile" contract.
    Silently dropping a page on a bad date is worse than keeping it visible for
    a knowledge base, so a malformed date is logged and treated as absent.

    Accepts a real :class:`datetime.date` (YAML auto-parses a bare
    ``YYYY-MM-DD`` scalar into one) or an ISO-8601 ``YYYY-MM-DD`` string
    (e.g. a quoted date). Anything else => ``None`` + a debug breadcrumb.
    """
    if value is None or value == "":
        return None
    # ``datetime`` subclasses ``date`` — reduce to a bare date so a later
    # ``as_of`` (a ``date``) comparison never hits the date-vs-datetime
    # TypeError. Slice 1 is date-resolution; any time component is dropped.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            log.debug(
                "temporal-validity: unparseable ISO date %r; treating as open "
                "(fail-open, claim stays active)",
                value,
            )
            return None
    log.debug(
        "temporal-validity: non-date value %r for a validity bound; treating "
        "as open (fail-open, claim stays active)",
        value,
    )
    return None


def parse_valid_from(meta: Mapping[str, object] | None) -> date | None:
    """Return the frontmatter ``valid_from`` as a date, or ``None`` (open lower bound).

    Fail-open: missing / unparseable => ``None`` (valid since always). Parsed for
    round-trip and for athenaeum#324's disjoint-validity comparison
    (:func:`validity_windows_disjoint`), but deliberately NOT part of the
    ``is_inactive_memory`` active predicate — see that function for why the lower
    bound stays ungated.
    """
    if not meta:
        return None
    return _coerce_iso_date(meta.get("valid_from"))


def parse_valid_until(meta: Mapping[str, object] | None) -> date | None:
    """Return the frontmatter ``valid_until`` as a date, or ``None`` (open upper bound).

    Fail-open: missing / unparseable => ``None`` (open interval, still valid).
    ``valid_until`` is the LAST date the claim was valid (inclusive).
    """
    if not meta:
        return None
    return _coerce_iso_date(meta.get("valid_until"))


def validity_bound_str(meta: dict[str, object] | None, key: str) -> str:
    """Return a ``valid_from`` / ``valid_until`` bound NORMALIZED to ``YYYY-MM-DD``.

    Used at :class:`AutoMemoryFile` construction to store the bound so the
    dataclass predicate (:meth:`AutoMemoryFile.is_inactive`, which re-parses this
    string) reaches the SAME verdict the dict predicate
    (:func:`is_inactive_memory`) reaches parsing the raw ``meta`` value directly.

    Critically, the bound is run through the SAME :func:`_coerce_iso_date` the
    dict path uses and re-emitted as an ISO date string, rather than a naive
    ``str(raw)``. ``str(raw)`` diverged from the dict path on two reachable YAML
    types: a ``datetime`` (``2026-06-30 12:00:00`` → ``str`` is not
    ``fromisoformat``-parseable → fail-open, but the dict path ``.date()``
    honors it) and an ``int`` (``20260630`` → ``str`` parses as a bogus date,
    but the dict path returns ``None``). Normalizing here makes both predicates
    parse identical text and agree on ``date``/``datetime``/``int``/``str``/
    malformed inputs. A genuinely unparseable value normalizes to ``""``
    (fail-open — the claim stays active), matching the dict path's ``None``.
    """
    if not meta:
        return ""
    coerced = _coerce_iso_date(meta.get(key))
    return coerced.isoformat() if coerced is not None else ""


def valid_until_expired(
    meta: dict[str, object] | None, as_of: date | None = None
) -> bool:
    """True when ``valid_until`` is strictly in the past relative to ``as_of``.

    The single shared upper-bound predicate wired into BOTH
    :func:`is_inactive_memory` (dict path, recall) and
    :meth:`AutoMemoryFile.is_inactive` (dataclass path, C3 compile) so they stay
    in lockstep. ``as_of`` defaults to :func:`date.today` — pass an explicit
    date (slice 3's ``--as-of``) to rewind the view. Open upper bound (absent /
    malformed ``valid_until``) => ``False`` (still valid). Inclusive last-valid
    date: inactive iff ``as_of > valid_until``.
    """
    until = parse_valid_until(meta)
    if until is None:
        return False
    return (as_of or date.today()) > until


# --- Staleness axis: observed_at (issue athenaeum#424) ---
#
# ``observed_at:`` is a THIRD date-ish frontmatter field, distinct from both
# ``created``/``updated`` (write-time bookkeeping) and ``valid_from``/
# ``valid_until`` (the claim-VALIDITY window, athenaeum#308). A standing-state fact
# ("Acme has 40 employees") is true-WHEN-OBSERVED, not necessarily
# currently-true — ``observed_at`` records the former without asserting the
# latter. Data-model + validation only here (this issue); no reader treats
# an old ``observed_at`` as inactive — that policy call is out of scope
# (would belong with athenaeum#433's enforcement work, not this data-model issue).


def parse_observed_at(meta: dict[str, object] | None) -> date | None:
    """Return the frontmatter ``observed_at`` as a date, or ``None`` if absent/unparseable.

    Fail-open, same posture as :func:`parse_valid_from` /
    :func:`parse_valid_until`: a missing, empty, or unparseable value
    returns ``None`` rather than raising.

    Intentional, retained helper (issue athenaeum#539 settling of §4.4). It has no
    in-repo caller today but is the documented parser for the ``observed_at``
    staleness axis — see ``docs/memory-taxonomy.md`` §5, which names it — and
    is the read-side companion to ``parse_valid_from`` / ``parse_valid_until``.
    Kept as an intentional internal helper (not on the stable ``__all__``
    surface); not dead.
    """
    if not meta:
        return None
    return _coerce_iso_date(meta.get("observed_at"))


def is_inactive_memory(
    meta: dict[str, object] | None, as_of: date | None = None
) -> bool:
    """True when a memory file is marked inactive and must not surface as a live claim.

    Inactive == frontmatter declares ANY of: a non-empty ``superseded_by``
    (keep_a/keep_b loser, issue athenaeum#191), a truthy ``deprecated`` flag
    (deprecate_both, issue athenaeum#191), OR a ``valid_until`` in the past relative to
    ``as_of`` (claim-level temporal validity, issue athenaeum#308). Inactive members are
    preserved on disk for audit but are skipped by recall (search index) and by
    the C3 merge compile so their claims drop out of the live wiki.

    ``as_of`` defaults to today; the past-``valid_until`` disjunct filters
    expired claims by default. An absent or malformed ``valid_until`` is an open
    interval (fail-open — the claim stays active).

    Note the predicate keys on the UPPER bound only. The lower bound
    (``valid_from``) is intentionally NOT gated here: issue athenaeum#324's
    disjoint-validity detector short-circuit relies on a future-dated member
    (``valid_from`` after today) remaining active so a sequential/disjoint pair
    can form — a not-yet-valid claim is a recorded FUTURE state, not a hidden
    one. Slice 3's ``as_of`` rewind therefore views history through the upper
    bound and the athenaeum#191 tombstones (both honored below), which is where the
    supersession-as-interval value lives.
    """
    if not meta:
        return False
    if parse_superseded_by(meta):
        return True
    if parse_deprecated(meta):
        return True
    return valid_until_expired(meta, as_of)


def validity_windows_disjoint(
    meta_a: Mapping[str, object] | None, meta_b: Mapping[str, object] | None
) -> bool:
    """True when two claims' validity windows cannot overlap in time (issue athenaeum#324).

    Two claims are DISJOINT — sequential states of the world that cannot
    contradict (A true through March, B true from April) — iff one side has a
    CLOSED upper bound (``valid_until``) ending strictly before the other side's
    lower bound (``valid_from``) begins::

        a_until is not None and b_from is not None and a_until < b_from
        # OR the symmetric
        b_until is not None and a_from is not None and b_until < a_from

    ``valid_until`` is the INCLUSIVE last-valid date, so the comparison is strict
    ``<``: A ending 2026-03-31 and B starting 2026-04-01 → ``03-31 < 04-01`` →
    disjoint; A ending 2026-04-01 and B starting 2026-04-01 → they share that
    day → NOT disjoint.

    Each bound is parsed with the fail-open :func:`parse_valid_from` /
    :func:`parse_valid_until`: a missing OR malformed value coerces to ``None``
    (an open bound). Open bounds overlap by default, so a claim with no window —
    or a malformed one — is never disjoint from anything (detection proceeds).
    This is the fail-open posture the contradiction detector needs; no separate
    malformed handling is added here because ``parse_*`` already does it.
    """
    a_from = parse_valid_from(meta_a)
    a_until = parse_valid_until(meta_a)
    b_from = parse_valid_from(meta_b)
    b_until = parse_valid_until(meta_b)
    if a_until is not None and b_from is not None and a_until < b_from:
        return True
    if b_until is not None and a_from is not None and b_until < a_from:
        return True
    return False


# --- Decay bucket (issue athenaeum#904) ---
#
# ``bucket:`` is an optional frontmatter classification declaring how a memory
# DECAYS over time: ``daily`` (rapidly-overwritten status — the latest value
# matters, not the history of prior values), ``weekly``, or ``durable``
# (long-lived; the athenaeum#308 default posture — never auto-swept). It sits
# BESIDE ``valid_from``/``valid_until`` (this file, above) rather than
# replacing them: ``bucket`` says HOW a memory decays; ``valid_from``/
# ``valid_until`` say over WHAT WINDOW a specific claim is true. A daily-bucket
# page with no ``valid_until`` is not yet expired (the athenaeum#308 fail-open
# posture is unchanged) — currency ranking (``mcp_server._is_deprioritized_for_
# currency``) and the deterministic sweep (``athenaeum.decay_sweep``) both key
# on the EXISTING ``valid_until_expired`` predicate above, not a new one.
#
# Closed enum, unlike ``source_type``/``memory_class``/etc: those are
# read-side, fail-open axes with an established legacy corpus that must never
# crash the nightly compile on a stale value. ``bucket`` has no legacy corpus
# (this is its first release) and the athenaeum#904 design brief is explicit that an
# invalid value must be REJECTED at the boundary, not silently coerced — so
# :func:`coerce_bucket` (write-time) raises, while :func:`parse_bucket`
# (read-time, for a value already on disk — e.g. a hand-edited page) stays
# fail-open like every other reader in this module.
MEMORY_BUCKETS: frozenset[str] = frozenset({"daily", "weekly", "durable"})


def coerce_bucket(value: object) -> str:
    """Validate a ``bucket:`` value against :data:`MEMORY_BUCKETS` — a WRITE-time
    boundary function, not a fail-open reader (issue athenaeum#904).

    Every write-time entry point that lets a caller declare a bucket
    (``mcp_server.remember_write``, a shape-rule-emitted correction record in
    ``corrections.process_correction_record``) calls this so an invalid value
    is rejected right there rather than silently persisted. ``None`` / ``""``
    (unset) returns ``""`` — unset is a valid, ordinary state that must behave
    exactly as it did before this field existed. Anything else must be an
    exact member of :data:`MEMORY_BUCKETS` or this raises ``ValueError``.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str) and value in MEMORY_BUCKETS:
        return value
    raise ValueError(
        f"invalid bucket {value!r}; expected one of {sorted(MEMORY_BUCKETS)}, or unset"
    )


def parse_bucket(meta: Mapping[str, object] | None) -> str:
    """Return the frontmatter ``bucket:`` value, or ``""`` when absent/invalid.

    Read-side companion to :func:`coerce_bucket`: fail-open (the same posture
    every other reader in this module takes) so a corrupted or hand-edited
    on-disk value degrades to "no bucket" rather than raising and breaking
    discovery/compile/recall/the sweep. Rejection happens once, at write time.
    """
    if not meta:
        return ""
    raw = meta.get("bucket")
    if raw is None:
        return ""
    if isinstance(raw, str) and raw in MEMORY_BUCKETS:
        return raw
    log.debug("parse_bucket: invalid bucket %r; treating as absent", raw)
    return ""


# --- Audience / access scoping (issue athenaeum#312) ---
#
# Read-scoping for secondary agents/routines. The audience model is
# RBAC-compatible: ``audience:`` is a free-form list of opaque role/group
# identifiers the operator aligns with an external directory (AD group, app
# role, routine name). The pre-existing schema-validated ``access:`` field
# (open/internal/confidential/personal) is reused as the COARSE visibility
# default and composes with ``audience:``.
#
# Every helper here is FAIL-CLOSED: malformed / unparseable input yields the
# most restrictive interpretation (audience-∅, i.e. owner-only), never
# "public". The owner (no serve-time audience pin, ``caller_audience=None``)
# bypasses every check and sees everything, so single-user behavior is
# unbroken.

# The ``access:`` level that maps to "world-readable by every audience".
_ACCESS_PUBLIC = "open"

# Internal sentinel token that marks a page public in the serialized index
# audience string. It is DELIBERATELY distinct from the ``open`` access word so
# "public" is decided ONLY by ``access == open`` at serialization time, never
# by an ``audience:`` role literally named ``open``. ``parse_audience`` also
# refuses this token (and the access-level words) as role ids, so no role can
# ever produce this marker — closing the collision at the source. Exported so
# the backends test the same marker instead of hardcoding a literal.
AUDIENCE_PUBLIC_TOKEN = "__access_open__"

# Words that are access levels / the internal public sentinel, NOT audience
# roles. Dropped from any ``audience:`` list so a mislabeled entry can never be
# read as a role grant (and can never forge the public marker).
_RESERVED_AUDIENCE_ROLES: frozenset[str] = frozenset(
    {"open", "internal", "confidential", "personal", AUDIENCE_PUBLIC_TOKEN}
)


def parse_access(meta: dict[str, object] | None) -> str:
    """Return the normalized ``access:`` level, or ``""`` when absent/malformed.

    Case-folded and whitespace-trimmed. A non-string value (a typo'd list or
    mapping) returns ``""`` — which, being neither ``open`` nor a granting
    ``audience:``, fails closed to owner-only for a restricted caller.
    """
    if not meta:
        return ""
    raw = meta.get("access")
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def parse_audience(meta: dict[str, object] | None) -> list[str]:
    """Coerce a frontmatter ``audience:`` value into a clean list of role ids.

    The single normalization point for the read-scoping control (issue athenaeum#312),
    sibling to :func:`parse_refines`/:func:`parse_supersedes`. Accepts:

    - ``None`` / missing key → ``[]`` (no explicit grant).
    - ``list[str]`` of non-empty role/group identifiers → case-folded,
      whitespace-trimmed list.

    Unlike :func:`parse_refines`, this **degrades to withhold rather than
    raise** on malformed input: a scalar ``audience:`` value, a list holding a
    non-string / empty entry, or any other bad shape returns ``[]`` (audience-∅
    → withheld from a restricted caller). This is the fail-closed posture the
    security boundary requires — one bad page must not crash a scheduled recall,
    and a malformed tag must never be read as "public". A debug breadcrumb is
    logged so the operator can find the offending page.

    Reserved words — the access-level names (``open`` / ``internal`` /
    ``confidential`` / ``personal``) and the internal public sentinel — are NOT
    valid role ids and are dropped: ``audience: [open]`` grants no role (public
    is decided only by ``access: open``), so it cannot be mistaken for a
    world-readable grant or forge the index's public marker.
    """
    if not meta:
        return []
    raw = meta.get("audience")
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.debug(
            "audience must be a list of role ids, got %s; withholding",
            type(raw).__name__,
        )
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            log.debug(
                "audience entries must be non-empty strings, got %r; withholding", entry
            )
            return []
        role = entry.strip().lower()
        if role in _RESERVED_AUDIENCE_ROLES:
            log.debug(
                "audience role %r is a reserved access-level word, not a role; "
                "dropping",
                role,
            )
            continue
        out.append(role)
    return out


def effective_audience(meta: dict[str, object] | None) -> tuple[set[str], bool]:
    """Return ``(granted_roles, is_public)`` for a page's frontmatter.

    - ``is_public`` is True iff ``access: open`` (world-readable).
    - ``granted_roles`` is the set of explicit ``audience:`` role ids. The
      coarse ``access:`` levels ``internal``/``confidential``/``personal``
      contribute NO roles (owner-only) unless an explicit ``audience:`` grant
      is present — the composition rule from the design.

    A page with neither ``access: open`` nor an ``audience:`` grant has
    ``(set(), False)`` — audience-∅, withheld from every restricted caller.
    """
    public = parse_access(meta) == _ACCESS_PUBLIC
    roles = set(parse_audience(meta))
    return roles, public


def is_page_authorized(
    meta: dict[str, object] | None,
    caller_audience: set[str] | None,
) -> bool:
    """True iff a caller pinned to ``caller_audience`` may read this page.

    ``caller_audience=None`` is the owner / default caller: authorized for
    EVERYTHING (untagged included). A non-None set is a restricted caller:
    authorized iff the page is public (``access: open``) OR the caller holds at
    least one role in the page's granted set. Fail-closed: an untagged or
    malformed page has an empty granted set and is withheld from a restricted
    caller.
    """
    if caller_audience is None:
        return True
    roles, public = effective_audience(meta)
    if public:
        return True
    return bool(caller_audience & roles)


def is_page_authorized_at(
    source: str | Path,
    caller_audience: set[str] | None,
    *,
    base: str | Path | None = None,
) -> bool:
    """Fail-closed authorize a caller against a page identified by PATH (athenaeum#538).

    The path-resolving counterpart to :func:`is_page_authorized`, used by the
    MCP pending-decision list tools (``list_pending_merges`` /
    ``list_pending_decisions`` / ``list_pending_questions``) to apply the exact
    same read predicate ``recall`` applies — so no tool returns page content a
    restricted caller could not get from ``recall``.

    ``caller_audience=None`` is the owner: authorized for everything, and the
    file is never even read. A non-None (restricted) caller is authorized iff
    the page at ``source`` is readable AND :func:`is_page_authorized` passes on
    its frontmatter. Fail-closed on every failure mode a restricted caller must
    not be able to route around: a missing/unreadable/mis-encoded file, or one
    with no parseable frontmatter, is WITHHELD (returns ``False``) — an attacker
    cannot widen scope by pointing at a path we cannot authorize.

    ``base`` (optional) is joined to a relative ``source`` before reading; an
    absolute ``source`` is used as-is (matching ``decisions.source_info``).
    """
    if caller_audience is None:
        return True
    path = Path(source).expanduser()
    if base is not None and not path.is_absolute():
        path = Path(base).expanduser() / path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False  # fail closed — unreadable page is never authorized
    meta, _ = parse_frontmatter(text)
    return is_page_authorized(meta if isinstance(meta, dict) else None, caller_audience)


def all_sources_authorized(
    sources: "Iterable[str | Path]",
    caller_audience: set[str] | None,
    *,
    base: str | Path | None = None,
) -> bool:
    """True iff a restricted caller may read EVERY page in ``sources`` (athenaeum#538).

    The fail-closed predicate the MCP pending-decision list tools apply to
    withhold any pending item whose underlying source pages a restricted caller
    could not read via ``recall``. Owner (``caller_audience=None``) is always
    authorized. For a restricted caller an EMPTY ``sources`` returns ``False``
    — there is nothing to authorize against, so the item is withheld rather
    than shown on a vacuous ``all([])``.
    """
    if caller_audience is None:
        return True
    sources = list(sources)
    if not sources:
        return False
    return all(is_page_authorized_at(s, caller_audience, base=base) for s in sources)


def delimited_index_string(values: Iterable[str]) -> str:
    """Serialize a set/list of tokens as a ``|``-delimited, anchored string.

    Issue athenaeum#964 (AC amendment 2): the ONE shared list-valued-field encoding
    every search backend materializes through, factored out of
    :func:`audience_index_string` (its original, sole caller) rather than each
    caller inventing its own delimiter convention. Anchored on both ends so a
    substring/``LIKE`` test can never cross a token boundary (``|ops|`` never
    matches ``|opsadmin|``) — the same property :func:`audience_index_string`
    already relied on. Empty input returns ``"|"`` (the empty-sentinel shape),
    not ``""``, so a caller can always safely wrap a probe token in ``|...|``.
    """
    parts = sorted({v for v in values if v})
    if not parts:
        return "|"
    return "|" + "|".join(parts) + "|"


def audience_index_string(meta: dict[str, object] | None) -> str:
    """Serialize a page's effective audience for storage in the search index.

    Returns a delimiter-anchored string so a substring/``LIKE`` test can never
    cross a role boundary (``|ops|`` never matches ``|opsadmin|``):

    - public page → ``"|__access_open__|"`` (may also include granted roles).
      The public marker is the internal sentinel, never the ``open`` word, so a
      role can't forge it.
    - roles ``{a, b}`` → ``"|a|b|"`` (sorted for deterministic rebuilds).
    - audience-∅ → ``"|"`` (empty sentinel).

    Stored UNINDEXED in FTS5 (out of the BM25 term space) and as chromadb
    metadata so Layer B can filter INSIDE each backend query.
    """
    roles, public = effective_audience(meta)
    parts: list[str] = [AUDIENCE_PUBLIC_TOKEN] if public else []
    return delimited_index_string(parts + sorted(roles))


def audience_string_authorized(
    audience_str: str,
    caller_audience: set[str] | None,
) -> bool:
    """Authorize a caller against a stored :func:`audience_index_string`.

    The string-based counterpart to :func:`is_page_authorized`, used by the
    vector backend's Python post-filter (chromadb metadata is scalar-only, so
    the audience is stored as this delimited string and filtered here).
    ``caller_audience=None`` (owner) is always authorized.
    """
    if caller_audience is None:
        return True
    if f"|{AUDIENCE_PUBLIC_TOKEN}|" in audience_str:
        return True
    return any(f"|{role}|" in audience_str for role in caller_audience)


def render_frontmatter(meta: dict[str, object]) -> str:
    """Render a dict as a YAML frontmatter block.

    Contract: key order preserved (``sort_keys=False``) for tier0
    byte-for-byte round-trip. Do not change without updating
    ``test_render_frontmatter_preserves_key_order``.
    """
    dumped = yaml.dump(
        meta, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    return f"---\n{dumped}---\n"


# --- Data classes ---


class RawFileTooLargeError(Exception):
    """Raised by :attr:`RawFile.content` when the file exceeds its per-file
    byte bound (issue athenaeum#898).

    Checked via ``stat()`` BEFORE any bytes are read into memory, so a
    pathological multi-megabyte artifact costs one syscall to reject, not a
    full read plus however many LLM calls it would otherwise have driven —
    the concrete failure this bound exists to prevent (one 9.7MB dry-run
    artifact accounted for 93% of timed entity-phase LLM calls for roughly
    three months before it was retired). ``size``/``limit`` are both in
    bytes.
    """

    def __init__(self, ref: str, size: int, limit: int) -> None:
        self.ref = ref
        self.size = size
        self.limit = limit
        super().__init__(
            f"{ref}: {size:,} bytes exceeds the {limit:,}-byte per-file limit"
        )


class RawFileOverBudgetError(Exception):
    """Raised by :func:`athenaeum.tiers.tier3_derive_actions` when a raw
    file's LLM-call count or wall-clock spend crosses its per-file bound
    (issue athenaeum#898, revised athenaeum#994).

    Checked INCREMENTALLY, after each entity action in the file's action
    list completes — not once, post-hoc, after the whole file's actions have
    all run. This is what makes the bound pre-emptive rather than post-hoc:
    a file whose Nth action pushes it over the bound never starts action
    N+1, so the file's *remaining* LLM spend is the one thing actually
    prevented (the pre-athenaeum#994 shape checked once at the end, so a file
    already destined to trip the bound still paid for every one of its
    actions before the check ever ran).

    ``new_entities`` / ``pending_updates`` / ``updated_uids`` / ``escalations``
    (athenaeum#994) carry every action that completed BEFORE the bound
    tripped, in exactly the shape :func:`athenaeum.tiers.tier3_derive_actions`
    itself returns them on a clean run. The catching caller
    (:func:`athenaeum.librarian.process_one`) writes these to disk — durable
    partial progress — before propagating the error, rather than discarding
    them. This supersedes the athenaeum#898-era contract (preserved verbatim
    in git history) under which NOTHING from an over-bound file was ever
    written; that all-or-nothing shape is what caused the same file to be
    redone in full, at full LLM cost, on consecutive nights (the athenaeum#994
    diagnosis). The un-started remainder of the file's actions is still
    discarded — only actions that had ALREADY completed land — and the raw
    file itself is left on disk exactly as before, so the entity loop's
    existing quarantine/backoff ledger (unchanged by athenaeum#994) still
    accumulates a consecutive-violation count and eventually quarantines a
    file that keeps tripping the bound, rather than reprocessing its
    unstarted remainder identically forever. ``bound`` is ``"llm_calls"`` or
    ``"wall_clock"``.
    """

    def __init__(
        self,
        ref: str,
        *,
        bound: str,
        detail: str,
        new_entities: list[WikiEntity] | None = None,
        pending_updates: list[tuple[Path, str]] | None = None,
        updated_uids: list[str] | None = None,
        escalations: list[EscalationItem] | None = None,
    ) -> None:
        self.ref = ref
        self.bound = bound
        self.detail = detail
        # Issue athenaeum#994: partial durable progress — everything derived
        # (LLM calls already made) before the bound tripped, not yet
        # written by tier3_derive_actions itself (it never writes) but
        # ready for the caller to write verbatim.
        self.new_entities: list[WikiEntity] = new_entities if new_entities is not None else []
        self.pending_updates: list[tuple[Path, str]] = (
            pending_updates if pending_updates is not None else []
        )
        self.updated_uids: list[str] = updated_uids if updated_uids is not None else []
        self.escalations: list[EscalationItem] = (
            escalations if escalations is not None else []
        )
        super().__init__(f"{ref}: over its {bound} bound — {detail}")


@dataclass
class RawFile:
    """A raw intake file from raw/{source}/{timestamp}-{uuid8}.md."""

    path: Path
    source: str
    timestamp: str
    uuid8: str
    _content: str | None = field(default=None, repr=False)
    # Issue athenaeum#898: per-file byte bound, enforced by `content` below.
    # `None` (the default) preserves pre-athenaeum#898 behaviour verbatim —
    # unbounded reads — for every caller that constructs a `RawFile` directly
    # (most of the test suite, and any future in-process caller) rather than
    # through `athenaeum.intake.discover_raw_files`, which is the one place
    # that resolves and sets this from `librarian.raw_file_max_bytes`
    # (:func:`athenaeum.config.resolve_raw_file_max_bytes`).
    max_content_bytes: int | None = None

    @property
    def content(self) -> str:
        if self._content is None:
            if self.max_content_bytes is not None:
                try:
                    size = self.path.stat().st_size
                except OSError:
                    size = None
                # A stat() failure (e.g. the file vanished between discovery
                # and read) falls through to the read below, which raises its
                # own OSError — fail-open on the BOUND check specifically,
                # never silent about a missing/unreadable file.
                if size is not None and size > self.max_content_bytes:
                    raise RawFileTooLargeError(self.ref, size, self.max_content_bytes)
            self._content = self.path.read_text(encoding="utf-8")
        return self._content

    @property
    def ref(self) -> str:
        """Short reference for footnotes."""
        return f"{self.source}/{self.path.name}"


@dataclass
class AutoMemoryFile:
    """A raw intake file from ``raw/auto-memory/<scope>/<prefix>_<slug>.md``.

    Parallel sibling to :class:`RawFile` — auto-memory uses a different
    naming convention (``feedback_*.md``, ``project_*.md``, ``reference_*.md``,
    ``user_*.md``, ``Recall_*.md``) and a different frontmatter schema
    (``type`` / ``originSessionId`` / ``originTurn`` / ``sources`` instead
    of the entity schema's ``uid`` / ``name``).

    ``origin_scope`` is the scope directory name verbatim — the full
    path-hash identifier (e.g. ``-Users-alice-Code-projectx``) or
    the literal ``_unscoped``. Preserving this on the record is C2/C3's
    routing key; the compile step downstream will carry it through to the
    wiki entry metadata.
    """

    path: Path
    origin_scope: str
    memory_type: str  # feedback|project|reference|user|recall
    name: str = ""
    description: str = ""
    origin_session_id: str | None = None
    origin_turn: int | None = None
    sources: list[str] = field(default_factory=list)
    # Lane 1 / athenaeum#167: declared relationships to other memories. Both
    # default to empty list. ``refines`` lists ``name:`` slugs of
    # memories this one narrows (general + exception — BOTH stay
    # active). ``supersedes`` lists ``{name, as_of, reason}`` records
    # declaring this memory replaces another (the superseded memory
    # stays for audit but is no longer active guidance). Matching is
    # by ``name:`` slug, not path.
    refines: list[str] = field(default_factory=list)
    supersedes: list[dict[str, str]] = field(default_factory=list)
    # Issue athenaeum#191: non-destructive inactive markers written by the resolver's
    # keep_a/keep_b (superseded_by = winner name) and deprecate_both
    # (deprecated = True) enactment. An inactive member is preserved on disk
    # for audit but excluded from recall + the C3 compile so it does not
    # resurface as a live claim.
    superseded_by: str = ""
    deprecated: bool = False
    # Issue athenaeum#260 (slice A of athenaeum#259): origin-traced provenance. ``source_type``
    # is one of :data:`SOURCE_TYPES` (default ``inferred`` so memories written
    # before the citation policy still parse). ``source_ref`` is the ULTIMATE
    # reference — session-id+turn, URL, or document path — NEVER this file's
    # own ``raw/auto-memory/...`` name. Empty when unestablished.
    source_type: str = DEFAULT_SOURCE_TYPE
    source_ref: str = ""
    # Issue athenaeum#326: channel-split provenance annotations. All three are
    # optional and empty by default so legacy auto-memory files round-trip
    # unchanged. ``model`` is the model-id for AI-attributed claims
    # (``agent-observed`` / ``inferred`` / ``model-prior``). ``on_behalf_of``
    # is the responsible human principal (W3C PROV
    # ``actedOnBehalfOf`` — model asserted, human accountable).
    # ``asserter`` is the IdP-compatible identity block for the human
    # who made a ``user-stated`` claim (or ``{}`` when absent). Identity
    # keys on (``iss``, ``sub``) via :func:`asserter_identity_key`.
    model: str = ""
    on_behalf_of: str = ""
    asserter: dict[str, object] = field(default_factory=dict)
    # Issue athenaeum#327: epistemic claim kind, classified once at intake by a cheap
    # LLM pass (:mod:`athenaeum.claim_kind`). One of :data:`CLAIM_KINDS` or ""
    # (unclassified). Routes the resolver's opinion-attribution short-circuit:
    # an ``opinion`` pair keeps BOTH sides with explicit attribution rather
    # than being resolved by source precedence. Empty by default so legacy /
    # unclassified members round-trip unchanged and keep pre-athenaeum#327 behavior.
    claim_kind: str = ""
    # Issue athenaeum#308 (slice 1): claim-level temporal validity. Both are the RAW
    # frontmatter string form (``YYYY-MM-DD`` or "" when absent) so the
    # dataclass predicate re-parses to the SAME date as the dict predicate
    # sees — keeping :meth:`is_inactive` in lockstep with
    # :func:`is_inactive_memory`. ``valid_until`` is the last date the claim was
    # valid (inclusive); absent => open interval (still valid).
    valid_from: str = ""
    valid_until: str = ""
    # Issue athenaeum#904: optional decay classification — one of
    # :data:`MEMORY_BUCKETS`, or ``""`` (unset, behaves exactly as before this
    # field existed). Read via :func:`parse_bucket` (fail-open), never
    # :func:`coerce_bucket` (that is the write-time boundary, not a discovery
    # read).
    bucket: str = ""
    _content: str | None = field(default=None, repr=False)

    @property
    def content(self) -> str:
        if self._content is None:
            self._content = self.path.read_text(encoding="utf-8")
        return self._content

    @property
    def ref(self) -> str:
        """Short reference for footnotes — scope/filename."""
        return f"{self.origin_scope}/{self.path.name}"

    def is_inactive(self, as_of: date | None = None) -> bool:
        """True when this member is inactive (athenaeum#191 marker OR expired
        athenaeum#308 validity).

        Mirrors :func:`is_inactive_memory` on the dataclass path (C3 compile):
        inactive iff a ``superseded_by`` pointer or ``deprecated`` flag is set,
        OR ``valid_until`` is in the past relative to ``as_of`` (default today).
        Delegates the temporal check to the shared :func:`valid_until_expired`
        helper — fed the raw ``valid_until`` string — so the two predicates
        cannot drift. An absent/malformed ``valid_until`` is an open interval
        (fail-open, stays active). The lower bound (``valid_from``) is
        intentionally not gated — see :func:`is_inactive_memory`.
        """
        if self.superseded_by or self.deprecated:
            return True
        return valid_until_expired({"valid_until": self.valid_until}, as_of)

    def supersedes_names(self) -> list[str]:
        """Return just the ``name`` keys from :attr:`supersedes` records."""
        out: list[str] = []
        for rec in self.supersedes:
            if isinstance(rec, dict):
                n = rec.get("name")
                if isinstance(n, str) and n:
                    out.append(n)
        return out


def _recorded_time_now() -> datetime:
    """Return the current instant for ``WikiEntity.recorded_at`` stamping.

    Issue athenaeum#1064: isolated to this one seam (rather than inlining
    ``datetime.now(timezone.utc)`` at the call site below) so a test that
    constructs entities across two passes needing the SAME ``recorded_at``
    (e.g. the batch/sync equivalence tests in ``test_batch_mode.py``) can
    freeze it via ``monkeypatch.setattr(models, "_recorded_time_now", ...)``
    instead of racing the real wall clock across a second boundary. Mirrors
    :func:`athenaeum.dimensions.stamp_recorded_time`'s injectable-``now``
    contract without importing it — see the cycle note in
    ``WikiEntity.__post_init__`` below for why that import is not taken.
    """
    return datetime.now(timezone.utc)


@dataclass
class WikiEntity:
    """An entity page in wiki/ using the full entity template format."""

    uid: str
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    access: str = "internal"
    tags: list[str] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    body: str = ""
    # Per-claim provenance (issue athenaeum#90 / athenaeum#95). Optional so old wikis
    # without provenance still round-trip cleanly. ``source`` is the
    # wiki-level default; ``field_sources`` overrides per field.
    source: str | dict | None = None
    # ``field_sources`` per-field value is ``str``/``dict`` (legacy)
    # OR ``list[dict]`` of ``{"value", "source"}`` records (per-value
    # attribution for list fields, issue athenaeum#102).
    field_sources: dict[str, str | dict | list] | None = None
    # Issue athenaeum#260: origin-traced provenance threaded onto the entity. Both
    # optional so legacy entities round-trip unchanged. ``source_type`` is one
    # of :data:`SOURCE_TYPES`; ``source_ref`` is the ultimate reference and is
    # never the raw ``auto-memory/...`` filename. Rendered into frontmatter
    # only when set.
    source_type: str | None = None
    source_ref: str | None = None
    # Issue athenaeum#326: channel-split provenance annotations. All three are
    # optional and rendered into frontmatter only when set — legacy
    # entities without them round-trip byte-for-byte unchanged. See the
    # matching fields on :class:`AutoMemoryFile` for semantics.
    model: str | None = None
    on_behalf_of: str | None = None
    asserter: dict[str, object] | None = None
    # Issue athenaeum#996: the memory-taxonomy axis (athenaeum#424) reaches the WRITE
    # model. It existed only on the read/validation model (``schemas.WikiBase``)
    # until now, so no code path could emit it and coverage could only ever be
    # zero — a backfill without this field would decay at the new-page rate.
    # Left ``None`` by a caller it is DERIVED from ``type`` in ``__post_init__``
    # via the adopted rule map, so every newly created page lands classed
    # without any call site having to know the taxonomy. An explicit value
    # always wins; an unmapped ``type`` stays ``None`` and renders no key.
    memory_class: str | None = None
    # Issue athenaeum#714 (dimension registry): four NEW write-side coordinate
    # fields, none of which collide with any existing frontmatter key (see
    # ``athenaeum/dimensions.py``'s module docstring for the collision
    # analysis that ruled out reusing the existing ``scope:`` key — and for
    # why this field is ``provenance_scope``, NOT ``origin_scope``: that
    # identifier is already taken, with an entirely different meaning, by
    # :class:`AutoMemoryFile`'s ``origin_scope`` field above (the raw-intake
    # scope-directory identifier, issue athenaeum#167 — NEVER stored in
    # frontmatter per ``resolutions.py``'s documented invariant). Reusing it
    # here would silently contradict that invariant for readers grepping the
    # name.
    # ``recorded_at`` is stamped unconditionally by ``__post_init__`` below
    # (never writer-supplied); the other three are left exactly as the
    # caller set them — a missing coordinate is not an error, it lands per
    # the dimension's null semantics.
    recorded_at: str | None = None
    # PROVENANCE (where/what context wrote this claim) — never auto-copied
    # into ``claimed_scope`` below. See ``dimensions.py``'s write-discipline
    # section and ``tests/test_dimensions.py::test_provenance_scope_never_populates_claimed_scope``.
    provenance_scope: str | None = None
    # The ASSERTED "scope" kernel-dimension coordinate (where the claim
    # APPLIES) — must be explicit (writer, classifier proposal, or queue
    # answer); never derived from ``provenance_scope``.
    claimed_scope: str | None = None
    # The "subject" kernel-dimension coordinate (identity kind).
    subject: str | None = None

    def __post_init__(self) -> None:
        # Lazy import: only this one call needs the vocabulary, and
        # ``memory_class`` is a leaf module (see its docstring for why the
        # constants do not live in ``schemas``).
        from athenaeum.memory_class import MEMORY_CLASSES, memory_class_for_type

        if self.memory_class is None or self.memory_class == "":
            self.memory_class = memory_class_for_type(self.type)
        elif self.memory_class not in MEMORY_CLASSES:
            # Warn-and-keep, matching how ``schemas.WikiBase`` treats an
            # unrecognized value (and the athenaeum#93 ``KNOWN_TYPES`` precedent it
            # cites): round-trip fidelity beats silently dropping a value the
            # operator deliberately wrote. The read path warns about it too.
            warnings.warn(
                f"unknown memory_class {self.memory_class!r} "
                f"(recognized: {sorted(MEMORY_CLASSES)})",
                UserWarning,
                stacklevel=2,
            )

        # Issue athenaeum#714: recorded-time kernel dimension — system transaction
        # time, stamped ONCE per construction call where ``recorded_at`` is
        # absent (mirroring ``created``'s own stamp-once-if-absent
        # convention above, NOT ``updated``'s always-refresh one). A caller
        # that reconstructs an already-recorded entity from an on-disk page
        # (an edit/merge round-trip) and threads the existing ``recorded_at``
        # back into the constructor preserves it rather than bumping it —
        # "monotonic per corpus" describes NEW claims entering the corpus,
        # not every subsequent touch of an existing one. The raw-intake path
        # (``intake.tier0_passthrough``) never forwards a raw frontmatter
        # ``recorded_at:`` into this constructor, so a writer cannot get a
        # self-supplied value accepted for a genuinely new page either way —
        # "never writer-supplied" holds because no caller threads untrusted
        # input into this field, not because this method rejects one.
        #
        # Inlined rather than calling ``athenaeum.dimensions.stamp_recorded_time``
        # (which duplicates this one-liner and is the canonical public
        # helper other callers should use): ``models`` is an L1 hub that
        # ``dimensions`` (L1/L2) already imports at module level for the
        # temporal parsers, so a ``models -> dimensions`` back-edge here
        # would close a real import cycle (caught by
        # ``tests/test_import_graph_acyclic.py``'s whole-graph SCC guard,
        # which counts function-local imports too, not just top-level ones).
        if not self.recorded_at:
            self.recorded_at = _recorded_time_now().isoformat(timespec="seconds")

    @property
    def filename(self) -> str:
        return f"{self.uid}-{slugify(self.name)}.md"

    def render(self) -> str:
        """Render to full markdown with YAML frontmatter."""
        meta: dict = {
            "uid": self.uid,
            "type": self.type,
            "name": self.name,
        }
        # Issue athenaeum#996: emitted immediately after ``type`` — the two type
        # axes read together (docs/memory-taxonomy.md §2). Absent when the
        # rule map does not decide, never defaulted to a class.
        if self.memory_class:
            meta["memory_class"] = self.memory_class
        if self.aliases:
            meta["aliases"] = self.aliases
        meta["access"] = self.access
        if self.tags:
            meta["tags"] = self.tags
        if self.related:
            meta["related"] = self.related
        if self.created:
            meta["created"] = self.created
        if self.updated:
            meta["updated"] = self.updated
        if self.source is not None:
            meta["source"] = self.source
        if self.field_sources:
            meta["field_sources"] = self.field_sources
        if self.source_type is not None:
            meta["source_type"] = self.source_type
        if self.source_ref is not None:
            meta["source_ref"] = self.source_ref
        if self.model is not None:
            meta["model"] = self.model
        if self.on_behalf_of is not None:
            meta["on_behalf_of"] = self.on_behalf_of
        if self.asserter is not None:
            meta["asserter"] = self.asserter
        # Issue athenaeum#714: dimension-registry coordinates. Rendered only when
        # set — legacy entities without them round-trip byte-for-byte
        # unchanged, matching every other optional-field convention above.
        if self.recorded_at is not None:
            meta["recorded_at"] = self.recorded_at
        if self.provenance_scope is not None:
            meta["provenance_scope"] = self.provenance_scope
        if self.claimed_scope is not None:
            meta["claimed_scope"] = self.claimed_scope
        if self.subject is not None:
            meta["subject"] = self.subject
        return render_frontmatter(meta) + "\n" + self.body


@dataclass
class ClassifiedEntity:
    """Output of Tier 2 classification."""

    name: str
    entity_type: str
    tags: list[str]
    access: str
    is_new: bool
    existing_uid: str | None = None
    observations: str = ""


@dataclass
class EntityAction:
    """A create or update action for Tier 3."""

    kind: Literal["create", "update"]
    name: str
    entity_type: str
    tags: list[str]
    access: str
    existing_uid: str | None
    observations: str


@dataclass
class EscalationItem:
    """An item to escalate to _pending_questions.md."""

    raw_ref: str
    entity_name: str
    conflict_type: str  # "principled" | "ambiguous" | "classification_failed"
    description: str
    # Optional resolver proposal threaded through from
    # :func:`athenaeum.resolutions.propose_resolution`. When present and
    # confidence >= the configured threshold, :func:`tier4_escalate`
    # auto-applies the resolution to the rendered block. Typed as
    # ``Any`` to avoid a circular import (resolutions.py imports
    # AutoMemoryFile from this module). The runtime type is
    # ``athenaeum.resolutions.ResolutionProposal | None``.
    proposal: Any = None
    # Absolute paths of the flagged member files in resolver ``a``/``b``
    # order (``members[0]`` is side ``a``, ``members[1]`` is side ``b``).
    # Populated by :func:`athenaeum.merge._emit_escalation` so the
    # enactment lane (athenaeum#166 follow-up) can DELETE the target member when a
    # high-confidence ``forget_*`` / ``correct_*`` verdict auto-applies.
    # Empty for non-source-attributed escalations (the enactment lane then
    # no-ops). Stored as strings to keep the dataclass trivially copyable.
    members: list[str] = field(default_factory=list)


# Per-model rate table (issue athenaeum#247). Maps a model-id PREFIX to its
# (input, output) price in USD per million tokens. Matched by LONGEST
# prefix so dated ids (``claude-haiku-4-5-20251001``) resolve to the
# right family. The SOURCE OF TRUTH for model IDs and pricing is the
# ``claude-api`` skill's model catalog — NOT a training prior (see hestia#1055,
# where four issues were falsely blocked by a recalled catalog). Cross-checked
# against Anthropic public pricing (https://www.anthropic.com/pricing).
# Verified 2026-08-01 for the Claude 5 family additions below.
#
# PERIODIC REVIEW: these are hard-coded Anthropic public list prices. They do
# NOT auto-update — Anthropic price changes (new model families, rate cuts,
# tier changes) require a manual edit HERE, after re-reading the ``claude-api``
# skill. This constant is the single update site for model pricing IN CODE;
# nothing else in the codebase hard-codes per-MTok rates.
#
# Issue athenaeum#783: at RUNTIME this table is no longer consulted directly by
# :func:`_rates_for_model` — it is instead the seed for two things: (1) the
# process-wide default installed into :data:`_ACTIVE_MODEL_RATES_USD_PER_MTOK`
# below (so any caller that never loads ``athenaeum.yaml`` — every existing
# unit test, a library caller instantiating :class:`TokenUsage` directly —
# sees byte-identical pricing to before athenaeum#783), and (2) the ``pricing:``
# section :func:`athenaeum.config.write_default_config` serializes into a
# fresh install's ``athenaeum.yaml`` (via :func:`default_model_rates`) so
# ``athenaeum init`` ships priced correctly out of the box. Editing this dict
# is still the one update site — the yaml a maintainer sees at ``init`` time
# is GENERATED from it, never hand-duplicated.
_MODEL_RATES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Claude 5 family (issue athenaeum#577, precondition B1 of epic athenaeum#516). Recorded
    # BEFORE any DEFAULT_*_MODEL moves to it (athenaeum#580) so a bump can never fall
    # through to the blended fallback and silently under-report spend. Sonnet 5
    # carries an introductory $2/$10 through 2026-08-31, but this table has NO
    # time dimension — a prefix-keyed rate cannot expire — so the STANDARD rate
    # is recorded (Occam decision 2026-07-31): encoding the promo would go
    # silently wrong on 2026-09-01, and standard errs toward over-reporting
    # spend, the safe direction for a financial consumer.
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    # claude-fable-5 / claude-mythos-5 (issue athenaeum#777): the "maintain the two
    # tables together" comment on _SAMPLING_PARAMS_REJECTED_PREFIXES below was
    # violated — that table has listed claude-fable-5 since athenaeum#577, but this
    # one never did, so any run tagged claude-fable-5 or claude-mythos-5 fell
    # through to the blended fallback and under-reported spend 6.67x ($1.50/$7.50
    # blended vs. the real $10.00/$50.00). claude-mythos-5 is Project
    # Glasswing-only but is a real, released model, priced identically to Fable.
    # Recorded BEFORE either can be armed by a DEFAULT_*_MODEL move, for the same
    # reason as the Claude 5 family above.
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    # Explicit 4.6/4.7/4.8-tier and Sonnet-4.6/Haiku-4.5 entries (issue athenaeum#777).
    # The shorter claude-opus-4 / claude-sonnet-4 / claude-haiku-4 prefixes below
    # already resolve these correctly via longest-prefix match — this is
    # legibility and future-proofing (a reader should not have to trust prefix
    # arithmetic to know these are priced), not a second pricing bug.
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}

# Blended fallback rate (USD per million tokens) for tokens accumulated
# WITHOUT a model tag, or tagged with an id that matches no prefix above
# (e.g. routed via a proxy). Matches the historical pre-athenaeum#247 estimate.
# Issue athenaeum#783: this fallback is UNCHANGED and stays reachable for genuinely
# untagged tokens — that is its original purpose (athenaeum#247). What changed is
# the *reachability* for a TAGGED model: :func:`athenaeum.config.preflight_model_rates`
# (wired into ``athenaeum run``'s precondition gate, mirroring
# :func:`athenaeum.provider.preflight_provider`) resolves every model-knob this
# run will actually use and fails the run at startup if any is unpriced under
# the ACTIVE table below — so a tagged model used by a real run never reaches
# this fallback silently. ``_rates_for_model`` itself is unchanged for a tag
# that was never resolved through a knob (e.g. a proxy-routed id) — see the
# athenaeum#783 issue's "Design decision" for why the preflight, not a change to this
# function, is the mechanism that keeps that promise.
_BLENDED_INPUT_USD_PER_MTOK = 1.50
_BLENDED_OUTPUT_USD_PER_MTOK = 7.50


def default_model_rates() -> dict[str, tuple[float, float]]:
    """Return a COPY of the code-default per-MTok rate table (issue athenaeum#783).

    Two callers: (1) ``athenaeum init``'s default-config generator
    (:func:`athenaeum.config.write_default_config`) serializes this into a
    fresh ``athenaeum.yaml``'s ``pricing:`` section, so a new install is
    priced correctly out of the box from the SAME literal a maintainer edits
    when a vendor price changes — never a hand-copied second list; (2)
    :func:`configure_model_rates`'s fallback when no yaml ``pricing:``
    section is set (pre-athenaeum#783 configs keep today's behavior unchanged
    until they gain one). A copy, not the live dict, so neither caller can
    mutate the module constant.
    """
    return dict(_MODEL_RATES_USD_PER_MTOK)


# The EFFECTIVE / ACTIVE per-MTok rate table :func:`_rates_for_model` and
# :func:`model_has_price` consult (issue athenaeum#783). Starts as the code default
# so any caller that never loads config sees byte-identical pricing to before
# athenaeum#783. A real ``athenaeum run`` REPLACES this wholesale via
# :func:`configure_model_rates`, once :func:`athenaeum.config.resolve_model_rates`
# has read the operator's ``athenaeum.yaml`` ``pricing:`` section — REPLACE
# semantics, not an overlay/merge with this code table. See the athenaeum#783
# issue's "Design decision" for why a per-prefix merge (yaml overlaying the
# code table, which stays the floor) was rejected: an omission in yaml would
# keep silently reading the code default — the invisible second source of
# truth the athenaeum#783 preflight exists to kill for a model the run actually uses.
_ACTIVE_MODEL_RATES_USD_PER_MTOK: dict[str, tuple[float, float]] = dict(
    _MODEL_RATES_USD_PER_MTOK
)


def configure_model_rates(rates: dict[str, tuple[float, float]] | None) -> None:
    """Install *rates* as the effective per-MTok table for this process (athenaeum#783).

    REPLACES the active table outright — never merges with the code default.
    A non-empty *rates* (validated entries from ``athenaeum.yaml``'s
    ``pricing:`` section, via :func:`athenaeum.config.resolve_model_rates`)
    becomes the WHOLE table: a prefix the operator's yaml does not mention is
    no longer priced from this call onward, by design — yaml is authoritative,
    not a floor the code table backfills (see the athenaeum#783 issue). ``None`` or
    an empty dict resets to the code default (:func:`default_model_rates`) —
    correct both for a config with no ``pricing:`` section (pre-athenaeum#783
    installs) and for test teardown. This function is unconditional (never a
    no-op on empty input) precisely so a stale table installed by a PRIOR call
    in the same process (a previous run, or an earlier test) can never leak
    into this one.
    """
    global _ACTIVE_MODEL_RATES_USD_PER_MTOK
    _ACTIVE_MODEL_RATES_USD_PER_MTOK = dict(rates) if rates else default_model_rates()


def _lookup_rate(
    model: str, rates: dict[str, tuple[float, float]]
) -> tuple[float, float] | None:
    """Longest-prefix match of *model* against *rates*, or ``None`` (athenaeum#783).

    Shared by :func:`_rates_for_model` (which falls back to the blended rate
    on a miss) and :func:`model_has_price` (the athenaeum#783 preflight's check,
    which must NOT fall back — a miss there is exactly the loud-failure signal
    the preflight exists to raise).
    """
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, candidate in rates.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = candidate, len(prefix)
    return best


def model_has_price(model: str) -> bool:
    """Return whether *model* resolves to a real rate under the ACTIVE table
    (issue athenaeum#783) — i.e. would NOT fall back to the blended rate.

    Used by the startup preflight (:func:`athenaeum.config.preflight_model_rates`)
    so a tagged model that would otherwise silently under-report at the
    blended rate instead fails the run loudly before any cost is computed.
    """
    return _lookup_rate(model, _ACTIVE_MODEL_RATES_USD_PER_MTOK) is not None


def _rates_for_model(model: str | None) -> tuple[float, float]:
    """Return ``(input, output)`` USD/MTok for *model* (longest-prefix match).

    Untagged (``None``) or unknown ids fall back to the blended rate. Reads
    the ACTIVE table (issue athenaeum#783's :data:`_ACTIVE_MODEL_RATES_USD_PER_MTOK`,
    replaceable via :func:`configure_model_rates`), not the code-default table
    directly, so a resolved ``athenaeum.yaml`` ``pricing:`` override changes
    cost immediately without threading config through every :class:`TokenUsage`
    call site.
    """
    if model:
        match = _lookup_rate(model, _ACTIVE_MODEL_RATES_USD_PER_MTOK)
        if match is not None:
            return match
    return (_BLENDED_INPUT_USD_PER_MTOK, _BLENDED_OUTPUT_USD_PER_MTOK)


# Model-level sampling-parameter capability (issue athenaeum#577; epic athenaeum#515 deliverable 4,
# which lands here ONCE — athenaeum#573 reads this rather than re-declaring it). Records
# where ``temperature`` / ``top_p`` / ``top_k`` return HTTP 400: the Claude 4.7+
# / 5-family request surface removed them, while earlier tiers still accept them.
# This is a DECLARATION for callers to consult, NOT a step toward sending sampling
# parameters — athenaeum sends none on any path. Verified 2026-08-01 against the
# ``claude-api`` skill (the source of truth for model facts, per hestia#1055).
# Keyed by the same longest-prefix ``startswith`` style as the rate table above;
# maintain the two tables together.
_SAMPLING_PARAMS_REJECTED_PREFIXES: dict[str, bool] = {
    # Rejected — sampling parameters return HTTP 400 on these models.
    "claude-opus-5": True,
    "claude-opus-4-8": True,
    "claude-opus-4-7": True,
    "claude-sonnet-5": True,
    "claude-fable-5": True,
    # Accepted — sampling parameters are still honored on these tiers.
    "claude-haiku-4-5": False,
    "claude-sonnet-4-6": False,
}


def _sampling_params_rejected(model: str | None) -> bool | None:
    """Whether ``temperature`` / ``top_p`` / ``top_k`` return HTTP 400 for
    *model* (longest-prefix match), or ``None`` if *model* matches no recorded
    prefix.

    Declaration only (issue athenaeum#577): athenaeum sends no sampling parameters —
    this exists so a caller (e.g. athenaeum#573) can consult one authoritative table
    instead of re-deriving the request-surface rule from a training prior.
    """
    if not model:
        return None
    best: bool | None = None
    best_len = -1
    for prefix, rejected in _SAMPLING_PARAMS_REJECTED_PREFIXES.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rejected, len(prefix)
    return best


# Model-level prompt-caching capability (issue athenaeum#927). Records the MINIMUM
# cacheable prefix length, in tokens. A ``cache_control`` breakpoint on a prefix
# shorter than this is accepted by the API and then silently ignored — no error,
# no warning, and ``cache_creation_input_tokens`` stays 0 forever. That silence is
# exactly what let athenaeum#790's detector breakpoint ship INERT and go unnoticed
# through two days of metered runs: a 630-token prefix marked cacheable, sent to a
# model whose floor is 4,096.
#
# The threshold is per-model and NOT monotonic across generations — the Claude 5
# tier halved it to 512 while Opus 4.6/4.5 and Haiku 4.5 sit at 4,096 — so it can
# be neither approximated by "newer is lower" nor collapsed to one constant. A
# prompt that caches on Opus 5 silently does not cache on Haiku 4.5.
#
# Verified 2026-08-15 against the ``claude-api`` skill (the source of truth for
# model facts, per hestia#1055); it agrees with the per-model figures already
# recorded in prose at ``resolutions.py``'s athenaeum#230 breakpoint comment.
# Keyed by the same longest-prefix ``startswith`` style as the rate table and
# _SAMPLING_PARAMS_REJECTED_PREFIXES above; maintain the three tables together.
_MIN_CACHEABLE_PREFIX_TOKENS: dict[str, int] = {
    # Claude 5 tier — 512.
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    # 1,024 tier.
    "claude-opus-4-8": 1024,
    "claude-opus-4-1": 1024,
    "claude-opus-4-0": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-sonnet-4-0": 1024,
    # 2,048 tier.
    "claude-opus-4-7": 2048,
    "claude-mythos-preview": 2048,
    "claude-3-5-haiku": 2048,
    # 4,096 tier — note Haiku 4.5 is the HIGHEST floor in the table, not the
    # lowest: the cheapest model is the hardest one to cache a short prefix on.
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
    # Family-level fallbacks, mirroring the short prefixes the rate table carries
    # so the two stay total over the same set of ids. Where a family spans more
    # than one floor, the fallback records the HIGHEST (most conservative) of
    # them: an id that only matches the family prefix is one we cannot pin to a
    # generation, and over-stating the floor can only refuse to certify a
    # breakpoint that would have worked, never certify one that is inert.
    # claude-opus-4 spans 1,024 (4.0/4.1/4.8) to 4,096 (4.5/4.6) -> 4,096.
    "claude-opus-4": 4096,
    # claude-sonnet-4 is 1,024 across 4.0/4.5/4.6 -> no ambiguity.
    "claude-sonnet-4": 1024,
    # claude-haiku-4 has exactly one member, Haiku 4.5.
    "claude-haiku-4": 4096,
}


def min_cacheable_prefix_tokens(model: str | None) -> int | None:
    """Minimum cacheable prefix length in tokens for *model* (longest-prefix
    match), or ``None`` if *model* matches no recorded prefix (issue athenaeum#927).

    ``None`` means UNKNOWN, never "no minimum" — a caller deciding whether to set
    a ``cache_control`` breakpoint must treat an unknown model as un-assertable
    rather than assuming the breakpoint will engage. Silence is the whole failure
    mode this table exists to make checkable.
    """
    if not model:
        return None
    best: int | None = None
    best_len = -1
    for prefix, minimum in _MIN_CACHEABLE_PREFIX_TOKENS.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = minimum, len(prefix)
    return best


#: Characters per token used by :func:`estimate_prompt_tokens`. Deliberately an
#: OVER-estimate of density so dividing by it yields a conservative LOWER bound on
#: the true token count. Calibrated against the two live ``count_tokens``
#: measurements recorded in issue athenaeum#927: ``_RESOLVE_SYSTEM`` is 11,728 chars /
#: 4,395 tokens (2.67 chars/token) and ``_DETECT_SYSTEM`` is 2,344 chars / 630
#: tokens (3.72). Both are denser than 4.0, so chars/4 under-counts both — which
#: is the direction that keeps :func:`estimate_prompt_tokens` honest.
_CHARS_PER_TOKEN_LOWER_BOUND = 4.0


def estimate_prompt_tokens(text: str) -> int:
    """A conservative LOWER bound on the token count of *text* (issue athenaeum#927).

    Exists so the minimum-cacheable-prefix property can be asserted offline, in a
    unit test, with no API key and no network call — the alternative (a live
    ``count_tokens`` request per prompt) is precisely the kind of check that does
    not run in CI and so does not catch the next inert breakpoint.

    Because the result under-counts, ``estimate_prompt_tokens(p) >= minimum``
    proves the real prefix clears *minimum*; the converse does NOT hold, so a
    prompt failing this bound is "not provably cacheable", not "provably
    uncacheable". That asymmetry is deliberate: it can only refuse to certify a
    breakpoint that would in fact have worked, never certify one that is inert.
    """
    return int(len(text) / _CHARS_PER_TOKEN_LOWER_BOUND)


@dataclass
class TokenUsage:
    """Accumulated API token usage for a pipeline run."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    # Prompt-caching counters (issue athenaeum#230). ``input_tokens`` from the API
    # excludes cached tokens, so these accumulate separately: creation is
    # billed at ~1.25x the input rate, reads at ~0.1x.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Batch API counters (issue athenaeum#236). Batch traffic is folded into the
    # main counters above (so totals and the run-summary log include it)
    # AND tracked separately here so ``estimated_cost_usd`` can apply the
    # Batch API's 50% discount to exactly the batch-attributed share.
    batch_input_tokens: int = 0
    batch_output_tokens: int = 0
    batch_cache_creation_input_tokens: int = 0
    batch_cache_read_input_tokens: int = 0
    # Per-model attribution (issue athenaeum#247). Keyed by the model-id string the
    # call site passed to ``messages.create``; each value tracks the same
    # six counters as the scalar fields above but for THAT model's share.
    # The scalar fields stay authoritative for totals/run-summary; this
    # dict is the additive subset that carries a model tag, letting
    # ``estimated_cost_usd`` price tagged tokens per model and fall back to
    # the blended rate for the untagged remainder. Excluded from ``repr``
    # to keep run-summary logging concise.
    per_model: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)
    # Per-knob attribution (issue athenaeum#781). Keyed by the model-KNOB string
    # (``classify`` / ``write`` / ``resolve`` / ``topic`` / ``reasoning_t1`` /
    # ``reasoning_t2`` — see ``prompt_registry._META_ROWS``, the single source
    # of truth) that the call site already passes to
    # :func:`athenaeum.config.resolve_model`. Mirrors ``per_model`` exactly:
    # same bucket shape, same additive-subset pattern (the scalar totals
    # above stay authoritative; this dict never feeds cost estimation, it is
    # purely a WHERE-did-the-tokens-go breakdown for ``athenaeum spend
    # --by-knob``). Excluded from ``repr`` to keep run-summary logging concise.
    per_knob: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)
    # Subscription-covered flag (issue athenaeum#330). When the run is served by the
    # ``claude-cli`` provider, the operator's Claude Code SUBSCRIPTION pays for
    # the tokens — there is no per-token API bill. Token COUNTS still
    # accumulate (and appear in the run summary) exactly as for the API
    # backend, but ``estimated_cost_usd`` reports $0 rather than pricing the
    # tokens at list rates. Set once at run start by the caller that resolved
    # the provider; defaults False so the API backend is unchanged.
    subscription_covered: bool = False
    # Tier-3 merge-call echo accounting (issue athenaeum#1184). ``merge_calls`` counts
    # every ``tier3_merge`` / ``tier3_merge_full`` LLM call (patch attempt AND
    # a subsequent full-echo fallback each count separately — they are two
    # distinct calls with two distinct prompts); ``merge_echoed_chars`` sums,
    # across those same calls, how many chars of the EXISTING page body each
    # call's prompt embedded (capped the same way the merge prompt itself
    # caps it — see ``tiers.record_merge_echo``'s call sites). Together these
    # give ``echoed_chars_per_call`` — the ~84%-of-prompt-is-echo cost term
    # athenaeum#1167 measured but nothing before this issue tracked per run.
    merge_calls: int = 0
    merge_echoed_chars: int = 0
    # Tier-3 create-path preamble guard (issue athenaeum#1171). A create
    # response can open with first-person planning/meta-commentary (e.g.
    # "Looking at the new observation, I need to...") that must never reach
    # a persisted page body — see ``athenaeum.tiers.strip_planning_preamble``.
    # ``preamble_stripped`` counts creates where such a leading preamble was
    # detected and removed, WITH substantive content surviving underneath
    # (the common case). ``preamble_rejected`` counts creates where the
    # preamble was the entire response — stripping it left nothing
    # substantive, so the create was rejected outright (see
    # ``athenaeum.tiers.PreambleOnlyResponseError``) rather than persist an
    # empty page. Additive across a run, same convention as ``merge_calls``
    # above; rendered in ``librarian-run-summary`` only when non-zero.
    preamble_stripped: int = 0
    preamble_rejected: int = 0
    # Issue athenaeum#1177: ATTEMPT vs SUCCESS, tracked independently of
    # ``api_calls`` (whose own increment convention already differs by call
    # site — see ``add()`` vs ``add_tokens()``'s docstrings — and which this
    # issue does not change, to avoid disturbing its existing cost-estimation/
    # spend-ledger/test surface). A four-day incident (credits exhausted,
    # every entity-phase Tier-2/3 call raising ``BadRequestError``) showed
    # ``api_calls`` alone cannot answer "did this run actually try anything":
    # the entity-phase call sites only ever bumped ``api_calls`` via
    # ``add()``, which is reached ONLY after a successful response, so an
    # all-failing run reported ``api_calls == 0`` -- indistinguishable from a
    # genuinely idle run with nothing to do, which is exactly what let the
    # ``athenaeum.zero_yield`` alarm's ``consecutive`` counter stay at 0
    # through the whole incident (see ``librarian._zero_yield_tripped``).
    #
    # ``attempted_calls`` is bumped by :meth:`record_attempt`, called by a
    # call site immediately BEFORE it dispatches a request -- mirrors the
    # pre-existing manual ``usage.api_calls += 1`` pattern the C4
    # detector/resolver loop (``merge.py``) already used for its OWN
    # attempt-before-call counting, now also wired into the entity phase's
    # shared ``tiers._timed_llm_call`` choke point (issue athenaeum#1177) so
    # EVERY LLM-serving phase's attempts are visible here, not just C4's.
    #
    # ``succeeded_calls`` is bumped inside :meth:`add_tokens` itself (called
    # ONLY when a real response's token/cache counts are being recorded) so
    # it is accurate for every current ``add_tokens``/``add`` call site with
    # no per-site changes beyond the two above. A call that attempted but
    # never landed a response (retries exhausted, a non-transient error
    # degrading the caller to a fallback) bumps ``attempted_calls`` but NOT
    # ``succeeded_calls`` -- the disagreement AC athenaeum#1177 asks for: a
    # counter must not be able to claim results a zero-token ledger
    # contradicts.
    attempted_calls: int = 0
    succeeded_calls: int = 0

    def record_attempt(self) -> None:
        """Record ONE call about to be dispatched, before its outcome is known.

        Call this immediately before the request goes out (issue
        athenaeum#1177) -- both from the entity phase's shared
        ``tiers._timed_llm_call`` choke point AND from merge.py's C4
        detector/resolver loop, which ALREADY pre-increments its own
        ``usage.api_calls``/local ``haiku_calls``/``resolve_calls`` counters
        for its own per-phase summary line; this method's counter is
        additive and separate -- a single RUN-LEVEL "was anything actually
        attempted" signal spanning every phase, which nothing before this
        issue aggregated in one place.
        """
        self.attempted_calls += 1

    def record_merge_echo(self, echoed_chars: int) -> None:
        """Record one Tier-3 merge LLM call's echoed-existing-page char count.

        Called once per ``tier3_merge``/``tier3_merge_full`` API call (issue
        athenaeum#1184) — a patch attempt that falls back to full-echo records
        TWICE, once per call, since each is its own prompt/response/cost.
        """
        self.merge_calls += 1
        self.merge_echoed_chars += max(0, echoed_chars)

    def _tag_model(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        *,
        is_batch: bool,
    ) -> None:
        """Accumulate this call's counts into the per-model subset (athenaeum#247)."""
        bucket = self.per_model.setdefault(
            model,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "batch_input_tokens": 0,
                "batch_output_tokens": 0,
                "batch_cache_creation_input_tokens": 0,
                "batch_cache_read_input_tokens": 0,
            },
        )
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_creation_input_tokens"] += cache_creation_input_tokens
        bucket["cache_read_input_tokens"] += cache_read_input_tokens
        if is_batch:
            bucket["batch_input_tokens"] += input_tokens
            bucket["batch_output_tokens"] += output_tokens
            bucket["batch_cache_creation_input_tokens"] += cache_creation_input_tokens
            bucket["batch_cache_read_input_tokens"] += cache_read_input_tokens

    def _tag_knob(
        self,
        knob: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        *,
        is_batch: bool,
    ) -> None:
        """Accumulate this call's counts into the per-knob subset (athenaeum#781).

        Same bucket shape and accumulation rule as :meth:`_tag_model` — kept
        as a separate method (rather than a shared helper parametrized on
        the target dict) so each call site can tag model and knob
        independently, exactly mirroring how ``model=`` already works.
        """
        bucket = self.per_knob.setdefault(
            knob,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "batch_input_tokens": 0,
                "batch_output_tokens": 0,
                "batch_cache_creation_input_tokens": 0,
                "batch_cache_read_input_tokens": 0,
            },
        )
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_creation_input_tokens"] += cache_creation_input_tokens
        bucket["cache_read_input_tokens"] += cache_read_input_tokens
        if is_batch:
            bucket["batch_input_tokens"] += input_tokens
            bucket["batch_output_tokens"] += output_tokens
            bucket["batch_cache_creation_input_tokens"] += cache_creation_input_tokens
            bucket["batch_cache_read_input_tokens"] += cache_read_input_tokens

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        model: str | None = None,
        knob: str | None = None,
    ) -> None:
        """Record tokens from one API call.

        *model* (issue athenaeum#247) is the serving model-id; when given, the
        counts are additionally attributed to that model for per-model
        cost estimation. Untagged calls fall back to the blended rate.

        *knob* (issue athenaeum#781) is the model-KNOB string (``classify`` /
        ``write`` / ``resolve`` / ``topic`` / ``reasoning_t1`` /
        ``reasoning_t2``) the call site already passes to
        :func:`athenaeum.config.resolve_model`; when given, the counts are
        additionally attributed to that knob in :attr:`per_knob`, mirroring
        *model*'s ``per_model`` accumulation.
        """
        self.add_tokens(
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            model=model,
            knob=knob,
        )
        self.api_calls += 1

    def add_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        model: str | None = None,
        knob: str | None = None,
    ) -> None:
        """Accumulate token counters WITHOUT counting an API call (athenaeum#239).

        For callees whose orchestrating call site counts ``api_calls``
        separately (attempt counting — e.g. the merge-phase detector/
        resolver loop and the athenaeum#188 reresolve pass): the call site bumps
        ``api_calls`` before the request; the callee lands the response's
        token + cache counts here once they are known.

        *model* (issue athenaeum#247) optionally tags the serving model-id for
        per-model cost attribution. *knob* (issue athenaeum#781) optionally tags the
        model-knob for per-knob attribution (:attr:`per_knob`) — independent
        of *model*, exactly mirroring how the two kwargs already coexist at
        every real call site (both sourced from the same config resolution).

        Also bumps :attr:`succeeded_calls` (issue athenaeum#1177) — this
        method is called ONLY when a real response's counts are known
        (every current call site: ``add()``, ``add_batch_tokens()``, the C4
        detector/resolver's own successful responses, the athenaeum#188
        reresolve pass), so it is the single accurate place to count
        genuine successes, independent of whatever attempt-counting
        convention (or lack of one) the call site otherwise uses.
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.succeeded_calls += 1
        if model:
            self._tag_model(
                model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                is_batch=False,
            )
        if knob:
            self._tag_knob(
                knob,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                is_batch=False,
            )

    def add_batch_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        model: str | None = None,
        knob: str | None = None,
    ) -> None:
        """Accumulate token counters from a Batch API result (athenaeum#236).

        Folds the counts into the main counters (so ``total_tokens`` and
        the run-summary line include batch traffic) and additionally into
        the batch-attributed counters so ``estimated_cost_usd`` applies
        the Batch API's 50% discount. Does NOT bump ``api_calls`` — batch
        call sites count one attempt per request at batch-assembly time
        (budget enforcement point, mirroring :meth:`add_tokens`'s
        attempt-counting contract from athenaeum#239).

        *model* (issue athenaeum#247) optionally tags the serving model-id; the
        batch share is attributed per model so the 50% discount composes
        with that model's rates. *knob* (issue athenaeum#781) optionally tags the
        model-knob; the batch share is attributed per knob the same way.
        """
        # Accumulate into the scalar + per-model/per-knob counters once
        # (untagged remainder stays blended); add_tokens with model=None/
        # knob=None here so the batch share is tagged via _tag_model /
        # _tag_knob below with is_batch=True.
        self.add_tokens(
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )
        self.batch_input_tokens += input_tokens
        self.batch_output_tokens += output_tokens
        self.batch_cache_creation_input_tokens += cache_creation_input_tokens
        self.batch_cache_read_input_tokens += cache_read_input_tokens
        if model:
            self._tag_model(
                model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                is_batch=True,
            )
        if knob:
            self._tag_knob(
                knob,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                is_batch=True,
            )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """Cache-inclusive token count (issue athenaeum#1137).

        ``total_tokens`` (above) excludes prompt-caching traffic (issue
        athenaeum#230) — correct for the metered API path, where cache
        creation/read are priced and reported separately via
        ``estimated_cost_usd``. The ``claude-cli`` SUBSCRIPTION path is
        different: its quota is consumed by cache traffic too, and a real
        recorded run measured 254 input + 59,916 output tokens against
        1,169,154 cache-creation + 2,144,653 cache-read tokens — a
        subscription ceiling gated on ``total_tokens`` alone undercounts
        real consumption by ~56x. This is the cache-inclusive counter the
        subscription branch of :func:`athenaeum.spend.ceiling_tripped` (and
        :func:`athenaeum.spend.spend_today`) compares against instead.

        Deliberately a SEPARATE counter from ``total_tokens`` rather than a
        redefinition of it: ``total_tokens`` is a cross-repo contract (see
        :func:`athenaeum.spend.tokens_by_model`'s docstring) matching
        hestia's ``cost-ledger.ts`` ``CostLedgerTokens`` shape, which
        excludes cache by definition — redefining it would silently change
        what hestia reads.

        Batch API tokens (issue athenaeum#236) are NOT added a second time
        here: :meth:`add_batch_tokens` already folds them into the four
        scalar counters below via ``add_tokens`` (see that method's body),
        so they are already included in each of the four terms summed here.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @staticmethod
    def _cost_for(
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        batch_input_tokens: int,
        batch_output_tokens: int,
        batch_cache_creation_input_tokens: int,
        batch_cache_read_input_tokens: int,
        rates_usd_per_mtok: tuple[float, float],
    ) -> float:
        """Price one model's share at *rates*, composing cache + batch (athenaeum#247).

        ``input_tokens`` from the API excludes cached tokens, so the cache
        counters are folded in at the documented multipliers (athenaeum#239): cache
        writes bill at 1.25x the input rate, cache reads at ~0.1x. Batch
        API traffic (athenaeum#236) bills at 50% of the synchronous rate, so half of
        the batch-attributed share is subtracted.
        """
        input_rate = rates_usd_per_mtok[0] / 1_000_000
        output_rate = rates_usd_per_mtok[1] / 1_000_000
        cost = (
            input_tokens * input_rate
            + output_tokens * output_rate
            + cache_creation_input_tokens * input_rate * 1.25
            + cache_read_input_tokens * input_rate * 0.10
        )
        batch_cost = (
            batch_input_tokens * input_rate
            + batch_output_tokens * output_rate
            + batch_cache_creation_input_tokens * input_rate * 1.25
            + batch_cache_read_input_tokens * input_rate * 0.10
        )
        return cost - 0.5 * batch_cost

    @property
    def estimated_cost_usd(self) -> float:
        """Estimate cost with per-model attribution (issue athenaeum#247).

        Tokens tagged with a known model (via the ``model=`` kwarg on the
        accumulation methods) price at that model's rates from the ACTIVE
        rate table (:data:`_ACTIVE_MODEL_RATES_USD_PER_MTOK` — the code
        default until an ``athenaeum run`` replaces it with the operator's
        ``athenaeum.yaml`` ``pricing:`` section, issue athenaeum#783), matched by
        longest id prefix. Tokens accumulated WITHOUT a model tag — or
        tagged with an id that matches no known prefix (e.g. routed through
        a proxy) — fall back to the blended rate ($1.50/M input, $7.50/M
        output). The cache multipliers (athenaeum#239) and the Batch API 50%
        discount (athenaeum#236) compose unchanged per model.

        Caveat: untagged/unknown-model traffic is still only approximated
        at the blended rate; it cannot be attributed to a specific model.

        Subscription-covered runs (issue athenaeum#330 ``claude-cli`` backend) short-
        circuit to $0: the operator's Claude Code subscription pays for the
        tokens, so pricing them at API list rates would be wrong. The token
        COUNTS remain in the accumulators and the run summary.
        """
        if self.subscription_covered:
            return 0.0
        return self._cost_at_api_rates()

    @property
    def notional_cost_usd(self) -> float:
        """Counterfactual API-rate cost of this run's tokens (issue athenaeum#487).

        The same per-model pricing as :attr:`estimated_cost_usd` but WITHOUT the
        subscription short-circuit — what these tokens WOULD have cost at API
        list rates even when the operator's Claude Code subscription actually
        paid $0 for them. It lets a subscription ledger row report a labelled
        counterfactual (``notional_usd``) instead of reading as $0 of activity,
        while ``estimated_cost_usd`` stays the real-dollars-paid figure ($0 on
        the subscription path). The two are NEVER summed — a reader keys on the
        row's ``billing_mode`` to choose which applies.
        """
        return self._cost_at_api_rates()

    def _cost_at_api_rates(self) -> float:
        """Price every accumulated token at API list rates, per model (athenaeum#247).

        Extracted from :attr:`estimated_cost_usd` so the provider-tagged
        estimate and the notional counterfactual (athenaeum#487) share one
        implementation rather than drifting apart.
        """
        total = 0.0
        # Per-model tagged share at each model's own rates.
        tagged = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "batch_input_tokens": 0,
            "batch_output_tokens": 0,
            "batch_cache_creation_input_tokens": 0,
            "batch_cache_read_input_tokens": 0,
        }
        for model, bucket in self.per_model.items():
            for key in tagged:
                tagged[key] += bucket.get(key, 0)
            total += self._cost_for(
                bucket.get("input_tokens", 0),
                bucket.get("output_tokens", 0),
                bucket.get("cache_creation_input_tokens", 0),
                bucket.get("cache_read_input_tokens", 0),
                bucket.get("batch_input_tokens", 0),
                bucket.get("batch_output_tokens", 0),
                bucket.get("batch_cache_creation_input_tokens", 0),
                bucket.get("batch_cache_read_input_tokens", 0),
                _rates_for_model(model),
            )
        # Untagged remainder (scalar totals minus the tagged subset) priced
        # at the blended rate. Clamped at 0 so a hypothetical double-count
        # can never make the remainder negative.
        blended_rates = (_BLENDED_INPUT_USD_PER_MTOK, _BLENDED_OUTPUT_USD_PER_MTOK)
        total += self._cost_for(
            max(self.input_tokens - tagged["input_tokens"], 0),
            max(self.output_tokens - tagged["output_tokens"], 0),
            max(
                self.cache_creation_input_tokens
                - tagged["cache_creation_input_tokens"],
                0,
            ),
            max(self.cache_read_input_tokens - tagged["cache_read_input_tokens"], 0),
            max(self.batch_input_tokens - tagged["batch_input_tokens"], 0),
            max(self.batch_output_tokens - tagged["batch_output_tokens"], 0),
            max(
                self.batch_cache_creation_input_tokens
                - tagged["batch_cache_creation_input_tokens"],
                0,
            ),
            max(
                self.batch_cache_read_input_tokens
                - tagged["batch_cache_read_input_tokens"],
                0,
            ),
            blended_rates,
        )
        return total


def cost_for_token_bucket(model: str | None, bucket: dict[str, int]) -> float:
    """Price ONE ledger ``tokens_by_model`` bucket at the ACTIVE rates (athenaeum#788).

    *bucket* is a single value from a spend-ledger record's ``tokens_by_model``
    map (issue athenaeum#487): the core ``{input, output, total}`` plus athenaeum's
    cache/batch detail keys. ``total`` is ignored — it is a derived convenience
    field (``input + output``, excluding cache) and pricing it would double-count.

    Exists so ``athenaeum spend --reprice`` (issue athenaeum#788) recomputes a
    historical row through the SAME arithmetic that wrote it — the cache
    multipliers and the Batch API 50% discount of :meth:`TokenUsage._cost_for`,
    at the same longest-prefix rates of :func:`_rates_for_model` — instead of
    reimplementing the formula in :mod:`athenaeum.spend` where it would silently
    drift from this one. Reads the ACTIVE table, so an ``athenaeum.yaml``
    ``pricing:`` override installed via :func:`configure_model_rates` reprices at
    the operator's current rates, which is the whole point of athenaeum#788.

    Note the deliberate asymmetry with :meth:`TokenUsage._cost_at_api_rates`:
    that method prices an untagged REMAINDER at the blended fallback, derived by
    subtracting the tagged subset from the run's scalar totals. A ledger row's
    per-model map carries no such remainder — a row that tagged no model has an
    EMPTY map and is *unpriceable* (see :func:`athenaeum.spend.summarize`), a
    state the repricing consumer must report rather than silently price at the
    blended rate. So this function prices exactly what it is given, and never
    reaches the blended fallback for an untagged row.
    """
    return TokenUsage._cost_for(
        int(bucket.get("input", 0) or 0),
        int(bucket.get("output", 0) or 0),
        int(bucket.get("cache_creation_input_tokens", 0) or 0),
        int(bucket.get("cache_read_input_tokens", 0) or 0),
        int(bucket.get("batch_input_tokens", 0) or 0),
        int(bucket.get("batch_output_tokens", 0) or 0),
        int(bucket.get("batch_cache_creation_input_tokens", 0) or 0),
        int(bucket.get("batch_cache_read_input_tokens", 0) or 0),
        _rates_for_model(model),
    )


def cache_usage_counts(response: object) -> tuple[int, int, int, int]:
    """Extract token counts from an Anthropic API response (issue athenaeum#230).

    Returns ``(input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens)``. Missing or non-int fields coerce to 0 so
    callers can log/accumulate without guarding against older SDK shapes
    or test doubles that omit the cache fields.

    **Usage-normalization contract (issue athenaeum#786's implementer note):**
    every :class:`athenaeum.provider.LLMBackend` response MUST expose a
    ``.usage`` object carrying these four counters (see
    :class:`athenaeum.provider.LLMUsage`) — this function is the ONE seam
    every call site funnels a response through before accumulating into
    :class:`TokenUsage`, so it is where a backend that normalizes its usage
    onto the wrong shape (or drops it entirely) is caught, rather than left to
    each adapter's good behavior. A response with NO ``.usage`` at all reads
    as $0 / 0 tokens downstream with no error — the same failure class as the
    Fable/Mythos pricing gap that motivated this note — so that case is
    flagged loudly here (a coerced-to-zero individual FIELD, e.g. a real SDK
    response's ``cache_creation_input_tokens=None`` on a no-cache-breakpoint
    call, is legitimate and stays silent; a missing ``.usage`` object
    entirely is not).
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        log.warning(
            "cache_usage_counts: response has no .usage attribute — this "
            "reads as $0/0-tokens downstream; if a backend adapter produced "
            "this response, it is almost certainly a usage-normalization bug "
            "(see the LLMUsage contract in athenaeum.provider), not a "
            "genuinely usage-less call"
        )

    def _count(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return (
        _count("input_tokens"),
        _count("output_tokens"),
        _count("cache_creation_input_tokens"),
        _count("cache_read_input_tokens"),
    )


@dataclass
class ProcessingResult:
    """Result of processing one raw file."""

    raw_file: RawFile
    created: list[WikiEntity] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalated: list[EscalationItem] = field(default_factory=list)
    #: Count of Tier-2 classification responses that dropped ALL entities
    #: because no parseable JSON array could be recovered (issue athenaeum#472). Almost
    #: always 0 or 1 per file; surfaced as ``degraded=N`` in the run summary.
    degraded: int = 0
    #: Count of Tier-2 classification responses that dropped ALL entities
    #: because they were TRUNCATED at the output-token budget
    #: (``stop_reason == "max_tokens"``, issue athenaeum#476). Kept SEPARATE from
    #: ``degraded`` — a truncation is fixed by a bigger budget, not escaping —
    #: and surfaced as ``truncated=N`` in the run summary.
    truncated: int = 0
    #: Count of Tier-1 programmatic matches this file produced (issue athenaeum#1184)
    #: — the fan-out driver: one match is one existing entity a raw file's
    #: index-key hits dispatched a merge decision for. Set by the caller right
    #: after ``tier1_programmatic_match`` runs; stays 0 on any early-return
    #: path that never reaches Tier 1 (e.g. the Tier-0 do-not-email/handle
    #: short-circuits), which is correct — those paths dispatch no matches.
    matched: int = 0
    #: Count of Tier-3 merges suppressed by the page-size invariant (issue
    #: athenaeum#1182): a page over ``librarian.page_size_threshold_chars``
    #: routes to escalation (``EscalationItem.conflict_type=
    #: "oversize_page"``) instead of another merge, and is left unmodified.
    #: Derived from ``escalated`` by conflict_type — see
    #: ``athenaeum.librarian._apply_tier3_results`` — rather than tracked as
    #: an independent counter, so it can never drift out of sync with what
    #: was actually escalated. Surfaced as ``oversize_suppressed=N`` in the
    #: run summary, mirroring the degraded/truncated convention above.
    oversize_suppressed: int = 0
    #: Count of NEW entity writes this file's Tier-3 create phase produced
    #: whose ``type`` was outside declared ∪ ``KNOWN_TYPES`` and was
    #: therefore REFUSED by the write-boundary guard (issue athenaeum#1196,
    #: :func:`athenaeum.wiki_write_guard.guard_entity_write_type`) rather
    #: than written to ``wiki/``. Not counted in ``created`` — a refused
    #: entity was never applied. Surfaced as ``type_rejected=N`` in the run
    #: summary, mirroring the ``degraded``/``truncated`` convention above.
    type_rejected: int = 0


# --- Schema loading ---


def load_schema_list(schema_path: Path, filename: str) -> list[str]:
    """Load a list of valid values from a schema markdown table.

    Parses standard markdown tables, extracting the first cell from each
    data row. Header and separator rows are skipped.
    """
    fpath = schema_path / filename
    if not fpath.exists():
        return []
    text = fpath.read_text(encoding="utf-8")
    lines = text.splitlines()
    values: list[str] = []
    # Collect separator row indices so we can skip headers
    separator_indices: set[int] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and all(c in "-| " for c in stripped):
            separator_indices.add(i)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip separator rows
        if i in separator_indices:
            continue
        # Skip header rows (the row immediately before a separator)
        if (i + 1) in separator_indices:
            continue
        cells = [c.strip() for c in stripped.split("|")]
        for cell in cells:
            if cell:
                values.append(cell)
                break
    return values


# --- Entity Index ---


class IndexEntry(NamedTuple):
    """One ``EntityIndex._by_name`` value: identity + type of an indexed key.

    ``uid`` and ``path`` are the original ``(uid_or_name, path)`` pair this
    index has always carried. ``type`` is new (issue athenaeum#1169): the page's
    ``type:`` frontmatter value (resolved via :func:`resolve_page_type`), so a
    matcher can gate on it without re-reading the page from disk.

    ``type`` is ``None`` when the page carries no ``type:`` frontmatter at
    all — an EXPLICIT sentinel, not an empty string, so a gate can branch on
    ``is None`` rather than an implicit falsy check. See
    :func:`athenaeum.tiers._passes_type_gate` for the KEPT-by-default policy
    this represents.

    Because ``NamedTuple`` subclasses ``tuple``, existing code that reads a
    ``_by_name`` value POSITIONALLY (``entry[0]`` for uid, ``entry[1]`` for
    path — e.g. :func:`athenaeum.reconcile`'s ``looked[1]``) keeps working
    unchanged, including against a plain ``(uid, path)`` 2-tuple poked
    directly into ``_by_name`` by tests that predate this issue — reading
    ``.type`` off one of those via ``getattr(entry, "type", None)`` degrades
    to "no type known" rather than raising.
    """

    uid: str
    path: Path
    type: str | None = None


#: Page types withheld from :meth:`EntityIndex.items` — the raw-text
#: MENTION-matching surface :func:`athenaeum.tiers.tier1_programmatic_match`
#: walks (issue athenaeum#1183). ``type: person`` pages are CRM-imported
#: contact records, not wiki entities that a raw observation should be
#: fuzzy-matched against by name. Every OTHER read/write path —
#: :meth:`EntityIndex.lookup` (name/alias, one specific name at a time),
#: :meth:`EntityIndex.get_by_uid`, :meth:`EntityIndex.has_entity_format`,
#: ``__iter__``/``__len__`` — is UNAFFECTED and keeps finding a person page
#: exactly as before: those back structured, no-LLM, name-or-uid-ADDRESSED
#: operations (:func:`athenaeum.corrections.resolve_target`,
#: :func:`athenaeum.tiers.validate_create_name`'s collision check, the
#: uid-less fallback in :func:`athenaeum.librarian.tier0_handle_upsert`,
#: :mod:`athenaeum.pii`'s excluded-read join, the handle-shaped-query
#: resolver in :mod:`athenaeum.identity_resolution`), which this issue does
#: not change — only raw-text mention MATCHING does. Person mentions instead
#: resolve via the consult-only :class:`athenaeum.person_registry.PersonRegistry`
#: (see that module and :func:`athenaeum.identity_resolution.resolve_person_mention`).
#: Demotion only — nothing here deletes a page or a field.
DEMOTED_NAME_MATCH_TYPES: frozenset[str] = frozenset({"person"})


class EntityIndex:
    """In-memory index of all wiki entities for name/alias lookup."""

    def __init__(self, wiki_root: Path) -> None:
        self.wiki_root = wiki_root
        self._by_name: dict[str, IndexEntry] = {}
        self._entities: dict[str, dict] = {}
        self._by_uid: dict[str, Path] = {}
        self._entity_format_paths: set[Path] = set()
        self._load()

    def _load(self) -> None:
        for fpath in sorted(self.wiki_root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue

            uid_raw = meta.get("uid", "")
            name_raw = meta.get("name", "")
            if not name_raw:
                continue
            # meta is dict[str, object] (arbitrary YAML-scalar values), but
            # uid/name are coerced to str at the frontmatter boundary (see
            # parse_frontmatter's identity-field coercion) — narrow here.
            assert isinstance(uid_raw, str)
            assert isinstance(name_raw, str)
            uid = uid_raw
            name = name_raw

            # Issue athenaeum#1169: carry the page's type through the index.
            # resolve_page_type is the single canonical precedence resolver
            # (top-level `type:` first, `metadata.type` fallback, else "") —
            # reused here rather than reading meta["type"] directly so this
            # index agrees with athenaeum.search / athenaeum.entity_schema on what a
            # page's type IS. "" (no type: found) is normalized to the
            # explicit IndexEntry.type=None sentinel below.
            page_type = resolve_page_type(meta)
            entry_type = page_type if page_type else None

            key = name.lower()
            self._by_name[key] = IndexEntry(uid or name, fpath, entry_type)
            if uid:
                self._entities[uid] = meta
                self._by_uid[uid] = fpath
                self._entity_format_paths.add(fpath)

            aliases_raw = meta.get("aliases", [])
            # Iterable (not list) preserves the pre-existing runtime
            # behavior of tolerating any iterable (e.g. a bare string would
            # iterate per-character); .lower() below assumes str elements,
            # same assumption the original code made.
            assert isinstance(aliases_raw, Iterable)
            for alias in aliases_raw:
                if alias:
                    assert isinstance(alias, str)
                    self._by_name[alias.lower()] = IndexEntry(uid or name, fpath, entry_type)

    def lookup(self, name: str) -> IndexEntry | None:
        """Look up by name or alias (case-insensitive).

        Returns an :class:`IndexEntry` (``.uid``/``.path``/``.type``, and
        still positionally ``(uid_or_name, path, type)`` since it is a
        tuple) or ``None``.
        """
        return self._by_name.get(name.lower())

    def get_by_uid(self, uid: str) -> Path | None:
        """Look up entity file path by UID. Returns None if not found."""
        return self._by_uid.get(uid)

    def has_entity_format(self, path: Path) -> bool:
        """Check if a wiki page uses the full entity template format (has uid field)."""
        return path in self._entity_format_paths

    def register(self, entity: WikiEntity) -> None:
        """Add a newly created entity to the index."""
        key = entity.name.lower()
        path = self.wiki_root / entity.filename
        self._by_name[key] = IndexEntry(entity.uid, path, entity.type or None)
        self._entities[entity.uid] = {
            "uid": entity.uid,
            "type": entity.type,
            "name": entity.name,
        }
        self._by_uid[entity.uid] = path
        self._entity_format_paths.add(path)
        for alias in entity.aliases:
            if alias:
                self._by_name[alias.lower()] = IndexEntry(entity.uid, path, entity.type or None)

    def __len__(self) -> int:
        """Number of unique name/alias keys indexed."""
        return len(self._by_name)

    def items(self) -> "Iterator[tuple[str, IndexEntry]]":
        """Iterate over ``(name_or_alias_key, IndexEntry(uid, path, type))``
        pairs for every key eligible for NAME/ALIAS matching.

        Replaces direct access to ``_by_name`` from callers that need to
        walk the index (e.g. tier-based scans) — :func:`athenaeum.tiers.
        tier1_programmatic_match` is, deliberately, the ONLY caller (see its
        module-level import). Issue athenaeum#1183: a
        :data:`DEMOTED_NAME_MATCH_TYPES` entry (``person``) is withheld HERE,
        at the sole matching call site, rather than at storage time — so
        :meth:`lookup` (used by :func:`athenaeum.corrections.resolve_target`,
        :func:`athenaeum.tiers.validate_create_name`'s collision check, and
        the uid-less fallback in :func:`athenaeum.librarian.tier0_handle_upsert`)
        keeps finding a person page by name/alias exactly as before —
        those are structured, no-LLM, name-addressed operations this issue
        does not change; only raw-text MENTION matching does. Returns a
        generator, not a live view of ``_by_name`` — do not mutate the index
        while iterating.
        """
        for key, entry in self._by_name.items():
            # getattr, not `entry.type`: a handful of existing tests poke
            # `_by_name` directly with a bare (pre-athenaeum#1169) 2-tuple that
            # carries no `.type` at all — `getattr(..., None)` degrades that
            # to "not a demoted type" (unfiltered, the pre-athenaeum#1183
            # behaviour) instead of raising AttributeError.
            if getattr(entry, "type", None) in DEMOTED_NAME_MATCH_TYPES:
                continue
            yield key, entry

    def __iter__(self) -> "Iterator[str]":
        """Iterate over indexed name/alias keys."""
        return iter(self._by_name)
