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

Layering: L2. Imports :mod:`athenaeum.models` (``load_schema_list``) and
:mod:`athenaeum.schemas` (``KNOWN_TYPES``, both L1); never imports
:mod:`athenaeum.mcp_server` or :mod:`athenaeum.librarian` — wiring is the
caller's job. Relevance rules are not ``erasure.RetentionRule``s and never
affect ranking.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from athenaeum.models import load_schema_list
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
        for rule in reversed(self.rules):
            if rule.matches(type_, tags, subject):
                return rule.verdict
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
    valid_types, valid_tags = policy_vocabulary(wiki_root)
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
