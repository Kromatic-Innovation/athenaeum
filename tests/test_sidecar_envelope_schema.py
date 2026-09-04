# SPDX-License-Identifier: Apache-2.0
"""Tests for the sidecar envelope schema (issue athenaeum#1359).

Each acceptance criterion gets its own test, with the issue's own
counter-example as a comment on the test it defeats.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from athenaeum import context_schema
from athenaeum.context import ENVELOPE_VERSION, build_context
from athenaeum.context_schema import (
    BUDGET_REQUIRED_FIELDS,
    CANDIDATE_REQUIRED_FIELDS,
    RENDER_REQUIRED_FIELDS,
    REQUIRED_FIELDS,
    SCHEMA_HISTORY,
    SCHEMA_VERSION,
    EnvelopeValidationError,
    validate_envelope,
)

CONTEXT_PY = Path(__file__).resolve().parent.parent / "src" / "athenaeum" / "context.py"
CONTEXT_SCHEMA_PY = (
    Path(__file__).resolve().parent.parent / "src" / "athenaeum" / "context_schema.py"
)


def _build_index(path: Path, extra_rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE VIRTUAL TABLE wiki USING fts5("
        "filename, name, tags, aliases, description, "
        "audience UNINDEXED, type UNINDEXED, memory_tier UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    conn.executemany(
        "INSERT INTO wiki VALUES (?,?,?,?,?,?,?,?)",
        extra_rows,
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Golden envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_envelope(tmp_path: Path) -> dict:
    """A real envelope, built by the real core against a tiny fixture index
    — not a hand-typed dict — so this test also catches the core silently
    drifting from what the schema describes."""
    _build_index(
        tmp_path / "wiki-index.db",
        [
            (
                "widget-page.md",
                "Golden Widget",
                "widget",
                "",
                "the golden fixture page",
                "|__access_open__|",
                "ref",
                "hot",
            )
        ],
    )
    return build_context("golden widget", "golden-session", cache_dir=tmp_path, use_llm=False)


def test_golden_envelope_validates(golden_envelope: dict) -> None:
    validate_envelope(golden_envelope)  # must not raise


def test_envelope_version_is_single_sourced_from_schema_version() -> None:
    """Sentry/Seer review finding on this issue's own PR: `context.py`'s
    `ENVELOPE_VERSION` and `context_schema.py`'s `SCHEMA_VERSION` were two
    independent literals that could silently desync on a future schema
    bump in one file without the matching edit in the other.
    `athenaeum.context` imports `SCHEMA_VERSION` directly (see its source)
    rather than hand-copying the literal, and this asserts the value that
    reaches a real, built envelope matches the schema module's version."""
    assert ENVELOPE_VERSION == SCHEMA_VERSION


def test_context_module_imports_schema_version_not_a_literal() -> None:
    """Belt-and-suspenders on the same finding: greps context.py's source
    for the import, so a future edit that reverts to a hand-copied literal
    (which could still equal 1 today and pass the value-equality test
    above) is caught at the source level too."""
    text = CONTEXT_PY.read_text(encoding="utf-8")
    assert "from athenaeum.context_schema import SCHEMA_VERSION" in text


def test_golden_envelope_has_a_hit(golden_envelope: dict) -> None:
    # Sanity: the fixture actually exercises the candidates[] path, not just
    # an empty-turn envelope.
    assert len(golden_envelope["candidates"]) == 1
    assert golden_envelope["candidates"][0]["filename"] == "widget-page.md"


# ---------------------------------------------------------------------------
# Versioning discipline — the issue's own counter-example
# ---------------------------------------------------------------------------


def test_schema_history_matches_current_required_fields() -> None:
    """Counter-example that must fail: adding a required field without
    bumping v — at ANY level of the envelope, not just the top level.
    SCHEMA_HISTORY is a hand-maintained snapshot per version; it does not
    derive from the REQUIRED_FIELDS dicts, so the two can disagree, and
    this test is what catches it when they do."""
    snapshot = SCHEMA_HISTORY[SCHEMA_VERSION]
    assert snapshot["envelope"] == frozenset(REQUIRED_FIELDS)
    assert snapshot["candidate"] == frozenset(CANDIDATE_REQUIRED_FIELDS) | {"relevance"}
    assert snapshot["budget"] == frozenset(BUDGET_REQUIRED_FIELDS)
    assert snapshot["render"] == frozenset(RENDER_REQUIRED_FIELDS)


