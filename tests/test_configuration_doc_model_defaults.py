# SPDX-License-Identifier: Apache-2.0
"""CI gate: docs/reference/configuration.md's Models table must match the code defaults
(issue athenaeum#1278).

The table's intro claims the **knob set** cannot go stale because it is
derived from `prompt_registry.KNOBS` (issue athenaeum#781) — true, but that
guarantee never covered the **Default** column values, which are hand-typed
prose and drifted silently (athenaeum#1176: the Writer and Reasoning-tier-2
rows both named a model id the resolvers no longer return). This test reads
the Default column back out of the doc and compares it against each knob's
actual `resolve_model`/`_get_*_model` code-level default, so a future model
bump that forgets the doc update fails CI instead of shipping silently wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

from athenaeum import config as athenaeum_config
from athenaeum import query_topics, reasoning_tiers, resolutions, rule_proposals, tiers

_DOC = Path(__file__).resolve().parent.parent / "docs" / "reference" / "configuration.md"

# One entry per row in the Models table's `models.<knob>` YAML key column,
# mapped to the same code-level constant each knob's resolver
# (`config.resolve_model` / the `resolutions._get_model` wrapper) actually
# falls back to when nothing overrides it. Keep in sync with the table.
_CODE_DEFAULTS: dict[str, str] = {
    "classify": athenaeum_config.DEFAULT_CLASSIFY_MODEL,
    "write": tiers.DEFAULT_WRITE_MODEL,
    "topic": query_topics.DEFAULT_TOPIC_MODEL,
    "resolve": resolutions.DEFAULT_RESOLVE_MODEL,
    "reasoning_t1": reasoning_tiers.DEFAULT_T1_MODEL,
    "reasoning_t2": reasoning_tiers.DEFAULT_T2_MODEL,
    "rule_proposals": rule_proposals.DEFAULT_RULE_PROPOSALS_MODEL,
}

# Matches a Models-table row: `| <Label> | <env cell> | `models.<knob>`
# [optional trailing prose in the same cell] | `<default>` | <used-by> |`
_ROW_RE = re.compile(
    r"^\|[^|]+\|[^|]+\|\s*`models\.(?P<knob>[a-z0-9_]+)`[^|]*\|\s*`(?P<default>[^`]+)`\s*\|"
)


def _parse_models_table() -> dict[str, str]:
    text = _DOC.read_text(encoding="utf-8")
    start = text.index("\n## Models\n")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    found: dict[str, str] = {}
    for line in section.splitlines():
        m = _ROW_RE.match(line)
        if m:
            found[m.group("knob")] = m.group("default")
    return found


def test_models_table_found_and_nonempty() -> None:
    # Guards the parser itself: if the heading or table shape ever changes
    # enough that the regex stops matching, fail loudly instead of the
    # comparison test below silently passing on an empty dict.
    documented = _parse_models_table()
    assert documented, "failed to parse any rows out of docs/reference/configuration.md's Models table"


def test_models_table_covers_every_known_knob() -> None:
    documented = _parse_models_table()
    assert set(documented) == set(_CODE_DEFAULTS), (
        "docs/reference/configuration.md's Models table knob set does not match the "
        f"knobs this test knows the code default for: doc={sorted(documented)} "
        f"code={sorted(_CODE_DEFAULTS)}"
    )


def test_models_table_defaults_match_code() -> None:
    documented = _parse_models_table()
    mismatches = {
        knob: {"doc": doc_default, "code": _CODE_DEFAULTS[knob]}
        for knob, doc_default in documented.items()
        if knob in _CODE_DEFAULTS and doc_default != _CODE_DEFAULTS[knob]
    }
    assert not mismatches, (
        "docs/reference/configuration.md's Models table Default column has drifted from "
        f"the resolver code defaults: {mismatches}"
    )
