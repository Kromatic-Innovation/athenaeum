# SPDX-License-Identifier: Apache-2.0
"""Tests for the merge-sidecar PII scrub (issue athenaeum#1276).

Covers the library transform (:mod:`athenaeum.pending_merges_pii`), the
``athenaeum merges scrub-pii`` CLI, and the migration-coupled pass wired into
``athenaeum storage migrate-pii``.

The driving bug: a proposal stores its ``draft_merged_body`` VERBATIM, so
migrating an entity page's PII off-corpus left a plain-text copy of the same
addresses in ``wiki/_pending_merges.md`` — the page read clean, the excluded
record existed, the index was refreshed, and ``storage lint-pii`` still found
the values under ``wiki/``. Conventions follow
``test_storage_migrate_pii.py`` (``EXCLUDED_CONFIG``, in-process
``cli.main([...])`` + ``capsys``) and ``test_pending_merges*.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.cli import main
from athenaeum.pending_merges import parse_pending_merges, write_pending_merge
from athenaeum.pending_merges_pii import scrub_pending_merges
from athenaeum.storage_migrate import INLINE_REDACTION_MARKER

EMAIL = "jane@example.com"
PHONE = "+1-555-0100"

PAGE_BODY = f"""---
uid: "12345"
name: Jane Springer
type: person
emails:
  - {EMAIL}
phones:
  - "{PHONE}"
---
Reach Jane at {EMAIL} or {PHONE}.
"""


def _seed_root(tmp_path: Path) -> Path:
    """A knowledge root with ``pii`` mapped to the excluded surface."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    (root / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
    )
    return root


def _write_page(root: Path, filename: str = "jane.md") -> Path:
    path = root / "wiki" / filename
    path.write_text(PAGE_BODY, encoding="utf-8")
    return path


def _queue_proposal(
    root: Path,
    *,
    draft: str = PAGE_BODY,
    target: str = "jane-springer",
    sources: list[str] | None = None,
) -> Path:
    """Queue one proposal whose draft body embeds *draft* verbatim."""
    merges_path = root / "wiki" / "_pending_merges.md"
    write_pending_merge(
        merges_path,
        merge_target_name=target,
        sources=sources or [str(root / "wiki" / "jane.md")],
        rationale="two pages cluster on the same person",
        draft_merged_body=draft,
        confidence=0.9,
    )
    return merges_path


# ---------------------------------------------------------------------------
# The library transform
# ---------------------------------------------------------------------------


