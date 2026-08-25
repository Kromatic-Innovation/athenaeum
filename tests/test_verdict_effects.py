# SPDX-License-Identifier: Apache-2.0
"""Tests for the five-verdict comparator's storage-side effects (issue athenaeum#715,
phase 2; issue athenaeum#658 D2/D3 regression coverage).

Test-class names map to the acceptance criteria in :mod:`athenaeum.verdict_effects`'s
module docstring, mirroring ``tests/test_comparator.py``'s ``TestACn`` convention
(this module numbers its own criteria ``EFn`` since it is not itself issue
athenaeum#715's numbered AC list). Fully offline: no LLM client anywhere, and
``athenaeum.supersession`` is stubbed via ``monkeypatch.setitem(sys.modules, ...)``
rather than assumed to exist (it is a parallel lane's module).
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import athenaeum.verdict_effects as ve_mod
from athenaeum.comparator import (
    COEXIST_SEPARATOR,
    VERDICT_CONTRADICTION,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    VERDICT_SPECIALIZATION,
    VERDICT_UNDERDETERMINED,
    ComparatorPage,
    CompareOutcome,
    page_from_text,
)
from athenaeum.decisions import list_pending_decisions
from athenaeum.models import parse_frontmatter
from athenaeum.tiers import tier4_escalate
from athenaeum.verdict_effects import (
    FOLD_EVIDENCE_DIRNAME,
    EffectResult,
    apply_verdict_effect,
    build_coordinate_request,
    build_fold_evidence,
    write_fold_evidence,
    write_refines_declaration,
)
from athenaeum.verdicts import make_pair_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page(
    page_id: str,
    *,
    name: str | None = None,
    claimed_scope: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    recorded_at: str | None = "2026-01-01T00:00:00+00:00",
    body: str = "some claim text",
) -> ComparatorPage:
    """Build a :class:`ComparatorPage` — mirrors ``test_comparator.py``'s ``_page``
    fixture, including its convention of YAML-quoting date-ish frontmatter
    values so they round-trip as plain strings, not PyYAML timestamps."""
    lines = ["---", f"name: {name or page_id}"]
    if claimed_scope is not None:
        lines.append(f"claimed_scope: {claimed_scope}")
    if valid_from is not None:
        lines.append(f'valid_from: "{valid_from}"')
    if valid_until is not None:
        lines.append(f'valid_until: "{valid_until}"')
    if recorded_at is not None:
        lines.append(f'recorded_at: "{recorded_at}"')
    lines.append("---")
    text = "\n".join(lines) + "\n" + body + "\n"
    return page_from_text(page_id, text)


def _write_page(path: Path, page: ComparatorPage) -> None:
    path.write_text(page.text, encoding="utf-8")


def _outcome(verdict: str | None, **kwargs: Any) -> CompareOutcome:
    return CompareOutcome(verdict=verdict, **kwargs)


class _FakeSupersessionDecision:
    def __init__(
        self,
        action: str,
        *,
        winner_id: str | None = None,
        loser_id: str | None = None,
        located_passages: list[str] | None = None,
        conditions: list[str] | None = None,
        blocked_by: list[str] | None = None,
        reason: str = "",
        rate_limited: bool = False,
    ) -> None:
        self.action = action
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.located_passages = located_passages or []
        self.conditions = conditions or []
        self.blocked_by = blocked_by or []
        self.reason = reason
        self.rate_limited = rate_limited


def _install_fake_supersession(monkeypatch: pytest.MonkeyPatch, decide: Any) -> None:
    fake_mod = types.ModuleType("athenaeum.supersession")
    fake_mod.decide_supersession = decide  # type: ignore[attr-defined]
    fake_mod.SUPERSESSION_APPLIED = "applied"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "athenaeum.supersession", fake_mod)


def _uninstall_supersession(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import athenaeum.supersession`` raise ImportError.

    Deleting the sys.modules entry is NOT enough once the real module exists
    on disk -- the import machinery simply re-imports it. Binding the name to
    ``None`` is the documented way to make an import of it fail, which is the
    condition this branch's fallback is for.
    """
    monkeypatch.setitem(sys.modules, "athenaeum.supersession", None)


