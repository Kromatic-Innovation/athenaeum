# SPDX-License-Identifier: Apache-2.0
"""Erasure classification and taint-propagation rules (athenaeum#985).

Split (c) of athenaeum#718's three-way re-scope (athenaeum#911 design lock
§8, `docs/extending/whole-store-adapter-design.md`): the nine acceptance criteria that
are **classification and taint-propagation logic**, sitting *above* wherever
the bytes land — decisions about *which* content is erasure-class and *what
may be written about it*, answerable without knowing whether the bytes land
on a filesystem or in a database. Split (b) (athenaeum#984, off-corpus
storage mechanics) and the store-contract work it depends on are **not**
touched or depended on here; this module builds its own test fixtures rather
than waiting for or wrapping either.

**No production caller was migrated onto this module in THIS slice (athenaeum#985).**
This mirrored the precedent :mod:`athenaeum.sensitivity` set for exactly this
shape of split (its own docstring: *"No production module imports this one
yet as of S1a/S1b ... that was slice S3"*) — the classification/taint logic
shipped fully implemented and fully tested, with wiring into a live write
path deferred as store-dependent, athenaeum#984-and-later territory. Every
function below has direct unit-test coverage exercising a real read/write
round trip, which is the "consumed, not dark" bar this repo's own precedent
sets for a slice in this position.

**That S3 migration is athenaeum#1116**, mirroring how :mod:`athenaeum.sensitivity`'s
own S3 (athenaeum#992) migrated S1a/S1b onto live callers. As of athenaeum#1116:
:mod:`athenaeum.merge` calls :func:`classify_inference_taint` at both compiled-page
write sites; :mod:`athenaeum.answers` calls :func:`classify_by_provenance` /
:func:`off_corpus_recall_source` on its re-ingestion path; and
:mod:`athenaeum.decay_sweep` calls :func:`reconcile_bucket_daily_with_pack`
(gated on an explicit ``data_class`` frontmatter field — see that function's
docstring). All three route to :mod:`athenaeum.off_corpus` when it is
configured, and log a structured warning naming the taint and the page when
it is not (a reversible default — no deployment breaks for lack of an
off-corpus surface).

This module covers:

- **AC1 — HMAC-keyed erasure-class hashes.** :func:`erasure_content_hash`
  plus the purgeable per-corpus key lifecycle (:func:`load_or_create_erasure_key`,
  :func:`purge_erasure_key`). A plain hash of a short, low-entropy erasure-class
  fact (an email, a role string) is dictionary-reversible and would smuggle
  pseudonymized personal data into git history through a ledger's back door;
  keying the hash means erasing the key erases linkability, without touching
  git history at all.
- **AC2 — opaque, uid-based person-entity slugs and pair ids.**
  :func:`opaque_person_slug`, :func:`opaque_pair_id`. Deliberately narrower
  than the ordinary wiki filename convention
  (``f"{uid}-{slugify(name)}.md"``, :class:`athenaeum.models.EntityIndex`),
  which embeds the display name for human readability — an erasure-class
  identity reference must never do that.
- **AC3 — conservative default classification.** :func:`classify_retention`
  enforces "a data subject whose jurisdiction is unknown at write time is
  classified erasure-class" *ahead of* any retention-pack lookup, so no pack
  can loosen it by omission.
- **AC4/AC5/AC6 — the three taint rules.**
  :func:`classify_inference_taint` (derivation — a claim/``## Inference``
  block whose basis cites erasure-class content is itself erasure-class),
  :func:`classify_by_provenance` (re-ingestion — content re-entering from an
  off-corpus recall is erasure-class by provenance, never re-guessed from
  content), and :data:`EGRESS_DISCLOSURE` (push-is-egress — the honest
  disclosure that a session transcript is an enumerable-but-unreachable copy
  the erasure cascade cannot reach), carried in every
  :class:`RedactionLedgerRecord`.
- **AC7 — named remediation path.** Documented in
  `docs/design/security-posture.md` ("Erasure remediation: misclassified in-git
  content"); :func:`build_history_rewrite_remediation_record` builds (never
  executes) the ledger entry that protocol requires.
- **AC8 — the redaction ledger.** :class:`RedactionLedgerRecord` records
  THAT something was redacted/erased and WHY — structurally, not by
  convention: the dataclass has no content/free-text field for a caller to
  misuse.
- **AC9 — retention policy packs as data.** :class:`RetentionPack` /
  :class:`RetentionRule`, loaded from the packaged YAML files under
  `src/athenaeum/retention_packs/` (never a Python literal — see that
  package's own docstring), selected/overridden via
  ``erasure.retention_pack`` / ``erasure.retention_packs.<name>`` in
  ``athenaeum.yaml`` (:func:`athenaeum.config.resolve_retention_pack_selection`
  / :func:`athenaeum.config.resolve_retention_pack_overrides`). Honors the
  ``bucket: daily`` -> ``delete-after`` mapping
  `docs/design/provenance-shape.md` §8.8 commits to
  (:func:`reconcile_bucket_daily_with_pack`) as a **documented mapping**,
  not a rewrite of :mod:`athenaeum.decay_sweep`'s current (unchanged)
  behavior — see that function's docstring.

**Layering:** L3 service, peer to :mod:`athenaeum.sensitivity` /
:mod:`athenaeum.provenance` / :mod:`athenaeum.fingerprint`. Imports
:mod:`athenaeum.atomic_io` and :mod:`athenaeum.store` (L0/L1, for the durable
write primitives), :mod:`athenaeum.models` (L1, for :func:`slugify`),
:mod:`athenaeum.provenance` (L1, for :class:`SourceRef` / :func:`parse_source`)
and :mod:`athenaeum.config` (L2, for ``resolve_cache_dir`` and the two new
``resolve_retention_pack_*`` functions) at module scope — the same
"L3 reach down to L2" direction :mod:`athenaeum.sensitivity` already uses.
:mod:`athenaeum.inference_blocks` is imported function-locally inside
:func:`classify_inference_taint` purely for locality with the one function
that needs it; there is no cycle either way (``inference_blocks`` has no
athenaeum imports at all).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.resources
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection

import yaml

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_cache_dir,
    resolve_retention_pack_overrides,
    resolve_retention_pack_selection,
)
from athenaeum.models import slugify
from athenaeum.provenance import SourceRef, parse_source
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AC1 — HMAC-keyed erasure-class hashes + purgeable per-corpus key
# ---------------------------------------------------------------------------

#: Key filename under the cache dir (machine-local / ``operational``, R3 —
#: `docs/extending/whole-store-adapter-design.md` §5.2). Deliberately NOT under
#: ``knowledge_root`` (wiki/raw): a key that ever entered git history would
#: need the same history-rewrite remediation (AC7) this module exists to
#: avoid for ordinary key rotation. Purging is then a plain file delete —
#: instant, no git operation, no clone to chase (see "Reversible defaults
#: taken" in the athenaeum#985 PR for the one-key-per-machine consequence
#: this choice carries).
ERASURE_KEY_FILENAME = "_erasure_hmac_key"

_ERASURE_KEY_BYTES = 32
_HEX_KEY_RE = re.compile(r"^[0-9a-f]{%d}$" % (_ERASURE_KEY_BYTES * 2))


class ErasureKeyError(RuntimeError):
    """Raised when the erasure HMAC key file exists but is unreadable/corrupt.

    Loud by design, mirroring every other fail-closed config/state error in
    this codebase (:class:`athenaeum.storage.StorageConfigError`,
    :class:`athenaeum.sensitivity.SensitivityConfigError`): a corrupt key
    file must never silently fall back to hashing unkeyed, since that would
    reintroduce exactly the plain-hash leak AC1 exists to close.
    """


def resolve_erasure_key_path(cache_dir: Path | None = None) -> Path:
    """Resolve the erasure HMAC key path: ``<cache_dir>/_erasure_hmac_key``.

    Same resolver shape as every other cache-dir artifact in this codebase
    (:func:`athenaeum.decay_sweep.sweep_ledger_path`,
    :func:`athenaeum.push_metrics.push_records_path`): ``arg >
    ATHENAEUM_CACHE_DIR env > ~/.cache/athenaeum``.
    """
    return resolve_cache_dir(cache_dir) / ERASURE_KEY_FILENAME


def load_or_create_erasure_key(cache_dir: Path | None = None) -> bytes:
    """Load the per-corpus HMAC key, generating and persisting one if absent.

    A 32-byte (256-bit) cryptographically random key
    (:func:`os.urandom`), persisted as its hex encoding via
    :func:`athenaeum.atomic_io.atomic_write_text` (never a plain
    ``Path.write_text`` — this is a fail-closed credential, not ordinary
    content) with ``0600`` permissions best-effort. Idempotent: a second
    call with the same *cache_dir* returns the SAME key, read back off disk.

    Raises :class:`ErasureKeyError` when the file exists but does not decode
    as the expected hex shape — a corrupt key must never be silently
    replaced (that would orphan every hash already computed with it, which
    is exactly what :func:`purge_erasure_key` is for, deliberately) or
    silently bypassed (that would fall back toward an unkeyed/plain hash).
    """
    path = resolve_erasure_key_path(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raw = None
    except OSError as exc:
        raise ErasureKeyError(f"erasure key at {path} is unreadable: {exc}") from exc

    if raw is not None:
        if not _HEX_KEY_RE.match(raw):
            raise ErasureKeyError(
                f"erasure key at {path} is corrupt (expected "
                f"{_ERASURE_KEY_BYTES * 2} hex chars); purge it explicitly "
                "with purge_erasure_key() before a new one can be generated"
            )
        return bytes.fromhex(raw)

    key = os.urandom(_ERASURE_KEY_BYTES)
    atomic_write_text(path, key.hex())
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform-dependent (e.g. some CI containers)
        log.debug("erasure: could not chmod 0600 on key file %s", path)
    return key


def purge_erasure_key(cache_dir: Path | None = None) -> bool:
    """Delete the per-corpus HMAC key. Returns ``True`` iff a key was deleted.

    This is the whole point of AC1: every hash previously computed with the
    purged key is now permanently unlinkable from the content that produced
    it — the content hash stays in whatever ledger recorded it (that ledger
    entry's own "that-and-why, never what" guarantee, AC8, was never the
    hash's job), but nobody can any longer take a candidate string, hash it
    with the (now-gone) key, and match it against that stored hash. A
    subsequent :func:`load_or_create_erasure_key` call generates a brand-new,
    unrelated key — this function performs no rotation bookkeeping and keeps
    none: purge is deliberately total, not a rotation with an old-key
    grace window (a grace window would keep the exact linkability this
    function exists to destroy).
    """
    path = resolve_erasure_key_path(cache_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def erasure_content_hash(text: str, *, key: bytes) -> str:
    """HMAC-SHA256 keyed hash of *text* — the ONLY hash an erasure-class
    claim's content may ever be reduced to for ledger/breadcrumb storage.

    Never a plain ``hashlib.sha256(text)`` / ``hashlib.sha1(text)`` /
    ``hashlib.md5(text)`` — see the module docstring's AC1 rationale. Two
    different keys over the SAME text produce two DIFFERENT, uncorrelatable
    hashes (verified by ``tests/test_erasure.py``'s
    ``test_different_keys_produce_different_hashes``) — the property that
    makes :func:`purge_erasure_key` meaningful: once the key that produced a
    stored hash is gone, no plain re-hash of a candidate string, with any
    key, can ever reproduce it again.
    """
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# AC2 — opaque, uid-based person-entity slugs and pair ids
# ---------------------------------------------------------------------------

#: The shape :func:`athenaeum.models.generate_uid` produces (8-char lowercase
#: hex from ``uuid4().hex[:8]``) — the SAME opaque identity primitive the
#: rest of the codebase already uses (:class:`athenaeum.models.EntityIndex`,
#: :func:`athenaeum.pii.uid_on_record`), not a new id scheme. This module
#: only adds NAME-FREE renderings of it for erasure-class ledgers/filenames;
#: it does not mint uids itself.
_UID_RE = re.compile(r"^[0-9a-f]{8}$")


class OpaqueIdentityError(ValueError):
    """Raised when a value handed to an opaque-identity builder is not a uid.

    Loud by design: silently accepting an arbitrary string (which could be a
    slugified NAME) here would defeat AC2's entire purpose.
    """


def _require_uid(uid: str) -> str:
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise OpaqueIdentityError(
            "uid must be an 8-char lowercase-hex id "
            "(athenaeum.models.generate_uid() shape); got "
            f"{uid!r} — erasure-class identity references must never be "
            "name-derived (AC2)"
        )
    return uid


def opaque_person_slug(uid: str) -> str:
    """Return a name-free identity reference for an erasure-class person.

    Deliberately narrower than the ordinary wiki filename convention
    (``f"{uid}-{slugify(name)}.md"``, :meth:`athenaeum.models.EntityRecord.filename`) —
    that convention embeds the display name for human readability, which is
    exactly what an erasure-class reference must never do. Any ledger,
    breadcrumb, or off-corpus key this module (or a future consumer)
    constructs for an erasure-class person MUST use this instead.
    """
    return f"person-{_require_uid(uid)}"


def opaque_pair_id(uid_a: str, uid_b: str) -> str:
    """Return an order-independent, name-free id for a pair of erasure-class entities.

    Sorts the two uids before joining, so ``opaque_pair_id(a, b) ==
    opaque_pair_id(b, a)`` — the same order-independence
    :func:`athenaeum.fingerprint.claim_pair_fingerprint` already establishes
    for claim-pair hashing, applied here to entity-pair identity instead.
    """
    a, b = sorted((_require_uid(uid_a), _require_uid(uid_b)))
    return f"pair-{a}-{b}"


# ---------------------------------------------------------------------------
# AC9 — retention policy packs as data (keyed on memory_class/data_class/jurisdiction)
# ---------------------------------------------------------------------------

#: The closed action vocabulary a retention rule may name (issue athenaeum#985's
#: own AC9 wording, verbatim).
RETENTION_ACTIONS: frozenset[str] = frozenset(
    {"refuse-write", "store-off-corpus", "demote-cold", "delete-after", "retain-until"}
)

#: The two actions whose meaning REQUIRES a ``period`` — the other three
#: (``refuse-write``, ``store-off-corpus``, ``demote-cold``) are unconditional.
_PERIOD_REQUIRED_ACTIONS: frozenset[str] = frozenset({"delete-after", "retain-until"})

#: The normalized token for "no jurisdiction recorded" — never a pack key
#: (see :func:`classify_retention`'s docstring for why).
UNKNOWN_JURISDICTION = "unknown"

#: AC3's hard, pack-independent conservative default. No retention pack,
#: built-in or operator-authored, may loosen this: it is enforced in
#: :func:`classify_retention` BEFORE any pack lookup runs, never read out of
#: a pack's own table (see that function's docstring).
UNKNOWN_JURISDICTION_ACTION = "store-off-corpus"

#: The two action names that mean "this content is erasure-class" (AC3's
#: "misclassifying toward the strict side is recoverable" reads: content the
#: system either refuses to keep in git-versioned form at all, or keeps only
#: off-corpus where a true delete is possible — see
#: `docs/extending/whole-store-adapter-design.md` §4.5, "git is a capability... git's
#: durability is precisely what makes erasure impossible").
ERASURE_CLASS_ACTIONS: frozenset[str] = frozenset({"refuse-write", "store-off-corpus"})

#: The two packaged pack names shipped under ``src/athenaeum/retention_packs/``.
PACKAGED_RETENTION_PACK_NAMES: tuple[str, ...] = ("us-default", "eu-gdpr")


class RetentionPackError(ValueError):
    """Raised when a retention pack (built-in or operator-authored) is malformed.

    Loud by design, matching :class:`athenaeum.sensitivity.SensitivityConfigError`
    / :class:`athenaeum.storage.StorageConfigError`: a bad pack must never
    silently resolve to "no rule" — that would be a classification gap with
    a data-protection consequence, not a cosmetic config typo.
    """


@dataclass(frozen=True)
class RetentionRule:
    """One ``(memory_class, data_class, jurisdiction) -> action`` mapping.

    ``period`` is an opaque string (e.g. ``"P7Y"``, ``"P1Y"`` — ISO-8601
    duration shorthand, never parsed here) carried through for a future
    enforcement slice to interpret; this issue ships the classification and
    the data shape, not execution against live data.
    """

    memory_class: str
    data_class: str
    jurisdiction: str
    action: str
    period: str | None = None

    def __post_init__(self) -> None:
        if self.action not in RETENTION_ACTIONS:
            raise RetentionPackError(
                f"retention rule action {self.action!r} must be one of {sorted(RETENTION_ACTIONS)}"
            )
        if self.action in _PERIOD_REQUIRED_ACTIONS and not self.period:
            raise RetentionPackError(f"retention rule action {self.action!r} requires a period")

    def is_erasure_class(self) -> bool:
        """``True`` iff this rule's action means the content is erasure-class."""
        return self.action in ERASURE_CLASS_ACTIONS


@dataclass(frozen=True)
class RetentionPack:
    """A named, ordered set of retention rules plus a fallback default.

    ``default_action``/``default_period`` govern any ``(memory_class,
    data_class, jurisdiction)`` triple the pack's own ``rules`` do not name —
    a pack need not enumerate every triple. This is genuinely different from
    AC3's hard-coded unknown-jurisdiction default
    (:data:`UNKNOWN_JURISDICTION_ACTION`): the pack default applies to a
    KNOWN jurisdiction the pack's table simply hasn't covered; the AC3
    default applies to an UNKNOWN jurisdiction and is never routed through a
    pack's default at all (see :func:`classify_retention`).
    """

    name: str
    default_action: str
    rules: tuple[RetentionRule, ...] = ()
    default_period: str | None = None

    def __post_init__(self) -> None:
        if self.default_action not in RETENTION_ACTIONS:
            raise RetentionPackError(
                f"retention pack {self.name!r}: default_action "
                f"{self.default_action!r} must be one of {sorted(RETENTION_ACTIONS)}"
            )
        if self.default_action in _PERIOD_REQUIRED_ACTIONS and not self.default_period:
            raise RetentionPackError(
                f"retention pack {self.name!r}: default_action "
                f"{self.default_action!r} requires default_period"
            )

    def lookup(self, *, memory_class: str, data_class: str, jurisdiction: str) -> RetentionRule:
        """The governing rule for one triple: an exact match, else this pack's default.

        *jurisdiction* here is always a KNOWN jurisdiction — callers route
        :data:`UNKNOWN_JURISDICTION` through :func:`classify_retention`'s
        hard-coded AC3 default instead of reaching this method at all.
        """
        for rule in self.rules:
            if (rule.memory_class, rule.data_class, rule.jurisdiction) == (
                memory_class,
                data_class,
                jurisdiction,
            ):
                return rule
        return RetentionRule(
            memory_class=memory_class,
            data_class=data_class,
            jurisdiction=jurisdiction,
            action=self.default_action,
            period=self.default_period,
        )


def _pack_from_dict(name: str, data: dict[str, Any]) -> RetentionPack:
    if not isinstance(data, dict):
        raise RetentionPackError(f"retention pack {name!r} must be a mapping")
    default_action = data.get("default_action")
    if not isinstance(default_action, str):
        raise RetentionPackError(f"retention pack {name!r} is missing a string default_action")
    default_period = data.get("default_period")
    if default_period is not None and not isinstance(default_period, str):
        raise RetentionPackError(f"retention pack {name!r}: default_period must be a string")

    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise RetentionPackError(f"retention pack {name!r}: rules must be a list")
    rules: list[RetentionRule] = []
    for i, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise RetentionPackError(f"retention pack {name!r}: rules[{i}] must be a mapping")
        try:
            rules.append(
                RetentionRule(
                    memory_class=str(raw["memory_class"]),
                    data_class=str(raw["data_class"]),
                    jurisdiction=str(raw["jurisdiction"]),
                    action=str(raw["action"]),
                    period=raw.get("period"),
                )
            )
        except KeyError as exc:
            raise RetentionPackError(
                f"retention pack {name!r}: rules[{i}] missing required key {exc}"
            ) from exc

    return RetentionPack(
        name=name,
        default_action=default_action,
        rules=tuple(rules),
        default_period=default_period,
    )


def _load_packaged_pack(name: str) -> dict[str, Any]:
    """Read one packaged pack YAML file via ``importlib.resources`` (never a Path literal)."""
    resource = importlib.resources.files("athenaeum.retention_packs").joinpath(f"{name}.yaml")
    raw = resource.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw) or {}
    if not isinstance(loaded, dict):
        raise RetentionPackError(f"packaged retention pack {name!r} did not parse to a mapping")
    return loaded