class TestScrubPendingMerges:
    def test_detects_and_redacts_embedded_contact_data(self, tmp_path: Path) -> None:
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)

        result = scrub_pending_merges(merges_path, apply=True)

        assert result.applied is True
        assert result.blocks_scanned == 1
        assert len(result.scrubbed) == 1
        assert set(result.scrubbed[0].values) == {EMAIL, PHONE}
        text = merges_path.read_text(encoding="utf-8")
        assert EMAIL not in text
        assert PHONE not in text
        assert INLINE_REDACTION_MARKER in text

    def test_dry_run_reports_but_writes_nothing(self, tmp_path: Path) -> None:
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        before = merges_path.read_text(encoding="utf-8")

        result = scrub_pending_merges(merges_path)

        assert result.applied is False
        assert result.values_redacted == 2
        assert merges_path.read_text(encoding="utf-8") == before

    def test_is_idempotent(self, tmp_path: Path) -> None:
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)

        scrub_pending_merges(merges_path, apply=True)
        after_first = merges_path.read_text(encoding="utf-8")
        second = scrub_pending_merges(merges_path, apply=True)

        assert second.is_clean
        assert second.applied is False
        assert merges_path.read_text(encoding="utf-8") == after_first

    def test_preserves_proposal_identity_and_untouched_blocks(
        self, tmp_path: Path
    ) -> None:
        """A scrub must not re-id a proposal or perturb a clean neighbour."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        write_pending_merge(
            merges_path,
            merge_target_name="widgets",
            sources=[str(root / "wiki" / "widgets.md")],
            rationale="unrelated, carries no contact data",
            draft_merged_body="# Widgets\n\nNothing personal here.\n",
            confidence=0.7,
        )
        before = parse_pending_merges(merges_path)
        clean_block_before = before[1].raw_block

        scrub_pending_merges(merges_path, apply=True)

        after = parse_pending_merges(merges_path)
        assert [m.id for m in after] == [m.id for m in before]
        assert [m.sources for m in after] == [m.sources for m in before]
        assert [m.confidence for m in after] == [m.confidence for m in before]
        assert [m.resolved for m in after] == [m.resolved for m in before]
        # The neighbour round-trips byte-for-byte.
        assert after[1].raw_block == clean_block_before

    def test_scrubs_resolved_proposals_too(self, tmp_path: Path) -> None:
        """A resolved block's body is still a verbatim copy under ``wiki/``.

        Deliberately unlike ``revalidate_pending_merges``, which archives whole
        proposals and so must leave resolved ones alone: this is a redaction,
        and leaving PII behind because a decision was already taken would
        defeat the migration just as thoroughly.
        """
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        merges_path.write_text(
            merges_path.read_text(encoding="utf-8").replace("- [ ] Approve", "- [x] Approve"),
            encoding="utf-8",
        )

        result = scrub_pending_merges(merges_path, apply=True)

        assert result.values_redacted == 2
        assert EMAIL not in merges_path.read_text(encoding="utf-8")

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        result = scrub_pending_merges(tmp_path / "nope.md", apply=True)

        assert result.is_clean
        assert result.blocks_scanned == 0
        assert result.applied is False

    def test_leaves_service_addresses_alone(self, tmp_path: Path) -> None:
        """``git@github.com`` is a transport identifier, not contact data."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(
            root, draft="Clone with git@github.com:acme/widgets.git\n"
        )

        result = scrub_pending_merges(merges_path, apply=True)

        assert result.is_clean
        assert "git@github.com" in merges_path.read_text(encoding="utf-8")

    def test_leaves_allowlisted_values_alone(self, tmp_path: Path) -> None:
        """An adjudicated value is not PII, does not fail ``lint-pii``, and
        must not be redacted — deleting a true non-personal fact is the
        athenaeum#691 mistake."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        (root / "wiki" / "_pii-allowlist.yml").write_text(
            f'- value: "{EMAIL}"\n  reason: "example-domain placeholder, not a person"\n',
            encoding="utf-8",
        )

        result = scrub_pending_merges(merges_path, apply=True)

        text = merges_path.read_text(encoding="utf-8")
        assert EMAIL in text
        assert PHONE not in text
        assert result.scrubbed[0].values == (PHONE,)

    def test_allowlist_covers_phones_too(self, tmp_path: Path) -> None:
        """The phone axis carries athenaeum#500's false positives (a 13-digit
        record id reads as a phone), so adjudication has to reach it."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        (root / "wiki" / "_pii-allowlist.yml").write_text(
            f'- value: "{PHONE}"\n  reason: "switchboard, not a person"\n',
            encoding="utf-8",
        )

        result = scrub_pending_merges(merges_path, apply=True)

        assert result.scrubbed[0].values == (EMAIL,)
        assert PHONE in merges_path.read_text(encoding="utf-8")

    def test_malformed_allowlist_entry_fails_closed(self, tmp_path, caplog) -> None:
        """An entry with no reason adjudicates nothing (athenaeum#936), so the
        value it would have covered is still redacted — never tolerated by
        omission."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        (root / "wiki" / "_pii-allowlist.yml").write_text(
            f'- value: "{EMAIL}"\n', encoding="utf-8"
        )

        result = scrub_pending_merges(merges_path, apply=True)

        assert set(result.scrubbed[0].values) == {EMAIL, PHONE}
        assert "allowlist entry ignored" in caplog.text

    def test_explicit_values_ignore_the_allowlist(self, tmp_path: Path) -> None:
        """``migrate-pii`` names values it has ALREADY moved off-corpus; that
        is an instruction about state, not a detection to adjudicate."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        (root / "wiki" / "_pii-allowlist.yml").write_text(
            f'- value: "{EMAIL}"\n  reason: "adjudicated elsewhere"\n', encoding="utf-8"
        )

        result = scrub_pending_merges(merges_path, values=[EMAIL], apply=True)

        assert result.scrubbed[0].values == (EMAIL,)
        assert EMAIL not in merges_path.read_text(encoding="utf-8")

    def test_identity_bearing_lines_are_reported_not_rewritten(
        self, tmp_path: Path
    ) -> None:
        """The header and ``**Sources**:`` paths carry the proposal's id.

        A page named after an email (the athenaeum#502 population) puts a
        value on those lines; rewriting one would re-id the proposal and
        repoint the fold target, so it is surfaced as residual instead.
        """
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(
            root,
            target=EMAIL,
            sources=[str(root / "wiki" / f"{EMAIL}.md")],
            draft="No contact data in this body.\n",
        )

        result = scrub_pending_merges(merges_path, apply=True)

        assert result.scrubbed == []
        assert len(result.residual) == 1
        # Both the header name and the source FILENAME are email-shaped here;
        # both are reported, neither is rewritten.
        assert EMAIL in result.residual[0].values
        # Untouched: the block still parses to the same id and sources.
        after = parse_pending_merges(merges_path)
        assert after[0].merge_target_name == EMAIL
        assert after[0].sources == [str(root / "wiki" / f"{EMAIL}.md")]


# ---------------------------------------------------------------------------
# `athenaeum merges scrub-pii` — the zero-LLM purge path (AC3)
# ---------------------------------------------------------------------------


