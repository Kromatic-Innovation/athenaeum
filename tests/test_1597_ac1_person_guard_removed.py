# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1597 AC1 — eval-first test for the guard removal.

Operator ruling on athenaeum#1600 (2026-09-10): "LLMs (the librarian) should
be rewriting everything. There should be no prohibition there." Per that
ruling, ``_refuse_person_rewrite`` / ``PersonNeverLLMRewriteError`` are
removed from ``athenaeum.tiers`` — ``tier3_create``, ``tier3_merge``,
``tier3_merge_full``, and ``tier3_write`` stop refusing a ``type: person``
target.

Per the standing eval-first ruling, this test is written to FAIL against
unmodified ``develop`` and PASS after the fix. See the PR body for the
recorded RED (pre-fix) and GREEN (post-fix) runs.

Scenario, chosen deliberately (see athenaeum#1597's "UNBLOCKED" comment and
athenaeum#1600's operator ruling comment, both of which flag this as the part
that must be MEASURED, not assumed): an ordinary free-text raw file mentions
a person who does NOT already have a wiki page. ``DEMOTED_NAME_MATCH_TYPES``
withholds ``type: person`` from ``EntityIndex.items()`` (unaffected, out of
scope for this issue), so Tier 1 cannot match this name either way. Tier 2
then classifies it as a NEW ``type: person`` entity (no ``existing_uid``),
and Tier 3's create path is reached — this is the one path the guard removal
alone unblocks, as distinct from an ALREADY-KNOWN person (which the tier-0
``resolve_person_mention`` / ``attribute_person_observation`` step claims
whole and never reaches Tier 3 at all — see ``tests/test_person_registry.py::
TestProductionRoundTrip``, unaffected by this change and still passing).

Before the fix: ``tier3_create`` raises ``PersonNeverLLMRewriteError`` before
any provider call; the librarian's generic per-file exception handler in
``librarian.py`` catches it, ledgers ``last_error: PersonNeverLLMRewriteError``
to the stuck-file JSON, and the raw file is neither compiled nor consumed.

After the fix: the same raw file compiles normally -- a new ``type: person``
page is created, the raw file is consumed, and the stuck ledger carries no
``PersonNeverLLMRewriteError`` entry.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURE_PERSON_NAME = "Priya Sharma"


def _seed_root(tmp_path: Path) -> Path:
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

    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")

    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

    # Dropped post-commit, so it is an uncommitted change when run() takes its
    # pre-processing snapshot. An ordinary, unstructured free-text mention of
    # a person with NO existing wiki page -- the shape that reaches
    # tier3_create rather than the tier-0 registry-consult short-circuit.
    (sessions / "20260910T090000Z-cc00ee11.md").write_text(
        f"Met {FIXTURE_PERSON_NAME} at the conference today -- she's a "
        "product designer working on onboarding flows.\n",
        encoding="utf-8",
    )
    return root


def _mock_client() -> MagicMock:
    """A client whose two sequential ``messages.create`` calls answer Tier 2
    classify (a new ``type: person`` entity) and Tier 3 create (the page
    body), in that order -- the exact two-call shape ``process_one`` makes
    for a brand-new person mention (T1 can't match; dry classify; create)."""
    classify_payload = json.dumps(
        [
            {
                "name": FIXTURE_PERSON_NAME,
                "entity_type": "person",
                "tags": [],
                "access": "internal",
                "observations": "Product designer, working on onboarding flows.",
            }
        ]
    )
    classify_response = MagicMock()
    classify_response.content = [MagicMock(text=classify_payload)]

    create_response = MagicMock()
    create_response.content = [
        MagicMock(
            text=(
                f"# {FIXTURE_PERSON_NAME}\n\n"
                "## Notes\n\n"
                "- 2026-09-10: Product designer, working on onboarding flows.\n"
            )
        )
    ]

    client = MagicMock()
    client.messages.create.side_effect = [classify_response, create_response]
    return client