def available_retention_packs(config: dict[str, Any] | None) -> dict[str, RetentionPack]:
    """Every retention pack available to this config, keyed by name.

    Precedence: the two packaged packs
    (:data:`PACKAGED_RETENTION_PACK_NAMES`), then
    ``erasure.retention_packs.<name>`` operator entries
    (:func:`athenaeum.config.resolve_retention_pack_overrides`) — an operator
    entry reusing a packaged name OVERRIDES it wholesale (no field-level
    merge), the same "config wins, wholesale" rule
    :func:`athenaeum.sensitivity.available_classes` already uses for
    sensitivity classes. Raises :class:`RetentionPackError` at build time for
    any malformed pack (built-in or operator-authored) — never a silent
    fallback to "no rule."
    """
    packs: dict[str, RetentionPack] = {}
    for name in PACKAGED_RETENTION_PACK_NAMES:
        packs[name] = _pack_from_dict(name, _load_packaged_pack(name))
    for name, raw in resolve_retention_pack_overrides(config).items():
        packs[name] = _pack_from_dict(name, raw)
    return packs


def resolve_active_retention_pack(config: dict[str, Any] | None) -> RetentionPack:
    """The single active :class:`RetentionPack` for this config.

    Selected by :func:`athenaeum.config.resolve_retention_pack_selection`
    (default ``"us-default"``). Raises :class:`RetentionPackError` when the
    selected name is not among :func:`available_retention_packs`'s result —
    a typo'd selection must never silently fall back to a different pack's
    rules.
    """
    selected = resolve_retention_pack_selection(config)
    packs = available_retention_packs(config)
    if selected not in packs:
        raise RetentionPackError(
            f"erasure.retention_pack {selected!r} is not a known pack; known packs: {sorted(packs)}"
        )
    return packs[selected]


