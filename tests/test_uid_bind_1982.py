# SPDX-License-Identifier: Apache-2.0
"""An explicit top-level ``uid:`` on raw intake binds the merge target
directly, bypassing name resolution (issue athenaeum#1982).

**The defect:** a raw intake file that names its target page explicitly
with a top-level ``uid:`` key was still resolved by entity NAME on the
librarian's tier path (:func:`athenaeum.tiers.tier1_programmatic_match`'s
raw-content word-boundary scan, and the tier-2/3 create-name gate's own
name/meaning-based resolver, :func:`athenaeum.tiers.
gate_create_name_classifications` -> :func:`athenaeum.tiers.
validate_create_name`). When the file's declared ``name:`` is generic (a
bare first name shared by several pages), name resolution either picks the
wrong page or cannot pick one at all and escalates the file as ambiguous —
even though the file already named its target unambiguously via ``uid:``.

:func:`athenaeum.librarian.process_one`'s uid-bind step (issue athenaeum#1982)
resolves the declared uid via a plain, LLM-free :meth:`~athenaeum.models.
EntityIndex.get_by_uid` lookup BEFORE any of those name-resolving tiers run,
and short-circuits straight to Tier 3's write-merge when it hits.

One test class per acceptance criterion this file covers:

- ``TestUidBindPicksRightPage`` (AC1/AC5): three fixture pages share the
  raw file's declared ``name:``; the uid-bound merge lands on the ONE
  the file's ``uid:`` actually names, Tier 1/2 never run, and the classify
  client is asserted to never receive a call (AC5's "no LLM call for the
  binding decision").
- ``TestUidBindUnknownFallsBack`` (AC2): a ``uid:`` matching no existing
  page falls back to today's name resolution (tier2_classify IS called),
  and the miss is recorded on ``ProcessingResult.uid_bind_unresolved``.
- ``TestUidBindNameMismatch`` (AC3): a ``uid:`` matching a page whose
  ``name:`` differs from the file's own still binds by uid; the mismatch
  is recorded on ``ProcessingResult.uid_bind_mismatch``, not refused.

- ``TestRunSummarySurfacesUidBound`` (AC4): the uid-bound count appears on
  the RENDERED human-readable run-summary line -- the literal `NC NU NE NF`
  snapshot message a human reads diagnosing a run (the surface the issue's
  own "0C 0U 11E 0F" observation was read off), for BOTH the normal
  end-of-run snapshot and the interrupted/partial-run snapshot. Asserted
  against the actual rendered text via a real :func:`athenaeum.librarian.run`
  call, not just the counter attribute -- a test that only checked
  ``ctx.total_uid_bound`` could not see a gap at this specific surface.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import process_one, run
from athenaeum.models import EntityIndex, RawFile, TokenUsage

# Reuse the deadline suite's fixtures verbatim -- same run() harness, same
# "mock process_one, read the git-commit subject back" idiom
# TestRunSummarySurfacesUidBound needs below.
from tests.test_librarian_deadline import _last_subject, _seed_knowledge_root

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


def _page(wiki: Path, filename: str, *, uid: str, name: str) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\naccess: internal\n---\n\n"
        f"# {name}\n\nPre-existing body.\n",
        encoding="utf-8",
    )
    return path


def _raw(raw_dir: Path, content: str, filename: str = "draft.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source="frontier-enrichment", timestamp="", uuid8="")


def _asserting_classify_client() -> MagicMock:
    """A classify client that fails the test if it is ever called.

    AC5: the uid-bind decision is LLM-free. When the bind hits, tier2's
    classify client must never be touched.
    """
    client = MagicMock()
    client.messages.create.side_effect = AssertionError(
        "tier2_classify must not be called when the uid bind resolved "
        "(issue athenaeum#1982 AC5)"
    )
    return client


class TestUidBindPicksRightPage:
    """AC1 + AC5: three pages share the file's declared name; the uid wins."""

    def test_binds_to_the_uid_named_page_among_three_same_name_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = tmp_path / "wiki"
        _page(wiki, "pat-001.md", uid="pat-001", name="Pat")
        target = _page(wiki, "pat-002.md", uid="pat-002", name="Pat")
        _page(wiki, "pat-003.md", uid="pat-003", name="Pat")
        index = EntityIndex(wiki)

        raw = _raw(
            tmp_path / "raw" / "frontier-enrichment",
            "---\nuid: pat-002\nname: Pat\n"
            "source: script:athenaeum_adapters.frontier_enrichment\n---\n\n"
            "Observed: Pat shipped the quarterly report early.\n",
        )

        captured_actions: list[object] = []

        def _fake_tier3_derive_actions(raw_, actions, *_args, **_kwargs):
            captured_actions.extend(actions)
            new_content = (
                "---\nuid: pat-002\ntype: person\nname: Pat\naccess: internal\n"
                "---\n\n# Pat\n\nPre-existing body.\n\nPat shipped the quarterly "
                "report early.[^1]\n\n[^1]: frontier-enrichment/draft.md\n"
            )
            return [], [(target, new_content)], ["pat-002"], []

        monkeypatch.setattr(
            "athenaeum.librarian.tier3_derive_actions", _fake_tier3_derive_actions
        )

        usage = TokenUsage()
        result = process_one(
            raw,
            index,
            wiki,
            _asserting_classify_client(),
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=usage,
            write_client=MagicMock(),
        )

        assert result.uid_bound == 1
        assert result.uid_bind_unresolved == 0
        assert result.uid_bind_mismatch == 0
        assert result.updated == ["pat-002"]
        # Tier 1/2 never ran: no name-resolution fan-out was dispatched.
        assert result.matched == 0
        assert result.escalated == []

        # Exactly one action, aimed at the uid-named page, never the other
        # two same-named pages.
        assert len(captured_actions) == 1
        assert captured_actions[0].existing_uid == "pat-002"
        assert captured_actions[0].kind == "update"

        # The write landed on the uid-named page; its same-name siblings
        # are untouched.
        assert "quarterly report" in target.read_text(encoding="utf-8")
        for other in ("pat-001.md", "pat-003.md"):
            assert "quarterly report" not in (wiki / other).read_text(encoding="utf-8")