# ---------------------------------------------------------------------------
# EF1 — public API shape: no LLM client, dataclass fields, exports
# ---------------------------------------------------------------------------


class TestEF1PublicAPIShape:
    def test_apply_verdict_effect_signature_has_no_llm_client_param(self) -> None:
        sig = inspect.signature(apply_verdict_effect)
        assert "client" not in sig.parameters
        assert "llm" not in sig.parameters
        assert set(sig.parameters) >= {
            "page_a",
            "page_b",
            "outcome",
            "wiki_root",
            "path_a",
            "path_b",
            "config",
            "now",
        }

    def test_effect_result_fields(self) -> None:
        result = EffectResult(verdict=VERDICT_DISTINCT, action="noop")
        assert result.verdict == VERDICT_DISTINCT
        assert result.action == "noop"
        assert result.artifacts == []
        assert result.queued == []
        assert result.details == {}

    def test_effect_result_is_frozen(self) -> None:
        result = EffectResult(verdict=VERDICT_DISTINCT, action="noop")
        with pytest.raises(Exception):
            result.action = "changed"  # type: ignore[misc]

    def test_module_exports_expected_names(self) -> None:
        assert set(ve_mod.__all__) == {
            "FOLD_EVIDENCE_DIRNAME",
            "EffectResult",
            "apply_verdict_effect",
            "build_coordinate_request",
            "build_fold_evidence",
            "write_fold_evidence",
            "write_refines_declaration",
        }
        for n in ve_mod.__all__:
            assert hasattr(ve_mod, n)

    def test_fold_evidence_dirname_constant(self) -> None:
        assert FOLD_EVIDENCE_DIRNAME == "_fold_evidence"

    def test_unknown_verdict_raises_value_error(self) -> None:
        outcome = _outcome("not-a-real-verdict")
        with pytest.raises(ValueError):
            apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=Path("/tmp/wontuse"))

    def test_none_verdict_raises_value_error_not_silent_noop(self, tmp_path: Path) -> None:
        outcome = _outcome(None, reason="llm-unavailable")
        with pytest.raises(ValueError, match="unavailable"):
            apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=tmp_path)


# ---------------------------------------------------------------------------
# EF2 — module never imports/uses an LLM backend
# ---------------------------------------------------------------------------


class TestEF2NoLLMBackend:
    def test_module_source_never_mentions_an_llm_backend(self) -> None:
        src = Path(ve_mod.__file__).read_text(encoding="utf-8")
        # Only mentions of these tokens allowed are inside the module
        # docstring's prose explaining the absence — assert the actual
        # import/usage surface is clean by checking no import statement
        # references them.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "anthropic" not in stripped
                assert "provider" not in stripped
                assert "LLMBackend" not in stripped

    def test_no_client_or_usage_kwarg_anywhere_in_public_api(self) -> None:
        for name in ve_mod.__all__:
            obj = getattr(ve_mod, name)
            if inspect.isfunction(obj):
                params = set(inspect.signature(obj).parameters)
                assert "client" not in params
                assert "usage" not in params


# ---------------------------------------------------------------------------
# EF3 — duplicate: evidence artifact, canonical side rule, no merged body
# ---------------------------------------------------------------------------