def classify_retention(
    *,
    memory_class: str,
    data_class: str,
    jurisdiction: str | None,
    pack: RetentionPack,
) -> RetentionRule:
    """The governing retention rule for one write, AC3 enforced ahead of any pack lookup.

    *jurisdiction* is normalized (stripped, lower-cased); ``None``/empty/
    whitespace-only all collapse to :data:`UNKNOWN_JURISDICTION`. When the
    normalized jurisdiction is unknown, this function returns
    :data:`UNKNOWN_JURISDICTION_ACTION` directly — it never calls
    ``pack.lookup`` for that case, so a pack cannot loosen the conservative
    default by defining (or failing to define) a ``jurisdiction: unknown``
    row of its own; neither packaged pack defines one, for the same reason
    (see each pack YAML's header comment).

    A known jurisdiction is routed through ``pack.lookup`` — an exact
    ``(memory_class, data_class, jurisdiction)`` match if the pack has one,
    else the pack's own ``default_action``.
    """
    jurisdiction_norm = (jurisdiction or "").strip().lower() or UNKNOWN_JURISDICTION
    if jurisdiction_norm == UNKNOWN_JURISDICTION:
        return RetentionRule(
            memory_class=memory_class,
            data_class=data_class,
            jurisdiction=UNKNOWN_JURISDICTION,
            action=UNKNOWN_JURISDICTION_ACTION,
        )
    return pack.lookup(
        memory_class=memory_class, data_class=data_class, jurisdiction=jurisdiction_norm
    )