class TestUidBindUnknownFallsBack:
    """AC2: an unresolvable uid falls back to name resolution, and the
    miss is recorded."""

    def test_unknown_uid_falls_back_and_records_the_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)

        raw = _raw(
            tmp_path / "raw" / "frontier-enrichment",
            "---\nuid: no-such-uid\nname: Nobody Indexed\n"
            "source: script:athenaeum_adapters.frontier_enrichment\n---\n\n"
            "Observed: nothing in particular.\n",
        )

        classify_calls: list[object] = []

        def _fake_tier2_classify(*_args, **_kwargs):
            classify_calls.append(True)
            return []

        monkeypatch.setattr(
            "athenaeum.librarian.tier2_classify", _fake_tier2_classify
        )

        usage = TokenUsage()
        result = process_one(
            raw,
            index,
            wiki,
            MagicMock(),
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=usage,
            write_client=MagicMock(),
        )

        assert result.uid_bound == 0
        assert result.uid_bind_unresolved == 1
        assert result.uid_bind_mismatch == 0
        # Fallback genuinely reached today's name-resolution path.
        assert classify_calls == [True]


class TestUidBindNameMismatch:
    """AC3: uid wins even when the file's own name disagrees with the
    bound page's name; the disagreement is only ever counted."""

    def test_mismatched_name_still_binds_by_uid_and_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = tmp_path / "wiki"
        target = _page(wiki, "pat-002.md", uid="pat-002", name="Pat")
        index = EntityIndex(wiki)

        raw = _raw(
            tmp_path / "raw" / "frontier-enrichment",
            "---\nuid: pat-002\nname: Patricia\n"
            "source: script:athenaeum_adapters.frontier_enrichment\n---\n\n"
            "Observed: Patricia shipped the quarterly report early.\n",
        )

        captured_actions: list[object] = []

        def _fake_tier3_derive_actions(raw_, actions, *_args, **_kwargs):
            captured_actions.extend(actions)
            new_content = (
                "---\nuid: pat-002\ntype: person\nname: Pat\naccess: internal\n"
                "---\n\n# Pat\n\nPre-existing body.\n\nPatricia shipped the "
                "quarterly report early.[^1]\n\n[^1]: frontier-enrichment/draft.md\n"
            )
            return [], [(target, new_content)], ["pat-002"], []

        monkeypatch.setattr(
            "athenaeum.librarian.tier3_derive_actions", _fake_tier3_derive_actions
        )

        usage = TokenUsage()
        result = process_one(
            raw,
            index,
            wiki,
            _asserting_classify_client(),
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=usage,
            write_client=MagicMock(),
        )

        assert result.uid_bound == 1
        assert result.uid_bind_mismatch == 1
        assert result.uid_bind_unresolved == 0
        assert result.updated == ["pat-002"]
        assert len(captured_actions) == 1
        assert captured_actions[0].existing_uid == "pat-002"
        # The bound page's OWN name wins over the file's disagreeing name --
        # the action is never attributed to "Patricia".
        assert captured_actions[0].name == "Pat"