class TestEF3DuplicateEvidence:
    def test_build_fold_evidence_never_contains_confidence_or_similarity(self) -> None:
        page_a = _page("alpha", body="The sky is blue. Something else entirely.")
        page_b = _page("beta", body="The sky is blue. A different unrelated line.")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        low = text.lower()
        assert "confidence" not in low
        assert "similarity" not in low
        assert "cosine" not in low

    def test_build_fold_evidence_states_it_is_not_a_merged_body(self) -> None:
        page_a = _page("alpha", body="claim one")
        page_b = _page("beta", body="claim two")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "not a merged" in text.lower() or "never a merged" in text.lower()
        assert "athenaeum#658" in text
        assert "athenaeum#715" in text

    def test_shared_sentences_appear_side_by_side(self) -> None:
        page_a = _page("alpha", body="The API timeout is 30 seconds. Unrelated line A.")
        page_b = _page("beta", body="Different intro. The API timeout is 30 seconds.")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "The API timeout is 30 seconds." in text
        assert "Side A:" in text
        assert "Side B:" in text

    def test_no_shared_sentences_still_produces_evidence_without_fabricating_overlap(self) -> None:
        page_a = _page("alpha", body="Completely different phrasing about topic one.")
        page_b = _page("beta", body="Totally other wording about the same topic.")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "No structurally-shared sentence" in text

    def test_coordinate_match_table_lists_widened_dimensions(self) -> None:
        page_a = _page("alpha", claimed_scope="engineering", body="x")
        page_b = _page("beta", claimed_scope="engineering", body="x restated")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={"scope": "engineering"})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "scope" in text
        assert "Coordinate match table" in text

    def test_write_fold_evidence_writes_under_fold_evidence_dir(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        page_a = _page("alpha", body="x")
        page_b = _page("beta", body="y")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        path = write_fold_evidence(page_a, page_b, outcome, wiki_root=wiki_root)
        pair_key = make_pair_key("alpha", "beta")
        assert path == wiki_root / FOLD_EVIDENCE_DIRNAME / f"{pair_key}.md"
        assert path.is_file()
        assert str(path).startswith(str(wiki_root))


class TestEF4CanonicalSideRule:
    def test_side_with_matching_widened_coordinate_wins(self) -> None:
        # side b's own scope already equals the widened value -> b is canonical.
        page_a = _page("alpha", claimed_scope="eng", recorded_at="2026-02-01T00:00:00+00:00")
        page_b = _page("beta", claimed_scope="eng/backend", recorded_at="2026-01-01T00:00:00+00:00")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={"scope": "eng"})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "**Chosen**: side a (`alpha`)" in text

    def test_tie_breaks_on_earlier_recorded_at(self) -> None:
        page_a = _page("alpha", recorded_at="2026-03-01T00:00:00+00:00")
        page_b = _page("beta", recorded_at="2026-01-01T00:00:00+00:00")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "**Chosen**: side b (`beta`)" in text

    def test_tie_breaks_on_page_id_when_recorded_at_also_ties(self) -> None:
        page_a = _page("alpha", recorded_at="2026-01-01T00:00:00+00:00")
        page_b = _page("beta", recorded_at="2026-01-01T00:00:00+00:00")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "**Chosen**: side a (`alpha`)" in text

    def test_rule_is_documented_in_the_evidence_text(self) -> None:
        page_a = _page("alpha")
        page_b = _page("beta")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        text = build_fold_evidence(page_a, page_b, outcome)
        assert "Rule (structural, not a model-scored value)" in text