def reconcile_bucket_daily_with_pack(
    *, memory_class: str, data_class: str, pack: RetentionPack
) -> RetentionRule:
    """What ``bucket: daily`` COMPILES TO under *pack*, per `docs/design/provenance-shape.md` §8.8.

    That doc's §8.8 commits to a specific, already-published mapping:
    ``bucket: daily`` is v0 shorthand for a ``delete-after <period>`` rule
    keyed by ``(memory_class, data_class)`` — once a policy pack exists for
    that key, THAT rule becomes authoritative for expiry/deletion, and
    ``bucket:`` stays as write-time sugar rather than a second, competing
    expiry mechanism. This function computes what that authoritative rule
    IS for a given pack (looked up with jurisdiction always
    :data:`UNKNOWN_JURISDICTION`, since ``bucket:`` carries no jurisdiction
    signal of its own — see the note below).

    **Wired into** :mod:`athenaeum.decay_sweep` **by issue athenaeum#1116.** This
    athenaeum#985 PR shipped the mapping COMPUTABLE and TESTED but did not wire
    it — ``decay_sweep.py`` imported nothing from this module then. athenaeum#1116
    is that wiring slice; see :func:`athenaeum.decay_sweep.build_sweep_report`.

    Because ``bucket:``-driven expiry never carried a jurisdiction, this
    reconciliation always evaluates at :data:`UNKNOWN_JURISDICTION`, which
    means it always resolves to :data:`UNKNOWN_JURISDICTION_ACTION`
    (``store-off-corpus``) rather than a ``delete-after`` rule from the pack
    table — a real behavior difference from a plain git-archival sweep, which
    is why athenaeum#1116's wiring gates this function's call on a page's
    frontmatter carrying an explicit ``data_class`` (no shipped write path
    stamps one, so the gate does not fire on any corpus produced by shipped
    code as of that issue) rather than calling it for every ``bucket: daily``
    page unconditionally — the latter would silently stop archiving EVERY
    daily-bucket page (PII or not) the moment any pack is active, which is
    always true by default. A period-bearing pack action
    (``delete-after``/``retain-until``) is deliberately NOT enforced by that
    wiring either — this function's own docstring above already establishes
    that ``period`` is opaque and unparsed; date arithmetic against it is a
    still-later slice's job, not this one's.
    """
    return classify_retention(
        memory_class=memory_class,
        data_class=data_class,
        jurisdiction=None,
        pack=pack,
    )


