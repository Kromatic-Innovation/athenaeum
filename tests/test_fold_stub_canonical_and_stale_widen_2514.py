# SPDX-License-Identifier: Apache-2.0
"""An approved fold stops stalling on a widen question (issue athenaeum#2514).

- ``TestStubCanonicalBackfill`` (AC3) -- a canonical page with NO
  frontmatter (or an empty ``{}`` block) is given the folded sources'
  separator coordinates in the fold PREFLIGHT, before the widen check, so
  the athenaeum#2115 AC3 "the canonical keeps its own class" rule has
  something to keep. The repair therefore reaches a fold that has never
  applied -- the only kind that can be stuck.
- ``TestStaleWidenQuestionWithdrawal`` (AC4) -- an open widen question on a
  dimension the fold settles WITHOUT a human is withdrawn on that same
  ``ingest-answers`` tick, even when the fold goes on to be refused on a
  different dimension. Twelve such questions, made stale by athenaeum#2083
  on 2026-10-22, were still open three weeks later because the only
  withdrawal ran after a fold applied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.answers import parse_pending_questions, raise_pending_question
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import (
    parse_pending_merges,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.provenance import read_merge_provenance
from tests.conftest import init_git_repo

#: The exact shape the live stubs carry: a "## From wiki/..." heading and no
#: frontmatter whatsoever.
HEADING_ONLY_STUB = "## From wiki/old-0.md\n\nWhatever the earlier pass wrote.\n"
#: The sixth live stub's shape: a frontmatter block containing an empty map.
EMPTY_FRONTMATTER_STUB = "---\n{}\n---\n## From wiki/old-0.md\n\nBody.\n"


def _page(path: Path, *, name: str, extra: dict[str, str], body: str = "x\n") -> None:
    lines = [f"name: {name}", "type: concept"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8")


def _setup(
    wiki: Path,
    *,
    canonical_text: str | None,
    canonical_extra: dict[str, str] | None,
    source_extras: list[dict[str, str]],
) -> tuple[Path, Path, str]:
    """A git wiki with one canonical page, N sources, and one unresolved
    ``fold-into-existing`` proposal. Returns ``(target, merges_path, id)``."""
    wiki.mkdir(parents=True)
    target = wiki / "canonical.md"
    if canonical_text is not None:
        target.write_text(canonical_text, encoding="utf-8")
    else:
        _page(target, name="Canonical", extra=canonical_extra or {}, body="OLD BODY\n")
    sources = []
    for i, extra in enumerate(source_extras):
        src = wiki / f"old-{i}.md"
        _page(src, name=f"Old {i}", extra=extra, body=f"{i}\n")
        sources.append(src)
    init_git_repo(wiki)
    merges_path = wiki / "_pending_merges.md"
    write_pending_merge(
        merges_path,
        merge_target_name="Canonical",
        sources=[str(s) for s in sources],
        rationale="athenaeum#1577 qualified-name scan: same qualified name",
        draft_merged_body="NEW BODY\n",
        confidence=0.9,
        write_kind="fold-into-existing",
    )
    return target, merges_path, parse_pending_merges(merges_path)[0].id


class TestStubCanonicalBackfill:
    """AC3."""

    @pytest.mark.parametrize(
        "stub_text", [HEADING_ONLY_STUB, EMPTY_FRONTMATTER_STUB], ids=["heading", "empty-map"]
    )
    def test_stub_canonical_applies_on_the_first_attempt(
        self, tmp_path: Path, stub_text: str
    ) -> None:
        """Counter-example (the behaviour this replaces): the fold is
        refused ``fold_widen_impossible`` and raises a question no backfill
        will ever answer, because the backfill only ran inside a fold that
        applied."""
        wiki = tmp_path / "wiki"
        target, merges_path, pm_id = _setup(
            wiki,
            canonical_text=stub_text,
            canonical_extra=None,
            source_extras=[{"memory_class": "guideline"}, {"memory_class": "fact"}],
        )
        meta_before, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert not meta_before

        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert result["ok"] is True, result
        # The FIRST source carrying the coordinate wins, and the differing
        # one is recorded rather than refused -- the athenaeum#2115 AC3 rule,
        # which now has a canonical value to apply.
        meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert meta["memory_class"] == "guideline"
        kept = read_merge_provenance(wiki)[0]["coordinates_kept"]["memory-class"]
        assert kept == {"kept": "guideline", "differing": ["fact"]}
        # Nothing was escalated to a human.
        assert not (wiki / "_pending_questions.md").exists()

    def test_backfill_covers_every_separator_dimension(self) -> None:
        """The helper is dimension-generic: it fills EVERY separator
        coordinate, from the first source carrying each, and reaches the
        two-key ``valid-time`` shape as well as the bare-key ones. Asserted
        on the helper directly because only ``memory-class`` has a
        downstream rule that reads the canonical's own value, so a
        fold-level assertion could not tell the other dimensions apart from
        the widen that already covers the draft."""
        from athenaeum.dimensions import DEFAULT_REGISTRY
        from athenaeum.pending_merges import _backfill_stub_canonical_coordinates

        stub: dict = {}
        filled = _backfill_stub_canonical_coordinates(
            stub,
            [
                {"memory_class": "guideline", "subject": "subject-y"},
                {
                    "memory_class": "fact",
                    "claimed_scope": "work/orca",
                    "valid_from": "2026-01-01",
                    "valid_until": "2026-06-30",
                },
            ],
            registry=DEFAULT_REGISTRY,
            human_approved=True,
        )
        assert set(filled) == {"memory-class", "subject", "scope", "valid-time"}
        # First source carrying each coordinate wins.
        assert stub["memory_class"] == "guideline"
        assert stub["subject"] == "subject-y"
        assert stub["claimed_scope"] == "work/orca"
        # ``valid_until`` round-trips back to its INCLUSIVE on-disk form.
        assert stub["valid_from"] == "2026-01-01"
        assert stub["valid_until"] == "2026-06-30"

    def test_backfill_helper_refuses_a_non_stub_and_a_non_human_fold(self) -> None:
        from athenaeum.dimensions import DEFAULT_REGISTRY
        from athenaeum.pending_merges import _backfill_stub_canonical_coordinates

        non_stub = {"name": "Canonical"}
        assert (
            _backfill_stub_canonical_coordinates(
                non_stub,
                [{"memory_class": "fact"}],
                registry=DEFAULT_REGISTRY,
                human_approved=True,
            )
            == {}
        )
        assert non_stub == {"name": "Canonical"}

        stub: dict = {}
        assert (
            _backfill_stub_canonical_coordinates(
                stub,
                [{"memory_class": "fact"}],
                registry=DEFAULT_REGISTRY,
                human_approved=False,
            )
            == {}
        )
        assert stub == {}

    def test_a_canonical_with_frontmatter_is_never_backfilled(
        self, tmp_path: Path
    ) -> None:
        """The negative control: only a STUB is repaired. A canonical that
        carries frontmatter but deliberately omits one coordinate still
        reaches the human the question exists to ask."""
        wiki = tmp_path / "wiki"
        _, merges_path, pm_id = _setup(
            wiki,
            canonical_text=None,
            canonical_extra={"claimed_scope": "work/orca"},
            source_extras=[{"memory_class": "guideline"}, {"memory_class": "fact"}],
        )
        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        assert result["error_code"] == "fold_widen_impossible"
        assert result["dimension"] == "memory-class"

    def test_a_non_human_fold_is_never_backfilled(self, tmp_path: Path) -> None:
        """A backfill invents a coordinate for a page from its sources. The
        human approval is what licenses that, exactly as it licenses the
        athenaeum#2115 AC3 rule it feeds, so an auto-applied fold keeps the
        pre-existing hard narrowing refusal."""
        wiki = tmp_path / "wiki"
        _, merges_path, pm_id = _setup(
            wiki,
            canonical_text=HEADING_ONLY_STUB,
            canonical_extra=None,
            source_extras=[{"memory_class": "guideline"}, {"memory_class": "fact"}],
        )
        result = resolve_merge(
            merges_path, pm_id, "approve", wiki_root=wiki, auto_applied=True
        )
        assert result["ok"] is False
        assert result["error_code"] == "fold_narrows_coordinate"


class TestStaleWidenQuestionWithdrawal:
    """AC4."""

    def _stalled(self, wiki: Path) -> tuple[Path, str]:
        """A fold that settles `subject` on its own (athenaeum#2083) but is
        refused on `memory-class` -- the exact live shape."""
        target, merges_path, pm_id = _setup(
            wiki,
            canonical_text=None,
            canonical_extra={"subject": "subject-x"},
            source_extras=[
                {"subject": "subject-y", "memory_class": "guideline"},
                {"subject": "subject-z", "memory_class": "fact"},
            ],
        )
        return merges_path, pm_id

    def test_settled_dimension_is_withdrawn_though_the_fold_never_applies(
        self, tmp_path: Path
    ) -> None:
        """Counter-example (the behaviour this replaces): the `subject`
        question stays open forever, because the withdrawal only fired once
        the fold applied -- and this fold never does."""
        wiki = tmp_path / "wiki"
        merges_path, pm_id = self._stalled(wiki)
        pending = wiki / "_pending_questions.md"
        raise_pending_question(
            pending,
            question=(
                "Fold of 'canonical' cannot auto-widen its 'subject' "
                "coordinate — what should the canonical's value be?"
            ),
            context=f"Human-approved fold (proposal {pm_id}) into 'canonical' x.",
            entity="canonical",
            source=str(wiki / "canonical.md"),
        )

        result = resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        # The fold itself is still refused -- on a DIFFERENT dimension.
        assert result["ok"] is False
        assert result["error_code"] == "fold_widen_impossible"
        assert result["dimension"] == "memory-class"
        by_dimension = {
            "subject" if "'subject'" in q.question else "memory-class": q
            for q in parse_pending_questions(pending)
        }
        # ...and `subject` no longer waits on anybody.
        assert by_dimension["subject"].answered is True
        assert "no longer needs an answer" in "\n".join(
            by_dimension["subject"].answer_lines
        )
        # The dimension that genuinely still needs a human is untouched.
        assert by_dimension["memory-class"].answered is False

    def test_withdrawal_happens_on_a_real_ingest_answers_tick(
        self, tmp_path: Path
    ) -> None:
        """The AC names the tick, so drive the whole tick: an approve answer
        file under ``raw/answers/`` -> ``apply_decision_answers`` ->
        ``resolve_merge``. The fold is still refused on `memory-class`, and
        `subject`'s question is withdrawn anyway."""
        from athenaeum.decision_answers import (
            apply_decision_answers,
            write_decision_answer,
        )

        wiki = tmp_path / "wiki"
        _, pm_id = self._stalled(wiki)
        raw_root = tmp_path / "raw"
        pending = wiki / "_pending_questions.md"
        raise_pending_question(
            pending,
            question=(
                "Fold of 'canonical' cannot auto-widen its 'subject' "
                "coordinate — what should the canonical's value be?"
            ),
            context=f"Human-approved fold (proposal {pm_id}) into 'canonical' x.",
            entity="canonical",
            source=str(wiki / "canonical.md"),
        )
        write_decision_answer(
            raw_root,
            decision_id=pm_id,
            decision_type="merge",
            verdict="approve",
            wiki_root=wiki,
        )

        report = apply_decision_answers(wiki, raw_root)

        # The merge answer could not be applied -- the fold refused.
        (outcome,) = report.outcomes
        assert outcome.applied is False
        assert outcome.error_code == "fold_widen_impossible"
        assert outcome.dimension_name == "memory-class"
        subject_q = next(
            q for q in parse_pending_questions(pending) if "'subject'" in q.question
        )
        assert subject_q.answered is True

    def test_a_question_for_another_proposal_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        merges_path, pm_id = self._stalled(wiki)
        pending = wiki / "_pending_questions.md"
        raise_pending_question(
            pending,
            question=(
                "Fold of 'canonical' cannot auto-widen its 'subject' "
                "coordinate — what should the canonical's value be?"
            ),
            context="Human-approved fold (proposal ffffffffffff) into 'canonical' x.",
            entity="canonical",
            source=str(wiki / "canonical.md"),
        )

        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        others = [
            q
            for q in parse_pending_questions(pending)
            if "ffffffffffff" in q.description
        ]
        assert others and all(q.answered is False for q in others)

    def test_withdrawal_repeats_cleanly_on_the_next_tick(self, tmp_path: Path) -> None:
        """`ingest-answers` retries a refused fold every tick; the
        withdrawal must be a no-op the second time rather than re-resolving
        an already-answered block."""
        wiki = tmp_path / "wiki"
        merges_path, pm_id = self._stalled(wiki)
        pending = wiki / "_pending_questions.md"
        raise_pending_question(
            pending,
            question=(
                "Fold of 'canonical' cannot auto-widen its 'subject' "
                "coordinate — what should the canonical's value be?"
            ),
            context=f"Human-approved fold (proposal {pm_id}) into 'canonical' x.",
            entity="canonical",
            source=str(wiki / "canonical.md"),
        )
        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)
        after_first = pending.read_text(encoding="utf-8")

        resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

        assert pending.read_text(encoding="utf-8") == after_first
