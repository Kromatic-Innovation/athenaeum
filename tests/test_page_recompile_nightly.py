# SPDX-License-Identifier: Apache-2.0
"""The nightly page-recompile phase, its budget, its modes and the
``page-recompile`` decision type (issue athenaeum#2547, slice S6 of
athenaeum#2525).

One class per acceptance criterion, named for the criterion it proves.

Every model call goes through :class:`_StubClient`, which dispatches on the
SYSTEM prompt (label / plan / verify are three different prompts) and counts
calls -- so the call COUNT is itself the oracle for "``off`` makes no call"
and for the per-run API-call ceiling. No network, no key.

Page fixtures and the plan-recording helpers are imported from
``tests/test_page_recompile_apply.py`` rather than re-written: that file
already owns a body whose segmentation produces a plan the APPLIER accepts,
which is exactly what this file needs to prove an ``approve`` really does
write pages. Every page in a given test is seeded with the SAME body, so
one canned plan payload is valid for all of them -- unit ids are
content-addressed, so identical bodies segment to identical ids.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from athenaeum.config import (
    DEFAULT_PAGE_RECOMPILE_MAX_API_CALLS_PER_RUN,
    DEFAULT_PAGE_RECOMPILE_MAX_INPUT_CHARS_PER_RUN,
    DEFAULT_PAGE_RECOMPILE_MAX_OPEN_ITEMS,
    DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN,
    DEFAULT_PAGE_RECOMPILE_MODE,
    DEFAULT_PAGE_RECOMPILE_OPENING_MAX_PAGES_PER_RUN,
    PAGE_RECOMPILE_AUTO_APPLY_CLASSES,
    PAGE_RECOMPILE_MODES,
    resolve_page_recompile_auto_apply_class,
    resolve_page_recompile_max_api_calls_per_run,
    resolve_page_recompile_max_input_chars_per_run,
    resolve_page_recompile_max_open_items,
    resolve_page_recompile_max_pages_per_run,
    resolve_page_recompile_mode,
    resolve_page_recompile_opening_max_pages_per_run,
)
from athenaeum.decision_answers import (
    VALID_DECISION_TYPES,
    apply_decision_answers,
    write_decision_answer,
)
from athenaeum.decision_framing import (
    ANSWERABLE_AS,
    REVERSIBILITY_REVERSIBLE_WITH_WORK,
    ROUTING_AUTHORITY,
    answerable_as,
    proposed_default_for,
    response_schema_for,
    reversibility_for,
    routing_for,
)
from athenaeum.decisions import list_pending_decisions
from athenaeum.models import TokenUsage, parse_frontmatter, render_frontmatter
from athenaeum.page_recompile import segment_page
from athenaeum.page_recompile_nightly import (
    REASON_DEADLINE_TRIPPED,
    REASON_MAX_OPEN_ITEMS,
    REASON_NO_CANDIDATES,
    PageRecompileRunSummary,
    page_budget,
    run_nightly_page_recompile,
)
from athenaeum.page_recompile_plan import (
    PAGE_RECOMPILE_LABEL_SYSTEM,
    PAGE_RECOMPILE_PLAN_SYSTEM,
    PAGE_RECOMPILE_VERIFY_SYSTEM,
    PLAN_KIND,
    read_page_recompile_ledger,
)
from athenaeum.page_recompile_queue import (
    APPROVE_KIND,
    DECISION_TYPE,
    EXCERPT_MAX_CHARS,
    QUEUE_KIND,
    REJECT_KIND,
    STALE_KIND,
    VERDICT_APPROVE,
    VERDICT_APPROVE_STRUCTURE_ONLY,
    VERDICT_REJECT,
    VERDICTS,
    build_plan_outline,
    list_pending_page_recompile_plans,
    nights_since_progress,
    plans_awaiting_approval,
    rejected_page_skip_reason,
)
from tests.test_page_recompile_apply import _BODY, _SUMMARY, _unit_ids, _write_page

# ---------------------------------------------------------------------------
# Stub client: dispatches on the SYSTEM prompt, counts calls.
# ---------------------------------------------------------------------------


class _StubBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _StubUsage:
    def __init__(self) -> None:
        self.input_tokens = 100
        self.output_tokens = 20
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _StubResponse:
    def __init__(self, text: str) -> None:
        self.content = [_StubBlock(text)]
        self.usage = _StubUsage()


class _StubMessages:
    def __init__(self, client: "_StubClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> _StubResponse:
        self._client.calls.append(kwargs)
        system = str(kwargs.get("system") or "")
        if system == PAGE_RECOMPILE_LABEL_SYSTEM:
            payload = self._client.labels
        elif system == PAGE_RECOMPILE_PLAN_SYSTEM:
            payload = self._client.plan
        elif system == PAGE_RECOMPILE_VERIFY_SYSTEM:
            payload = self._client.verify
        else:  # pragma: no cover - the planner has only three prompts
            raise AssertionError(f"unexpected system prompt: {system[:60]!r}")
        return _StubResponse(json.dumps(payload))


class _StubClient:
    """Replays one canned payload per prompt KIND and counts every call."""

    def __init__(
        self,
        *,
        labels: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        verify: dict[str, Any] | None = None,
    ) -> None:
        ids = _unit_ids(_BODY)
        self.labels = labels if labels is not None else _labels_payload(ids)
        self.plan = plan if plan is not None else _plan_payload(ids)
        self.verify = verify if verify is not None else _verify_payload()
        self.calls: list[dict[str, Any]] = []
        self.messages = _StubMessages(self)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _labels_payload(ids: list[str]) -> dict[str, Any]:
    return {
        "labels": [
            {
                "unit": unit_id,
                "topic": "widgets",
                "belongs": "page",
                "about": "",
                "kind": "fact",
            }
            for unit_id in ids
        ]
    }


def _plan_payload(ids: list[str]) -> dict[str, Any]:
    """The same structure-only shape ``test_page_recompile_apply._simple_plan``
    records by hand: the lead paragraph and the third section stay on the
    parent, the ``## Pricing`` section becomes one sub-page group."""
    return {
        "groups": [
            {
                "key": "g1",
                "name": "Pricing",
                "description": "Pricing details",
                "confidence": 0.9,
            }
        ],
        "assignments": [
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "child:g1"},
            {"unit": ids[2], "disposition": "parent"},
        ],
        "merges": [],
        "page_actions": [],
        "summary": _SUMMARY,
    }


def _verify_payload() -> dict[str, Any]:
    return {"sentences": [{"index": 0, "verdict": "supported"}]}