# ---------------------------------------------------------------------------
# AC4 — taint rule 1: derivation (## Inference blocks)
# ---------------------------------------------------------------------------


def classify_inference_taint(text: str, *, erasure_class_slugs: Collection[str]) -> list[Any]:
    """The ``## Inference`` blocks in *text* whose basis cites erasure-class content.

    A paraphrase in git is the same leak as a quote: any block whose
    ``**Basis**:`` wikilinks name a page in *erasure_class_slugs* is itself
    erasure-class, regardless of what its own prose says. Both sides are
    compared through :func:`athenaeum.models.slugify` so a basis wikilink's
    raw text (``[[Fact A]]``) matches a page slug (``fact-a``) the same way
    the rest of the codebase already normalizes wiki references.

    Returns the tainted subset of :class:`athenaeum.inference_blocks.InferenceBlock`
    (empty when none are tainted, or when *text* has no ``## Inference``
    blocks at all) — callers decide what "erasure-class" means for the page
    as a whole (e.g. "at least one tainted block" vs. "every block").
    """
    from athenaeum.inference_blocks import parse_inference_blocks

    normalized_erasure = {slugify(s) for s in erasure_class_slugs}
    if not normalized_erasure:
        return []
    tainted = []
    for block in parse_inference_blocks(text):
        if any(slugify(basis) in normalized_erasure for basis in block.basis):
            tainted.append(block)
    return tainted