class TestScrubPiiCli:
    def test_dry_run_reports_without_writing(self, tmp_path, capsys) -> None:
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)
        before = merges_path.read_text(encoding="utf-8")

        rc = main(["merges", "scrub-pii", "--path", str(root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Would redact 2 contact value(s)" in out
        # Never echo the values themselves — this command exists to get them
        # out of reach, not to reprint them.
        assert EMAIL not in out
        assert merges_path.read_text(encoding="utf-8") == before

    def test_apply_redacts_without_resolving_the_merge(self, tmp_path, capsys) -> None:
        """The whole point of the purge path: the values go, the decision stays."""
        root = _seed_root(tmp_path)
        merges_path = _queue_proposal(root)

        rc = main(["merges", "scrub-pii", "--path", str(root), "--apply"])

        assert rc == 0
        assert "Redacted 2 contact value(s)" in capsys.readouterr().out
        text = merges_path.read_text(encoding="utf-8")
        assert EMAIL not in text
        assert PHONE not in text
        merges = parse_pending_merges(merges_path)
        assert len(merges) == 1
        assert merges[0].resolved is False
        assert merges[0].decision == ""

    def test_json_output(self, tmp_path, capsys) -> None:
        root = _seed_root(tmp_path)
        _queue_proposal(root)

        rc = main(["merges", "scrub-pii", "--path", str(root), "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is False
        assert payload["blocks_scanned"] == 1
        assert payload["values_redacted"] == 2
        assert payload["scrubbed"][0]["merge_target_name"] == "jane-springer"

    def test_clean_sidecar_reports_zero(self, tmp_path, capsys) -> None:
        root = _seed_root(tmp_path)
        _queue_proposal(root, draft="# Widgets\n\nNothing personal here.\n")

        rc = main(["merges", "scrub-pii", "--path", str(root)])

        assert rc == 0
        assert "0 proposals carry contact data" in capsys.readouterr().out

    def test_missing_sidecar_is_not_an_error(self, tmp_path, capsys) -> None:
        root = _seed_root(tmp_path)

        rc = main(["merges", "scrub-pii", "--path", str(root)])

        assert rc == 0
        assert "0 proposals carry contact data" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `storage migrate-pii` no longer leaves a copy behind (AC1 + AC2)
# ---------------------------------------------------------------------------


class TestMigratePiiScrubsTheSidecar:
    def test_migration_clears_the_embedded_copy(self, tmp_path, capsys) -> None:
        """AC2, verbatim: page with PII + pending proposal embedding that page
        → migrate → the proposal body no longer carries the values."""
        root = _seed_root(tmp_path)
        page = _write_page(root)
        merges_path = _queue_proposal(root)
        assert EMAIL in merges_path.read_text(encoding="utf-8")

        rc = main(
            ["storage", "migrate-pii", "--path", str(root), "--page", str(page), "--apply"]
        )

        assert rc == 0
        # The page itself is clean (the pre-existing guarantee) ...
        assert EMAIL not in page.read_text(encoding="utf-8")
        # ... and so is the sidecar (issue athenaeum#1276).
        sidecar = merges_path.read_text(encoding="utf-8")
        assert EMAIL not in sidecar
        assert PHONE not in sidecar
        assert INLINE_REDACTION_MARKER in sidecar
        assert "pending merge proposal(s)" in capsys.readouterr().out

    def test_dry_run_migration_previews_but_writes_nothing(
        self, tmp_path, capsys
    ) -> None:
        root = _seed_root(tmp_path)
        page = _write_page(root)
        merges_path = _queue_proposal(root)
        before = merges_path.read_text(encoding="utf-8")

        rc = main(["storage", "migrate-pii", "--path", str(root), "--page", str(page)])

        assert rc == 0
        assert "[DRY RUN] would redact" in capsys.readouterr().out
        assert merges_path.read_text(encoding="utf-8") == before

    def test_bulk_migration_clears_the_embedded_copy(self, tmp_path, capsys) -> None:
        root = _seed_root(tmp_path)
        _write_page(root)
        merges_path = _queue_proposal(root)

        rc = main(["storage", "migrate-pii", "--path", str(root), "--all", "--apply"])

        assert rc == 0
        sidecar = merges_path.read_text(encoding="utf-8")
        assert EMAIL not in sidecar
        assert PHONE not in sidecar

    def test_migration_with_no_queue_is_a_no_op(self, tmp_path, capsys) -> None:
        """No sidecar, no proposals — the migration must not grow a new failure."""
        root = _seed_root(tmp_path)
        page = _write_page(root)

        rc = main(
            ["storage", "migrate-pii", "--path", str(root), "--page", str(page), "--apply"]
        )

        assert rc == 0
        assert not (root / "wiki" / "_pending_merges.md").exists()
