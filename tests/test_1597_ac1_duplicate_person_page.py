# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1597 AC1 follow-on — does restoring tier1 person-matching
actually prevent the duplicate-entity-page defect?

A separate, binding operator ruling reads: "Thin type:source pages are BY
DESIGN. The defect is a second ENTITY page or an orphaned source page."
That ruling is why ``DEMOTED_NAME_MATCH_TYPES`` (issue athenaeum#1183, which
withheld ``type: person`` from :meth:`~athenaeum.models.EntityIndex.items`,
so :func:`~athenaeum.tiers.tier1_programmatic_match` could never match one)
was removed: a live-corpus sample found real cases of a person already
having a wiki page under a name/alias variant the tier-0 registry consult
did not catch, with tier1 restoration proposed as the fix so such a mention
resolves to a tier-3 MERGE into the existing page instead of a tier-3 CREATE
of a duplicate.

**This file MEASURES whether that actually works, rather than assuming it.**
Two scenarios, both driven through the real ``athenaeum.librarian.run()``
pipeline:

1. ``TestExactNameMatchIsHandledButNotByTier1`` — a CLEAN, exact-name
   mention of an existing person with no decoration on either side.
   Passes today. But the run log shows WHY it passes: the tier-0
   ``resolve_person_mention``/``attribute_person_observation`` consult
   (unaffected by the demotion the whole time — it was never gated by
   ``DEMOTED_NAME_MATCH_TYPES``) claims the file first, zero LLM calls,
   and tier1 never gets a chance to run at all (``matched == 0`` in the
   run summary). Restoring tier1 gets ZERO credit for this case.

2. ``TestDecoratedNameMismatchStillDuplicates`` — the real shape sampled
   from the live corpus (see the PR body's "Duplicate entity pages"
   section): an existing page whose ``name:`` frontmatter carries
   decorative characters picked up by a CRM import (e.g. ``"Bill Lennan
   \U0001F4AD"``, no ``aliases:`` registered), and a raw mention using the
   clean form ("Bill Lennan"). **This is marked ``xfail(strict=True)``,
   not asserted as passing** — measured directly against this PR's own
   current head (after both the tier-3 guard removal AND the
   ``DEMOTED_NAME_MATCH_TYPES`` removal), the outcome is TWO person pages,
   not one. Neither the tier-0 registry consult nor the now-restored tier1
   match can bridge this gap: both require the target's full registered
   ``name``/``aliases`` string to appear as a literal (word-boundary,
   case-insensitive) substring of the new raw text -- see
   :func:`athenaeum.identity_resolution.match_person_mentions` and
   :func:`athenaeum.tiers.tier1_programmatic_match`. A decorated `name:`
   field with no clean alias registered defeats both identically, so
   restoring tier1 wins nothing here either.

**Conclusion recorded in the PR body:** restoring tier1 person-matching is
correct, low-risk, semantically-restorative infrastructure, and it is NOT
provably wrong to build (the operator explicitly asked for it) -- but
measured against the live corpus's actual 305 held ``PersonNeverLLMRewriteError``
entries, it produces ZERO additional tier1 person-type matches (0 of 305,
verified by running the real ``tier1_programmatic_match`` against the real
wiki + the real still-on-disk raw files), because every one of those 305
already failed an equivalent-strength match test (the tier-0 consult, which
runs first, unconditionally, over the identical underlying data on an
unmigrated corpus) -- that is WHY each became a ``create`` action in the
first place. The duplicate-page count this PR's own earlier measurement put
at ~25/233 unique names (~30-35/305 entries) high-confidence does NOT drop
to zero, or measurably at all, from this change alone. See the PR body for
the full accounting and the recommended follow-up (key normalization, a
fuzzy/alias-backed match, or a corpus name-field cleanup -- none built here,
all separate, unscoped decisions).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _seed_root(tmp_path: Path, *, existing_name_field: str, mention_text: str) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n| company |\n"
    )
    (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
    (wiki / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )

    existing = wiki / "aaaaaaaa-bill-lennan.md"
    existing.write_text(
        f"---\nuid: aaaaaaaa\ntype: person\nname: {existing_name_field}\n"
        "access: internal\n---\n\n"
        "# Bill Lennan\n\n## Notes\n\n- 2026-01-01: Founder of 40 Percent Better.\n",
        encoding="utf-8",
    )

    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")

    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

    (sessions / "20260910T090000Z-aa11bb22.md").write_text(
        f"Caught up with {mention_text} today -- he's launching a new coaching program.\n",
        encoding="utf-8",
    )
    return root


def _person_pages(root: Path) -> list[Path]:
    return [p for p in (root / "wiki").glob("*.md") if not p.name.startswith("_")]


class TestExactNameMatchIsHandledButNotByTier1:
    def test_clean_mention_of_a_cleanly_named_existing_person_updates_not_duplicates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run
        from athenaeum.run_summary_log import parse_run_summary_text

        root = _seed_root(
            tmp_path,
            existing_name_field="Bill Lennan",  # no decoration
            mention_text="Bill Lennan",  # exact match
        )

        # No classify/merge call should even be needed -- the tier-0 registry
        # consult should claim this file whole before either LLM tier runs.
        mock_client = MagicMock()
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.INFO, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=10,
        )

        assert exit_code == 0
        assert len(_person_pages(root)) == 1, "a clean exact-name mention must not duplicate"
        mock_client.messages.create.assert_not_called()

        records = parse_run_summary_text(caplog.text)
        entity = records[-1].phases["entity"]
        assert int(entity["matched"]) == 0, (
            "tier1 never got a chance to run -- the tier-0 registry consult "
            "claimed the file first (see this file's module docstring); "
            "restoring tier1 person-matching gets zero credit for this case"
        )