# ---------------------------------------------------------------------------
# AC5 — taint rule 2: re-ingestion (classify by provenance, never re-guess)
# ---------------------------------------------------------------------------

#: The ``SourceRef.type`` a re-ingested off-corpus recall is written with.
#: Deliberately a NEW value inside the free-form ``<type>:<ref>`` scalar
#: namespace (:mod:`athenaeum.provenance`'s ``_SCALAR_RE`` accepts any
#: ``[a-z][a-z0-9_-]*`` type) rather than an addition to the CLOSED
#: ``source_type`` channel vocabulary (``models.SOURCE_TYPES`` —
#: `docs/design/provenance-shape.md` §10.1). That vocabulary carries an explicit
#: cross-file lock discipline (resolver prompt + golden snapshot + the
#: conflict-resolution doc all have to move together for any change to it),
#: which this issue's taint rule has no need to touch — a source `type` is
#: exactly the right-sized, low-blast-radius place for this.
OFF_CORPUS_RECALL_SOURCE_TYPE = "recall-offcorpus"


def off_corpus_recall_source(ref: str) -> str:
    """Build the ``source:`` scalar a re-ingested off-corpus recall should carry.

    A write path that re-ingests content whose basis was an off-corpus
    recall (rather than fresh user input) should attribute it with this
    scalar's shape (``"recall-offcorpus:<ref>"``) so
    :func:`classify_by_provenance` recognizes it on the next round trip —
    "the system knows where it came from; it must not re-guess" (AC5).
    *ref* should be an opaque reference to the recalled record (e.g.
    :func:`opaque_person_slug`'s output, or a pair id) — never the content
    itself, and never a bare name.
    """
    return f"{OFF_CORPUS_RECALL_SOURCE_TYPE}:{ref}"