def _strings_in(value: Any) -> list[str]:
    """Every string anywhere in a nested payload, for the AC7 body sweep."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings_in(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings_in(v)]
    return []


def _client_for(body: str) -> _StubClient:
    """A stub whose canned payloads name the unit ids of *body*.

    Needed wherever a test EDITS a page: unit ids are content-addressed, so
    a payload canned against the original body names ids the edited page no
    longer has, and the planner would (correctly) reject the plan."""
    ids = _unit_ids(body)
    return _StubClient(labels=_labels_payload(ids), plan=_plan_payload(ids))


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

#: Small enough that the shared ``_BODY`` fixture is a candidate without
#: having to be 10,000 characters long, and written into the test config so
#: these tests also prove the phase reads the RESOLVED knob.
_THRESHOLD = 200

#: Above ``len(_BODY)``, so no page is over the hard ceiling by default and
#: the STEADY page budget applies. Tests that need the OPENING budget drop
#: it below ``len(_BODY)`` instead of growing a second page fixture.
_CEILING = 100_000


def _config(**page_recompile: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        # AC3 of the applier slice: the shared fixture's parent prose clears
        # the real 200-char floor comfortably, but pin it anyway so a test
        # that is not about that floor can never be confused by it.
        "min_parent_chars": 1,
    }
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


def _seed(wiki: Path, count: int = 1) -> list[str]:
    """*count* candidate pages, all carrying the SAME body (see the module
    docstring for why). Returns their uids in filename order."""
    uids = []
    for index in range(count):
        uid = f"page{index:04d}"
        _write_page(wiki, uid=uid, name=f"Acme {index}", body=_BODY)
        uids.append(uid)
    return uids


def _run(
    wiki: Path,
    *,
    client: Any = None,
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    **kwargs: Any,
) -> PageRecompileRunSummary:
    return run_nightly_page_recompile(
        wiki,
        client=client if client is not None else _StubClient(),
        config=config if config is not None else _config(),
        usage=usage if usage is not None else TokenUsage(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC1: the phase function -- placement, the three skips, the deadline, and
# no reserved share of its own.
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)


@pytest.fixture
def run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The minimum a real ``librarian.run()`` needs: a git-init knowledge
    root with empty ``wiki/``/``raw/`` and a config file."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    (root / "athenaeum.yaml").write_text(
        "librarian:\n  page_recompile:\n    mode: queue\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Phase Test")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    # `run()` refuses outright without a key unless `dry_run=True`, and a
    # dry run would change which phases are reached -- which is the very
    # thing these tests measure. No call is ever made: the entity phase is
    # patched out and the page-recompile phase is the spy.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    return root


class TestAC1PhasePlacementAndSkips:
    """The phase runs after entity/auto-memory and before finalize, is not
    reached by a merge-only or cluster-only run, is skipped on a tripped
    deadline, checks the deadline between pages, and carves no share."""

    def test_reached_by_an_ordinary_run(self, run_root: Path) -> None:
        """The positive control, without which the two negatives below prove
        nothing: a run that is neither merge-only nor cluster-only DOES reach
        the phase."""
        from athenaeum import librarian

        with (
            patch.object(librarian, "_run_entity_tier_phase"),
            patch.object(librarian, "_run_auto_memory_phase", return_value=None),
            patch.object(librarian, "_run_page_recompile_phase") as spy,
        ):
            librarian.run(
                raw_root=run_root / "raw",
                wiki_root=run_root / "wiki",
                knowledge_root=run_root,
            )
        assert spy.call_count == 1

    def test_not_reached_by_a_merge_only_run(self, run_root: Path) -> None:
        from athenaeum import librarian

        with patch.object(librarian, "_run_page_recompile_phase") as spy:
            librarian.run(
                raw_root=run_root / "raw",
                wiki_root=run_root / "wiki",
                knowledge_root=run_root,
                merge_only=True,
            )
        spy.assert_not_called()

    def test_not_reached_by_a_cluster_only_run(self, run_root: Path) -> None:
        from athenaeum import librarian

        with patch.object(librarian, "_run_page_recompile_phase") as spy:
            librarian.run(
                raw_root=run_root / "raw",
                wiki_root=run_root / "wiki",
                knowledge_root=run_root,
                cluster_only=True,
            )
        spy.assert_not_called()

    def test_runs_after_entity_and_auto_memory_and_before_finalize(
        self, run_root: Path
    ) -> None:
        from athenaeum import librarian

        order: list[str] = []

        def _spy(name: str, result: Any = None) -> Any:
            def _hook(*_args: Any, **_kwargs: Any) -> Any:
                order.append(name)
                return result

            return _hook

        with (
            patch.object(librarian, "_run_entity_tier_phase", _spy("entity")),
            patch.object(librarian, "_run_auto_memory_phase", _spy("auto-memory")),
            patch.object(librarian, "_run_page_recompile_phase", _spy("page-recompile")),
            patch.object(librarian, "_run_finalize_phase", _spy("finalize", 0)),
        ):
            librarian.run(
                raw_root=run_root / "raw",
                wiki_root=run_root / "wiki",
                knowledge_root=run_root,
            )
        assert order.index("entity") < order.index("page-recompile")
        assert order.index("auto-memory") < order.index("page-recompile")
        assert order.index("page-recompile") < order.index("finalize")

    def test_phase_skipped_when_the_deadline_already_tripped(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.librarian import _run_page_recompile_phase

        ctx = _phase_ctx(tmp_path)
        ctx.deadline_tripped = True
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_page_recompile_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.page_recompile_summary is None
        assert ("page-recompile", 0.0, {"reason": "deadline-tripped", "mode": "queue"}) in (
            ctx.run_profile
        )

    def test_phase_skipped_when_the_deadline_has_expired(self, tmp_path: Path) -> None:
        from athenaeum.librarian import _run_page_recompile_phase

        ctx = _phase_ctx(tmp_path)
        ctx.run_deadline = 0.0  # already elapsed
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_page_recompile_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.page_recompile_summary is None

    def test_the_deadline_is_checked_between_pages(self, wiki: Path) -> None:
        """Three candidates, a deadline that trips after the FIRST page: the
        remaining two are reported ``skipped_deadline``, not planned."""
        _seed(wiki, count=3)
        checks = {"n": 0}

        def _deadline() -> bool:
            checks["n"] += 1
            return checks["n"] > 1

        summary = _run(wiki, deadline_check=_deadline)
        assert summary.reason == REASON_DEADLINE_TRIPPED
        assert summary.planned == 1
        assert summary.skipped_deadline == 2

    def test_the_phase_carves_no_share_of_the_run_deadline(self, tmp_path: Path) -> None:
        """AC1's "carves no share of its own": the phase must leave
        ``run_deadline`` exactly as it found it -- the whole reason it runs
        last is that there is no fresh share left to take."""
        from athenaeum.librarian import _run_page_recompile_phase

        ctx = _phase_ctx(tmp_path)
        ctx.run_deadline = 9_999_999_999.0
        before = ctx.run_deadline
        with patch("athenaeum.librarian.build_llm_client", return_value=_StubClient()):
            _run_page_recompile_phase(ctx)
        assert ctx.run_deadline == before


def _phase_ctx(tmp_path: Path, **config_overrides: Any) -> Any:
    """A ``RunContext`` shaped the way ``run()`` hands one to the phase."""
    from athenaeum.librarian import RunContext

    knowledge_root = tmp_path / "knowledge"
    wiki_root = knowledge_root / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(
        raw_root=knowledge_root / "raw",
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=None,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=None,
        push_after_run=None,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=None,
        heartbeat=None,
        out_run_stats=None,
    )
    ctx.config = _config(**config_overrides)
    ctx.provider = "api"
    ctx.usage = TokenUsage()
    return ctx


# ---------------------------------------------------------------------------
# AC2: the four modes.
# ---------------------------------------------------------------------------


class TestAC2Modes:
    def test_the_four_modes_are_accepted(self) -> None:
        assert PAGE_RECOMPILE_MODES == ("off", "shadow", "queue", "auto")
        for mode in PAGE_RECOMPILE_MODES:
            assert (
                resolve_page_recompile_mode(
                    {"librarian": {"page_recompile": {"mode": mode}}}
                )
                == mode
            )

    def test_the_default_in_this_slice_is_queue(self) -> None:
        assert DEFAULT_PAGE_RECOMPILE_MODE == "queue"
        assert resolve_page_recompile_mode(None) == "queue"
        assert resolve_page_recompile_mode({}) == "queue"

    def test_an_unknown_value_falls_back_to_the_default_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            resolved = resolve_page_recompile_mode(
                {"librarian": {"page_recompile": {"mode": "aggressive"}}}
            )
        assert resolved == DEFAULT_PAGE_RECOMPILE_MODE
        assert any(
            "unknown page-recompile mode" in record.getMessage()
            for record in caplog.records
        )

    def test_off_builds_no_client_and_makes_no_call(self, tmp_path: Path) -> None:
        """The AC's own oracle for ``off``: the client is never BUILT, which
        is a stronger claim than "no call was made with it"."""
        from athenaeum.librarian import _run_page_recompile_phase

        ctx = _phase_ctx(tmp_path, mode="off")
        _seed(ctx.wiki_root, count=2)
        with patch("athenaeum.librarian.build_llm_client") as mock_build:
            _run_page_recompile_phase(ctx)
        mock_build.assert_not_called()
        assert ctx.page_recompile_summary is None
        assert ctx.usage.api_calls == 0
        assert ("page-recompile", 0.0, {"reason": "off"}) in ctx.run_profile

    def test_shadow_writes_no_page_and_raises_no_item(self, wiki: Path) -> None:
        uids = _seed(wiki, count=1)
        before = (wiki / f"{uids[0]}.md").read_text(encoding="utf-8")
        pages_before = sorted(p.name for p in wiki.glob("*.md"))

        summary = _run(wiki, config=_config(mode="shadow"))

        assert summary.planned == 1
        assert summary.queued == 0
        assert list_pending_page_recompile_plans(wiki) == []
        assert (wiki / f"{uids[0]}.md").read_text(encoding="utf-8") == before
        assert sorted(p.name for p in wiki.glob("*.md")) == pages_before
        # It DID record the plan -- that is the difference between `shadow`
        # and `off`.
        kinds = {r.get("kind") for r in read_page_recompile_ledger(wiki)}
        assert PLAN_KIND in kinds
        assert QUEUE_KIND not in kinds

    def test_queue_drafts_and_raises_an_item(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        summary = _run(wiki, config=_config(mode="queue"))
        assert summary.planned == 1
        assert summary.queued == 1
        assert len(list_pending_page_recompile_plans(wiki)) == 1

    def test_auto_behaves_as_queue_in_this_slice(self, wiki: Path) -> None:
        """Out of scope for this slice: "auto-apply without approval". So
        ``auto`` is accepted (a config written for the auto-apply slice does
        not fall back and warn) and raises an item exactly like ``queue``,
        writing no page."""
        uids = _seed(wiki, count=1)
        before = (wiki / f"{uids[0]}.md").read_text(encoding="utf-8")
        summary = _run(wiki, config=_config(mode="auto"))
        assert summary.queued == 1
        assert (wiki / f"{uids[0]}.md").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# AC3: auto_apply_class is READ in this slice.
# ---------------------------------------------------------------------------


class TestAC3AutoApplyClassIsReadOnly:
    def test_the_two_classes_are_accepted(self) -> None:
        assert PAGE_RECOMPILE_AUTO_APPLY_CLASSES == ("none", "structure-only")
        for value in PAGE_RECOMPILE_AUTO_APPLY_CLASSES:
            assert (
                resolve_page_recompile_auto_apply_class(
                    {"librarian": {"page_recompile": {"auto_apply_class": value}}}
                )
                == value
            )

    def test_the_default_is_none(self) -> None:
        assert resolve_page_recompile_auto_apply_class(None) == "none"

    def test_an_unknown_value_falls_back_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            resolved = resolve_page_recompile_auto_apply_class(
                {"librarian": {"page_recompile": {"auto_apply_class": "everything"}}}
            )
        assert resolved == "none"
        assert caplog.records

    def test_it_is_read_and_reported_but_changes_nothing(self, wiki: Path) -> None:
        """READ in this slice: the resolved value reaches the run summary,
        and setting it to ``structure-only`` still writes no page and still
        raises an item -- the behaviour it enables is the auto-apply slice's
        job."""
        uids = _seed(wiki, count=1)
        before = (wiki / f"{uids[0]}.md").read_text(encoding="utf-8")
        summary = _run(wiki, config=_config(auto_apply_class="structure-only"))
        assert summary.auto_apply_class == "structure-only"
        assert summary.queued == 1
        assert (wiki / f"{uids[0]}.md").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# AC4: the budget keys, each validated like page_size_threshold_chars.
# ---------------------------------------------------------------------------

#: ``(resolver, yaml key, documented default)`` for every athenaeum#2547
#: budget key, so the validation contract is asserted from one list rather
#: than five near-identical test bodies.
_BUDGET_KEYS = (
    (
        resolve_page_recompile_max_pages_per_run,
        "max_pages_per_run",
        DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN,
    ),
    (
        resolve_page_recompile_opening_max_pages_per_run,
        "opening_max_pages_per_run",
        DEFAULT_PAGE_RECOMPILE_OPENING_MAX_PAGES_PER_RUN,
    ),
    (
        resolve_page_recompile_max_input_chars_per_run,
        "max_input_chars_per_run",
        DEFAULT_PAGE_RECOMPILE_MAX_INPUT_CHARS_PER_RUN,
    ),
    (
        resolve_page_recompile_max_api_calls_per_run,
        "max_api_calls_per_run",
        DEFAULT_PAGE_RECOMPILE_MAX_API_CALLS_PER_RUN,
    ),
    (
        resolve_page_recompile_max_open_items,
        "max_open_items",
        DEFAULT_PAGE_RECOMPILE_MAX_OPEN_ITEMS,
    ),
)


class TestAC4BudgetKeys:
    def test_the_two_documented_defaults(self) -> None:
        assert DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN == 3
        assert DEFAULT_PAGE_RECOMPILE_OPENING_MAX_PAGES_PER_RUN == 10
        assert DEFAULT_PAGE_RECOMPILE_MAX_OPEN_ITEMS == 5

    @pytest.mark.parametrize("resolver,key,default", _BUDGET_KEYS)
    def test_default_when_absent(self, resolver: Any, key: str, default: int) -> None:
        assert resolver(None) == default
        assert resolver({}) == default
        assert resolver({"librarian": {}}) == default
        assert resolver({"librarian": {"page_recompile": {}}}) == default

    @pytest.mark.parametrize("resolver,key,default", _BUDGET_KEYS)
    def test_a_valid_value_is_honoured(self, resolver: Any, key: str, default: int) -> None:
        assert resolver({"librarian": {"page_recompile": {key: 7}}}) == 7

    @pytest.mark.parametrize("resolver,key,default", _BUDGET_KEYS)
    @pytest.mark.parametrize("bad", [True, False, 0, -1, "3", 3.5, None, [3]])
    def test_validated_like_page_size_threshold_chars(
        self, resolver: Any, key: str, default: int, bad: Any
    ) -> None:
        """The same contract ``resolve_page_size_threshold_chars`` applies:
        an ``int`` that is not a ``bool`` and is ``>= 1``. ``True`` matters
        most -- ``max_pages_per_run: yes`` in yaml must not silently become a
        budget of 1."""
        assert resolver({"librarian": {"page_recompile": {key: bad}}}) == default

    def test_max_open_items_stops_new_items_and_makes_no_call(
        self, wiki: Path
    ) -> None:
        """No new item is raised while ``max_open_items`` are unanswered --
        and no model call is made either, because a plan that cannot be
        raised is spend with no outcome."""
        _seed(wiki, count=1)
        first = _StubClient()
        _run(wiki, client=first, config=_config(max_open_items=1))
        assert len(list_pending_page_recompile_plans(wiki)) == 1
        assert first.call_count > 0

        # A second page appears; the cap is already reached.
        _write_page(wiki, uid="page0001", name="Acme 1", body=_BODY)
        second = _StubClient()
        summary = _run(wiki, client=second, config=_config(max_open_items=1))
        assert summary.reason == REASON_MAX_OPEN_ITEMS
        assert summary.planned == 0
        assert summary.queued == 0
        assert summary.skipped_open_items == 2
        assert second.call_count == 0
        assert len(list_pending_page_recompile_plans(wiki)) == 1

    def test_max_input_chars_per_run_skips_a_page_that_does_not_fit(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=3)
        body_chars = len(_BODY)
        summary = _run(
            wiki,
            config=_config(max_input_chars_per_run=body_chars + 1),
        )
        assert summary.planned == 1
        assert summary.input_chars == body_chars
        assert summary.skipped_budget == 2

    def test_max_api_calls_per_run_bounds_the_phase(self, wiki: Path) -> None:
        _seed(wiki, count=3)
        client = _StubClient()
        summary = _run(wiki, client=client, config=_config(max_api_calls_per_run=1))
        # The first page spends its calls; the ceiling then refuses the rest.
        assert summary.planned == 1
        assert summary.skipped_budget == 2
        assert summary.api_calls == client.call_count

    def test_calls_are_counted_against_the_run_level_budget(self, wiki: Path) -> None:
        """"counted against the run-level budget": the phase increments the
        run's OWN ``TokenUsage``, which is what ``max_api_calls`` enforces,
        rather than a private counter the run cannot see."""
        _seed(wiki, count=1)
        usage = TokenUsage()
        usage.api_calls = 5  # as though earlier phases had already spent
        client = _StubClient()
        summary = _run(wiki, client=client, usage=usage)
        assert usage.api_calls == 5 + client.call_count
        assert summary.api_calls == client.call_count


# ---------------------------------------------------------------------------
# AC5: the two budget tests the issue names.
# ---------------------------------------------------------------------------


class TestAC5BudgetBehaviour:
    def test_one_planned_two_skipped_budget(self, wiki: Path) -> None:
        """The AC verbatim: ``max_pages_per_run: 1``, no over-ceiling
        candidate, three candidates -- one planned, two ``skipped_budget``."""
        _seed(wiki, count=3)
        summary = _run(wiki, config=_config(max_pages_per_run=1))
        assert summary.backlog == 3
        assert summary.over_ceiling == 0
        assert summary.opening_budget is False
        assert summary.page_budget == 1
        assert summary.planned == 1
        assert summary.skipped_budget == 2

    def test_the_opening_budget_applies_while_an_over_ceiling_candidate_remains(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=1)
        config = _config(max_pages_per_run=1, opening_max_pages_per_run=9)
        # A ceiling below the fixture body makes every candidate
        # over-ceiling, which is what opens the budget.
        config["librarian"]["oversize_hard_ceiling_chars"] = _THRESHOLD
        summary = _run(wiki, config=config)
        assert summary.over_ceiling == 1
        assert summary.opening_budget is True
        assert summary.page_budget == 9

    def test_the_steady_budget_applies_once_none_remains(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        summary = _run(
            wiki, config=_config(max_pages_per_run=1, opening_max_pages_per_run=9)
        )
        assert summary.over_ceiling == 0
        assert summary.opening_budget is False
        assert summary.page_budget == 1

    def test_page_budget_switches_on_the_corpus_not_a_second_knob(self) -> None:
        """The same config, two corpora: the switch is a property of the
        candidates, so a corpus that clears its over-ceiling backlog drops to
        the steady budget with no config change at all."""
        from athenaeum.page_recompile import RecompileCandidate

        config = _config(max_pages_per_run=3, opening_max_pages_per_run=10)

        def _candidate(over: bool) -> RecompileCandidate:
            return RecompileCandidate(
                path=Path("p.md"),
                uid="u",
                chars=1,
                flagged=False,
                over_ceiling=over,
                updated="",
            )

        assert page_budget([_candidate(False)], config) == (3, False)
        assert page_budget([_candidate(False), _candidate(True)], config) == (10, True)
        assert page_budget([], config) == (3, False)

    def test_an_empty_backlog_costs_nothing(self, wiki: Path) -> None:
        client = _StubClient()
        summary = _run(wiki, client=client)
        assert summary.reason == REASON_NO_CANDIDATES
        assert summary.backlog == 0
        assert client.call_count == 0


# ---------------------------------------------------------------------------
# AC6: the trigger threshold is the existing page_size_threshold_chars.
# ---------------------------------------------------------------------------


class TestAC6TriggerThreshold:
    def test_the_threshold_decides_which_pages_are_picked_up(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=1)
        under = _config()
        under["librarian"]["page_size_threshold_chars"] = len(_BODY) + 1
        assert _run(wiki, config=under).backlog == 0

        over = _config()
        over["librarian"]["page_size_threshold_chars"] = len(_BODY) - 1
        assert _run(wiki, config=over).backlog == 1

    def test_the_docs_for_this_pass_name_the_setting(self) -> None:
        """AC6's second half: the docs must name
        ``librarian.page_size_threshold_chars`` as the setting that decides
        which pages this pass picks up."""
        repo = Path(__file__).resolve().parents[1]
        readme = (repo / "README.md").read_text(encoding="utf-8")
        guide = (repo / "docs" / "guides" / "daily-operation.md").read_text(
            encoding="utf-8"
        )
        for text in (readme, guide):
            assert "librarian.page_size_threshold_chars" in text


# ---------------------------------------------------------------------------
# AC7: the page-recompile decision type.
# ---------------------------------------------------------------------------


class TestAC7DecisionType:
    def test_it_appears_in_list_pending_decisions(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        _run(wiki)
        items = [d for d in list_pending_decisions(wiki) if d["type"] == DECISION_TYPE]
        assert len(items) == 1
        assert items[0]["id"]
        assert items[0]["created_at"]

    def test_its_framing_row(self) -> None:
        assert reversibility_for(DECISION_TYPE) == REVERSIBILITY_REVERSIBLE_WITH_WORK
        assert routing_for(DECISION_TYPE) == ROUTING_AUTHORITY
        assert proposed_default_for(DECISION_TYPE)["action"].startswith("reject")

    def test_its_three_verdicts(self) -> None:
        assert VERDICTS == ("approve", "approve-structure-only", "reject")
        schema = response_schema_for(DECISION_TYPE)
        assert schema["properties"]["verdict"]["enum"] == list(VERDICTS)

    def test_it_is_registered_on_every_surface(self) -> None:
        """An unregistered type silently answers as ``dimension-proposal``
        via ``_UNKNOWN_FRAMING``, so each of these is a real trap."""
        from athenaeum.decision_framing import _TYPE_FRAMING, _UNKNOWN_FRAMING

        assert DECISION_TYPE in _TYPE_FRAMING
        assert _TYPE_FRAMING[DECISION_TYPE] is not _UNKNOWN_FRAMING
        assert ANSWERABLE_AS[DECISION_TYPE] == DECISION_TYPE
        assert answerable_as(DECISION_TYPE) == DECISION_TYPE
        assert DECISION_TYPE in VALID_DECISION_TYPES

    def test_the_item_carries_the_plan_outline(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        _run(wiki)
        item = next(d for d in list_pending_decisions(wiki) if d["type"] == DECISION_TYPE)
        outline = item["payload"]["outline"]
        # Group NAMES.
        assert [g["name"] for g in outline["groups"]] == ["Pricing"]
        # Unit COUNTS.
        assert outline["unit_count"] == len(_unit_ids(_BODY))
        assert outline["groups"][0]["units"] == 1
        assert outline["parent"]["units"] == 2
        # SIZES.
        assert outline["page_chars"] == len(_BODY)
        assert outline["groups"][0]["chars"] > 0
        # FIRST-LINE EXCERPTS.
        assert outline["groups"][0]["excerpt"]
        assert "\n" not in outline["groups"][0]["excerpt"]

    def test_the_item_never_carries_a_page_body(self, wiki: Path) -> None:
        """The AC's hard prohibition. Checked as a whole-payload text sweep,
        not a per-key allowlist: a new key that smuggled a body in would
        pass a key check and fail this one."""
        _seed(wiki, count=1)
        _run(wiki)
        item = next(d for d in list_pending_decisions(wiki) if d["type"] == DECISION_TYPE)
        payload = item["payload"]
        blob = json.dumps(payload)
        assert _BODY not in blob
        # No prose paragraph of the page travels: an excerpt is ONE line,
        # capped, so every substantive paragraph is longer than anything the
        # payload may carry.
        for paragraph in [p.strip() for p in _BODY.split("\n\n") if p.strip()]:
            if len(paragraph) > EXCERPT_MAX_CHARS:
                assert paragraph not in blob
        # And the structural guarantee behind that: no string anywhere in
        # the payload is longer than one capped excerpt.
        for value in _strings_in(payload):
            assert len(value) <= EXCERPT_MAX_CHARS, value

    def test_the_outline_drops_a_merged_sections_model_written_text(
        self, wiki: Path
    ) -> None:
        """``MergedSection.text`` is the one genuinely body-sized field a
        plan record carries, so the projection has to drop it explicitly."""
        from athenaeum.page_recompile_plan import (
            MergedSection,
            PlanGroup,
            RecompilePlan,
            page_content_hash,
        )

        secret = "THIS IS THE MERGED PROSE THAT MUST NOT TRAVEL"
        ids = _unit_ids(_BODY)
        plan = RecompilePlan(
            uid="page0000",
            content_hash=page_content_hash(_BODY),
            dispositions=tuple((unit_id, "child:g1") for unit_id in ids),
            groups=(PlanGroup(key="g1", name="G1", description="d", confidence=1.0),),
            merges=(MergedSection(group="g1", text=secret, unit_ids=tuple(ids)),),
            page_actions=(),
            summary="",
            scope="structure-only",
            issues=(),
            verified=True,
        )
        outline = build_plan_outline(plan, segment_page(_BODY).units)
        assert secret not in json.dumps(outline)
        assert outline["groups"][0]["merged"] is True


# ---------------------------------------------------------------------------
# AC8: approve applies, reject suppresses, a changed page is re-planned.
# ---------------------------------------------------------------------------


def _answer(
    wiki: Path, *, decision_id: str, verdict: str, lock: Any = None
) -> Any:
    """Write a decision-answer file and apply it, the way a real answer
    surface does."""
    knowledge_root = wiki.parent
    raw_root = knowledge_root / "raw"
    (raw_root / "answers").mkdir(parents=True, exist_ok=True)
    path = write_decision_answer(
        raw_root,
        decision_id=decision_id,
        decision_type=DECISION_TYPE,
        verdict=verdict,
    )
    report = apply_decision_answers(wiki, raw_root, config=_config(), lock=lock)
    # An applied answer file stays on disk, so a second call re-reads the
    # first one too; pick out the outcome for the file THIS call wrote.
    return next(o for o in report.outcomes if o.path == path)


class _FakeLock:
    """Stands in for an acquired ``RunLock``. The applier only needs a
    non-``None`` object -- it never calls a method on it (the real apply path
    takes the lock in the CLI, exactly as ``_apply_coordinate_answer``'s own
    ``lock`` parameter is used)."""


class TestAC8Answers:
    def _queue_one(self, wiki: Path) -> str:
        _seed(wiki, count=1)
        summary = _run(wiki)
        assert summary.queued == 1
        return str(list_pending_page_recompile_plans(wiki)[0]["id"])

    def test_approve_applies_the_plan_through_the_applier(self, wiki: Path) -> None:
        plan_id = self._queue_one(wiki)
        pages_before = sorted(p.name for p in wiki.glob("*.md"))

        outcome = _answer(wiki, decision_id=plan_id, verdict=VERDICT_APPROVE, lock=_FakeLock())

        assert outcome.applied is True, outcome.message
        pages_after = sorted(p.name for p in wiki.glob("*.md"))
        assert len(pages_after) == len(pages_before) + 1
        # The item left the queue, recorded as approved.
        assert list_pending_page_recompile_plans(wiki) == []
        kinds = [r.get("kind") for r in read_page_recompile_ledger(wiki)]
        assert APPROVE_KIND in kinds

    def test_approve_structure_only_also_applies_a_structure_only_plan(
        self, wiki: Path
    ) -> None:
        plan_id = self._queue_one(wiki)
        outcome = _answer(
            wiki,
            decision_id=plan_id,
            verdict=VERDICT_APPROVE_STRUCTURE_ONLY,
            lock=_FakeLock(),
        )
        assert outcome.applied is True, outcome.message
        approve = next(
            r for r in read_page_recompile_ledger(wiki) if r.get("kind") == APPROVE_KIND
        )
        assert approve["verdict"] == VERDICT_APPROVE_STRUCTURE_ONLY

    def test_approve_requires_the_run_lock(self, wiki: Path) -> None:
        """An approve WRITES pages, so it is refused without an acquired run
        lock -- the same refusal, for the same single-writer reason, that a
        ``coordinate`` answer makes."""
        plan_id = self._queue_one(wiki)
        pages_before = sorted(p.name for p in wiki.glob("*.md"))

        outcome = _answer(wiki, decision_id=plan_id, verdict=VERDICT_APPROVE, lock=None)

        assert outcome.applied is False
        assert outcome.error_code == "lock_required"
        assert sorted(p.name for p in wiki.glob("*.md")) == pages_before
        # Refused, so the item is still answerable.
        assert len(list_pending_page_recompile_plans(wiki)) == 1

    def test_reject_writes_nothing_and_needs_no_lock(self, wiki: Path) -> None:
        plan_id = self._queue_one(wiki)
        pages_before = sorted(p.name for p in wiki.glob("*.md"))

        outcome = _answer(wiki, decision_id=plan_id, verdict=VERDICT_REJECT, lock=None)

        assert outcome.applied is True, outcome.message
        assert sorted(p.name for p in wiki.glob("*.md")) == pages_before
        assert list_pending_page_recompile_plans(wiki) == []
        kinds = [r.get("kind") for r in read_page_recompile_ledger(wiki)]
        assert REJECT_KIND in kinds

    def test_reject_skips_the_page_until_its_content_hash_changes(
        self, wiki: Path
    ) -> None:
        plan_id = self._queue_one(wiki)
        _answer(wiki, decision_id=plan_id, verdict=VERDICT_REJECT)

        # The next nightly pass must not re-ask the same question.
        suppressed = _StubClient()
        summary = _run(wiki, client=suppressed)
        assert summary.skipped_suppressed == 1
        assert summary.planned == 0
        assert suppressed.call_count == 0
        assert list_pending_page_recompile_plans(wiki) == []

        # Once the page genuinely moves, the suppression lifts on its own --
        # it is a hash comparison, not a permanent block.
        path = wiki / "page0000.md"
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        path.write_text(
            render_frontmatter(meta) + "\n" + body + "\nA newly added line.[^src-1]\n",
            encoding="utf-8",
        )
        assert rejected_page_skip_reason(wiki, uid="page0000", body=body) != ""
        _meta2, edited = parse_frontmatter(path.read_text(encoding="utf-8"))
        after = _run(wiki, client=_client_for(edited))
        assert after.skipped_suppressed == 0
        assert after.planned == 1

    def test_an_answer_against_a_changed_page_is_not_applied_and_is_replanned(
        self, wiki: Path
    ) -> None:
        """AC8's third case. The plan is pinned to a content hash; the page
        moves before the answer lands. Nothing is written, the stale draft
        leaves the queue, and -- crucially -- NO suppression is recorded, so
        the next pass drafts a fresh plan over the new bytes."""
        plan_id = self._queue_one(wiki)
        path = wiki / "page0000.md"
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        edited = body + "\nSomeone edited this page after the plan.[^src-1]\n"
        path.write_text(render_frontmatter(meta) + "\n" + edited, encoding="utf-8")
        pages_before = sorted(p.name for p in wiki.glob("*.md"))

        outcome = _answer(
            wiki, decision_id=plan_id, verdict=VERDICT_APPROVE, lock=_FakeLock()
        )

        assert outcome.applied is False
        assert outcome.error_code == "plan_stale"
        assert sorted(p.name for p in wiki.glob("*.md")) == pages_before
        # The stale draft left the queue...
        assert list_pending_page_recompile_plans(wiki) == []
        kinds = [r.get("kind") for r in read_page_recompile_ledger(wiki)]
        assert STALE_KIND in kinds
        assert REJECT_KIND not in kinds
        # ...and the page is re-planned rather than suppressed.
        assert rejected_page_skip_reason(wiki, uid="page0000", body=edited) == ""
        again = _run(wiki, client=_client_for(edited))
        assert again.planned == 1
        assert again.queued == 1

    def test_an_unknown_id_is_refused_cleanly(self, wiki: Path) -> None:
        self._queue_one(wiki)
        outcome = _answer(wiki, decision_id="deadbeefdeadbeef", verdict=VERDICT_REJECT)
        assert outcome.applied is False
        assert outcome.error_code == "id_not_found"

    def test_an_unknown_verdict_is_refused_cleanly(self, wiki: Path) -> None:
        plan_id = self._queue_one(wiki)
        outcome = _answer(wiki, decision_id=plan_id, verdict="maybe")
        assert outcome.applied is False
        assert outcome.error_code == "invalid_decision"
        assert len(list_pending_page_recompile_plans(wiki)) == 1

    def test_an_already_answered_item_is_refused(self, wiki: Path) -> None:
        plan_id = self._queue_one(wiki)
        _answer(wiki, decision_id=plan_id, verdict=VERDICT_REJECT)
        outcome = _answer(wiki, decision_id=plan_id, verdict=VERDICT_REJECT)
        assert outcome.applied is False
        assert outcome.error_code in ("already_resolved", "superseded")


class TestAC8MergeCarryingPlanIsVerifiable:
    """Async-reviewer finding on this PR, HIGH: ``write_apply`` records a
    ``merge-verify-unavailable`` issue and refuses the WHOLE plan when it is
    given no client, so approving any plan carrying a ``merge`` group -- the
    planner's default behaviour -- would always have been refused. AC8 says
    approve applies the plan through the applier, not "applies the subset of
    plans that merge nothing"."""

    def _queue_merge_plan(self, wiki: Path) -> str:
        """Queue an item whose outline reports a merged group, without
        needing the planner to produce a merge: the client decision is read
        off the OUTLINE, which is the contract under test."""
        from athenaeum.page_recompile_plan import (
            MergedSection,
            PlanGroup,
            RecompilePlan,
            append_plan_record,
            page_content_hash,
        )
        from athenaeum.page_recompile_queue import queue_plan_for_approval

        _seed(wiki, count=1)
        ids = _unit_ids(_BODY)
        plan = RecompilePlan(
            uid="page0000",
            content_hash=page_content_hash(_BODY),
            dispositions=tuple((unit_id, "merge:g1") for unit_id in ids),
            groups=(PlanGroup(key="g1", name="G1", description="d", confidence=1.0),),
            # The merged text must carry EVERY citation from the merged
            # units, or `compute_apply` refuses on `merge-dropped-citation`
            # before it ever reaches the verify gate this class is about.
            merges=(
                MergedSection(
                    group="g1",
                    text=(
                        "Acme builds widgets.[^src-1] They cost ten "
                        "pounds.[^src-2] Beta Corp resells them.[^src-3]"
                    ),
                    unit_ids=tuple(ids),
                ),
            ),
            page_actions=(),
            summary=_SUMMARY,
            scope="structure-only",
            issues=(),
            verified=True,
        )
        outline = build_plan_outline(plan, segment_page(_BODY).units)
        assert outline["groups"][0]["merged"] is True
        # Record the plan too, not just the queue item: `write_apply`
        # resolves the plan by id out of the SAME ledger, so a queued item
        # with no plan record behind it would refuse for the wrong reason.
        plan_id = append_plan_record(plan, wiki)
        record = queue_plan_for_approval(
            wiki, plan_id=plan_id, plan=plan, outline=outline
        )
        return str(record["id"])

    def test_a_merge_carrying_plan_gets_a_verify_client(self, wiki: Path) -> None:
        from athenaeum.decision_answers import _page_recompile_verify_client
        from athenaeum.page_recompile_queue import find_queued_plan

        plan_id = self._queue_merge_plan(wiki)
        item = find_queued_plan(wiki, plan_id=plan_id)
        assert item is not None
        sentinel = object()
        with patch(
            "athenaeum.provider.build_llm_client", return_value=sentinel
        ) as mock_build:
            resolved = _page_recompile_verify_client(item, _config())
        mock_build.assert_called_once()
        assert mock_build.call_args.kwargs["knob"] == "verify"
        assert resolved is sentinel

    def test_a_plan_with_no_merge_group_builds_no_client(self, wiki: Path) -> None:
        """The module's own "no model call" invariant still holds for every
        answer that does not need the check -- which is most of them."""
        from athenaeum.decision_answers import _page_recompile_verify_client
        from athenaeum.page_recompile_queue import find_queued_plan

        _seed(wiki, count=1)
        _run(wiki)
        plan_id = str(list_pending_page_recompile_plans(wiki)[0]["id"])
        item = find_queued_plan(wiki, plan_id=plan_id)
        assert item is not None
        with patch("athenaeum.provider.build_llm_client") as mock_build:
            assert _page_recompile_verify_client(item, _config()) is None
        mock_build.assert_not_called()

    def test_the_applier_receives_that_client_on_approve(self, wiki: Path) -> None:
        """End to end through the real answer path: the client reaches
        ``write_apply``, which is where the refusal came from."""
        plan_id = self._queue_merge_plan(wiki)
        stub = _StubClient()
        seen: dict[str, Any] = {}

        def _fake_write_apply(_wiki: Path, **kwargs: Any) -> Any:
            seen.update(kwargs)
            from athenaeum.page_recompile_apply import ApplyOutcome

            return ApplyOutcome(ok=True, reason="applied", plan_id=plan_id, uid="page0000")

        with (
            patch("athenaeum.provider.build_llm_client", return_value=stub),
            patch("athenaeum.page_recompile_apply.write_apply", _fake_write_apply),
        ):
            outcome = _answer(
                wiki, decision_id=plan_id, verdict=VERDICT_APPROVE, lock=_FakeLock()
            )
        assert outcome.applied is True, outcome.message
        assert seen["client"] is stub

    def test_no_configured_client_refuses_and_leaves_the_item_open(
        self, wiki: Path
    ) -> None:
        """With nothing configured ``build_llm_client`` returns ``None``; the
        applier then reports ``merge-verify-unavailable``. The honest
        outcome -- and the item must stay answerable rather than be closed
        with nothing done."""
        plan_id = self._queue_merge_plan(wiki)
        with patch("athenaeum.provider.build_llm_client", return_value=None):
            outcome = _answer(
                wiki, decision_id=plan_id, verdict=VERDICT_APPROVE, lock=_FakeLock()
            )
        assert outcome.applied is False
        assert outcome.error_code == "apply_refused"
        assert "merge-verify-unavailable" in outcome.message
        assert len(list_pending_page_recompile_plans(wiki)) == 1