class TestDecoratedNameMismatchStillDuplicates:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "athenaeum#1597 AC1 follow-on, measured not assumed: restoring tier1 "
            "person-matching does not fix a decorated-name/clean-mention "
            "mismatch. Both the tier-0 registry consult and the now-restored "
            "tier1 match require the EXISTING page's full registered "
            "name/alias string to appear as a literal substring of the new "
            "raw text; a `name:` field polluted with decorative characters "
            "(the real shape sampled from the live corpus) and no clean "
            "alias defeats both identically. See the PR body's 'Duplicate "
            "entity pages' section for the live-corpus measurement (0 of "
            "305 stuck entries newly resolve via tier1) and the recommended, "
            "separately-scoped follow-up (key normalization / alias "
            "backfill / fuzzy matching)."
        ),
    )
    def test_clean_mention_of_a_decoratively_named_existing_person_does_not_duplicate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = _seed_root(
            tmp_path,
            existing_name_field="Bill Lennan \U0001f4ad",  # decorative emoji, no alias
            mention_text="Bill Lennan",  # clean mention, the real corpus shape
        )

        # Realistic: since neither tier-0 nor tier1 can match this mention,
        # tier2 classify has no "already known" signal and (like a real LLM
        # facing an apparently-new name) classifies it as NEW; tier3_create
        # then succeeds (the athenaeum#1597 AC1 guard removal already
        # shipped) and mints a second page.
        classify_response = MagicMock()
        classify_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Bill Lennan",
                            "entity_type": "person",
                            "tags": [],
                            "access": "internal",
                            "observations": "Launching a new coaching program.",
                        }
                    ]
                )
            )
        ]
        create_response = MagicMock()
        create_response.content = [
            MagicMock(
                text=(
                    "# Bill Lennan\n\n## Notes\n\n"
                    "- 2026-09-10: Launching a new coaching program.\n"
                )
            )
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [classify_response, create_response]
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=10,
        )

        assert len(_person_pages(root)) == 1, (
            "expected exactly one person page -- if this assertion now "
            "PASSES, the xfail above must be removed and the PR body's "
            "'does not go to approximately zero' finding is stale"
        )