class TestEF5DuplicateEffectQueuesProposal(object):
    def test_apply_verdict_effect_writes_evidence_and_queues(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        page_a = _page("alpha", body="claim one")
        page_b = _page("beta", body="claim one restated")
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        result = apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root)
        assert result.verdict == VERDICT_DUPLICATE
        assert result.action == "fold-proposal"
        assert len(result.artifacts) == 1
        assert Path(result.artifacts[0]).is_file()
        assert result.queued
        assert "canonical_side" in result.details
        questions = wiki_root / "_pending_questions.md"
        assert questions.is_file()
        assert "Approve folding" in questions.read_text(encoding="utf-8")

    def test_duplicate_never_writes_pending_merges_file(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        apply_verdict_effect(_page("alpha"), _page("beta"), outcome, wiki_root=wiki_root)
        assert not (wiki_root / "_pending_merges.md").exists()

    def test_duplicate_queue_item_is_visible_in_unified_decisions(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        apply_verdict_effect(_page("alpha"), _page("beta"), outcome, wiki_root=wiki_root)
        decisions = list_pending_decisions(wiki_root)
        assert any(d["type"] == "question" for d in decisions)
        assert all(d["confidence"] is None for d in decisions if d["type"] == "question")


# ---------------------------------------------------------------------------
# EF6 — specialization: refines: written on the specific side only
# ---------------------------------------------------------------------------


class TestEF6SpecializationWritesRefines:
    def test_specific_side_a_gets_refines_naming_general_b(self, tmp_path: Path) -> None:
        path_a = tmp_path / "alpha.md"
        path_b = tmp_path / "beta.md"
        page_a = _page("alpha", claimed_scope="eng", body="specific claim")
        page_b = _page("beta", claimed_scope="eng/backend", body="general claim")
        _write_page(path_a, page_a)
        _write_page(path_b, page_b)
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side="a")
        wiki_root = tmp_path / "wiki"
        result = apply_verdict_effect(
            page_a, page_b, outcome, wiki_root=wiki_root, path_a=path_a, path_b=path_b
        )
        assert result.action == "refines-written"
        assert result.artifacts == [str(path_a)]
        meta, _body = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        assert meta["refines"] == ["beta"]
        # General side untouched.
        assert path_b.read_text(encoding="utf-8") == page_b.text

    def test_specific_side_b_gets_refines_naming_general_a(self, tmp_path: Path) -> None:
        path_a = tmp_path / "alpha.md"
        path_b = tmp_path / "beta.md"
        page_a = _page("alpha", body="general claim")
        page_b = _page("beta", body="specific claim")
        _write_page(path_a, page_a)
        _write_page(path_b, page_b)
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side="b")
        wiki_root = tmp_path / "wiki"
        result = apply_verdict_effect(
            page_a, page_b, outcome, wiki_root=wiki_root, path_a=path_a, path_b=path_b
        )
        assert result.action == "refines-written"
        meta, _body = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        assert meta["refines"] == ["alpha"]
        assert path_a.read_text(encoding="utf-8") == page_a.text

    def test_write_refines_declaration_is_idempotent(self, tmp_path: Path) -> None:
        path_a = tmp_path / "alpha.md"
        _write_page(path_a, _page("alpha"))
        write_refines_declaration(path_a, "beta")
        write_refines_declaration(path_a, "beta")
        meta, _body = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        assert meta["refines"] == ["beta"]

    def test_write_refines_declaration_preserves_body(self, tmp_path: Path) -> None:
        path_a = tmp_path / "alpha.md"
        page = _page("alpha", body="the actual claim text\nsecond line")
        _write_page(path_a, page)
        write_refines_declaration(path_a, "beta")
        _meta, body = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        assert "the actual claim text" in body
        assert "second line" in body


class TestEF7SpecializationNoSilentNoop:
    def test_none_specific_side_queues_not_noop(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        page_a = _page("alpha")
        page_b = _page("beta")
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side=None)
        result = apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root)
        assert result.action == "queued"
        assert result.action != "noop"
        assert result.details["reason"] == "no_specific_side_determined"
        assert result.queued

    def test_missing_path_for_specific_side_queues_with_reason(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        page_a = _page("alpha")
        page_b = _page("beta")
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side="a")
        result = apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root, path_a=None)
        assert result.action == "queued"
        assert result.details["reason"] == "specific_side_path_missing"
        assert result.details["general_id"] == "beta"

    def test_missing_path_never_raises_and_never_writes_a_page(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        page_a = _page("alpha")
        page_b = _page("beta")
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side="b")
        result = apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root, path_b=None)
        assert result.artifacts == []
        assert result.action == "queued"


