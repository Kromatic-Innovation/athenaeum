# SPDX-License-Identifier: Apache-2.0
"""``storage.mapping`` completeness lint + the deferred `(read_policy, adapter)`
pair check (issue athenaeum#993 — slice S5 of
``docs/sensitivity-class-vocabulary.md`` §9).

Implements the two checks that design note deliberately defers rather than
enforcing inside the resolver itself:

1. **``storage.mapping`` completeness** (design note §6 point 2). Every
   sensitivity class name a scanned corpus's content still carries should
   have a live ``storage.mapping`` entry resolving to an existing adapter
   (:func:`athenaeum.storage.available_adapters`). A class name with NO
   ``storage.mapping`` entry falls through, at read time, to the default
   ``wiki-markdown-embedded`` surface — silently: this is the exact footgun
   the design note names ("removing the matching ``storage.mapping`` entry
   while old content still carries that class name is what an operator must
   not do"). A class name mapped to an adapter name absent from
   :func:`~athenaeum.storage.available_adapters` already raises
   :class:`athenaeum.storage.StorageConfigError` at
   :func:`athenaeum.storage.resolve_adapter_for_class`'s READ time; this
   lint catches both cases at CONFIG-CHANGE time instead, before either one
   happens on an operator's box.
2. **The deferred `(read_policy, storage adapter)` pair check** (design note
   §7 Decision D4). A class whose resolved ``read_policy.access`` is
   ``confidential`` or ``personal`` but whose mapped adapter's
   ``corpus_policy.embedded`` is ``True`` is reported: the read-policy layer
   believes this class is restricted while the storage layer routes it into
   the ordinary embedded corpus. Decision D4 explicitly declined to enforce
   this as a floor inside the resolver ("a false sense of security on the
   layer that matters less") and named exactly this lint as the honest place
   for it instead.

**What this lint does NOT guarantee.** It reports on the sensitivity-class
names present in the scanned corpus tree AT SCAN TIME — it is not a proof
that a mapping is complete for content it has not seen. A class that exists
only in ``sensitivity.classes`` with no content written under it yet is
invisible to the completeness check (nothing to scan); a live knowledge store
this lint has never been pointed at is simply unaudited. It never runs
against a hardcoded or environment-derived path — every call site supplies
both *config* and *corpus_root* explicitly (see
:func:`lint_storage_mapping_completeness`). It never rewrites anything: both
checks are read-only over *config* and the corpus tree — see
``tests/test_sensitivity_lint.py``'s ``TestReadOnly`` for the byte-identical-
before-and-after assertion. No enforcement is added to
:func:`athenaeum.sensitivity.available_classes` or
:func:`athenaeum.storage.resolve_adapter_for_class` by this module — both are
only ever called, never modified, from here.

**The content-scanning convention.** Neither :mod:`athenaeum.storage` nor
:mod:`athenaeum.sensitivity` currently writes anything that marks a compiled
page with the sensitivity class its content was classified under —
:func:`athenaeum.sensitivity.classify` is a pure in-memory detector with no
write path, and none of athenaeum#992's (S3) migrated callers persist a
classification result onto frontmatter either. This lint therefore defines
the one explicit, dedicated signal it reads: a page's own
``sensitivity_class:`` frontmatter field
(:data:`SENSITIVITY_CLASS_FRONTMATTER_FIELD`), read via
:func:`athenaeum.models.parse_frontmatter`. This is deliberately NOT the
general entity ``type:`` field — most entity classes (``person``, ``vendor``,
``concept``, …) never have, and never need, a ``storage.mapping`` entry;
treating every scanned ``type:`` value as a completeness candidate would flag
the ordinary, correct default-routing case as a false positive on nearly
every corpus. A future writer that stamps sensitivity classification onto
compiled content (out of scope here) should reuse this same field name so
this lint requires no change to pick it up.

Layering: L4 (cross-cutting operator tooling), sibling to
:mod:`athenaeum.storage_migrate`. Imports :mod:`athenaeum.config` (L2),
:mod:`athenaeum.models` (L1), :mod:`athenaeum.pii` (L3, for
:func:`~athenaeum.pii.iter_corpus_files` — the same corpus walker
``storage lint-pii`` already uses), :mod:`athenaeum.sensitivity` (L3) and
:mod:`athenaeum.storage` (L1). None of those import this module, so this adds
no cycle (``tests/test_import_graph_acyclic.py``'s zero-tolerance SCC guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_storage_mapping
from athenaeum.models import parse_frontmatter
from athenaeum.pii import iter_corpus_files
from athenaeum.sensitivity import available_classes
from athenaeum.storage import available_adapters, resolve_adapter_for_class

#: The frontmatter field this lint reads to learn which sensitivity class a
#: scanned page carries. See the module docstring's "content-scanning
#: convention" for why this is a dedicated field rather than the general
#: entity ``type:`` field.
SENSITIVITY_CLASS_FRONTMATTER_FIELD = "sensitivity_class"

#: Finding kinds the completeness check reports — kept distinct per this
#: issue's own AC ("reports it, distinctly from the first case").
FINDING_MISSING_MAPPING = "missing_mapping"
FINDING_DANGLING_ADAPTER = "dangling_adapter"
#: The Decision D4 pair-check finding kind — advisory, never gates the CLI's
#: exit code (see ``athenaeum._cmd_storage``'s ``lint-mapping`` subcommand).
FINDING_POLICY_MISMATCH = "policy_mismatch"

#: Access levels the D4 pair check treats as "restricted" — this lint's own
#: reading of design note §7 Decision D4 ("read_policy.access is
#: `confidential` or `personal`"), not a new vocabulary of its own.
_RESTRICTED_ACCESS_LEVELS = frozenset({"confidential", "personal"})


@dataclass(frozen=True)
class MappingFinding:
    """One lint finding — either a completeness gap or a D4 policy mismatch.

    ``advisory`` is True only for :data:`FINDING_POLICY_MISMATCH` — Decision
    D4's pair check is deliberately non-blocking (this issue's own AC: "an
    operator can act on a genuine broken mapping without being blocked by a
    policy-shape opinion"). ``paths`` is the sorted set of scanned files that
    carried *class_name* — always empty for a D4 finding, since that check is
    config-only and never scans the corpus.
    """

    kind: str
    class_name: str
    detail: str
    paths: tuple[Path, ...] = ()

    @property
    def advisory(self) -> bool:
        return self.kind == FINDING_POLICY_MISMATCH


def scan_sensitivity_class_names(corpus_root: Path) -> dict[str, list[Path]]:
    """Collect every distinct ``sensitivity_class:`` value under *corpus_root*.

    Walks *corpus_root* with :func:`athenaeum.pii.iter_corpus_files` (every
    regular file, recursively, `_`-prefixed and non-``*.md`` included — a
    custom/excluded surface is not necessarily top-level-only markdown). A
    file that is not readable UTF-8 text, carries no frontmatter, or has the
    field absent/blank is silently skipped: this is a lint over what the
    corpus DOES declare, not a schema-validation pass. Returns each class
    name's matching paths sorted, for deterministic reporting.
    """
    found: dict[str, list[Path]] = {}
    for path in iter_corpus_files(corpus_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        raw = meta.get(SENSITIVITY_CLASS_FRONTMATTER_FIELD)
        if not isinstance(raw, str) or not raw.strip():
            continue
        found.setdefault(raw.strip(), []).append(path)
    return {name: sorted(paths) for name, paths in found.items()}


def lint_storage_mapping_completeness(
    config: dict[str, Any] | None, corpus_root: Path
) -> list[MappingFinding]:
    """The completeness check, over *corpus_root*'s own scanned content.

    For every sensitivity class name :func:`scan_sensitivity_class_names`
    finds: a name with no ``storage.mapping`` entry is reported as
    :data:`FINDING_MISSING_MAPPING`; a name whose entry names an adapter
    absent from :func:`athenaeum.storage.available_adapters` is reported as
    :data:`FINDING_DANGLING_ADAPTER`. *corpus_root* is always caller-supplied
    — this function never falls back to a hardcoded or environment-derived
    path. Findings are sorted by class name for deterministic output.
    """
    observed = scan_sensitivity_class_names(corpus_root)
    mapping = resolve_storage_mapping(config)
    adapters = available_adapters(config)

    findings: list[MappingFinding] = []
    for class_name in sorted(observed):
        paths = tuple(observed[class_name])
        if class_name not in mapping:
            findings.append(
                MappingFinding(
                    kind=FINDING_MISSING_MAPPING,
                    class_name=class_name,
                    detail=(
                        f"class {class_name!r} appears on {len(paths)} page(s) "
                        f"under {corpus_root} but has no storage.mapping entry "
                        "-- it falls through to the default wiki-markdown-"
                        "embedded surface at read time"
                    ),
                    paths=paths,
                )
            )
            continue
        adapter_name = mapping[class_name]
        if adapter_name not in adapters:
            findings.append(
                MappingFinding(
                    kind=FINDING_DANGLING_ADAPTER,
                    class_name=class_name,
                    detail=(
                        f"class {class_name!r} maps to unknown adapter "
                        f"{adapter_name!r}; known adapters: {sorted(adapters)}"
                    ),
                    paths=paths,
                )
            )
    return findings


def lint_read_policy_adapter_pairs(
    config: dict[str, Any] | None,
) -> list[MappingFinding]:
    """The deferred Decision D4 `(read_policy, storage adapter)` pair check.

    Config-only — never scans the corpus. For every class
    :func:`athenaeum.sensitivity.available_classes` resolves, reports
    :data:`FINDING_POLICY_MISMATCH` when its resolved ``read_policy.access``
    is ``confidential`` or ``personal`` but its mapped adapter's
    ``corpus_policy.embedded`` is ``True`` — the read-policy layer believes
    this class is restricted while storage routes it into the ordinary
    embedded corpus. Every finding here is :attr:`MappingFinding.advisory`.
    Findings are sorted by class name for deterministic output.
    """
    findings: list[MappingFinding] = []
    for name, sensitivity_class in sorted(available_classes(config).items()):
        access = sensitivity_class.read_policy.access
        if access not in _RESTRICTED_ACCESS_LEVELS:
            continue
        adapter = resolve_adapter_for_class(name, config)
        if adapter.corpus_policy.embedded:
            findings.append(
                MappingFinding(
                    kind=FINDING_POLICY_MISMATCH,
                    class_name=name,
                    detail=(
                        f"class {name!r} has read_policy.access={access!r} "
                        f"(restricted) but maps to adapter {adapter.name!r} "
                        "whose corpus_policy.embedded is True -- it is routed "
                        "into the ordinary embedded corpus despite its "
                        "restricted read policy"
                    ),
                )
            )
    return findings


@dataclass(frozen=True)
class SensitivityMappingLintResult:
    """Both checks' findings together, kept distinguishable (this issue's AC:
    "distinguishes the two finding kinds in its output... severity is
    separable from the completeness check").
    """

    completeness: tuple[MappingFinding, ...]
    policy: tuple[MappingFinding, ...]

    @property
    def findings(self) -> tuple[MappingFinding, ...]:
        """Every finding, completeness first, in scan/resolution order."""
        return self.completeness + self.policy

    @property
    def is_clean(self) -> bool:
        """True iff there are no BLOCKING findings.

        Mirrors ``athenaeum._cmd_storage``'s ``lint-pii`` "found something to
        act on" gate: only :data:`FINDING_MISSING_MAPPING` /
        :data:`FINDING_DANGLING_ADAPTER` block. A D4 policy-mismatch finding
        never does — it is advisory by design (see
        :attr:`MappingFinding.advisory`) — so a corpus with ONLY
        policy-mismatch findings is still :attr:`is_clean`.
        """
        return not self.completeness


def lint_sensitivity_storage_mapping(
    config: dict[str, Any] | None, corpus_root: Path
) -> SensitivityMappingLintResult:
    """This lint's single entry point — both checks over one (config, corpus_root).

    A stable, non-zero-vs-zero-findings return shape suitable for a CI gate:
    :attr:`SensitivityMappingLintResult.is_clean` is the boolean a caller
    gates on; ``athenaeum._cmd_storage``'s ``lint-mapping`` subcommand wraps
    this in the CLI's own process-exit-code convention.
    """
    return SensitivityMappingLintResult(
        completeness=tuple(lint_storage_mapping_completeness(config, corpus_root)),
        policy=tuple(lint_read_policy_adapter_pairs(config)),
    )