class TestAC1PersonGuardRemovalDrainsTheEntityPhase:
    def test_unknown_person_mention_reaches_tier3_create_and_compiles(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import STUCK_MANIFEST_NAME, run
        from athenaeum.run_summary_log import parse_run_summary_text

        root = _seed_root(tmp_path)
        mock_client = _mock_client()
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.INFO, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=10,
        )

        # A page for the new person was created and the raw file consumed --
        # non-zero "files"/"window" for this raw file, not "held_stuck".
        created_pages = [p for p in (root / "wiki").glob("*.md") if not p.name.startswith("_")]
        assert exit_code == 0
        assert len(created_pages) == 1, (
            f"expected exactly one new person page, found {created_pages} -- "
            "before the fix, tier3_create raises PersonNeverLLMRewriteError "
            "and no page is ever written"
        )
        assert FIXTURE_PERSON_NAME in created_pages[0].read_text(encoding="utf-8")
        assert not (
            root / "raw" / "sessions" / "20260910T090000Z-cc00ee11.md"
        ).exists(), "the raw file must be consumed (retire-on-success), not left stuck"

        records = parse_run_summary_text(caplog.text)
        entity = records[-1].phases["entity"]
        assert int(entity["files"]) > 0, (
            f"expected a non-zero entity-phase files count, got {entity!r} -- "
            "before the fix this raw file fails and contributes 0"
        )
        assert int(entity["window"]) > 0

        # No PersonNeverLLMRewriteError anywhere -- not in the log, and not
        # ledgered as a stuck failure.
        assert "PersonNeverLLMRewriteError" not in caplog.text
        stuck_ledger = root / "wiki" / STUCK_MANIFEST_NAME
        if stuck_ledger.exists():
            assert "PersonNeverLLMRewriteError" not in stuck_ledger.read_text(encoding="utf-8")


class TestStuckLedgerDropsRetiredPersonRefusals:
    """athenaeum#1597 AC1 item 3: the 305 pre-existing
    ``PersonNeverLLMRewriteError`` entries in a live stuck ledger must be
    cleared so those files are re-attempted, rather than left escalated
    forever against a refusal that no longer exists in the code.

    Mechanism chosen (see the PR body for the alternatives considered): a
    ledger-LOAD-time drop in :func:`athenaeum.stuck_ledger.load_stuck_ledger`
    of any entry whose ``last_error`` names a retired class, rather than a
    one-off migration script run once against the live
    ``~/knowledge/wiki/_stuck_files.json``. This is exercised here directly
    against the leaf function (offline, no real ledger touched) rather than
    against the live host file, per this lane's build-environment scope.
    """

    def test_a_retired_last_error_entry_is_dropped_on_load(self, tmp_path: Path) -> None:
        from athenaeum.stuck_ledger import STUCK_MANIFEST_NAME, load_stuck_ledger

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / STUCK_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "updated": "2026-09-10T00:00:00Z",
                    "files": {
                        "sessions/retired-refusal.md": {
                            "failures": 3,
                            "hash": "deadbeef",
                            "escalated": True,
                            "last_error": "PersonNeverLLMRewriteError",
                            "last_failed": "2026-09-10T00:00:00Z",
                            "first_failed": "2026-09-01T00:00:00Z",
                        },
                        "sessions/still-broken.md": {
                            "failures": 3,
                            "hash": "cafef00d",
                            "escalated": True,
                            "last_error": "BadRequestError",
                            "last_failed": "2026-09-10T00:00:00Z",
                            "first_failed": "2026-09-01T00:00:00Z",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        ledger = load_stuck_ledger(wiki)

        assert "sessions/retired-refusal.md" not in ledger, (
            "a PersonNeverLLMRewriteError entry must be dropped at load time "
            "so the file is re-attempted, not left escalated against a "
            "refusal that no longer exists (athenaeum#1597 AC1)"
        )
        assert "sessions/still-broken.md" in ledger, (
            "an entry escalated against a real, still-existing error class "
            "must NOT be dropped -- only the retired-class entries are"
        )
