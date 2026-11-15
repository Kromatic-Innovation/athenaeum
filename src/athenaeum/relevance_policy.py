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

* ``Verdict`` — ``always`` or ``never``.
* ``Type`` / ``Tag`` — a value from the ``types.md`` / ``tags.md`` vocabulary
  (same fallbacks the librarian uses when those files are absent). Blank or
  ``-`` means "not constrained".
* ``Subject`` — an optional entity uid the rule is scoped to. Blank or ``-``
  means any subject.

A rule needs at least one of Type / Tag / Subject. When a rule names several
keys it matches only when ALL of them match. A rule's Tag also matches the
tag's ``/``-separated descendants (``personal`` covers ``personal/travel``).

Semantics — **last matching rule wins** (gitignore-style). A general rule goes
first and its exceptions after it, so the example above resolves a
``personal/travel`` fact to ``always`` while any other ``personal`` fact stays
``never``. A fact no rule matches resolves to ``None`` (no policy; default
behaviour). When a fact carries several tags, each rule is tested against all
of them, so file order — not tag order — decides.

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
hint (a later slice) and exempts nothing here.

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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import load_schema_list, parse_frontmatter
from athenaeum.schemas import KNOWN_TYPES

#: Filename under ``wiki/_schema/``.
POLICY_FILENAME = "relevance-policy.md"

#: The verdicts this slice accepts. ``restrict`` (per-audience) is deferred to
#: the store access model (athenaeum#2623).
VERDICTS: frozenset[str] = frozenset({"always", "never"})

# Mirrors ``librarian.FALLBACK_TAGS`` (kept local: this module is below the
# librarian in the layering and must not import it).
_FALLBACK_TAGS = ("active", "archived", "blocked")

_HEADER = ("verdict", "type", "tag", "subject")


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
        }

    def describe(self) -> str:
        keys = [
            f"{k}={v}"
            for k, v in (("type", self.type), ("tag", self.tag), ("subject", self.subject))
            if v is not None
        ]
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
        """The LAST matching rule itself (what decided the verdict), or ``None``."""
        for rule in reversed(self.rules):
            if rule.matches(type_, tags, subject):
                return rule
        return None

    def canonical_hash(self) -> str:
        """sha256 of the effective rules (file order), blind to comments/whitespace."""
        payload = json.dumps(
            [[r.verdict, r.type, r.tag, r.subject] for r in self.rules],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "hash": self.canonical_hash()[:8] if self.present else None,
            "semantics": "last-match-wins",
            "rules": [r.to_dict() for r in self.rules],
        }


def _cell(raw: str) -> str | None:
    value = raw.strip().strip("`").strip()
    return None if value in ("", "-") else value


def parse_relevance_policy(
    text: str,
    valid_types: "set[str] | frozenset[str]",
    valid_tags: "set[str] | frozenset[str]",
) -> RelevancePolicy:
    """Parse and validate policy text. Raises :class:`RelevancePolicyError`."""
    rules: list[RelevanceRule] = []
    errors: list[str] = []
    seen_header = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(c and set(c) <= set("-: ") for c in cells):
            continue  # separator row
        if not seen_header:
            if tuple(c.lower() for c in cells[: len(_HEADER)]) != _HEADER:
                errors.append(
                    f"line {lineno}: expected header row "
                    f"'| Verdict | Type | Tag | Subject |', got {stripped!r}"
                )
                break
            seen_header = True
            continue
        cells += [""] * (len(_HEADER) - len(cells))
        number = len(rules) + 1
        name = f"rule {number} (line {lineno})"
        verdict = (_cell(cells[0]) or "").lower()
        type_, tag, subject = _cell(cells[1]), _cell(cells[2]), _cell(cells[3])
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
        rules.append(RelevanceRule(number, lineno, verdict, type_, tag, subject))
    if errors:
        raise RelevancePolicyError(
            f"invalid {POLICY_FILENAME}: " + "; ".join(errors)
        )
    return RelevancePolicy(rules=tuple(rules), present=True)


def load_relevance_policy(wiki_root: Path) -> RelevancePolicy:
    """Load ``<wiki_root>/_schema/relevance-policy.md``.

    A missing file returns the empty policy (``present=False``) — a complete
    no-op. A present-but-invalid file raises :class:`RelevancePolicyError`.
    """
    schema_dir = wiki_root / "_schema"
    path = schema_dir / POLICY_FILENAME
    if not path.is_file():
        return RelevancePolicy()
    text = path.read_text(encoding="utf-8")
    valid_types = set(load_schema_list(schema_dir, "types.md") or KNOWN_TYPES)
    valid_tags = set(load_schema_list(schema_dir, "tags.md") or _FALLBACK_TAGS)
    return parse_relevance_policy(text, valid_types, valid_tags)


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
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deterministic pre-tier gate (athenaeum#2644)
# ---------------------------------------------------------------------------

#: Prefix of every drop reason this gate emits, so ``athenaeum drops list``
#: readers (and tests) can tell a policy drop from any other ``other`` drop.
DROP_REASON_PREFIX = "relevance-policy"

_EXCERPT_CHARS = 500


@dataclass(frozen=True)
class RelevanceDrop:
    """One item a ``never`` rule covered, and the rule that decided it."""

    rule: RelevanceRule
    item_index: int
    excerpt: str

    @property
    def label(self) -> str:
        return f"{self.rule.describe()} (rule {self.rule.number}, line {self.rule.line})"

    @property
    def reason(self) -> str:
        return f"{DROP_REASON_PREFIX}: {self.label}"


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


def gate_raw_content(
    policy: RelevancePolicy | None, content: str, *, jsonl: bool = False
) -> RelevanceGateResult:
    """Decide, per item, which declared facts a ``never`` rule covers.

    Pure and deterministic: no I/O, no LLM, no clock. ``None`` or an empty
    policy returns the no-op result immediately, so a deployment with no
    policy file is byte-identical to before this gate existed.
    """
    if policy is None or not policy.rules:
        return RelevanceGateResult(kept_content=content)
    items = _split_jsonl(content) if jsonl else _split_markdown(content)
    drops: list[RelevanceDrop] = []
    kept: list[str] = []
    for index, item in enumerate(items):
        rule = policy.match(*_declared(item.meta))
        if rule is not None and rule.verdict == "never":
            drops.append(RelevanceDrop(rule, index, item.text.strip()[:_EXCERPT_CHARS]))
        else:
            kept.append(item.text)
    if not drops:
        return RelevanceGateResult(kept_content=content, n_items=len(items))
    return RelevanceGateResult(tuple(drops), "".join(kept), len(items))


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