def classify_by_provenance(source: str | SourceRef | None) -> bool:
    """``True`` iff *source* traces to an off-corpus recall (taint rule 2, AC5).

    Accepts the same shapes :func:`athenaeum.provenance.parse_source` does —
    a raw ``"<type>:<ref>"`` scalar, an already-parsed :class:`SourceRef`, or
    ``None`` (returns ``False``: no provenance recorded is not itself a
    taint signal). This function never inspects the CONTENT the source is
    attached to — that is the entire point of a provenance-keyed taint rule
    over a content-keyed one: a paraphrase of off-corpus content might not
    trip any sensitivity recogniser, but its `source:` still says where it
    came from.
    """
    if source is None:
        return False
    ref = source if isinstance(source, SourceRef) else parse_source(source)
    if ref is None:
        return False
    return ref.type == OFF_CORPUS_RECALL_SOURCE_TYPE


# ---------------------------------------------------------------------------
# AC6 — taint rule 3: push is egress (honest disclosure, carried in the ledger)
# ---------------------------------------------------------------------------

#: The honest guarantee this module (and `docs/design/security-posture.md`) states
#: about erasure's actual reach. Embedded verbatim in every
#: :class:`RedactionLedgerRecord`'s :meth:`~RedactionLedgerRecord.to_dict`
#: output (AC6: "record that disclosure in the redaction ledger's output"),
#: and in `docs/design/security-posture.md`'s "Erasure egress disclosure" section —
#: ONE string, not two independently-drifting copies (the doc section quotes
#: this constant rather than restating it).
EGRESS_DISCLOSURE = (
    "Erasure is a single-store delete of every copy the system controls "
    "(corpus + off-corpus + the HMAC-keyed hash pointers this module writes), "
    "PLUS enumerable-but-unreachable copies in session transcripts and "
    "downstream agent outputs. Recall into a session is an egress event; "
    "the erasure cascade cannot reach a session transcript or a cache built "
    "from one — that is a disclosed gap, not a silent one. See "
    "docs/design/security-posture.md, 'Erasure egress disclosure'."
)


# ---------------------------------------------------------------------------
# AC7 — named remediation path (documented; this builds the ledger entry, not
# the history rewrite itself)
# ---------------------------------------------------------------------------

#: Reason code for a redaction-ledger entry recording that the last-resort
#: in-git history-rewrite remediation (`docs/design/security-posture.md`, "Erasure
#: remediation: misclassified in-git content") was invoked for one piece of
#: misclassified content.
HISTORY_REWRITE_REMEDIATION_REASON = "history-rewrite-remediation"


# ---------------------------------------------------------------------------
# AC8 — the redaction ledger: that-and-why, never what
# ---------------------------------------------------------------------------

#: Filename under the cache dir — same durable-JSONL-outside-the-corpus
#: convention as :data:`athenaeum.decay_sweep.SWEEP_LEDGER_FILENAME` /
#: :data:`athenaeum.push_metrics.PUSH_RECORDS_FILENAME`.
REDACTION_LEDGER_FILENAME = "_redaction_ledger.jsonl"

REDACTION_LEDGER_SCHEMA_VERSION = 1

#: The closed vocabulary a redaction-ledger entry's ``reason_code`` may take.
#: Closed (not free text) for the same reason ``action`` is closed: a
#: free-text reason field is exactly where an operator/agent would be
#: tempted to paste the redacted content "for context" — the failure mode
#: AC8 exists to structurally prevent.
REDACTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "erasure-demand",
        "misclassification-correction",
        "jurisdiction-update",
        HISTORY_REWRITE_REMEDIATION_REASON,
    }
)


class RedactionLedgerError(ValueError):
    """Raised when a :class:`RedactionLedgerRecord` is built with an invalid field."""


@dataclass(frozen=True)
class RedactionLedgerRecord:
    """One redaction-ledger row: THAT something was redacted/erased, and WHY — never WHAT.

    This is a STRUCTURAL guarantee, not a convention a caller could violate
    by passing the wrong string: there is no content/free-text field on this
    dataclass at all. ``subject_ref`` is an opaque reference
    (:func:`opaque_person_slug` / :func:`opaque_pair_id` output — never a
    name); ``content_hash`` is an :func:`erasure_content_hash` output (HMAC-
    keyed, AC1 — never a plain hash, and never the content itself).
    """

    record_id: str
    ts: str
    reason_code: str
    subject_ref: str
    data_class: str
    memory_class: str
    content_hash: str
    action_taken: str

    def __post_init__(self) -> None:
        if self.reason_code not in REDACTION_REASON_CODES:
            raise RedactionLedgerError(
                f"reason_code {self.reason_code!r} must be one of {sorted(REDACTION_REASON_CODES)}"
            )
        if self.action_taken not in RETENTION_ACTIONS:
            raise RedactionLedgerError(
                f"action_taken {self.action_taken!r} must be one of {sorted(RETENTION_ACTIONS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape. Carries :data:`EGRESS_DISCLOSURE` (AC6) in every row."""
        return {
            "v": REDACTION_LEDGER_SCHEMA_VERSION,
            "record_id": self.record_id,
            "ts": self.ts,
            "reason_code": self.reason_code,
            "subject_ref": self.subject_ref,
            "data_class": self.data_class,
            "memory_class": self.memory_class,
            "content_hash": self.content_hash,
            "action_taken": self.action_taken,
            "egress_guarantee": EGRESS_DISCLOSURE,
        }


def _make_record_id(subject_ref: str, ts: str) -> str:
    payload = f"{subject_ref}:{ts}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def build_redaction_record(
    *,
    reason_code: str,
    subject_ref: str,
    data_class: str,
    memory_class: str,
    content_hash: str,
    action_taken: str,
    ts: str | None = None,
) -> RedactionLedgerRecord:
    """Build one :class:`RedactionLedgerRecord`, stamping ``record_id``/``ts``."""
    stamp = ts or now_iso()
    return RedactionLedgerRecord(
        record_id=_make_record_id(subject_ref, stamp),
        ts=stamp,
        reason_code=reason_code,
        subject_ref=subject_ref,
        data_class=data_class,
        memory_class=memory_class,
        content_hash=content_hash,
        action_taken=action_taken,
    )