class TestEF8RefinesReservedForSpecialization:
    """athenaeum#658 D3: `reject`/any other verdict must never write a false
    `refines:`. Every other branch must leave `refines:` entirely untouched."""

    def _run(
        self,
        tmp_path: Path,
        outcome: CompareOutcome,
        *,
        monkeypatch: pytest.MonkeyPatch | None = None,
    ) -> tuple[EffectResult, Path, Path]:
        wiki_root = tmp_path / "wiki"
        path_a = tmp_path / "alpha.md"
        path_b = tmp_path / "beta.md"
        page_a = _page("alpha", body="claim a")
        page_b = _page("beta", body="claim b")
        _write_page(path_a, page_a)
        _write_page(path_b, page_b)
        result = apply_verdict_effect(
            page_a, page_b, outcome, wiki_root=wiki_root, path_a=path_a, path_b=path_b
        )
        return result, path_a, path_b

    def test_distinct_never_writes_refines(self, tmp_path: Path) -> None:
        outcome = _outcome(VERDICT_DISTINCT, separator=["scope"])
        _result, path_a, path_b = self._run(tmp_path, outcome)
        assert "refines" not in path_a.read_text(encoding="utf-8")
        assert "refines" not in path_b.read_text(encoding="utf-8")

    def test_underdetermined_never_writes_refines(self, tmp_path: Path) -> None:
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope"])
        _result, path_a, path_b = self._run(tmp_path, outcome)
        assert "refines" not in path_a.read_text(encoding="utf-8")
        assert "refines" not in path_b.read_text(encoding="utf-8")

    def test_duplicate_never_writes_refines(self, tmp_path: Path) -> None:
        outcome = _outcome(VERDICT_DUPLICATE, widened_coords={})
        _result, path_a, path_b = self._run(tmp_path, outcome)
        assert "refines" not in path_a.read_text(encoding="utf-8")
        assert "refines" not in path_b.read_text(encoding="utf-8")

    def test_contradiction_never_writes_refines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _uninstall_supersession(monkeypatch)
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"])
        _result, path_a, path_b = self._run(tmp_path, outcome)
        assert "refines" not in path_a.read_text(encoding="utf-8")
        assert "refines" not in path_b.read_text(encoding="utf-8")

    def test_only_specialization_ever_calls_write_refines_declaration(self) -> None:
        src = Path(ve_mod.__file__).read_text(encoding="utf-8")
        call_sites = [
            i
            for i, line in enumerate(src.splitlines())
            if "write_refines_declaration(" in line and "def write_refines_declaration" not in line
        ]
        assert len(call_sites) == 1  # only inside _apply_specialization


# ---------------------------------------------------------------------------
# EF9 — distinct: ledger-only breadcrumb, never writes
# ---------------------------------------------------------------------------


class TestEF9Distinct:
    def test_distinct_action_is_noop(self) -> None:
        outcome = _outcome(VERDICT_DISTINCT, separator=["scope"])
        result = apply_verdict_effect(
            _page("a"), _page("b"), outcome, wiki_root=Path("/tmp/unused-wiki")
        )
        assert result.action == "noop"
        assert result.artifacts == []
        assert result.queued == []

    def test_distinct_breadcrumbs_separator(self) -> None:
        outcome = _outcome(VERDICT_DISTINCT, separator=["scope", "valid-time"])
        result = apply_verdict_effect(
            _page("a"), _page("b"), outcome, wiki_root=Path("/tmp/unused-wiki")
        )
        assert result.details["separator"] == ["scope", "valid-time"]

    def test_distinct_breadcrumbs_coexist_marker(self) -> None:
        outcome = _outcome(VERDICT_DISTINCT, separator=[COEXIST_SEPARATOR])
        result = apply_verdict_effect(
            _page("a"), _page("b"), outcome, wiki_root=Path("/tmp/unused-wiki")
        )
        assert result.details["coexist"] is True
        assert result.details["separator"] == [COEXIST_SEPARATOR]

    def test_distinct_never_touches_either_page(self, tmp_path: Path) -> None:
        path_a = tmp_path / "alpha.md"
        path_b = tmp_path / "beta.md"
        page_a = _page("alpha")
        page_b = _page("beta")
        _write_page(path_a, page_a)
        _write_page(path_b, page_b)
        outcome = _outcome(VERDICT_DISTINCT, separator=["scope"])
        apply_verdict_effect(
            page_a, page_b, outcome, wiki_root=tmp_path / "wiki", path_a=path_a, path_b=path_b
        )
        assert path_a.read_text(encoding="utf-8") == page_a.text
        assert path_b.read_text(encoding="utf-8") == page_b.text

    def test_distinct_never_creates_any_wiki_root_files(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_DISTINCT, separator=["scope"])
        apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        assert not wiki_root.exists()


# ---------------------------------------------------------------------------
# EF10 — underdetermined: small coordinate request, no bodies, no merge
# ---------------------------------------------------------------------------