class TestRunSummarySurfacesUidBound:
    """AC4: the uid-bound count is on the RENDERED human-readable snapshot
    line -- not just the structured ``run_profile``/durable-ledger field.

    Drives a real :func:`athenaeum.librarian.run`, with ``process_one``
    replaced by a stand-in that reports ``uid_bound`` on its
    ``ProcessingResult``-shaped return, and reads the actual committed
    snapshot subject back -- the same thing a human reads when diagnosing a
    run.
    """

    def test_normal_completion_snapshot_carries_the_uid_bound_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=1)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        def _fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
            return SimpleNamespace(
                created=[],
                updated=["pat-002"],
                escalated=[],
                skipped=[],
                uid_bound=1,
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", _fake_process_one)

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
            max_runtime=1000,
        )

        assert rc == 0
        subject = _last_subject(root)
        assert subject.startswith("librarian: processed 1 file(s)")
        # The literal C/U/E/F line, now carrying uid-bound beside it.
        assert "(0C 1U 0E 0F 1B)" in subject

    def test_interrupted_run_snapshot_also_carries_the_uid_bound_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OTHER snapshot site (``librarian.py``'s signal-interrupt
        partial-run commit, issue athenaeum#337) -- a SIGTERM mid-run, same
        harness as ``test_librarian_interrupt.py``, not the wall-clock
        deadline path (that one renders the ordinary ``processed N
        file(s)`` message, already covered by the normal-completion test
        above)."""
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)

        state = {"n": 0}

        def _fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
            state["n"] += 1
            page = (root / "wiki") / f"entity-{state['n']}.md"
            page.write_text(f"# Entity {state['n']}\n", encoding="utf-8")
            if state["n"] == 2:
                # Signal fires mid-call, BEFORE this (second) call returns --
                # file 1's result below is already folded into ctx.total_*
                # by the time it arrives, mirroring
                # test_librarian_interrupt.py's own interrupt_on=2 shape.
                os.kill(os.getpid(), signal.SIGTERM)
            return SimpleNamespace(
                created=[],
                updated=["pat-002"],
                escalated=[],
                skipped=[],
                uid_bound=1,
            )

        monkeypatch.setattr("athenaeum.librarian.process_one", _fake_process_one)

        # Safety net mirroring test_librarian_interrupt.py's sentinel: if
        # run() ever regresses and fails to install its own handler, this
        # turns the self-sent signal into a clean AssertionError instead of
        # killing the whole pytest process.
        def _sentinel(signum: int, frame: object) -> None:
            raise AssertionError(
                f"run() did not install a signal {signum} handler"
            )

        prev = signal.signal(signal.SIGTERM, _sentinel)
        try:
            with pytest.raises(SystemExit) as excinfo:
                run(
                    raw_root=root / "raw",
                    wiki_root=root / "wiki",
                    knowledge_root=root,
                    max_api_calls=100,
                    install_signal_handlers=True,
                )
        finally:
            signal.signal(signal.SIGTERM, prev)

        assert excinfo.value.code == 124
        subject = _last_subject(root)
        assert subject.startswith("librarian: partial run (interrupted after 1 file(s)")
        assert "0C 1U 0E 0F 1B)" in subject
