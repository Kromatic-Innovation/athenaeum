# SPDX-License-Identifier: Apache-2.0
"""Relevance policy — operator-declared ``always`` / ``never`` capture rules.

Issue athenaeum#2643 (slice 1 of the relevance-policy epic athenaeum#2598): the
policy FILE, its loader, its validator and its read-back. Nothing here
enforces anything — later slices read :func:`load_relevance_policy` and gate
write paths on :meth:`RelevancePolicy.resolve`. This slice only makes the
policy exist, validate and be visible.

File: ``wiki/_schema/relevance-policy.md``, an operator-owned markdown file in
the same ``wiki/_schema/`` family as ``observation-filter.md`` / ``types.md`` /
``tags.md``. Unlike ``observation-filter.md`` it is NOT shipped by ``init``:
**a missing file is a complete no-op** (empty policy, no run-summary token).
Rules are one markdown table, read top to bottom::

    | Verdict | Type | Tag | Subject |
    |---------|------|-----|---------|
    | never   |      | personal | |
    | always  |      | personal/travel | |

* ``Verdict`` — ``always`` or ``never`` (capture), or ``route`` (destination).
* ``Type`` / ``Tag`` — a value from the ``types.md`` / ``tags.md`` vocabulary
  (same fallbacks the librarian uses when those files are absent). Blank or
  ``-`` means "not constrained".
* ``Subject`` — an optional entity uid the rule is scoped to. Blank or ``-``
  means any subject.

* ``Store`` — an OPTIONAL fifth column (athenaeum#2796). Required on a
  ``route`` row, blank on ``always`` / ``never``: the ``store_id`` the matching
  facts should go to, or the reserved word ``personal``. Never an adapter
  name. A table without the column is valid and has no routes.

A rule needs at least one of Type / Tag / Subject. When a rule names several
keys it matches only when ALL of them match. A rule's Tag also matches the
tag's ``/``-separated descendants (``personal`` covers ``personal/travel``).

Semantics — **last matching rule wins** (gitignore-style). A general rule goes
first and its exceptions after it, so the example above resolves a
``personal/travel`` fact to ``always`` while any other ``personal`` fact stays
``never``. A fact no rule matches resolves to ``None`` (no policy; default
behaviour). When a fact carries several tags, each rule is tested against all
of them, so file order — not tag order — decides.

Two questions, one table (athenaeum#2796). Capture — :meth:`RelevancePolicy.match`
and :meth:`~RelevancePolicy.resolve` — is the last matching ``always`` /
``never`` row and ignores ``route`` rows. Destination —
:meth:`RelevancePolicy.match_route` — is the last matching ``route`` row and
ignores the others. Neither kind of row affects the other's answer, so
``never`` beats ``route``. Nothing acts on a ``route`` row yet.

Validation failures (unknown verdict, unknown type or tag, empty rule) raise
:class:`RelevancePolicyError` naming each offending rule by its number and
file line. :func:`policy_hash_token` never raises (the run summary must not
fail on a bad file): it reports ``invalid`` instead.

Enforcement (athenaeum#2644, slice 2): :func:`gate_raw_content` is the ONE
deterministic pre-tier decision. ``librarian.process_one``,
``batch.process_batch_run`` and the auto-memory phase all call it, so the two
pipeline transports cannot drift. It reads only DECLARED metadata (frontmatter
``type`` / ``tags`` / ``uid``), never the body and never an LLM. A fact is
dropped only when the last matching rule is ``never``; ``always`` is a prompt
hint (a later slice) and exempts nothing here. ``always`` has a second effect,
on removal (athenaeum#3152): :func:`protecting_rule` is the ONE helper every
path that removes or proposes removing a page (``retire``,
``decay-sweep``, the audit's retirement candidates) asks "is this page protected,
and by which rule", the analogue of :func:`gate_raw_content` for capture. The one exemption is the
explicit-remember marker (``explicit_remember: true`` in the item's
frontmatter, athenaeum#2646): a flagged fact is kept and reported in
``overrides`` instead of dropped.

An *item* is one declared fact inside a raw: a ``.jsonl`` record, or one of
several concatenated ``---``-delimited frontmatter documents in a markdown
raw (a block counts as a document boundary only when it parses to a mapping
declaring ``type`` or ``tags``, so a horizontal rule is never mistaken for
one). Any other raw is a single item. Items are judged independently; a raw
that mixes covered and uncovered items loses only the covered ones.

Layering: L2. Imports :mod:`athenaeum.models` (``load_schema_list``) and
:mod:`athenaeum.schemas` (``KNOWN_TYPES``, both L1); never imports
:mod:`athenaeum.mcp_server` or :mod:`athenaeum.librarian` — wiring is the
caller's job. Relevance rules are not ``erasure.RetentionRule``s and never
affect ranking.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from athenaeum.intake_decision import (
    CAPTURE_DROP,
    CONFIDENCE_HIGH,
    Description,
    RuleLike,
    always_rule,
    decide,
)
from athenaeum.models import load_schema_list, parse_frontmatter
from athenaeum.schemas import KNOWN_TYPES
from athenaeum.store_access import is_store_id

#: Filename under ``wiki/_schema/``.
POLICY_FILENAME = "relevance-policy.md"

#: The verdicts this slice accepts. ``restrict`` (per-audience) is deferred to
#: the store access model (athenaeum#2623). ``route`` (athenaeum#2796) names a
#: destination and takes no part in the capture question.
VERDICTS: frozenset[str] = frozenset({"always", "never", "route"})

#: The verdicts that answer the capture question.
CAPTURE_VERDICTS: frozenset[str] = frozenset({"always", "never"})

#: The verdict that answers the destination question.
ROUTE_VERDICT = "route"

#: Reserved ``Store`` value: the install owner's own store.
PERSONAL_STORE = "personal"

# Mirrors ``librarian.FALLBACK_TAGS`` (kept local: this module is below the
# librarian in the layering and must not import it).
_FALLBACK_TAGS = ("active", "archived", "blocked")

_HEADER = ("verdict", "type", "tag", "subject")
_HEADER_WITH_STORE = (*_HEADER, "store")


class RelevancePolicyError(ValueError):
    """The policy file is malformed; the message names every bad rule."""


@dataclass(frozen=True)
class RelevanceRule:
    """One ordered rule. ``number`` is 1-based among rules; ``line`` is the file line."""

    number: int
    line: int
    verdict: str
    type: str | None = None
    tag: str | None = None
    subject: str | None = None
    #: Destination of a ``route`` row (a ``store_id`` or ``personal``); ``None``
    #: on every other row. A result, never a match key.
    store: str | None = None

    def matches(
        self, type_: str | None, tags: "list[str] | tuple[str, ...]", subject: str | None
    ) -> bool:
        """True when every key this rule names matches the fact."""
        if self.type is not None and self.type != type_:
            return False
        if self.tag is not None and not any(
            t == self.tag or t.startswith(self.tag + "/") for t in tags
        ):
            return False
        if self.subject is not None and self.subject != subject:
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.number,
            "line": self.line,
            "verdict": self.verdict,
            "type": self.type,
            "tag": self.tag,
            "subject": self.subject,
            "store": self.store,
        }

    def describe(self) -> str:
        keys = [
            f"{k}={v}"
            for k, v in (("type", self.type), ("tag", self.tag), ("subject", self.subject))
            if v is not None
        ]
        if self.store is not None:
            keys.append(f"store={self.store}")
        return f"{self.verdict} {' '.join(keys)}"


@dataclass(frozen=True)
class RelevancePolicy:
    """The effective, validated rules in file order. Empty == no policy."""

    rules: tuple[RelevanceRule, ...] = ()
    present: bool = False

    def resolve(
        self,
        type_: str | None = None,
        tags: "list[str] | tuple[str, ...]" = (),
        subject: str | None = None,
    ) -> str | None:
        """The verdict of the LAST matching rule, or ``None`` when none match."""
        rule = self.match(type_, tags, subject)
        return rule.verdict if rule is not None else None

    def match(
        self,
        type_: str | None = None,
        tags: "list[str] | tuple[str, ...]" = (),
        subject: str | None = None,
    ) -> RelevanceRule | None:
        """The LAST matching ``always`` / ``never`` rule itself, or ``None``.

        The capture question: ``route`` rows are skipped, so a ``route`` row
        after a ``never`` row cannot shadow it (athenaeum#2796).
        """
        for rule in reversed(self.rules):
            if rule.verdict in CAPTURE_VERDICTS and rule.matches(type_, tags, subject):
                return rule
        return None

    def match_route(
        self,
        type_: str | None = None,
        tags: "list[str] | tuple[str, ...]" = (),
        subject: str | None = None,
    ) -> RelevanceRule | None:
        """The LAST matching ``route`` rule, or ``None``.

        The destination question; ``always`` / ``never`` rows are skipped.
        """
        for rule in reversed(self.rules):
            if rule.verdict == ROUTE_VERDICT and rule.matches(type_, tags, subject):
                return rule
        return None

    def canonical_hash(self) -> str:
        """sha256 of the effective rules (file order), blind to comments/whitespace."""
        payload = json.dumps(
            [
                [r.verdict, r.type, r.tag, r.subject]
                + ([r.store] if r.verdict == ROUTE_VERDICT else [])
                for r in self.rules
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "hash": self.canonical_hash()[:8] if self.present else None,
            "semantics": "last-match-wins",
            "removal_protection": REMOVAL_PROTECTION_NOTE,
            "rules": [r.to_dict() for r in self.rules],
        }


#: Read-back sentence (CLI and MCP) for the second effect of ``always``.
REMOVAL_PROTECTION_NOTE = (
    "an `always` rule is also a capture hint AND protects every page it resolves to "
    "from retirement: retire refuses it (unless --force-retire), decay-sweep skips it, "
    "and the audit never lists it as a retirement candidate"
)


def _cell(raw: str) -> str | None:
    value = raw.strip().strip("`").strip()
    return None if value in ("", "-") else value


def _store_problem(
    store: str, valid_stores: "set[str] | frozenset[str] | None"
) -> str | None:
    """Why a ``Store`` value is unusable, or ``None`` when it is fine."""
    if store == PERSONAL_STORE:
        return None
    if not is_store_id(store):
        return (
            f"Store {store!r} is neither 'personal' nor shaped like a store_id "
            "('st_' plus 12 lowercase base32 characters); an adapter name is not a Store"
        )
    if valid_stores is not None and store not in valid_stores:
        return f"Store {store!r} is not a store this install maps"
    return None


def parse_relevance_policy(
    text: str,
    valid_types: "set[str] | frozenset[str]",
    valid_tags: "set[str] | frozenset[str]",
    valid_stores: "set[str] | frozenset[str] | None" = None,
) -> RelevancePolicy:
    """Parse and validate policy text. Raises :class:`RelevancePolicyError`.

    *valid_stores* is the set of ``store_id``s this install maps to a store,
    resolved by the caller (this module stays below the storage layer). A
    ``Store`` value is ``personal`` or a member of it. ``None`` (the caller has
    no set) checks the ``store_id`` shape only.
    """
    rules: list[RelevanceRule] = []
    errors: list[str] = []
    seen_header = False
    has_store = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(c and set(c) <= set("-: ") for c in cells):
            continue  # separator row
        if not seen_header:
            lowered = tuple(c.lower() for c in cells)
            if lowered[: len(_HEADER)] != _HEADER or (
                len(lowered) > len(_HEADER) and lowered != _HEADER_WITH_STORE
            ):
                errors.append(
                    f"line {lineno}: expected header row "
                    f"'| Verdict | Type | Tag | Subject |' (optionally followed by "
                    f"'| Store |'), got {stripped!r}"
                )
                break
            has_store = len(lowered) == len(_HEADER_WITH_STORE)
            seen_header = True
            continue
        cells += [""] * (len(_HEADER_WITH_STORE) - len(cells))
        number = len(rules) + 1
        name = f"rule {number} (line {lineno})"
        verdict = (_cell(cells[0]) or "").lower()
        type_, tag, subject = _cell(cells[1]), _cell(cells[2]), _cell(cells[3])
        store = _cell(cells[4]) if has_store else None
        if verdict not in VERDICTS:
            errors.append(
                f"{name}: unknown verdict {verdict!r}; expected one of {sorted(VERDICTS)}"
            )
        if type_ is not None and type_ not in valid_types:
            errors.append(f"{name}: unknown type {type_!r} (not in types.md vocabulary)")
        if tag is not None and tag not in valid_tags:
            errors.append(f"{name}: unknown tag {tag!r} (not in tags.md vocabulary)")
        if type_ is None and tag is None and subject is None:
            errors.append(f"{name}: names no type, tag or subject")
        if verdict == ROUTE_VERDICT:
            if store is None:
                errors.append(f"{name}: a route rule needs a Store (a store_id or 'personal')")
            else:
                problem = _store_problem(store, valid_stores)
                if problem:
                    errors.append(f"{name}: {problem}")
        elif store is not None and verdict in CAPTURE_VERDICTS:
            errors.append(f"{name}: a {verdict} rule must leave Store blank, got {store!r}")
        rules.append(RelevanceRule(number, lineno, verdict, type_, tag, subject, store))
    if errors:
        raise RelevancePolicyError(
            f"invalid {POLICY_FILENAME}: " + "; ".join(errors)
        )
    return RelevancePolicy(rules=tuple(rules), present=True)


def policy_vocabulary(wiki_root: Path) -> tuple[set[str], set[str]]:
    """The ``(valid_types, valid_tags)`` a rule is validated against.

    Read from ``<wiki_root>/_schema/types.md`` / ``tags.md`` with the same
    fallbacks as :func:`load_relevance_policy`; exposed so a writer
    (:mod:`athenaeum.relevance_policy_edit`) validates against exactly the
    vocabulary the loader will.
    """
    schema_dir = wiki_root / "_schema"
    valid_types = set(load_schema_list(schema_dir, "types.md") or KNOWN_TYPES)
    valid_tags = set(load_schema_list(schema_dir, "tags.md") or _FALLBACK_TAGS)
    return valid_types, valid_tags


def load_relevance_policy(
    wiki_root: Path, valid_stores: "set[str] | frozenset[str] | None" = None
) -> RelevancePolicy:
    """Load ``<wiki_root>/_schema/relevance-policy.md``.

    *valid_stores* is passed through to :func:`parse_relevance_policy`.

    A missing file returns the empty policy (``present=False``) — a complete
    no-op. A present-but-invalid file raises :class:`RelevancePolicyError`.
    """
    schema_dir = wiki_root / "_schema"
    path = schema_dir / POLICY_FILENAME
    if not path.is_file():
        return RelevancePolicy()
    text = path.read_text(encoding="utf-8")
    valid_types, valid_tags = policy_vocabulary(wiki_root)
    return parse_relevance_policy(text, valid_types, valid_tags, valid_stores)


def policy_hash_token(wiki_root: Path) -> str | None:
    """Run-summary token: 8-hex rules hash, ``invalid``, or ``None`` when no file.

    Never raises. ``None`` keeps a deployment with no policy file
    byte-identical in its run summary.
    """
    try:
        policy = load_relevance_policy(wiki_root)
    except RelevancePolicyError:
        return "invalid"
    except OSError:
        return None
    return policy.canonical_hash()[:8] if policy.present else None


def render_policy(policy: RelevancePolicy) -> str:
    """Human-readable read-back: effective rules in evaluation order."""
    if not policy.present:
        return (
            f"No relevance policy ({POLICY_FILENAME} not present): "
            "nothing is constrained; default capture behaviour applies.\n"
        )
    lines = [
        f"Relevance policy hash={policy.canonical_hash()[:8]}; "
        f"{len(policy.rules)} rule(s), evaluated top to bottom, last match wins:"
    ]
    for r in policy.rules:
        lines.append(f"  {r.number}. {r.describe()}")
    lines.append(f"Note: {REMOVAL_PROTECTION_NOTE}.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deterministic pre-tier gate (athenaeum#2644)
# ---------------------------------------------------------------------------

#: Prefix of every drop reason this gate emits, so ``athenaeum drops list``
#: readers (and tests) can tell a policy drop from any other ``other`` drop.
DROP_REASON_PREFIX = "relevance-policy"

#: Prefix of the reason for a page type a ``storage.routes`` rule discards
#: (athenaeum#3168). The reason names the rule and carries no content.
DROP_REASON_DISCARD = "storage-discard"

_EXCERPT_CHARS = 500

#: Confidence value the classifier (and the reasoning tier) must emit for a
#: category to be acted on without escalation (athenaeum#2647). Any other value,
#: or none, is *unsure*.
CONFIDENCE_LOW = "low"
CONFIDENCES: frozenset[str] = frozenset({CONFIDENCE_HIGH, CONFIDENCE_LOW})

#: Drop-reason prefix for a fact the reasoning tier could not place either
#: (athenaeum#2647): fail closed, the fact is dropped and not kept.
DROP_REASON_UNSURE = "relevance-unsure"

#: Frontmatter key ``remember(explicit_request=True)`` stamps on the raw
#: (athenaeum#2646). A fact carrying a truthy marker is a deliberate human
#: "remember this": the gate never drops it, whatever ``never`` rule matches,
#: and reports the rule it overrode in :attr:`RelevanceGateResult.overrides`
#: so the caller (which knows the transport and the clock) can audit it.
EXPLICIT_REMEMBER_KEY = "explicit_remember"


@dataclass(frozen=True)
class RelevanceDrop:
    """One item a ``never`` rule covered, and the rule that decided it."""

    rule: RuleLike | None
    item_index: int
    excerpt: str
    #: Set instead of ``rule`` when a ``storage.routes`` rule with ``to: []``
    #: discards the page type (athenaeum#3168); the excerpt is then empty.
    discard_rule: str | None = None

    @property
    def label(self) -> str:
        if self.discard_rule is not None:
            return f"{self.discard_rule} (page type discarded: to is empty)"
        assert self.rule is not None
        return f"{self.rule.describe()} (rule {self.rule.number}, line {self.rule.line})"

    @property
    def reason(self) -> str:
        prefix = DROP_REASON_DISCARD if self.discard_rule is not None else DROP_REASON_PREFIX
        return f"{prefix}: {self.label}"


@dataclass(frozen=True)
class RelevanceGateResult:
    """Outcome of :func:`gate_raw_content` for one raw.

    ``drops`` is empty when nothing was covered (the common case, and every
    case with no policy). ``kept_content`` is the raw with covered items
    removed; it equals the input when nothing was dropped and is meaningless
    when ``fully_dropped``.
    """

    drops: tuple[RelevanceDrop, ...] = ()
    kept_content: str = ""
    n_items: int = 0
    #: Items a ``never`` rule covered but the explicit-remember marker
    #: exempted (athenaeum#2646). Kept, never dropped; each names its rule.
    overrides: tuple[RelevanceDrop, ...] = ()

    @property
    def fully_dropped(self) -> bool:
        return bool(self.drops) and len(self.drops) >= self.n_items

    @property
    def reason(self) -> str:
        """One reason string naming every distinct matching rule."""
        labels: list[str] = []
        for d in self.drops:
            if d.label not in labels:
                labels.append(d.label)
        return f"{DROP_REASON_PREFIX}: " + "; ".join(labels)


@dataclass
class _Item:
    text: str
    meta: dict[str, object] = field(default_factory=dict)


def _declared_tags(meta: dict[str, object]) -> list[str]:
    raw = meta.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, Iterable):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def _is_explicit_remember(meta: dict[str, object]) -> bool:
    value = meta.get(EXPLICIT_REMEMBER_KEY)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return value is True


def _declared(meta: dict[str, object]) -> tuple[str | None, list[str], str | None]:
    type_ = str(meta.get("type") or "").strip() or None
    subject = str(meta.get("uid") or "").strip() or None
    return type_, _declared_tags(meta), subject


def _split_jsonl(content: str) -> list[_Item]:
    items: list[_Item] = []
    for line in content.splitlines(keepends=True):
        if not line.strip():
            continue
        meta: dict[str, object] = {}
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            meta = obj
        items.append(_Item(line, meta))
    return items


def _split_markdown(content: str) -> list[_Item]:
    meta, body = parse_frontmatter(content)
    preamble = content[: len(content) - len(body)]
    lines = body.splitlines(keepends=True)
    boundaries: list[int] = []
    boundary_meta: dict[int, dict[str, object]] = {}
    i = 0
    while i < len(lines):
        if lines[i].strip() == "---":
            j = next(
                (k for k in range(i + 1, len(lines)) if lines[k].strip() == "---"), None
            )
            if j is not None:
                try:
                    doc = yaml.safe_load("".join(lines[i + 1 : j]))
                except yaml.YAMLError:
                    doc = None
                if isinstance(doc, dict) and ("type" in doc or "tags" in doc):
                    boundaries.append(i)
                    boundary_meta[i] = {str(k): v for k, v in doc.items()}
                    i = j + 1
                    continue
        i += 1
    if not boundaries:
        return [_Item(content, meta)]
    items = [_Item(preamble + "".join(lines[: boundaries[0]]), meta)]
    for n, start in enumerate(boundaries):
        end = boundaries[n + 1] if n + 1 < len(boundaries) else len(lines)
        items.append(_Item("".join(lines[start:end]), boundary_meta[start]))
    return items


def raw_is_explicit_remember(content: str, *, jsonl: bool = False) -> bool:
    """True when the whole raw carries the explicit-remember marker (athenaeum#2646).

    A markdown raw is flagged by its own top frontmatter (the block
    ``remember(explicit_request=True)`` stamps), which covers every fact in
    the note. A ``.jsonl`` raw is flagged only when every record carries the
    marker, so one flagged record cannot shelter its unflagged neighbours.
    Used by the post-classify and Tier-3 relevance gates
    (:mod:`athenaeum.relevance_classify`), which judge facts the classifier
    extracted from the raw rather than declared items.
    """
    if jsonl:
        items = _split_jsonl(content)
        return bool(items) and all(_is_explicit_remember(i.meta) for i in items)
    return _is_explicit_remember(parse_frontmatter(content)[0])


def policy_categories(policy: RelevancePolicy | None) -> list[str]:
    """Distinct tag keys the policy's rules name, in file order.

    These are the *categories* the classifier is asked to choose from: a
    category is a tag from the ``tags.md`` vocabulary, possibly a
    ``/``-separated descendant (``personal/travel``). Empty when no rule names
    a tag, in which case classify is not asked for a category at all.
    """
    seen: list[str] = []
    for rule in policy.rules if policy is not None else ():
        if rule.verdict == ROUTE_VERDICT:
            continue  # athenaeum#2796: route tags join the category list in a later slice
        if rule.tag is not None and rule.tag not in seen:
            seen.append(rule.tag)
    return seen


def gate_raw_content(
    policy: RelevancePolicy | None,
    content: str,
    *,
    jsonl: bool = False,
    discarded: Mapping[str, str] | None = None,
) -> RelevanceGateResult:
    """Decide, per item, which declared facts a ``never`` rule covers.

    Pure and deterministic: no I/O, no LLM, no clock. ``None`` or an empty
    policy, with no *discarded* page types, returns the no-op result
    immediately, so a deployment with no policy file is byte-identical to
    before this gate existed. *discarded* maps a page type to the
    ``storage.routes`` rule (``to: []``) that discards it (athenaeum#3168): an
    item of that type is dropped with a reason naming the rule.
    """
    if (policy is None or not policy.rules) and not discarded:
        return RelevanceGateResult(kept_content=content)
    if policy is None:
        policy = RelevancePolicy()
    items = _split_jsonl(content) if jsonl else _split_markdown(content)
    # The explicit-remember marker on the raw note's own (top) frontmatter
    # covers every item in it: ``remember(explicit_request=True)`` stamps only
    # that block, and the human asked for the whole note (Seer review on PR
    # athenaeum#2680). An item may also carry the marker itself.
    note_explicit = not jsonl and bool(items) and _is_explicit_remember(items[0].meta)
    drops: list[RelevanceDrop] = []
    overrides: list[RelevanceDrop] = []
    kept: list[str] = []
    for index, item in enumerate(items):
        type_, tags, subject = _declared(item.meta)
        decision = decide(
            Description(
                type_,
                tuple(tags),
                subject,
                explicit=note_explicit or _is_explicit_remember(item.meta),
            ),
            policy,
            discarded=discarded,
        )
        if decision.discard_rule is not None:
            # Nothing of the item is kept in the drop: no excerpt.
            drops.append(RelevanceDrop(None, index, "", discard_rule=decision.discard_rule))
        elif decision.overridden is not None:
            # explicit human request beats ``never``
            overrides.append(
                RelevanceDrop(decision.overridden, index, item.text.strip()[:_EXCERPT_CHARS])
            )
            kept.append(item.text)
        elif decision.capture == CAPTURE_DROP and decision.rule is not None:
            drops.append(
                RelevanceDrop(decision.rule, index, item.text.strip()[:_EXCERPT_CHARS])
            )
        else:
            kept.append(item.text)
    if not drops:
        return RelevanceGateResult(
            kept_content=content, n_items=len(items), overrides=tuple(overrides)
        )
    return RelevanceGateResult(tuple(drops), "".join(kept), len(items), tuple(overrides))


def filter_relevance_never(
    files: Iterable[Any],
    policy: RelevancePolicy | None,
) -> tuple[list[Any], list[tuple[Any, RelevanceGateResult]]]:
    """Partition auto-memory files into ``(kept, refused)`` (athenaeum#2644).

    A file is refused only when EVERY item it declares is covered by a
    ``never`` rule. Auto-memory files belong to the host tool, never to this
    pipeline, so a mixed file cannot be rewritten: it is kept whole. Nothing
    is ever deleted here. An unreadable file is kept (it could not be
    compiled either). A no-op returning every file when *policy* is empty.
    """
    if policy is None or not policy.rules:
        return list(files), []
    kept: list[Any] = []
    refused: list[tuple[Any, RelevanceGateResult]] = []
    for am in files:
        try:
            text = am.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            kept.append(am)
            continue
        gate = gate_raw_content(policy, text)
        if gate.fully_dropped:
            refused.append((am, gate))
        else:
            kept.append(am)
    return kept, refused


def declared_facts(meta: dict[str, object]) -> tuple[str | None, list[str], str | None]:
    """The ``(type, tags, uid)`` a rule is matched against, from a metadata mapping.

    Public form of the reading :func:`gate_raw_content` uses, so a gate that
    judges a wiki page's own frontmatter (athenaeum#2647) reads it the same way.
    """
    return _declared(meta)


@dataclass(frozen=True)
class NeverMatch:
    """One existing wiki page a ``never`` rule covers (athenaeum#2650)."""

    path: str
    rule: RuleLike
    type: str | None
    tags: tuple[str, ...]
    uid: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "rule": self.rule.number,
            "line": self.rule.line,
            "describe": self.rule.describe(),
            "type": self.type,
            "tags": list(self.tags),
            "uid": self.uid,
        }


def report_never_matches(policy: RelevancePolicy, wiki_root: Path) -> list[NeverMatch]:
    """Existing wiki pages whose LAST matching rule is a ``never`` rule.

    Read-only: reads each top-level ``wiki/*.md`` page's frontmatter (the
    ``_``-prefixed queue, index and archive files are not pages) and matches
    its declared ``type`` / ``tags`` / ``uid`` exactly as the deterministic
    gate matches a raw's, via :func:`declared_facts` and
    :meth:`RelevancePolicy.match`. A page an ``always`` exception rescues is
    not listed. A ``never`` rule applies forward only, so this is the operator's
    list of what it would have covered; nothing is changed.
    """
    if not policy.rules or not wiki_root.is_dir():
        return []
    out: list[NeverMatch] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict):
            continue  # a list/scalar frontmatter declares nothing to match
        type_, tags, uid = declared_facts(meta)
        decision = decide(Description(type_, tuple(tags), uid), policy)
        if decision.capture == CAPTURE_DROP and decision.rule is not None:
            out.append(NeverMatch(path.name, decision.rule, type_, tuple(tags), uid))
    return out


# ---------------------------------------------------------------------------
# Removal protection (athenaeum#3152)
# ---------------------------------------------------------------------------


def protecting_rule(
    policy: RelevancePolicy | None, meta: "Mapping[str, object]"
) -> RelevanceRule | None:
    """The ``always`` rule that protects a page from removal, or ``None``.

    THE shared decision for every removal path (``retire``,
    ``decay-sweep``, the audit's retirement candidates): *meta* is the page's
    frontmatter, matched exactly as capture matches a raw (declared ``type`` /
    ``tags`` / ``uid`` via :func:`declared_facts`, last matching rule wins). A
    page is protected only when the LAST matching ``always`` / ``never`` rule
    is ``always``; a ``never`` exception after it unprotects the page. Pure: no
    I/O. ``None`` or an empty policy protects nothing, so a deployment with no
    policy file behaves exactly as before.
    """
    if policy is None or not policy.rules or not isinstance(meta, Mapping):
        return None
    type_, tags, uid = declared_facts(dict(meta))
    rule = always_rule(policy, Description(type_, tuple(tags), uid))
    return rule if isinstance(rule, RelevanceRule) else None


class AlwaysMatch(NeverMatch):
    """One existing wiki page an ``always`` rule protects from removal."""


def report_always_matches(policy: RelevancePolicy, wiki_root: Path) -> list[AlwaysMatch]:
    """Existing wiki pages :func:`protecting_rule` protects, one entry per page.

    The ``always`` counterpart of :func:`report_never_matches`: read-only, same
    page scan, so an operator sees how many live pages each ``always`` rule
    protects from retirement.
    """
    if not policy.rules or not wiki_root.is_dir():
        return []
    out: list[AlwaysMatch] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        rule = protecting_rule(policy, meta)
        if rule is not None:
            type_, tags, uid = declared_facts(meta)
            out.append(AlwaysMatch(path.name, rule, type_, tuple(tags), uid))
    return out