class TestEF10UnderdeterminedCoordinateRequest:
    def test_request_names_missing_dimensions(self) -> None:
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope", "valid-time"])
        req = build_coordinate_request(_page("a"), _page("b"), outcome)
        assert req["dimensions"] == ["scope", "valid-time"]
        assert "scope" in req["question"]
        assert "valid-time" in req["question"]

    def test_request_never_embeds_page_bodies(self) -> None:
        page_a = _page("alpha", body="a very long confidential body of claim text")
        page_b = _page("beta", body="another long body that must not leak into the queue")
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope"])
        req = build_coordinate_request(page_a, page_b, outcome)
        blob = str(req)
        assert "confidential body" not in blob
        assert "must not leak" not in blob

    def test_request_carries_only_short_id_and_title_per_side(self) -> None:
        page_a = _page("alpha", name="Alpha Page")
        page_b = _page("beta", name="Beta Page")
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope"])
        req = build_coordinate_request(page_a, page_b, outcome)
        assert req["sides"]["a"] == {"id": "alpha", "title": "Alpha Page"}
        assert req["sides"]["b"] == {"id": "beta", "title": "Beta Page"}

    def test_apply_verdict_effect_queues_and_never_creates_merge_proposal(
        self, tmp_path: Path
    ) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope"])
        result = apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        assert result.action == "queued"
        assert result.queued
        assert not (wiki_root / "_pending_merges.md").exists()
        assert "conflict" not in result.details

    def test_apply_verdict_effect_sets_no_conflict_flag(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope"])
        result = apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        for key in result.details:
            assert "conflict" not in key

    def test_question_first_line_is_the_small_answerable_question(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        outcome = _outcome(VERDICT_UNDERDETERMINED, missing=["scope"])
        apply_verdict_effect(_page("alpha"), _page("beta"), outcome, wiki_root=wiki_root)
        decisions = list_pending_decisions(wiki_root)
        assert len(decisions) == 1
        assert "differ by scope" in decisions[0]["summary"]


# ---------------------------------------------------------------------------
# EF11 — contradiction: supersession routing (applied / queue / unavailable)
# ---------------------------------------------------------------------------


class TestEF11ContradictionSupersessionUnavailable:
    def test_missing_supersession_module_falls_back_to_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _uninstall_supersession(monkeypatch)
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["a says X", "b says Y"])
        result = apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        assert result.action == "queued"
        assert result.details["supersession_available"] is False
        assert result.queued

    def test_unavailable_queue_carries_located_passages_not_full_bodies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _uninstall_supersession(monkeypatch)
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        page_a = _page("alpha", body="a" * 5000)
        page_b = _page("beta", body="b" * 5000)
        outcome = _outcome(
            VERDICT_CONTRADICTION,
            conflicting_passages=["short conflict snippet a", "short conflict snippet b"],
        )
        apply_verdict_effect(page_a, page_b, outcome, wiki_root=wiki_root)
        text = (wiki_root / "_pending_questions.md").read_text(encoding="utf-8")
        assert "short conflict snippet a" in text
        assert "a" * 5000 not in text


class TestEF12ContradictionSupersessionApplied:
    def test_applied_is_recorded_not_enacted_by_this_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        decision = _FakeSupersessionDecision(
            "applied", winner_id="alpha", loser_id="beta", located_passages=["p1"]
        )
        _install_fake_supersession(monkeypatch, lambda *a, **kw: decision)
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"])
        result = apply_verdict_effect(_page("alpha"), _page("beta"), outcome, wiki_root=wiki_root)
        assert result.action == "superseded"
        assert result.details["winner_id"] == "alpha"
        assert result.details["loser_id"] == "beta"
        assert result.queued == []
        # This module doesn't itself write any file when supersession applied —
        # enactment is entirely athenaeum.supersession's job.
        assert result.artifacts == []
        assert not wiki_root.exists()

    def test_applied_passes_wiki_root_config_and_now_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def _decide(page_a, page_b, outcome, *, wiki_root, config=None, now=None, live_claims=()):
            calls.append({"wiki_root": wiki_root, "config": config})
            return _FakeSupersessionDecision("applied", winner_id="a", loser_id="b")

        _install_fake_supersession(monkeypatch, _decide)
        wiki_root = tmp_path / "wiki"
        cfg = {"some": "config"}
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p"])
        apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root, config=cfg)
        assert calls[0]["wiki_root"] == wiki_root
        assert calls[0]["config"] == cfg