def build_history_rewrite_remediation_record(
    *,
    subject_ref: str,
    data_class: str,
    memory_class: str,
    content_hash: str,
    ts: str | None = None,
) -> RedactionLedgerRecord:
    """Build (never execute) the ledger entry for the last-resort history-rewrite remediation.

    AC7: "Documented, not implemented — but documented precisely enough to
    execute." The protocol itself — forced re-clone of every machine, ledger
    re-anchor — is documented in `docs/design/security-posture.md` ("Erasure
    remediation: misclassified in-git content"); this function builds the
    redaction-ledger entry that protocol requires, with ``action_taken``
    fixed to ``"refuse-write"`` — the remediation's outcome is that the
    misclassified content stops being writable in-git going forward, which
    is the strictest action this vocabulary has, matching the "last resort"
    framing.
    """
    return build_redaction_record(
        reason_code=HISTORY_REWRITE_REMEDIATION_REASON,
        subject_ref=subject_ref,
        data_class=data_class,
        memory_class=memory_class,
        content_hash=content_hash,
        action_taken="refuse-write",
        ts=ts,
    )


def redaction_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolve the redaction-ledger path: ``<cache_dir>/_redaction_ledger.jsonl``."""
    return resolve_cache_dir(cache_dir) / REDACTION_LEDGER_FILENAME


def append_redaction_ledger(
    records: Collection[RedactionLedgerRecord], *, cache_dir: Path | None = None
) -> None:
    """Append *records* to the durable redaction ledger, via
    :func:`athenaeum.store.append_line_durable` (``O_APPEND`` + ``fsync``,
    the same single shared primitive every other ledger in this codebase
    uses — `docs/extending/whole-store-adapter-design.md` §2.4/§6.2). Raises on
    failure, matching :func:`athenaeum.decay_sweep.write_sweep_ledger`'s
    posture rather than the best-effort ``record_merge_provenance``/
    ``record_push`` one: this ledger sits upstream of a redaction/erasure
    decision, and "zero destructive operations without a ledger entry" is
    only enforceable if a write failure propagates to the caller instead of
    being logged and swallowed.
    """
    if not records:
        return
    path = redaction_ledger_path(cache_dir)
    lines = "".join(json.dumps(rec.to_dict(), separators=(",", ":")) + "\n" for rec in records)
    append_line_durable(path, lines.encode("utf-8"))


def read_redaction_ledger(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every redaction-ledger record. Tolerates a torn trailing line; never raises."""
    path = redaction_ledger_path(cache_dir)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


__all__ = [
    # AC1
    "ErasureKeyError",
    "resolve_erasure_key_path",
    "load_or_create_erasure_key",
    "purge_erasure_key",
    "erasure_content_hash",
    # AC2
    "OpaqueIdentityError",
    "opaque_person_slug",
    "opaque_pair_id",
    # AC9 (+ AC3's enforcement point)
    "RETENTION_ACTIONS",
    "UNKNOWN_JURISDICTION",
    "UNKNOWN_JURISDICTION_ACTION",
    "ERASURE_CLASS_ACTIONS",
    "PACKAGED_RETENTION_PACK_NAMES",
    "RetentionPackError",
    "RetentionRule",
    "RetentionPack",
    "available_retention_packs",
    "resolve_active_retention_pack",
    "classify_retention",
    "reconcile_bucket_daily_with_pack",
    # AC4
    "classify_inference_taint",
    # AC5
    "OFF_CORPUS_RECALL_SOURCE_TYPE",
    "off_corpus_recall_source",
    "classify_by_provenance",
    # AC6
    "EGRESS_DISCLOSURE",
    # AC7
    "HISTORY_REWRITE_REMEDIATION_REASON",
    "build_history_rewrite_remediation_record",
    # AC8
    "REDACTION_LEDGER_FILENAME",
    "REDACTION_LEDGER_SCHEMA_VERSION",
    "REDACTION_REASON_CODES",
    "RedactionLedgerError",
    "RedactionLedgerRecord",
    "build_redaction_record",
    "redaction_ledger_path",
    "append_redaction_ledger",
    "read_redaction_ledger",
]
