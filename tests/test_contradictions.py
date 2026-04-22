# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.contradictions` (C4, issue #198).

Covers:

- Positive detection: detector returns ``detected=True`` with conflict
  type + quoted passages.
- Negative detection: near-duplicate cluster → ``detected=False``.
- Fallback: ``client=None`` → ``detected=False``,
  ``rationale="llm-unavailable"``.
- Malformed JSON from the detector → ``detected=False`` with a warning.
- Singleton cluster → short-circuits to ``detected=False`` (no API call).
- Per-member body trim at :data:`PER_MEMBER_BODY_CHARS`.

The tests do NOT make network calls; every "client" is a
``unittest.mock.MagicMock`` built to mirror the shape of
``anthropic.Anthropic().messages.create(...)``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import (
    PER_MEMBER_BODY_CHARS,
    ContradictionResult,
    detect_contradictions,
)
from athenaeum.models import AutoMemoryFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_am(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    origin_scope: str = "scope-x",
) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: probe\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name="probe",
    )


def _fake_client(payload_text: str) -> MagicMock:
    """Build a MagicMock that mirrors the Anthropic SDK response shape."""
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# Positive path
# ---------------------------------------------------------------------------


class TestPositiveDetection:
    def test_contradiction_detected_with_two_members(
        self, tmp_path: Path,
    ) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(
            scope, "feedback_a.md",
            "Always commit directly to develop. Do not park on WIP.",
        )
        m2 = _write_am(
            scope, "feedback_b.md",
            "Park prior-session debris on a WIP branch. Do not commit directly.",
        )
        payload = (
            '{"detected": true, "conflict_type": "prescriptive", '
            '"members_involved": ['
            f'"{m1.origin_scope}/{m1.path.name}", '
            f'"{m2.origin_scope}/{m2.path.name}"], '
            '"conflicting_passages": ['
            '"Always commit directly to develop.", '
            '"Park prior-session debris on a WIP branch."], '
            '"rationale": "One says commit directly; other says park on WIP."}'
        )
        result = detect_contradictions([m1, m2], _fake_client(payload))
        assert result.detected is True
        assert result.conflict_type == "prescriptive"
        assert len(result.members_involved) == 2
        assert len(result.conflicting_passages) == 2
        assert "commit" in result.conflicting_passages[0].lower()
        assert "WIP" in result.conflicting_passages[1]
        assert "rationale" in result.rationale or len(result.rationale) > 0

    def test_factual_conflict_type_supported(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "X is in city A.")
        m2 = _write_am(scope, "b.md", "X is in city B.")
        payload = (
            '{"detected": true, "conflict_type": "factual", '
            f'"members_involved": ["{m1.origin_scope}/{m1.path.name}", '
            f'"{m2.origin_scope}/{m2.path.name}"], '
            '"conflicting_passages": ["X is in city A.", "X is in city B."], '
            '"rationale": "Incompatible city claims."}'
        )
        result = detect_contradictions([m1, m2], _fake_client(payload))
        assert result.detected is True
        assert result.conflict_type == "factual"

    def test_prose_before_json_is_tolerated(self, tmp_path: Path) -> None:
        """Detector is allowed to prefix or suffix the JSON with prose."""
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "Facts A.")
        m2 = _write_am(scope, "b.md", "Facts B.")
        payload = (
            "Here is the analysis:\n"
            '{"detected": true, "conflict_type": "factual", '
            f'"members_involved": ["{m1.origin_scope}/{m1.path.name}", '
            f'"{m2.origin_scope}/{m2.path.name}"], '
            '"conflicting_passages": ["Facts A.", "Facts B."], '
            '"rationale": "r"}'
            "\nEnd of analysis."
        )
        result = detect_contradictions([m1, m2], _fake_client(payload))
        assert result.detected is True


# ---------------------------------------------------------------------------
# Negative path
# ---------------------------------------------------------------------------