class TestAlreadyQueuedPageCostsNothing:
    """Async-reviewer finding on this PR, MEDIUM. A page with an unanswered
    plan pinned to its current bytes must not be re-planned: the plan id
    would be identical, so the spend buys nothing -- and it would consume a
    slot of the page budget that a page further down the backlog needs."""

    def test_an_unanswered_page_is_skipped_and_costs_nothing(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=1)
        first = _StubClient()
        assert _run(wiki, client=first).queued == 1
        assert first.call_count > 0

        second = _StubClient()
        summary = _run(wiki, client=second)
        assert summary.skipped_already_queued == 1
        assert summary.planned == 0
        assert second.call_count == 0
        assert len(list_pending_page_recompile_plans(wiki)) == 1

    def test_the_budget_still_reaches_the_rest_of_the_backlog(
        self, wiki: Path
    ) -> None:
        """The defect this closes: with the first page queued, a one-page
        budget must spend itself on the SECOND page, not re-plan the first."""
        _seed(wiki, count=1)
        config = _config(max_pages_per_run=1)
        assert _run(wiki, config=config).queued == 1

        _write_page(wiki, uid="page0001", name="Acme 1", body=_BODY)
        summary = _run(wiki, config=config)
        assert summary.skipped_already_queued == 1
        assert summary.planned == 1
        assert summary.queued == 1
        queued_uids = {
            str(r.get("uid")) for r in list_pending_page_recompile_plans(wiki)
        }
        assert queued_uids == {"page0000", "page0001"}

    def test_an_edited_page_is_replanned_rather_than_skipped(
        self, wiki: Path
    ) -> None:
        """The skip is pinned to the CONTENT HASH, not the uid: an item
        drafted over bytes the page no longer holds is one no answer could
        apply, so the page must get a fresh plan."""
        _seed(wiki, count=1)
        assert _run(wiki).queued == 1

        path = wiki / "page0000.md"
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        edited = body + "\nSomeone edited this page.[^src-1]\n"
        path.write_text(render_frontmatter(meta) + "\n" + edited, encoding="utf-8")

        summary = _run(wiki, client=_client_for(edited))
        assert summary.skipped_already_queued == 0
        assert summary.planned == 1
        assert summary.queued == 1

    def test_shadow_mode_does_not_apply_the_skip(self, wiki: Path) -> None:
        """`shadow` raises no items, so there is no queue for it to consult
        -- it keeps re-drafting, which is what a shadow pass is for."""
        _seed(wiki, count=1)
        assert _run(wiki).queued == 1
        summary = _run(wiki, config=_config(mode="shadow"))
        assert summary.skipped_already_queued == 0
        assert summary.planned == 1

    def test_the_counter_reaches_the_run_summary(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        _run(wiki)
        summary = _run(wiki)
        assert "skipped_already_queued" in summary.as_profile_fields()
        assert summary.to_dict()["skipped_already_queued"] == 1


# ---------------------------------------------------------------------------
# AC9: the heartbeat is refreshed between pages.
# ---------------------------------------------------------------------------


class TestAC9Heartbeat:
    def test_the_heartbeat_is_refreshed_between_pages(self, wiki: Path) -> None:
        _seed(wiki, count=3)
        beats: list[int] = []
        summary = _run(
            wiki,
            config=_config(max_pages_per_run=3),
            heartbeat=lambda: beats.append(1),
        )
        assert summary.planned == 3
        # One per page boundary -- a long pass must not let the run lock's
        # heartbeat go stale and read as wedged.
        assert len(beats) == 3

    def test_the_phase_passes_the_run_contexts_own_heartbeat(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.librarian import _run_page_recompile_phase

        beats: list[int] = []
        ctx = _phase_ctx(tmp_path)
        ctx.heartbeat = lambda: beats.append(1)
        _seed(ctx.wiki_root, count=2)
        with patch("athenaeum.librarian.build_llm_client", return_value=_StubClient()):
            _run_page_recompile_phase(ctx)
        assert len(beats) == 2

    def test_a_phase_with_no_heartbeat_still_runs(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        assert _run(wiki, heartbeat=None).planned == 1


# ---------------------------------------------------------------------------
# AC10: spend per knob, the run summary's five figures, and status.
# ---------------------------------------------------------------------------


class TestAC10SpendRunSummaryAndStatus:
    def test_spend_is_recorded_per_knob(self, wiki: Path) -> None:
        """Every call is tagged with the knob that MADE it, which is what
        ``athenaeum spend --by-knob`` attributes on."""
        _seed(wiki, count=1)
        usage = TokenUsage()
        _run(wiki, usage=usage)
        assert usage.api_calls > 0
        assert usage.input_tokens > 0
        # The planner's three passes run on three knobs; each must appear.
        assert {"classify", "page_recompile", "verify"} <= set(usage.per_knob)
        for knob in ("classify", "page_recompile", "verify"):
            assert usage.per_knob[knob]["input_tokens"] > 0

    def test_the_phase_records_the_knobs_provider_and_model(
        self, tmp_path: Path
    ) -> None:
        """So ``spend.record_spend_per_knob_provider`` attributes this
        phase's spend to its own knob rather than the unmodeled default."""
        from athenaeum.librarian import _run_page_recompile_phase

        ctx = _phase_ctx(tmp_path)
        _seed(ctx.wiki_root, count=1)
        with patch("athenaeum.librarian.build_llm_client", return_value=_StubClient()):
            _run_page_recompile_phase(ctx)
        assert ctx.knob_providers.get("page_recompile")
        assert ctx.knob_models.get("page_recompile")

    def test_the_run_summary_reports_the_five_required_figures(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.librarian import _run_page_recompile_phase

        ctx = _phase_ctx(tmp_path, max_pages_per_run=1)
        _seed(ctx.wiki_root, count=3)
        with patch("athenaeum.librarian.build_llm_client", return_value=_StubClient()):
            _run_page_recompile_phase(ctx)

        assert ctx.page_recompile_summary is not None
        entry = next(row for row in ctx.run_profile if row[0] == "page-recompile")
        fields = entry[2]
        for required in ("planned", "queued", "invalid", "skipped_budget", "backlog"):
            assert required in fields, required
        assert fields["backlog"] == 3
        assert fields["planned"] == 1
        assert fields["queued"] == 1
        assert fields["skipped_budget"] == 2
        assert fields["invalid"] == 0

    def test_the_profile_entry_reaches_the_rendered_run_summary(
        self, tmp_path: Path
    ) -> None:
        """``run_profile`` is the shared input to BOTH the prose line and the
        durable JSONL record, so one append wires both."""
        from athenaeum.librarian import _render_run_summary

        summary = PageRecompileRunSummary(
            reason="ok", mode="queue", auto_apply_class="none", backlog=3, planned=1
        )
        rendered = _render_run_summary([("page-recompile", 0.1, summary.as_profile_fields())])
        assert "page-recompile" in rendered
        assert "backlog" in rendered

    def test_status_fills_plans_awaiting_approval_and_nights_since_progress(
        self, wiki: Path
    ) -> None:
        from athenaeum.page_recompile import find_recompile_candidates
        from athenaeum.status import build_page_recompile_backlog

        _seed(wiki, count=1)
        _run(wiki)

        scan = find_recompile_candidates(wiki, _config())
        block = build_page_recompile_backlog(
            scan, threshold_chars=_THRESHOLD, ceiling_chars=_CEILING, wiki_root=wiki
        )
        assert block["plans_awaiting_approval"] == 1
        assert block["nights_since_progress"] == 0

    def test_both_fields_stay_not_available_with_no_ledger(self, wiki: Path) -> None:
        """"Nothing has ever proposed a plan" and "the queue is empty" are
        different facts; only the first is a starvation signal, so an absent
        ledger must still read as not-available rather than a healthy zero."""
        from athenaeum.page_recompile import find_recompile_candidates
        from athenaeum.status import build_page_recompile_backlog

        _seed(wiki, count=1)
        scan = find_recompile_candidates(wiki, _config())
        block = build_page_recompile_backlog(
            scan, threshold_chars=_THRESHOLD, ceiling_chars=_CEILING, wiki_root=wiki
        )
        assert block["plans_awaiting_approval"] is None
        assert block["nights_since_progress"] is None

    def test_plans_awaiting_approval_is_a_real_zero_once_answered(
        self, wiki: Path
    ) -> None:
        _seed(wiki, count=1)
        _run(wiki)
        plan_id = str(list_pending_page_recompile_plans(wiki)[0]["id"])
        _answer(wiki, decision_id=plan_id, verdict=VERDICT_REJECT)
        assert plans_awaiting_approval(wiki) == 0

    def test_nights_since_progress_counts_whole_days(self, wiki: Path) -> None:
        _seed(wiki, count=1)
        _run(wiki)
        assert nights_since_progress(wiki, now=date.today()) == 0
        assert nights_since_progress(wiki, now=date.today() + timedelta(days=4)) == 4

    def test_the_status_render_shows_the_filled_figures(self, wiki: Path) -> None:
        from athenaeum.status import format_status, status

        _seed(wiki, count=1)
        _run(wiki)
        (wiki.parent / "raw").mkdir(exist_ok=True)
        (wiki.parent / "athenaeum.yaml").write_text(
            "librarian:\n"
            f"  page_size_threshold_chars: {_THRESHOLD}\n"
            f"  oversize_hard_ceiling_chars: {_CEILING}\n",
            encoding="utf-8",
        )
        out = format_status(status(wiki.parent))
        block = out.split("Page recompile backlog")[1]
        line = next(ln for ln in block.splitlines() if "plans awaiting approval" in ln)
        assert line.strip().endswith("1"), line


# ---------------------------------------------------------------------------
# AC11: README, docs and CHANGELOG say it is on, what it costs, and how to
# turn it off.
# ---------------------------------------------------------------------------


class TestAC11Documentation:
    @staticmethod
    def _repo() -> Path:
        return Path(__file__).resolve().parents[1]

    def test_readme_has_the_section(self) -> None:
        readme = self._repo() / "README.md"
        text = readme.read_text(encoding="utf-8")
        assert "Page recompile" in text
        # On by default, what it does, that it costs model calls nightly,
        # and the ONE setting that turns it off.
        lowered = text.lower()
        assert "on by default" in lowered
        assert "librarian.page_recompile.mode" in text
        assert "off" in lowered

    def test_every_budget_key_is_documented_with_its_default(self) -> None:
        reference = (
            self._repo() / "docs" / "reference" / "configuration.md"
        ).read_text(encoding="utf-8")
        keys = {
            "mode": repr(DEFAULT_PAGE_RECOMPILE_MODE),
            "auto_apply_class": "'none'",
            "max_pages_per_run": str(DEFAULT_PAGE_RECOMPILE_MAX_PAGES_PER_RUN),
            "opening_max_pages_per_run": str(
                DEFAULT_PAGE_RECOMPILE_OPENING_MAX_PAGES_PER_RUN
            ),
            "max_input_chars_per_run": str(
                DEFAULT_PAGE_RECOMPILE_MAX_INPUT_CHARS_PER_RUN
            ),
            "max_api_calls_per_run": str(
                DEFAULT_PAGE_RECOMPILE_MAX_API_CALLS_PER_RUN
            ),
            "max_open_items": str(DEFAULT_PAGE_RECOMPILE_MAX_OPEN_ITEMS),
        }
        for key, default in keys.items():
            heading = f"### `resolve_page_recompile_{key}`"
            assert heading in reference, key
            section = reference.split(heading)[1].split("### ")[0]
            assert f"`librarian.page_recompile.{key}`" in section, key
            assert f"**Default:** `{default}`" in section, (key, default)

    def test_the_changelog_unreleased_entry_leads_with_the_three_facts(self) -> None:
        changelog = (self._repo() / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## [Unreleased]")[1].split("\n## [")[0]
        entry = next(
            block
            for block in unreleased.split("\n- ")
            if "page recompile" in block.lower() and "nightly" in block.lower()
        )
        # "The first line for this change" is its bolded lead, not the first
        # physical line -- the file is hard-wrapped at 80 columns, so a
        # physical-line check would pin the wrapping rather than the claim.
        lead = " ".join(entry.split("**")[1].split()).lower()
        assert "on by default" in lead
        assert "model call" in lead or "api call" in lead
        assert "mode: off" in lead or "mode: `off`" in lead