def test_golden_envelope_field_set_matches_schema(golden_envelope: dict) -> None:
    """The other half of the pinning story: validate_envelope() tolerates
    EXTRA fields on an envelope (so DIAGNOSTIC_FIELDS and a future
    adapter-specific extension don't fail it) — which means a field
    silently ADDED to the core's real output would pass validate_envelope()
    without this test. Exact field-set equality against the golden envelope
    is what catches the core drifting ahead of what this schema documents.
    """
    expected_envelope = set(REQUIRED_FIELDS) | set(context_schema.DIAGNOSTIC_FIELDS)
    assert set(golden_envelope) == expected_envelope

    candidate = golden_envelope["candidates"][0]
    assert set(candidate) == set(CANDIDATE_REQUIRED_FIELDS) | {"relevance"}
    assert set(golden_envelope["budget"]) == set(BUDGET_REQUIRED_FIELDS)
    assert set(golden_envelope["render"]) == set(RENDER_REQUIRED_FIELDS)


def test_every_schema_version_has_a_migration_note() -> None:
    assert set(context_schema.MIGRATIONS) == set(SCHEMA_HISTORY)


def test_missing_required_field_fails_validation(golden_envelope: dict) -> None:
    broken = dict(golden_envelope)
    del broken["candidates"]
    with pytest.raises(EnvelopeValidationError, match="candidates"):
        validate_envelope(broken)


def test_wrong_type_fails_validation(golden_envelope: dict) -> None:
    broken = dict(golden_envelope)
    broken["session_id"] = 12345  # should be str
    with pytest.raises(EnvelopeValidationError, match="session_id"):
        validate_envelope(broken)


def test_version_mismatch_fails_validation(golden_envelope: dict) -> None:
    broken = dict(golden_envelope)
    broken["v"] = 2
    with pytest.raises(EnvelopeValidationError, match="v="):
        validate_envelope(broken)


def test_candidate_missing_required_field_fails_validation(golden_envelope: dict) -> None:
    broken = dict(golden_envelope)
    broken_candidate = dict(broken["candidates"][0])
    del broken_candidate["memory_tier"]
    broken["candidates"] = [broken_candidate]
    with pytest.raises(EnvelopeValidationError, match="memory_tier"):
        validate_envelope(broken)


def test_elapsed_ms_is_diagnostic_not_required() -> None:
    """`elapsed_ms` is real output but explicitly outside the versioned
    contract — a wall-clock value can't sit in a golden fixture's exact
    comparison. Confirms it's declared where the module says it is."""
    assert "elapsed_ms" not in REQUIRED_FIELDS
    assert "elapsed_ms" in context_schema.DIAGNOSTIC_FIELDS


def test_diagnostic_and_required_fields_are_disjoint() -> None:
    assert set(REQUIRED_FIELDS) & set(context_schema.DIAGNOSTIC_FIELDS) == set()


# ---------------------------------------------------------------------------
# No Claude-specific field anywhere in the core's render path
# (issue athenaeum#1359: "the load-bearing criterion of the issue")
# ---------------------------------------------------------------------------


def test_no_claude_specific_field_in_core_or_schema_source() -> None:
    """Counter-example that must fail: a future edit inlining the Claude
    Code envelope into the core 'as a convenience'. Checked against the
    RENDER PATH source (context.py) and the SCHEMA source
    (context_schema.py) — not just a freshly-built envelope, which would
    pass trivially today since the core has no such fields yet. This is
    the same literal-grep discipline as
    tests/test_context_core.py::test_no_hook_specific_output_in_core_source,
    covering the schema module that test predates."""
    for path in (CONTEXT_PY, CONTEXT_SCHEMA_PY):
        text = path.read_text(encoding="utf-8")
        assert "hookSpecificOutput" not in text, path
        assert "additionalContext" not in text, path


def test_no_claude_specific_field_in_a_live_envelope(golden_envelope: dict) -> None:
    import json

    blob = json.dumps(golden_envelope)
    assert "hookSpecificOutput" not in blob
    assert "additionalContext" not in blob