class TestEF13ContradictionSupersessionQueues:
    def test_queue_action_routes_with_blocked_by(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        decision = _FakeSupersessionDecision(
            "queue", blocked_by=["needs-human-ratification"], reason="ambiguous winner"
        )
        _install_fake_supersession(monkeypatch, lambda *a, **kw: decision)
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"])
        result = apply_verdict_effect(_page("alpha"), _page("beta"), outcome, wiki_root=wiki_root)
        assert result.action == "queued"
        assert result.details["blocked_by"] == ["needs-human-ratification"]
        text = (wiki_root / "_pending_questions.md").read_text(encoding="utf-8")
        assert "needs-human-ratification" in text

    def test_rate_limited_flag_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        decision = _FakeSupersessionDecision("queue", rate_limited=True)
        _install_fake_supersession(monkeypatch, lambda *a, **kw: decision)
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"])
        result = apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        assert result.details["rate_limited"] is True


# ---------------------------------------------------------------------------
# EF14 — no confidence/similarity scalars anywhere in emitted artifacts
# ---------------------------------------------------------------------------


class TestEF14NoConfidenceOrSimilarityScalars:
    def _all_results(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[EffectResult]:
        _uninstall_supersession(monkeypatch)
        results = []
        wr = tmp_path / "wiki1"
        results.append(
            apply_verdict_effect(
                _page("a1"),
                _page("b1"),
                _outcome(VERDICT_DUPLICATE, widened_coords={}),
                wiki_root=wr,
            )
        )
        wr = tmp_path / "wiki2"
        results.append(
            apply_verdict_effect(
                _page("a2"),
                _page("b2"),
                _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side=None),
                wiki_root=wr,
            )
        )
        wr = tmp_path / "wiki3"
        results.append(
            apply_verdict_effect(
                _page("a3"),
                _page("b3"),
                _outcome(VERDICT_DISTINCT, separator=["scope"]),
                wiki_root=wr,
            )
        )
        wr = tmp_path / "wiki4"
        results.append(
            apply_verdict_effect(
                _page("a4"),
                _page("b4"),
                _outcome(VERDICT_UNDERDETERMINED, missing=["scope"]),
                wiki_root=wr,
            )
        )
        wr = tmp_path / "wiki5"
        results.append(
            apply_verdict_effect(
                _page("a5"),
                _page("b5"),
                _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"]),
                wiki_root=wr,
            )
        )
        return results

    def test_no_confidence_key_in_any_details_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for result in self._all_results(tmp_path, monkeypatch):
            assert "confidence" not in result.details
            assert "similarity" not in result.details
            assert "cosine" not in result.details

    def test_no_confidence_word_in_any_pending_questions_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._all_results(tmp_path, monkeypatch)
        for wiki_root in [tmp_path / f"wiki{i}" for i in (1, 2, 4, 5)]:
            qpath = wiki_root / "_pending_questions.md"
            if qpath.is_file():
                # Strip any embedded tmp-path/evidence-path strings first —
                # pytest's own tmp_path can legitimately contain the
                # substring "confidence" (from THIS test's name), which
                # would otherwise be a false positive unrelated to anything
                # the module emitted.
                text = qpath.read_text(encoding="utf-8").replace(str(tmp_path), "")
                assert "confidence" not in text.lower()

    def test_no_pending_merges_file_ever_created_by_this_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._all_results(tmp_path, monkeypatch)
        for i in range(1, 6):
            assert not (tmp_path / f"wiki{i}" / "_pending_merges.md").exists()


# ---------------------------------------------------------------------------
# EF15 — no silent no-ops: every non-primary action explains itself
# ---------------------------------------------------------------------------


class TestEF15NoSilentNoops:
    def test_specialization_queue_paths_always_carry_a_reason(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_SPECIALIZATION, separator=["scope"], specific_side=None)
        result = apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        assert result.action == "queued"
        assert "reason" in result.details and result.details["reason"]

    def test_contradiction_unavailable_supersession_explains_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _uninstall_supersession(monkeypatch)
        wiki_root = tmp_path / "wiki"
        outcome = _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"])
        result = apply_verdict_effect(_page("a"), _page("b"), outcome, wiki_root=wiki_root)
        assert result.details.get("supersession_available") is False

    def test_distinct_is_the_only_legitimate_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for result in TestEF14NoConfidenceOrSimilarityScalars()._all_results(tmp_path, monkeypatch):
            if result.action == "noop":
                assert result.verdict == VERDICT_DISTINCT
            else:
                # every non-noop, non-primary-success action carries details
                assert result.details


# ---------------------------------------------------------------------------
# EF16 — all I/O stays under wiki_root; never touches ~/knowledge
# ---------------------------------------------------------------------------


class TestEF16IOScopedToWikiRoot:
    def test_no_write_reaches_outside_wiki_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_sentinel = tmp_path / "poisoned-home"
        home_sentinel.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_sentinel))
        _uninstall_supersession(monkeypatch)
        wiki_root = tmp_path / "wiki"
        apply_verdict_effect(
            _page("a"),
            _page("b"),
            _outcome(VERDICT_DUPLICATE, widened_coords={}),
            wiki_root=wiki_root,
        )
        apply_verdict_effect(
            _page("c"),
            _page("d"),
            _outcome(VERDICT_UNDERDETERMINED, missing=["scope"]),
            wiki_root=wiki_root,
        )
        apply_verdict_effect(
            _page("e"),
            _page("f"),
            _outcome(VERDICT_CONTRADICTION, conflicting_passages=["p1", "p2"]),
            wiki_root=wiki_root,
        )
        assert list(home_sentinel.iterdir()) == []

    def test_evidence_and_queue_paths_are_all_under_wiki_root(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        result = apply_verdict_effect(
            _page("a"),
            _page("b"),
            _outcome(VERDICT_DUPLICATE, widened_coords={}),
            wiki_root=wiki_root,
        )
        for artifact in result.artifacts:
            assert Path(artifact).resolve().is_relative_to(wiki_root.resolve())


# ---------------------------------------------------------------------------
# EF17 — house style: SPDX header, future-annotations, offline test posture
# ---------------------------------------------------------------------------


class TestEF17HouseStyle:
    def test_spdx_header_is_first_line(self) -> None:
        src = Path(ve_mod.__file__).read_text(encoding="utf-8")
        assert src.splitlines()[0] == "# SPDX-License-Identifier: Apache-2.0"

    def test_future_annotations_imported(self) -> None:
        src = Path(ve_mod.__file__).read_text(encoding="utf-8")
        assert "from __future__ import annotations" in src

    def test_issue_references_are_never_bare(self) -> None:
        src = Path(ve_mod.__file__).read_text(encoding="utf-8")
        import re as _re

        bare = _re.findall(r"(?<!athenaeum)#\d{2,}", src)
        assert bare == []

    def test_module_docstring_cites_both_issues(self) -> None:
        assert ve_mod.__doc__ is not None
        assert "athenaeum#715" in ve_mod.__doc__
        assert "athenaeum#658" in ve_mod.__doc__

    def test_tier4_escalate_used_offline_without_stubbing(self, tmp_path: Path) -> None:
        # Sanity check on the routing choice itself (module docstring,
        # "Queue routing"): tier4_escalate is safely callable with no LLM
        # client and no config, exactly as this module calls it.
        from athenaeum.models import EscalationItem

        pending = tmp_path / "_pending_questions.md"
        tier4_escalate(
            [
                EscalationItem(
                    raw_ref="comparator:test",
                    entity_name="test",
                    conflict_type="ambiguous",
                    description="A small question.",
                )
            ],
            pending,
        )
        assert pending.is_file()
