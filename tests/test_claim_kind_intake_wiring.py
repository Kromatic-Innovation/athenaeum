# SPDX-License-Identifier: Apache-2.0
"""Tests for wiring ``claim_kind.stamp_claim_kind`` into the intake path (issue athenaeum#742).

Covers the production call site added in
:func:`athenaeum.librarian._stamp_unclassified_claim_kinds` (invoked from
:func:`athenaeum.librarian._run_auto_memory_phase`, right after C1 discovery
and before the C2 cluster pass):

- AC1: the stamper is called for a raw auto-memory file carrying no
  author-supplied ``claim_kind:``, and the frontmatter gets stamped.
- AC2: an author-supplied ``claim_kind:`` is never overwritten (no LLM call
  fires at all — ``stamp_claim_kind``'s own idempotency).
- AC3: the stamped ``claim_kind`` round-trips through the intake path so a
  fresh :func:`athenaeum.librarian.discover_auto_memory_files` (and, for the
  entity-schema sibling path, :func:`athenaeum.librarian.tier0_passthrough`)
  reads it back via :func:`athenaeum.models.parse_claim_kind` — a real
  integration across the discover -> stamp -> re-read boundary, not a
  same-process field check.
- AC4: ``resolutions._stance_attribution_verdict`` / ``propose_resolution``'s
  ``attribute_both`` short-circuit fires on a pair of opinion claims that
  were classified and stamped THROUGH the wired intake call site (not
  hand-written ``claim_kind:`` frontmatter).
- AC6: the classifier's import stays lazy at the call site (never at
  ``athenaeum.librarian`` module scope) and the recall hot path
  (``athenaeum.query_topics`` / ``athenaeum._cmd_query``) never imports the
  claim_kind classifier or ``athenaeum.llm_schemas``.
- AC8: once stamping is wired, a detector-``stance``-routed pair whose
  members carry STAMPED non-opinion kinds is blocked from the attribution
  short-circuit (falls through to source precedence instead) — pinned here
  so the behavior change is explicit, not discovered in production.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.librarian import (
    _stamp_unclassified_claim_kinds,
    discover_auto_memory_files,
    tier0_passthrough,
)
from athenaeum.models import EntityIndex, RawFile, TokenUsage, parse_claim_kind, parse_frontmatter
from athenaeum.resolutions import propose_resolution

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    response.usage = MagicMock(
        input_tokens=1,
        output_tokens=1,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    client.messages.create.return_value = response
    return client


def _responder_client(kind_by_snippet_substr: dict[str, str]) -> MagicMock:
    """A client whose response varies by a substring of the outgoing prompt.

    Lets a single fake client classify two different opinion snippets
    distinctly in one AC4 test, mirroring how a real Haiku call would.
    """
    client = MagicMock()

    def _create(**kwargs):
        user_msg = kwargs["messages"][0]["content"]
        for substr, kind in kind_by_snippet_substr.items():
            if substr in user_msg:
                response = MagicMock()
                response.content = [MagicMock(text=f'{{"claim_kind": "{kind}"}}')]
                response.usage = MagicMock(
                    input_tokens=1,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                )
                return response
        raise AssertionError(f"no canned kind for prompt: {user_msg!r}")

    client.messages.create.side_effect = _create
    return client


def _seed_auto_memory_root(knowledge_root: Path) -> Path:
    auto = knowledge_root / "raw" / "auto-memory"
    auto.mkdir(parents=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return auto


def _write_auto_memory_file(
    scope_dir: Path,
    filename: str,
    *,
    name: str,
    body: str,
    claim_kind: str | None = None,
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    lines = ["---", f"name: {name}", "type: feedback"]
    if claim_kind is not None:
        lines.append(f"claim_kind: {claim_kind}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC1 — the wired call site classifies an unclassified raw memory
# ---------------------------------------------------------------------------


class TestAC1CallSiteFires:
    def test_stamps_unclassified_auto_memory_file(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        path = _write_auto_memory_file(
            scope,
            "feedback_onboarding.md",
            name="Onboarding feel",
            body="The onboarding flow feels clunky.",
        )

        files = discover_auto_memory_files(knowledge_root)
        assert files[0].claim_kind == ""  # nothing stamped yet

        client = _client('{"claim_kind": "opinion"}')
        usage = TokenUsage()
        _stamp_unclassified_claim_kinds(files, client, None, usage)

        client.messages.create.assert_called_once()
        assert files[0].claim_kind == "opinion"
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("claim_kind") == "opinion"
        assert usage.api_calls == 1

    def test_noop_when_client_is_none(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        _write_auto_memory_file(
            scope, "feedback_x.md", name="X", body="Some claim."
        )
        files = discover_auto_memory_files(knowledge_root)
        _stamp_unclassified_claim_kinds(files, None, None, None)
        assert files[0].claim_kind == ""

    def test_empty_file_list_is_noop(self) -> None:
        client = _client('{"claim_kind": "opinion"}')
        _stamp_unclassified_claim_kinds([], client, None, None)
        client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# AC2 — an author-supplied claim_kind is never overwritten
# ---------------------------------------------------------------------------


class TestAC2NeverOverwritesAuthorSupplied:
    def test_author_supplied_kind_survives_a_disagreeing_classifier(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        path = _write_auto_memory_file(
            scope,
            "feedback_declared.md",
            name="Declared fact",
            body="The develop tip is abc123.",
            claim_kind="fact",
        )

        files = discover_auto_memory_files(knowledge_root)
        assert files[0].claim_kind == "fact"

        # Even if the classifier would say something else, it must never be
        # consulted for an already-classified file.
        client = _client('{"claim_kind": "opinion"}')
        _stamp_unclassified_claim_kinds(files, client, None, None)

        client.messages.create.assert_not_called()
        assert files[0].claim_kind == "fact"
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("claim_kind") == "fact"


# ---------------------------------------------------------------------------
# AC3 — round-trip integration: intake -> stamp -> tier0 -> compiled frontmatter
# ---------------------------------------------------------------------------


class TestAC3RoundTripIntegration:
    def test_entity_schema_stamp_round_trips_through_tier0(self, tmp_path: Path) -> None:
        """Entity-schema sibling path: raw file -> stamp -> tier0_passthrough
        -> compiled wiki page frontmatter -> parse_claim_kind reads it back.

        This is the entity-schema (uid/type/name) intake path, a parallel
        sibling to the auto-memory path AC4 exercises below. Both round-trip
        through the SAME ``claim_kind:`` frontmatter key and the SAME
        ``parse_claim_kind`` reader.
        """
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw_path = tmp_path / "raw.md"
        raw_path.write_text(
            "---\n"
            "uid: '99999'\n"
            "type: reference\n"
            "name: Deploy target opinion\n"
            "---\n"
            "Fly.io is the nicer platform than Heroku.\n",
            encoding="utf-8",
        )
        # Stamp BEFORE tier0 reads the file, exactly like the wired call
        # site runs before C2/C3 consume the auto-memory sibling files.
        from athenaeum.claim_kind import stamp_claim_kind

        client = _client('{"claim_kind": "opinion"}')
        kind = stamp_claim_kind(raw_path, client)
        assert kind == "opinion"

        # RawFile.content is a cached property read from disk lazily; a
        # fresh RawFile instance (as a real discovery pass would produce)
        # observes the just-written stamp.
        rf_after_stamp = RawFile(
            path=raw_path,
            source="sessions",
            timestamp="20260805T090000Z",
            uuid8="deadbeef",
        )
        index = EntityIndex(wiki_root)
        entity = tier0_passthrough(rf_after_stamp, index, wiki_root, ["reference"])
        assert entity is not None

        written = (wiki_root / entity.filename).read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(written)
        assert meta.get("claim_kind") == "opinion"
        assert parse_claim_kind(meta) == "opinion"

    def test_auto_memory_stamp_round_trips_through_discovery(
        self, tmp_path: Path
    ) -> None:
        """Auto-memory path: intake dir -> wired stamp call -> a FRESH
        discover_auto_memory_files() re-read sees the stamped claim_kind."""
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        _write_auto_memory_file(
            scope,
            "feedback_tabs.md",
            name="Tabs opinion",
            body="Tabs are better than spaces.",
        )

        files = discover_auto_memory_files(knowledge_root)
        client = _client('{"claim_kind": "opinion"}')
        _stamp_unclassified_claim_kinds(files, client, None, None)

        # Re-discover from scratch (a fresh process boundary in production —
        # the NEXT run's C1 discovery, or this same run's reresolve path)
        # rather than trusting the in-memory mutation.
        refreshed = discover_auto_memory_files(knowledge_root)
        assert refreshed[0].claim_kind == "opinion"


# ---------------------------------------------------------------------------
# AC4 — attribute_both fires on an auto-stamped opinion pair built THROUGH intake
# ---------------------------------------------------------------------------


class TestAC4AttributeBothOnAutoStampedPair:
    def test_stance_pair_stamped_through_intake_gets_attribute_both(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        _write_auto_memory_file(
            scope, "feedback_tabs.md", name="Tabs opinion", body="Tabs win."
        )
        _write_auto_memory_file(
            scope, "feedback_spaces.md", name="Spaces opinion", body="Spaces win."
        )

        # C1 discover, then the wired C1.5 stamp — exactly the order
        # _run_auto_memory_phase drives them in.
        files = discover_auto_memory_files(knowledge_root)
        assert {f.claim_kind for f in files} == {""}

        client = _responder_client({"Tabs win.": "opinion", "Spaces win.": "opinion"})
        _stamp_unclassified_claim_kinds(files, client, None, None)
        assert {f.claim_kind for f in files} == {"opinion"}

        by_name = {f.name: f for f in files}
        a = by_name["Tabs opinion"]
        b = by_name["Spaces opinion"]
        detector_result = ContradictionResult(
            detected=True,
            conflict_type="stance",
            members_involved=[f"{a.origin_scope}/{a.path.name}", f"{b.origin_scope}/{b.path.name}"],
            conflicting_passages=["Tabs win.", "Spaces win."],
            rationale="opposing evaluative preferences",
        )

        # No asserter identity on either side (the common Claude-session
        # case) — a resolver client is passed but must NEVER be called,
        # since the deterministic short-circuit resolves it before any LLM
        # reasoning is needed.
        resolver_client = MagicMock()
        proposal = propose_resolution(detector_result, [a, b], resolver_client)

        resolver_client.messages.create.assert_not_called()
        assert proposal.action == "attribute_both"
        assert proposal.recommended_winner == "neither"
        # Not resolved by source precedence — no precedence citation.
        assert proposal.source_precedence_used == []


# ---------------------------------------------------------------------------
# AC8 — the engagement gate BLOCKS attribution once a stamped non-opinion
# kind appears on a detector-`stance`-routed pair (behavior change pin)
# ---------------------------------------------------------------------------


class TestAC8StampedNonOpinionBlocksAttribution:
    def test_stance_routed_pair_with_stamped_fact_falls_through_to_llm(
        self, tmp_path: Path
    ) -> None:
        """Before athenaeum#742, an unclassified stance-routed pair always hit the
        attribute_both short-circuit (both sides carried claim_kind == "").
        Now that stamping runs at intake, a pair where ONE side is
        classified (through the SAME wired call site) as an explicit
        non-opinion kind (`fact`) must NOT be treated as an opinion pair —
        the engagement gate returns None and the normal LLM resolver path
        runs instead. Pinned here rather than discovered in production.
        """
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        _write_auto_memory_file(
            scope,
            "feedback_claim_a.md",
            name="Claim A",
            body="The develop tip is abc123.",
        )
        _write_auto_memory_file(
            scope,
            "feedback_claim_b.md",
            name="Claim B",
            body="The develop tip is xyz789.",
        )

        files = discover_auto_memory_files(knowledge_root)
        client = _responder_client(
            {"abc123": "fact", "xyz789": "fact"}
        )
        _stamp_unclassified_claim_kinds(files, client, None, None)
        assert {f.claim_kind for f in files} == {"fact"}

        by_name = {f.name: f for f in files}
        a = by_name["Claim A"]
        b = by_name["Claim B"]
        # The detector still routes this as "stance" (e.g. a borderline
        # classification) even though the stamped kind is now `fact` on
        # both sides — exactly the scenario the docstring on
        # _stance_attribution_verdict describes as newly reachable once
        # stamping is wired.
        detector_result = ContradictionResult(
            detected=True,
            conflict_type="stance",
            members_involved=[f"{a.origin_scope}/{a.path.name}", f"{b.origin_scope}/{b.path.name}"],
            conflicting_passages=["abc123", "xyz789"],
            rationale="disagreement over the develop tip SHA",
        )

        resolver_client = _client(
            '{"winner": "b", "action": "keep_b", "rationale": "newer", "confidence": 0.9}'
        )
        proposal = propose_resolution(detector_result, [a, b], resolver_client)

        # The engagement gate blocked the attribution short-circuit (an
        # explicit non-opinion kind on both sides), so the normal LLM
        # resolver path ran instead — proven by the resolver client
        # actually being called.
        resolver_client.messages.create.assert_called_once()
        assert proposal.action != "attribute_both"


# ---------------------------------------------------------------------------
# AC6 — no recall-hot-path reach, lazy import at the call site
# ---------------------------------------------------------------------------


class TestAC6NoHotPathLazyImport:
    def test_call_site_imports_claim_kind_lazily_not_at_module_scope(self) -> None:
        """athenaeum.librarian must NOT import athenaeum.claim_kind at module
        scope — only inside _stamp_unclassified_claim_kinds (or another
        function body), matching the deferred-import pattern already used
        for athenaeum.batch.process_batch_run in the same module.
        """
        src = Path(__file__).resolve().parent.parent / "src" / "athenaeum" / "librarian.py"
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        module_level_imports: set[str] = set()
        for node in tree.body:  # top-level statements ONLY
            if isinstance(node, ast.ImportFrom) and node.module:
                module_level_imports.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_level_imports.add(alias.name)
        assert "athenaeum.claim_kind" not in module_level_imports

        # It IS imported somewhere (inside the stamping function) — confirm
        # the deferred edge exists at all, so this test cannot pass by the
        # wiring silently being removed.
        full_src = src.read_text(encoding="utf-8")
        assert "from athenaeum.claim_kind import stamp_claim_kind" in full_src

    def test_recall_hot_path_never_imports_claim_kind_or_llm_schemas(self) -> None:
        """athenaeum.query_topics / athenaeum._cmd_query (the UserPromptSubmit
        hook's 3s-budget recall path) must not pull in the claim_kind
        classifier or athenaeum.llm_schemas as a side effect of import —
        run in a fresh subprocess so no other test's imports leak in.
        """
        probe = (
            "import sys\n"
            "import athenaeum.query_topics\n"
            "import athenaeum._cmd_query\n"
            "assert 'athenaeum.claim_kind' not in sys.modules, "
            "'claim_kind classifier reached from the recall hot path'\n"
            "assert 'athenaeum.llm_schemas' not in sys.modules, "
            "'llm_schemas reached from the recall hot path via bare import'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout

    def test_query_topics_extract_topics_never_calls_claim_kind_classifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders on the same guarantee at the call level: the
        recall hot path's own LLM call goes through ``extract_topics``'s own
        client, never a second (claim_kind) call. Asserted via the fake
        client's call count/params rather than ``sys.modules`` — this test
        runs in-process alongside others in this file that legitimately
        import ``athenaeum.claim_kind``, so a module-presence check here
        would be a false positive; the subprocess test above is the
        authoritative import-absence guard.
        """
        import anthropic

        from athenaeum import query_topics
        from tests.conftest import FakeLLMClient

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        fake = FakeLLMClient(text='["topic one"]')
        monkeypatch.setattr(anthropic, "Anthropic", fake)

        topics = query_topics.extract_topics("What's the status of the migration?")
        assert topics == ["topic one"]
        # Exactly one call, and it is query_topics' own topic-extraction
        # system prompt — not the claim_kind classifier's system prompt.
        assert len(fake.calls) == 1
        assert "EPISTEMIC KIND" not in fake.calls[0].get("system", "")


# ---------------------------------------------------------------------------
# Phase-level integration — the ACTUAL _run_auto_memory_phase call site
# (not just the extracted helper) performs the stamp.
# ---------------------------------------------------------------------------


class TestRunAutoMemoryPhaseInvokesStamping:
    def test_phase_stamps_before_compile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the real ``_run_auto_memory_phase`` (not the extracted
        helper in isolation) far enough to prove IT calls the stamper with
        ``ctx.classify_client`` (issue athenaeum#841 — the ``classify`` knob's
        client; ``claim_kind`` stamping shares that knob) before compile
        runs. ``_compile_auto_memory`` and the reresolve pass are stubbed to
        no-ops — this test's job is to pin the phase function's own
        wiring/ordering, not to re-exercise clustering/merge (covered
        elsewhere).
        """
        from athenaeum import librarian

        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = auto / "-Users-alice-Code-project"
        _write_auto_memory_file(
            scope, "feedback_x.md", name="X opinion", body="X is better than Y."
        )
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir()

        client = _client('{"claim_kind": "opinion"}')

        monkeypatch.setattr(librarian, "_compile_auto_memory", lambda *a, **kw: [])
        monkeypatch.setattr(librarian, "_run_reresolve_pass", lambda *a, **kw: 0)

        ctx = librarian.RunContext(
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
            retire=False,
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
        from athenaeum.config import load_config

        ctx.config = load_config(knowledge_root)
        ctx.classify_client = client

        librarian._run_auto_memory_phase(ctx)

        client.messages.create.assert_called_once()
        meta, _ = parse_frontmatter(
            (scope / "feedback_x.md").read_text(encoding="utf-8")
        )
        assert meta.get("claim_kind") == "opinion"