class TestNegativeDetection:
    def test_near_duplicates_not_flagged(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "The build takes 8 minutes.")
        m2 = _write_am(scope, "b.md", "The build takes about 8 minutes.")
        payload = (
            '{"detected": false, "conflict_type": null, '
            '"members_involved": [], "conflicting_passages": [], '
            '"rationale": "Paraphrase of the same fact."}'
        )
        result = detect_contradictions([m1, m2], _fake_client(payload))
        assert result.detected is False
        assert result.conflict_type is None
        assert result.members_involved == []
        assert result.conflicting_passages == []

    def test_singleton_cluster_short_circuits(self, tmp_path: Path) -> None:
        """Size-1 clusters cannot contradict themselves; no API call."""
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "Single fact.")
        client = MagicMock()
        result = detect_contradictions([m1], client)
        assert result.detected is False
        assert result.rationale == "singleton"
        client.messages.create.assert_not_called()

    def test_empty_cluster_short_circuits(self) -> None:
        result = detect_contradictions([], None)
        assert result.detected is False


# ---------------------------------------------------------------------------
# Fallback path
# ---------------------------------------------------------------------------


class TestFallback:
    def test_no_client_returns_llm_unavailable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "A")
        m2 = _write_am(scope, "b.md", "B")
        with caplog.at_level("WARNING"):
            result = detect_contradictions([m1, m2], None)
        assert result.detected is False
        assert result.rationale == "llm-unavailable"
        assert any("no Anthropic client" in rec.message for rec in caplog.records)

    def test_api_exception_falls_back_to_llm_unavailable(
        self, tmp_path: Path,
    ) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "A")
        m2 = _write_am(scope, "b.md", "B")
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("api down")
        result = detect_contradictions([m1, m2], client)
        assert result.detected is False
        assert result.rationale == "llm-unavailable"

    def test_non_json_response_returns_detected_false(
        self, tmp_path: Path,
    ) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "A")
        m2 = _write_am(scope, "b.md", "B")
        result = detect_contradictions(
            [m1, m2], _fake_client("no json here, just prose"),
        )
        assert result.detected is False

    def test_invalid_conflict_type_returns_detected_false(
        self, tmp_path: Path,
    ) -> None:
        """Detector claims detected=true but with an invalid conflict_type."""
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "a.md", "A")
        m2 = _write_am(scope, "b.md", "B")
        payload = (
            '{"detected": true, "conflict_type": "stylistic", '
            '"members_involved": [], "conflicting_passages": [], '
            '"rationale": "r"}'
        )
        result = detect_contradictions([m1, m2], _fake_client(payload))
        assert result.detected is False


# ---------------------------------------------------------------------------
# Prompt construction + trimming
# ---------------------------------------------------------------------------


class TestPromptBuilding:
    def test_body_is_trimmed_to_per_member_cap(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        big = "x" * (PER_MEMBER_BODY_CHARS * 3)
        m1 = _write_am(scope, "a.md", big)
        m2 = _write_am(scope, "b.md", big)
        client = _fake_client(
            '{"detected": false, "conflict_type": null, '
            '"members_involved": [], "conflicting_passages": [], '
            '"rationale": ""}'
        )
        detect_contradictions([m1, m2], client)
        # Each body trims to <= PER_MEMBER_BODY_CHARS chars — the user
        # message we sent should not contain 3× the cap in a row.
        user_content = client.messages.create.call_args.kwargs["messages"][0][
            "content"
        ]
        assert user_content.count("x" * (PER_MEMBER_BODY_CHARS + 1)) == 0
        # Sanity: we still embed the (trimmed) body.
        assert "x" * 100 in user_content

    def test_all_members_mentioned_in_prompt(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope"
        m1 = _write_am(scope, "alpha.md", "A body")
        m2 = _write_am(scope, "bravo.md", "B body")
        m3 = _write_am(scope, "charlie.md", "C body")
        client = _fake_client(
            '{"detected": false, "conflict_type": null, '
            '"members_involved": [], "conflicting_passages": [], '
            '"rationale": ""}'
        )
        detect_contradictions([m1, m2, m3], client)
        user_content = client.messages.create.call_args.kwargs["messages"][0][
            "content"
        ]
        for name in ("alpha.md", "bravo.md", "charlie.md"):
            assert name in user_content


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


class TestContradictionResult:
    def test_defaults(self) -> None:
        r = ContradictionResult(detected=False)
        assert r.detected is False
        assert r.conflict_type is None
        assert r.members_involved == []
        assert r.conflicting_passages == []
        assert r.rationale == ""
