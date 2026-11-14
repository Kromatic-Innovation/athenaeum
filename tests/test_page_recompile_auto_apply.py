# SPDX-License-Identifier: Apache-2.0
"""Page recompile: auto-apply structure-only plans by default (issue
athenaeum#2549, slice S8 of the page-recompile design, athenaeum#2525).

One class per acceptance criterion, named for the criterion it proves --
the same shape ``tests/test_page_recompile_cross_page.py`` and
``tests/test_page_recompile_nightly.py`` use. AC1 (the two flipped
defaults) is covered directly in
``tests/test_config_resolver_parity_generic.py``
(``TestClosedVocabularyResolversDirect``) and in
``tests/test_page_recompile_nightly.py``'s own ``TestAC2Modes``/
``TestAC3AutoApplyClass`` -- not duplicated here.

Reuses the stub-client machinery and canned plan payloads from
``tests/test_page_recompile_nightly.py`` (dispatches on the system prompt,
counts calls, no network and no key) and the page fixtures from
``tests/test_page_recompile_apply.py`` (a body whose real segmentation
produces a plan the applier accepts), the same two modules
``test_page_recompile_nightly.py`` itself draws on.

AC2/AC3/AC5/AC8 are exercised through the REAL nightly phase
(:func:`athenaeum.page_recompile_nightly.run_nightly_page_recompile`)
rather than a helper in isolation, per this issue's own testing standard.
AC7's round trip needs a REAL git repository with a REAL pre-apply commit
-- the same discipline ``tests/test_page_recompile_undo.py`` documents --
because that is the only thing that makes a pre-image blob readable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum.config import (
    DEFAULT_PAGE_RECOMPILE_MAX_AUTO_APPLIES_PER_RUN,
    DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN,
    DEFAULT_PAGE_RECOMPILE_NEVER_AUTO_TYPES,
    resolve_page_recompile_max_auto_applies_per_run,
    resolve_page_recompile_never_auto_types,
)
from athenaeum.page_recompile import find_recompile_candidates
from athenaeum.page_recompile_apply import APPLY_KIND
from athenaeum.page_recompile_nightly import PageRecompileRunSummary, run_nightly_page_recompile
from athenaeum.page_recompile_plan import (
    invalid_plan_skip_reason,
    read_page_recompile_ledger,
)
from athenaeum.page_recompile_queue import applied_last_7_days, list_pending_page_recompile_plans
from athenaeum.page_recompile_undo import UNDO_DIRECT, write_undo
from athenaeum.status import build_page_recompile_backlog
from tests.test_page_recompile_apply import _BODY, _SUMMARY, _unit_ids, _write_page
from tests.test_page_recompile_nightly import _client_for, _labels_payload, _StubClient

# ---------------------------------------------------------------------------
# Fixtures. See tests/test_page_recompile_nightly.py's own comment for why
# these figures are pinned the way they are.
# ---------------------------------------------------------------------------

_THRESHOLD = 200
_CEILING = 100_000


def _config(**page_recompile: Any) -> dict[str, Any]:
    section: dict[str, Any] = {"min_parent_chars": 1}
    section.update(page_recompile)
    return {
        "librarian": {
            "page_size_threshold_chars": _THRESHOLD,
            "oversize_hard_ceiling_chars": _CEILING,
            "page_recompile": section,
        }
    }


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


def _seed(wiki: Path, count: int = 1, **extra: Any) -> list[str]:
    uids = []
    for index in range(count):
        uid = f"page{index:04d}"
        _write_page(wiki, uid=uid, name=f"Acme {index}", body=_BODY, **extra)
        uids.append(uid)
    return uids


def _run(
    wiki: Path,
    *,
    client: Any = None,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> PageRecompileRunSummary:
    return run_nightly_page_recompile(
        wiki,
        client=client if client is not None else _StubClient(),
        config=config if config is not None else _config(),
        **kwargs,
    )


def _cross_page_plan_payload(ids: list[str]) -> dict[str, Any]:
    """A ``standalone:`` group -- cross-page by construction
    (:data:`athenaeum.page_recompile_plan.STRUCTURE_ONLY_VERBS` excludes
    ``standalone``), so the WHOLE plan classes ``cross-page`` even though
    two of its three units stay on the parent."""
    return {
        "groups": [
            {
                "key": "g1",
                "name": "Session Notes",
                "description": "An unrelated note.",
                "confidence": 0.8,
            }
        ],
        "assignments": [
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "standalone:g1"},
            {"unit": ids[2], "disposition": "parent"},
        ],
        "merges": [],
        "page_actions": [],
        "summary": _SUMMARY,
    }


def _invalid_plan_payload(ids: list[str]) -> dict[str, Any]:
    """A structure-only shape with an EMPTY summary -- ``check_summary_citations``
    always rejects that with an ``empty-summary`` issue, so ``plan.valid`` is
    ``False`` with no page named by the model at all."""
    return {
        "groups": [
            {"key": "g1", "name": "Pricing", "description": "Pricing details", "confidence": 0.9}
        ],
        "assignments": [
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "child:g1"},
            {"unit": ids[2], "disposition": "parent"},
        ],
        "merges": [],
        "page_actions": [],
        "summary": "",
    }


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Auto Apply Test")


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def _page_bytes(wiki: Path) -> dict[str, bytes]:
    return {
        p.name: p.read_bytes() for p in sorted(wiki.glob("*.md")) if not p.name.startswith("_")
    }


# ---------------------------------------------------------------------------
# AC2: auto + structure-only applies unattended; auto + none queues
# everything.
# ---------------------------------------------------------------------------


class TestAC2AutoAppliesStructureOnly:
    def test_a_valid_structure_only_plan_applies_with_no_decision_item(
        self, wiki: Path
    ) -> None:
        uids = _seed(wiki, count=1)
        before = (wiki / f"{uids[0]}.md").read_text(encoding="utf-8")

        summary = _run(wiki, config=_config())  # mode/class default to auto/structure-only

        assert summary.mode == "auto"
        assert summary.auto_apply_class == "structure-only"
        assert summary.applied == 1
        assert summary.queued == 0
        assert list_pending_page_recompile_plans(wiki) == []
        assert (wiki / f"{uids[0]}.md").read_text(encoding="utf-8") != before
        kinds = {r.get("kind") for r in read_page_recompile_ledger(wiki)}
        assert APPLY_KIND in kinds

    def test_class_none_queues_every_plan_instead(self, wiki: Path) -> None:
        uids = _seed(wiki, count=1)
        before = (wiki / f"{uids[0]}.md").read_text(encoding="utf-8")

        summary = _run(wiki, config=_config(auto_apply_class="none"))

        assert summary.applied == 0
        assert summary.queued == 1
        assert len(list_pending_page_recompile_plans(wiki)) == 1
        assert (wiki / f"{uids[0]}.md").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# AC3: a cross-page plan is never auto-applied, and its structure-only part
# is not applied on its own either -- the whole plan is queued.
# ---------------------------------------------------------------------------


class TestAC3CrossPageNeverAutoApplied:
    def test_the_whole_plan_is_queued_not_partially_applied(self, wiki: Path) -> None:
        uids = _seed(wiki, count=1)
        before = (wiki / f"{uids[0]}.md").read_text(encoding="utf-8")
        ids = _unit_ids(_BODY)
        client = _StubClient(labels=_labels_payload(ids), plan=_cross_page_plan_payload(ids))

        summary = _run(wiki, client=client, config=_config())

        assert summary.applied == 0
        assert summary.queued == 1
        items = list_pending_page_recompile_plans(wiki)
        assert len(items) == 1
        assert items[0].get("scope") == "cross-page"
        # Not even the structure-only two-thirds of it landed on disk.
        assert (wiki / f"{uids[0]}.md").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# AC4: a plan that fails validation/verification is neither applied nor
# queued, counted invalid, and the page is skipped until its content hash
# changes.
# ---------------------------------------------------------------------------


class TestAC4InvalidPlanNeitherAppliedNorQueued:
    def test_invalid_plan_is_counted_invalid_only(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        ids = _unit_ids(_BODY)
        client = _StubClient(labels=_labels_payload(ids), plan=_invalid_plan_payload(ids))

        summary = _run(wiki, client=client, config=_config())

        assert summary.invalid == 1
        assert summary.applied == 0
        assert summary.queued == 0
        assert list_pending_page_recompile_plans(wiki) == []

    def test_the_page_is_skipped_until_its_content_hash_changes(self, wiki: Path) -> None:
        uids = _seed(wiki, count=1)
        ids = _unit_ids(_BODY)

        first = _run(
            wiki,
            client=_StubClient(labels=_labels_payload(ids), plan=_invalid_plan_payload(ids)),
            config=_config(),
        )
        assert first.invalid == 1

        # Second run, same bytes: no model call at all, skipped_invalid
        # fires instead of re-planning (and re-failing) the identical plan.
        second_client = _StubClient(
            labels=_labels_payload(ids), plan=_invalid_plan_payload(ids)
        )
        second = _run(wiki, client=second_client, config=_config())
        assert second.invalid == 0
        assert second.skipped_invalid == 1
        assert second_client.call_count == 0
        assert (
            invalid_plan_skip_reason(wiki, uid=uids[0], body=_BODY) != ""
        )

        # Editing the page clears the skip: the hash no longer matches. The
        # edit stays WITHIN the existing "Beta Corp" unit (rather than
        # adding a fourth one) so the default `_plan_payload` shape --
        # three units, positionally assigned -- still fits the edited body.
        edited = _BODY.replace(
            "across the same market.[^src-3]",
            "across several neighbouring markets.[^src-3]",
        )
        assert edited != _BODY
        _write_page(wiki, uid=uids[0], name="Acme 0", body=edited)
        third = _run(wiki, client=_client_for(edited), config=_config())
        assert third.skipped_invalid == 0
        assert third.planned == 1


# ---------------------------------------------------------------------------
# AC5: max_auto_applies_per_run, default equal to the per-run page budget.
# ---------------------------------------------------------------------------


class TestAC5MaxAutoAppliesPerRun:
    def test_default_equals_the_steady_per_run_page_budget(self) -> None:
        assert (
            DEFAULT_PAGE_RECOMPILE_MAX_AUTO_APPLIES_PER_RUN
            == DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN
        )
        assert (
            resolve_page_recompile_max_auto_applies_per_run(None)
            == DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN
        )

    def test_bound_of_one_with_two_valid_plans_applies_one_and_defers_the_other(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=2)

        summary = _run(wiki, config=_config(max_auto_applies_per_run=1))

        assert summary.planned == 2
        assert summary.applied == 1
        assert summary.skipped_auto_apply_budget == 1
        # The deferred plan became neither an apply NOR a decision item.
        assert summary.queued == 0
        assert list_pending_page_recompile_plans(wiki) == []


# ---------------------------------------------------------------------------
# AC6: the run summary names the applied plan and its counts; `recompile
# list --applied`; status fills "applied in the last 7 days".
# ---------------------------------------------------------------------------


class TestAC6RunSummaryListAndStatus:
    def test_the_run_summary_names_the_applied_plan_and_its_counts(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=1)

        summary = _run(wiki, config=_config())

        assert summary.applied == 1
        assert len(summary.applied_plans) == 1
        row = summary.applied_plans[0]
        assert row["plan_id"]
        assert row["sub_pages_created"] == 1
        assert row["sections_merged"] == 0
        # And it travels on the run-profile line (what the finalize phase's
        # ledger write actually sees).
        assert "applied_plans" in summary.as_profile_fields()
        assert summary.as_profile_fields()["applied"] == 1

    def test_recompile_list_applied_shows_it(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum.cli import main as cli_main

        _seed(wiki, count=1)
        summary = _run(wiki, config=_config())
        plan_id = summary.applied_plans[0]["plan_id"]

        rc = cli_main(
            ["recompile", "list", "--applied", "--json", "--path", str(wiki.parent)]
        )
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert any(row["plan_id"] == plan_id for row in rows)

    def test_status_fills_applied_in_the_last_7_days(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        _run(wiki, config=_config())

        scan = find_recompile_candidates(wiki, _config())
        block = build_page_recompile_backlog(
            scan, threshold_chars=_THRESHOLD, ceiling_chars=_CEILING, wiki_root=wiki
        )
        assert block["applied_last_7_days"] == 1
        assert applied_last_7_days(wiki) == 1


# ---------------------------------------------------------------------------
# AC7: each auto-apply is its own snapshot commit, and `recompile undo`
# works on it -- the round trip leaves the wiki byte-identical.
# ---------------------------------------------------------------------------


class TestAC7UndoWorksOnAnAutoApply:
    def test_auto_apply_then_undo_leaves_the_wiki_byte_identical(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _init_git_repo(knowledge)
        _write_page(wiki, uid="page0001", name="Acme 0", body=_BODY)
        _commit_all(knowledge, "seed")
        before = _page_bytes(wiki)

        summary = _run(wiki, config=_config())
        assert summary.applied == 1
        plan_id = summary.applied_plans[0]["plan_id"]
        assert _page_bytes(wiki) != before  # the auto-apply really did write

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert outcome.ok
        assert _page_bytes(wiki) == before

    def test_each_auto_apply_makes_its_own_snapshot_commit(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _init_git_repo(knowledge)
        _seed(wiki, count=2)
        _commit_all(knowledge, "seed")

        summary = _run(wiki, config=_config())

        assert summary.applied == 2
        shas = {row["plan_id"] for row in summary.applied_plans}
        assert len(shas) == 2
        log_lines = _git(knowledge, "log", "--oneline", "--grep=page-recompile apply")
        assert len(log_lines.splitlines()) == 2


# ---------------------------------------------------------------------------
# AC8: never_auto_types routes a plan to the queue regardless of class or
# scope.
# ---------------------------------------------------------------------------


class TestAC8NeverAutoTypes:
    def test_default_is_empty(self) -> None:
        assert DEFAULT_PAGE_RECOMPILE_NEVER_AUTO_TYPES == ()
        assert resolve_page_recompile_never_auto_types(None) == ()

    def test_a_listed_type_is_queued_instead_of_applied(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme 0", body=_BODY, type="policy")

        summary = _run(wiki, config=_config(never_auto_types=["policy"]))

        assert summary.applied == 0
        assert summary.queued == 1
        assert len(list_pending_page_recompile_plans(wiki)) == 1

    def test_an_unlisted_type_still_auto_applies(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme 0", body=_BODY, type="reference")

        summary = _run(wiki, config=_config(never_auto_types=["policy"]))

        assert summary.applied == 1
        assert summary.queued == 0


# ---------------------------------------------------------------------------
# AC9: README and CHANGELOG document the behaviour change.
# ---------------------------------------------------------------------------


class TestAC9Documentation:
    @staticmethod
    def _repo() -> Path:
        return Path(__file__).resolve().parents[1]

    def test_readme_names_how_to_see_undo_and_switch_back(self) -> None:
        text = (self._repo() / "README.md").read_text(encoding="utf-8")
        lowered = text.lower()
        assert "automatically" in lowered
        assert "recompile list --applied" in text
        assert "recompile undo" in text
        assert "librarian.page_recompile.mode" in text
        assert "queue" in lowered and "off" in lowered

    def test_changelog_has_a_behaviour_change_entry_for_this_slice(self) -> None:
        changelog = (self._repo() / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## [Unreleased]")[1].split("\n## [")[0]
        entry = next(
            block
            for block in unreleased.split("\n- ")
            if "athenaeum#2549" in block
        )
        lead = " ".join(entry.split("**")[1].split()).lower()
        assert "automatically by default" in lead
        assert "behaviour change" in lead
        assert "recompile list --applied" in entry
        assert "recompile undo" in entry
        assert "mode: queue" in entry or "mode: `queue`" in entry
        assert "mode: off" in entry or "mode: `off`" in entry
        # Marked a Changed entry, not merely Added -- the AC's own wording.
        changed_section = unreleased.split("### Changed")[1]
        assert "athenaeum#2549" in changed_section
