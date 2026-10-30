# SPDX-License-Identifier: Apache-2.0
"""Absorbed-uid contact records: the merge hook and the repair (issue athenaeum#2184).

Structure follows the issue's acceptance criteria:

- ``TestMergeHook`` — a fold whose absorbed record carries an address the
  canonical lacks (moved, with provenance); one carrying only addresses the
  canonical already lists (dropped, retired); every address field plus
  phones; per-address facts travel; a minted canonical record; idempotence.
- ``TestMergePathsCallTheHook`` — ``dedupe persons`` and a
  ``fold-into-existing`` approve both fold the absorbed uid's record.
- ``TestRepairAbsorbedOrphans`` — tombstone / ``merged_from`` / merge-ledger
  mappings, a page-less uid with no merge record left untouched, an
  ambiguous canonical left untouched.
- ``TestRepairDuplicates`` — identical, subset, divergent and
  both-uid-prefixed pairs; zero uid collisions logged after the repair.
- ``TestRetirement`` — rename-not-delete, stamped provenance, ledger row,
  reversible, mode preserved.
- ``TestRepairCli`` — counts only, exit codes, run lock.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum.contact_record_fold import FOLD_SOURCE, fold_absorbed_contact_records
from athenaeum.contact_record_repair import (
    apply_contact_record_repair,
    plan_contact_record_repair,
)
from athenaeum.dedupe import DuplicatePair, merge_duplicate_persons
from athenaeum.models import FOLDED_INTO_FIELD, parse_frontmatter

CANON = "c0ffee01"
ABSORBED = "abad1dea"
OTHER = "0ddba11e"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _yaml_list(name: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [f"{name}:"] + [f"  - '{v}'" for v in values]


def _record(
    root: Path,
    filename: str,
    uid: str,
    *,
    emails: list[str] = (),
    alt_emails: list[str] = (),
    former_emails: list[str] = (),
    phones: list[str] = (),
    extra: list[str] = (),
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: {uid}", "pii: true"]
    lines += _yaml_list("emails", list(emails))
    lines += _yaml_list("alt_emails", list(alt_emails))
    lines += _yaml_list("former_emails", list(former_emails))
    lines += _yaml_list("phones", list(phones))
    lines += list(extra)
    lines += ["---", "Synthetic contact record.", ""]
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _page(wiki: Path, filename: str, uid: str, name: str, extra: list[str] = ()) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: {uid}", f"name: {name}", "type: person", *extra, "---", "", "Body.", ""]
    path = wiki / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _meta(path: Path) -> dict:
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta


def _retired(path: Path) -> Path:
    return path.with_name(path.name + pii.RETIRED_RECORD_SUFFIX)


def _ledger(root: Path) -> list[dict]:
    log_path = pii.default_record_retirement_log_path(root)
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# (1) The merge hook
# ---------------------------------------------------------------------------


class TestMergeHook:
    def test_address_the_canonical_lacks_is_moved_with_provenance(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        canon = _record(root, f"{CANON}-casey.md", CANON, emails=["casey@example.org"])
        absorbed = _record(
            root, f"{ABSORBED}-casey-dup.md", ABSORBED, alt_emails=["casey.alt@example.org"]
        )

        summary = fold_absorbed_contact_records(root, [(ABSORBED, CANON)])

        meta = _meta(canon)
        assert meta["emails"] == ["casey@example.org"]
        assert meta["alt_emails"] == ["casey.alt@example.org"]
        assert meta["field_sources"]["alt_emails"] == [
            {"value": "casey.alt@example.org", "source": FOLD_SOURCE}
        ]
        assert summary.records_folded == 1
        assert summary.values_added == {"alt_emails": 1}
        # Retired, never deleted.
        assert not absorbed.exists()
        assert _retired(absorbed).exists()
        # Every address now resolves to exactly one uid.
        index = pii.ExcludedRecordIndex(root)
        assert index.all_by_identifier("casey.alt@example.org") == [canon]
        assert index.all_by_uid(ABSORBED) == []

    def test_only_already_present_addresses_are_dropped_and_retired(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        canon = _record(root, f"{CANON}-casey.md", CANON, emails=["casey@example.org"])
        before = canon.read_bytes()
        absorbed = _record(root, f"{ABSORBED}-dup.md", ABSORBED, emails=["Casey@Example.org"])

        summary = fold_absorbed_contact_records(root, [(ABSORBED, CANON)])

        assert canon.read_bytes() == before
        assert summary.values_added == {}
        assert not absorbed.exists()
        assert _retired(absorbed).exists()
        assert pii.ExcludedRecordIndex(root).all_by_identifier("casey@example.org") == [canon]

    def test_every_address_field_and_phones_move(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        canon = _record(root, f"{CANON}.md", CANON, phones=["+1 555 0100"])
        _record(
            root,
            f"{ABSORBED}.md",
            ABSORBED,
            emails=["e@example.org"],
            alt_emails=["a@example.org"],
            former_emails=["f@example.org"],
            phones=["(555) 0100", "+1 555 0199"],
        )

        summary = fold_absorbed_contact_records(root, [(ABSORBED, CANON)])

        meta = _meta(canon)
        assert meta["emails"] == ["e@example.org"]
        assert meta["alt_emails"] == ["a@example.org"]
        assert meta["former_emails"] == ["f@example.org"]
        # "+1 555 0100" and "(555) 0100" differ in digits, so both stay; the
        # new number is appended.
        assert "+1 555 0199" in meta["phones"]
        assert summary.values_added["emails"] == 1
        assert summary.values_added["former_emails"] == 1

    def test_per_address_facts_travel_and_original_provenance_is_kept(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        canon = _record(root, f"{CANON}.md", CANON, emails=["c@example.org"])
        _record(
            root,
            f"{ABSORBED}.md",
            ABSORBED,
            emails=["old@example.org"],
            extra=[
                "field_sources:",
                "  emails:",
                "  - value: old@example.org",
                "    source: script:synthetic-importer",
                "identifier_validity:",
                "- identifier: old@example.org",
                "  valid_until: '2026-01-02'",
                "  bounce_diagnostic: 550 no such user",
            ],
        )

        fold_absorbed_contact_records(root, [(ABSORBED, CANON)])

        meta = _meta(canon)
        assert meta["field_sources"]["emails"] == [
            {"value": "old@example.org", "source": "script:synthetic-importer"}
        ]
        assert [e["identifier"] for e in meta["identifier_validity"]] == ["old@example.org"]

    def test_canonical_without_a_record_gets_one_minted(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        absorbed = _record(root, f"{ABSORBED}.md", ABSORBED, emails=["x@example.org"])

        summary = fold_absorbed_contact_records(root, [(ABSORBED, CANON)])

        minted = pii.default_uid_record_path(root, CANON)
        assert _meta(minted)["uid"] == CANON
        assert _meta(minted)["emails"] == ["x@example.org"]
        assert summary.canonical_records_minted == 1
        assert not absorbed.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        canon = _record(root, f"{CANON}.md", CANON, emails=["c@example.org"])
        _record(root, f"{ABSORBED}.md", ABSORBED, emails=["a@example.org"])

        fold_absorbed_contact_records(root, [(ABSORBED, CANON)])
        after_first = canon.read_bytes()
        second = fold_absorbed_contact_records(root, [(ABSORBED, CANON)])

        assert second.records_folded == 0
        assert canon.read_bytes() == after_first
        assert len(_ledger(root)) == 1

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        canon = _record(root, f"{CANON}.md", CANON, emails=["c@example.org"])
        absorbed = _record(root, f"{ABSORBED}.md", ABSORBED, emails=["a@example.org"])
        before = canon.read_bytes()

        summary = fold_absorbed_contact_records(root, [(ABSORBED, CANON)], dry_run=True)

        assert summary.values_added == {"emails": 1}
        assert canon.read_bytes() == before
        assert absorbed.exists()
        assert _ledger(root) == []

    def test_missing_surface_is_a_no_op(self, tmp_path: Path) -> None:
        summary = fold_absorbed_contact_records(tmp_path / "nope", [(ABSORBED, CANON)])
        assert summary.records_folded == 0


# ---------------------------------------------------------------------------
# The merge paths call the hook
# ---------------------------------------------------------------------------


class TestMergePathsCallTheHook:
    def test_dedupe_persons_apply_folds_the_absorbed_record(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        canon_page = _page(wiki, f"{CANON}-casey-lee.md", CANON, "Casey Lee")
        absorbed_page = _page(wiki, f"{ABSORBED}-casey-lee.md", ABSORBED, "Casey Lee")
        canon = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["casey@example.org"])
        absorbed = _record(root, f"{ABSORBED}-casey-lee.md", ABSORBED, emails=["cl@example.org"])
        pair = DuplicatePair(
            canonical_uid=CANON,
            canonical_path=str(canon_page),
            absorbed_uid=ABSORBED,
            absorbed_path=str(absorbed_page),
            match_signal="name_exact",
        )

        report = merge_duplicate_persons([pair], apply=True, wiki_root=wiki, contacts_root=root)

        assert report.merged == 1
        assert report.contact_records_folded == 1
        assert report.contact_values_added == 1
        assert _meta(canon)["emails"] == ["casey@example.org", "cl@example.org"]
        assert not absorbed.exists()

    def test_dedupe_without_contacts_root_is_unchanged(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        canon_page = _page(wiki, f"{CANON}-a.md", CANON, "A Person")
        absorbed_page = _page(wiki, f"{ABSORBED}-a.md", ABSORBED, "A Person")
        absorbed = _record(root, f"{ABSORBED}-a.md", ABSORBED, emails=["a@example.org"])
        pair = DuplicatePair(
            canonical_uid=CANON,
            canonical_path=str(canon_page),
            absorbed_uid=ABSORBED,
            absorbed_path=str(absorbed_page),
            match_signal="name_exact",
        )

        merge_duplicate_persons([pair], apply=True, wiki_root=wiki)

        assert absorbed.exists()

    def _approve_fold(
        self, tmp_path: Path, target_name: str = "casey-lee.md", src_name: str = "casey-lee-2.md"
    ) -> dict:
        from athenaeum.pending_merges import (
            parse_pending_merges,
            resolve_merge,
            write_pending_merge,
        )
        from tests.conftest import init_git_repo

        wiki = tmp_path / "wiki"
        target = wiki / target_name
        src = wiki / src_name
        init_git_repo(wiki)
        merges_path = wiki / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="Casey Lee",
            sources=[str(src)],
            rationale="r",
            draft_merged_body=target.read_text(encoding="utf-8"),
            confidence=0.9,
            write_kind="fold-into-existing",
        )
        pm_id = parse_pending_merges(merges_path)[0].id
        return resolve_merge(merges_path, pm_id, "approve", wiki_root=wiki)

    def test_fold_into_existing_approve_folds_the_absorbed_record(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        wiki = tmp_path / "wiki"
        root = pii.contacts_surface_root(tmp_path, {"storage": {"mapping": {"pii": "excluded"}}})
        _page(wiki, "casey-lee.md", CANON, "Casey Lee")
        _page(wiki, "casey-lee-2.md", ABSORBED, "Casey Lee 2")
        canon = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["casey@example.org"])
        absorbed = _record(root, f"{ABSORBED}-casey-lee.md", ABSORBED, emails=["c2@example.org"])

        result = self._approve_fold(tmp_path)

        assert result["ok"] is True, result
        assert result["contact_records_folded"] == 1
        assert _meta(canon)["emails"] == ["casey@example.org", "c2@example.org"]
        assert not absorbed.exists()
        assert _retired(absorbed).exists()

    def test_fold_into_a_uid_prefixed_canonical_page(self, tmp_path: Path) -> None:
        """The real corpus shape: the canonical lives at ``<uid>-<slug>.md``."""
        (tmp_path / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        wiki = tmp_path / "wiki"
        root = pii.contacts_surface_root(tmp_path, {"storage": {"mapping": {"pii": "excluded"}}})
        _page(wiki, f"{CANON}-casey-lee.md", CANON, "Casey Lee")
        _page(wiki, f"{ABSORBED}-casey-lee-2.md", ABSORBED, "Casey Lee 2")
        canon = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["casey@example.org"])
        absorbed = _record(root, f"{ABSORBED}-casey-lee.md", ABSORBED, emails=["c2@example.org"])

        result = self._approve_fold(tmp_path, f"{CANON}-casey-lee.md", f"{ABSORBED}-casey-lee-2.md")

        assert result["ok"] is True, result
        assert "c2@example.org" in _meta(canon)["emails"]
        assert not absorbed.exists()
        # And the repair would now map the tombstone the fold left (bare
        # `folded_into` slug) to the uid-prefixed canonical: nothing left to do.
        plan = plan_contact_record_repair(tmp_path, root)
        assert plan.actionable == 0

    def test_concept_fold_never_scans_the_contacts_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "casey-lee.md").write_text(
            f"---\nuid: {CANON}\nname: Casey Lee\ntype: concept\n---\nB.\n", encoding="utf-8"
        )
        (wiki / "casey-lee-2.md").write_text(
            f"---\nuid: {ABSORBED}\nname: Casey Lee 2\ntype: concept\n---\nB.\n",
            encoding="utf-8",
        )
        calls: list[Path] = []
        monkeypatch.setattr(
            pii.ExcludedRecordIndex,
            "_ensure_loaded",
            lambda self: calls.append(self.contacts_root),
        )

        result = self._approve_fold(tmp_path)

        assert result["ok"] is True, result
        assert calls == []

    def test_fold_approve_without_an_excluded_pii_mapping_touches_nothing(
        self, tmp_path: Path
    ) -> None:
        """Unmapped, the pii surface resolves to the wiki root itself — the
        hook must not treat wiki pages as contact records."""
        wiki = tmp_path / "wiki"
        _page(wiki, "casey-lee.md", CANON, "Casey Lee")
        src = _page(wiki, "casey-lee-2.md", ABSORBED, "Casey Lee 2")

        result = self._approve_fold(tmp_path)

        assert result["ok"] is True, result
        assert "contact_records_folded" not in result or result["contact_records_folded"] == 0
        assert src.exists()
        assert not _retired(src).exists()


# ---------------------------------------------------------------------------
# (2) The repair: absorbed orphans
# ---------------------------------------------------------------------------


class TestRepairAbsorbedOrphans:
    def test_tombstone_maps_the_absorbed_uid(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, "casey-lee.md", CANON, "Casey Lee")
        _page(
            wiki,
            "casey-lee-2.md",
            ABSORBED,
            "Casey Lee 2",
            extra=["status: folded", "folded_into: casey-lee", "embedded: false"],
        )
        canon = _record(root, f"{CANON}.md", CANON, emails=["c@example.org"])
        absorbed = _record(root, f"{ABSORBED}.md", ABSORBED, emails=["c2@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)

        assert plan.counts()["absorbed_uids_to_fold"] == 1
        assert plan.counts()["absorbed_values_to_add"] == 1
        result = apply_contact_record_repair(plan, root)
        assert result.fold.records_folded == 1
        assert "c2@example.org" in _meta(canon)["emails"]
        assert not absorbed.exists()

    def test_merged_from_maps_the_absorbed_uid(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, f"{CANON}-x.md", CANON, "X", extra=["merged_from:", f"- {ABSORBED}"])
        _record(root, f"{CANON}.md", CANON, emails=["x@example.org"])
        _record(root, f"{ABSORBED}.md", ABSORBED, emails=["x2@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)

        assert [(u, c) for u, c, _ in plan.orphan_folds] == [(ABSORBED, CANON)]

    def test_merge_ledger_maps_an_absorbed_uid_whose_page_is_gone(self, tmp_path: Path) -> None:
        from athenaeum.provenance import record_merge_provenance

        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, "canonical-person.md", CANON, "Canonical Person")
        record_merge_provenance(
            wiki,
            merge_id="m1",
            write_kind="fold-into-existing",
            canonical_slug="canonical-person",
            source_paths=[str(wiki / f"{ABSORBED}-gone.md")],
        )
        _record(root, f"{ABSORBED}.md", ABSORBED, emails=["g@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)

        assert [(u, c) for u, c, _ in plan.orphan_folds] == [(ABSORBED, CANON)]

    def test_pageless_uid_without_merge_record_is_never_touched(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, "someone.md", CANON, "Someone")
        lonely = _record(root, f"{OTHER}.md", OTHER, emails=["lonely@example.org"])
        before = lonely.read_bytes()

        plan = plan_contact_record_repair(tmp_path, root)
        apply_contact_record_repair(plan, root)

        assert plan.counts()["pageless_no_merge_record"] == 1
        assert plan.actionable == 0
        assert lonely.read_bytes() == before
        assert _ledger(root) == []

    def test_ambiguous_canonical_is_reported_not_folded(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, f"{CANON}.md", CANON, "One", extra=["merged_from:", f"- {ABSORBED}"])
        _page(wiki, f"{OTHER}.md", OTHER, "Two", extra=["merged_from:", f"- {ABSORBED}"])
        absorbed = _record(root, f"{ABSORBED}.md", ABSORBED, emails=["a@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)
        apply_contact_record_repair(plan, root)

        assert plan.counts()["absorbed_ambiguous_canonical"] == 1
        assert absorbed.exists()

    def test_bare_fold_slug_maps_to_a_uid_prefixed_canonical(self, tmp_path: Path) -> None:
        """A fold records the BARE target slug; the live page is ``<uid>-<slug>.md``."""
        from athenaeum.provenance import record_merge_provenance

        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, f"{CANON}-casey-lee.md", CANON, "Casey Lee")
        _page(
            wiki,
            f"{ABSORBED}-casey-l.md",
            ABSORBED,
            "Casey L",
            extra=["status: folded", "folded_into: casey-lee", "embedded: false"],
        )
        record_merge_provenance(
            wiki,
            merge_id="m2",
            write_kind="fold-into-existing",
            canonical_slug="casey-lee",
            source_paths=[str(wiki / f"{OTHER}-gone.md")],
        )
        _record(root, f"{ABSORBED}.md", ABSORBED, emails=["a@example.org"])
        _record(root, f"{OTHER}.md", OTHER, emails=["o@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)

        assert sorted((u, c) for u, c, _ in plan.orphan_folds) == sorted(
            [(ABSORBED, CANON), (OTHER, CANON)]
        )

    def test_merge_record_whose_canonical_cannot_be_resolved_is_its_own_count(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(
            wiki,
            f"{ABSORBED}-casey-l.md",
            ABSORBED,
            "Casey L",
            extra=["status: folded", "folded_into: nobody-owns-this", "embedded: false"],
        )
        absorbed = _record(root, f"{ABSORBED}.md", ABSORBED, emails=["a@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)
        apply_contact_record_repair(plan, root)

        assert plan.counts()["absorbed_canonical_unresolved"] == 1
        assert plan.counts()["pageless_no_merge_record"] == 0
        assert absorbed.exists()

    def test_chain_follows_to_the_live_end(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, "final.md", CANON, "Final")
        _page(
            wiki,
            "middle.md",
            OTHER,
            "Middle",
            extra=["status: folded", "folded_into: final", "embedded: false"],
        )
        _page(
            wiki,
            "first.md",
            ABSORBED,
            "First",
            extra=["status: folded", "folded_into: middle", "embedded: false"],
        )
        _record(root, f"{ABSORBED}.md", ABSORBED, emails=["a@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)

        assert [(u, c) for u, c, _ in plan.orphan_folds] == [(ABSORBED, CANON)]


# ---------------------------------------------------------------------------
# (2) The repair: duplicate records for one live uid
# ---------------------------------------------------------------------------


class TestRepairDuplicates:
    def _live(self, tmp_path: Path) -> Path:
        _page(tmp_path / "wiki", f"{CANON}-casey-lee.md", CANON, "Casey Lee")
        return tmp_path / "excluded"

    def test_identical_pair_retires_the_slug_copy(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = self._live(tmp_path)
        keep = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        copy = _record(root, "casey-lee.md", CANON, emails=["c@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)
        assert plan.counts()["duplicate_copies_to_retire"] == 1
        result = apply_contact_record_repair(plan, root)

        assert result.duplicates_retired == 1
        assert keep.exists()
        assert not copy.exists()
        assert _retired(copy).exists()
        row = _ledger(root)[0]
        assert row["reason"] == "duplicate-record"
        assert row["record"] == "casey-lee.md"
        # The AC: after the run, the index logs zero uid collisions.
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=pii.log.name):
            index = pii.ExcludedRecordIndex(root)
            index.uid_map()
        assert index.all_by_uid(CANON) == [keep]
        assert not [r for r in caplog.records if r.getMessage().startswith("uid ")]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_subset_pair_retires_the_slug_copy(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["c@example.org", "c2@example.org"],
            phones=["555 0100"],
        )
        copy = _record(root, "casey-lee.md", CANON, emails=["C@example.org"], phones=["5550100"])

        plan = plan_contact_record_repair(tmp_path, root)
        apply_contact_record_repair(plan, root)

        assert not copy.exists()

    def test_divergent_value_on_the_copy_is_folded_then_retired(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        keep = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        copy = _record(
            root, "casey-lee.md", CANON, emails=["c@example.org"], former_emails=["old@example.org"]
        )

        plan = plan_contact_record_repair(tmp_path, root)
        assert plan.counts()["duplicate_divergent_to_fold"] == 1
        assert plan.counts()["duplicate_divergent_values_to_add"] == 1
        assert plan.counts()["duplicate_divergent_reported"] == 0
        assert plan.counts()["duplicate_copies_to_retire"] == 0
        result = apply_contact_record_repair(plan, root)

        assert result.duplicates_folded.copies_folded == 1
        assert result.duplicates_folded.values_added == {"former_emails": 1}
        assert not result.errors
        meta = _meta(keep)
        assert meta["emails"] == ["c@example.org"]
        assert meta["former_emails"] == ["old@example.org"]
        assert meta["field_sources"]["former_emails"] == [
            {"value": "old@example.org", "source": FOLD_SOURCE}
        ]
        assert not copy.exists()
        assert _retired(copy).exists()
        row = _ledger(root)[0]
        assert row["reason"] == "duplicate-fold"
        assert pii.ExcludedRecordIndex(root).all_by_uid(CANON) == [keep]

    def test_values_split_both_ways_keep_everything(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        keep = _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["a@example.org", "b@example.org"],
            phones=["555 0100"],
        )
        _record(root, "casey-lee.md", CANON, emails=["a@example.org", "c@example.org"])

        apply_contact_record_repair(plan_contact_record_repair(tmp_path, root), root)

        meta = _meta(keep)
        assert meta["emails"] == ["a@example.org", "b@example.org", "c@example.org"]
        assert meta["phones"] == ["555 0100"]

    def test_a_per_address_fact_only_on_the_copy_is_carried(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        keep = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        copy = _record(
            root,
            "casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=[
                "identifier_validity:",
                "- identifier: c@example.org",
                "  valid_until: '2026-01-02'",
            ],
        )

        result = apply_contact_record_repair(plan_contact_record_repair(tmp_path, root), root)

        assert result.duplicates_folded.facts_carried == 1
        assert not copy.exists()
        assert pii.is_bounced_identifier(_meta(keep), "c@example.org")

    def test_classification_only_on_the_copy_is_carried(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        keep = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        _record(
            root,
            "casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=[
                "contact_classification:",
                "- identifier: c@example.org",
                "  usage_class: observed",
                "  source: script:example",
                "  observed_at: '2026-03-01'",
            ],
        )

        apply_contact_record_repair(plan_contact_record_repair(tmp_path, root), root)

        entries = pii.contact_classification_entries(_meta(keep))
        assert [(e["identifier"], e["usage_class"]) for e in entries] == [
            ("c@example.org", "observed")
        ]

    @pytest.mark.parametrize(
        ("keep_cls", "keep_at", "copy_cls", "copy_at", "expected"),
        [
            ("provider", "2026-05-01", "observed", "2026-01-01", ("observed", "2026-01-01")),
            ("observed", "2026-01-01", "provider", "2026-05-01", ("observed", "2026-01-01")),
            ("observed", "2026-01-01", "observed", "2026-05-01", ("observed", "2026-05-01")),
            ("observed", "2026-05-01", "observed", "2026-01-01", ("observed", "2026-05-01")),
        ],
        ids=["copy-stronger", "keep-stronger", "equal-copy-newer", "equal-keep-newer"],
    )
    def test_same_address_classification_takes_the_stronger_then_newer(
        self,
        tmp_path: Path,
        keep_cls: str,
        keep_at: str,
        copy_cls: str,
        copy_at: str,
        expected: tuple[str, str],
    ) -> None:
        def cls(usage: str, at: str) -> list[str]:
            return [
                "contact_classification:",
                "- identifier: c@example.org",
                f"  usage_class: {usage}",
                "  source: script:example",
                f"  observed_at: '{at}'",
            ]

        root = self._live(tmp_path)
        keep = _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=cls(keep_cls, keep_at),
        )
        copy = _record(
            root, "casey-lee.md", CANON, emails=["C@example.org"], extra=cls(copy_cls, copy_at)
        )

        result = apply_contact_record_repair(plan_contact_record_repair(tmp_path, root), root)

        assert not copy.exists()
        entries = pii.contact_classification_entries(_meta(keep))
        assert len(entries) == 1
        assert entries[0]["identifier"] == "c@example.org"
        assert (entries[0]["usage_class"], str(entries[0]["observed_at"])) == expected
        winner_is_copy = expected == (copy_cls, copy_at) and expected != (keep_cls, keep_at)
        assert result.duplicates_folded.classifications_reconciled == int(winner_is_copy)

    def test_differing_names_need_review_and_are_never_touched(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        keep = _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=["name: Casey Lee"],
        )
        copy = _record(
            root,
            "casey-lee.md",
            CANON,
            emails=["d@example.org"],
            extra=["name: Dana Moss"],
        )
        before = (keep.read_bytes(), copy.read_bytes())

        plan = plan_contact_record_repair(tmp_path, root)
        result = apply_contact_record_repair(plan, root)

        assert plan.counts()["duplicate_divergent_reported"] == 1
        assert plan.counts()["duplicate_divergent_name_differs"] == 1
        assert plan.counts()["duplicate_divergent_to_fold"] == 0
        assert result.duplicates_folded.copies_folded == 0
        assert (keep.read_bytes(), copy.read_bytes()) == before

    def test_name_formatting_alone_is_not_a_difference(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=["name: Casey Lee"],
        )
        copy = _record(
            root,
            "casey-lee.md",
            CANON,
            emails=["c2@example.org"],
            extra=["name: 'casey  lee.'"],
        )

        apply_contact_record_repair(plan_contact_record_repair(tmp_path, root), root)

        assert not copy.exists()

    def test_differing_validity_for_one_address_needs_review(self, tmp_path: Path) -> None:
        def closed(on: str) -> list[str]:
            return ["identifier_validity:", "- identifier: c@example.org", f"  valid_until: '{on}'"]

        root = self._live(tmp_path)
        keep = _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=closed("2026-01-02"),
        )
        copy = _record(
            root, "casey-lee.md", CANON, emails=["c@example.org"], extra=closed("2026-04-09")
        )
        before = (keep.read_bytes(), copy.read_bytes())

        plan = plan_contact_record_repair(tmp_path, root)
        apply_contact_record_repair(plan, root)

        assert plan.counts()["duplicate_divergent_validity_differs"] == 1
        assert (keep.read_bytes(), copy.read_bytes()) == before

    def test_divergent_fold_refuses_a_review_pair_even_if_called_directly(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.contact_record_fold import fold_duplicate_record

        root = self._live(tmp_path)
        keep = _record(
            root,
            f"{CANON}-casey-lee.md",
            CANON,
            emails=["c@example.org"],
            extra=["name: Casey Lee"],
        )
        copy = _record(
            root, "casey-lee.md", CANON, emails=["d@example.org"], extra=["name: Dana Moss"]
        )

        summary = fold_duplicate_record(root, CANON, copy, keep)

        assert summary.errors == ["duplicate needs review (name-differs)"]
        assert copy.exists()

    def test_divergent_fold_dry_run_writes_nothing_and_rerun_is_clean(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        keep = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        copy = _record(root, "casey-lee.md", CANON, emails=["d@example.org"])
        before = (keep.read_bytes(), copy.read_bytes())

        plan = plan_contact_record_repair(tmp_path, root)
        assert (keep.read_bytes(), copy.read_bytes()) == before
        assert plan.actionable == 1

        apply_contact_record_repair(plan, root)
        again = plan_contact_record_repair(tmp_path, root)

        assert again.actionable == 0
        assert again.counts()["duplicate_divergent_to_fold"] == 0

    def test_both_uid_prefixed_is_reported_never_retired(self, tmp_path: Path) -> None:
        root = self._live(tmp_path)
        a = _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        b = _record(root, f"{CANON}-casey-lee-2.md", CANON, emails=["c@example.org"])

        plan = plan_contact_record_repair(tmp_path, root)
        apply_contact_record_repair(plan, root)

        assert plan.counts()["duplicate_both_uid_prefixed"] == 1
        assert a.exists() and b.exists()


# ---------------------------------------------------------------------------
# Retirement itself
# ---------------------------------------------------------------------------


class TestRetirement:
    def test_rename_stamp_ledger_and_reversible(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        record = _record(root, "sub/casey.md", ABSORBED, emails=["c@example.org"])
        os.chmod(record, 0o600)
        original = _meta(record)

        retirement = pii.retire_contact_record(root, record, folded_into=CANON, reason="merge-fold")

        assert not record.exists()
        retired = retirement.retired_as
        assert retired == _retired(record)
        assert stat.S_IMODE(retired.stat().st_mode) == 0o600
        meta = _meta(retired)
        assert meta[FOLDED_INTO_FIELD] == CANON
        assert meta["emails"] == ["c@example.org"]
        row = _ledger(root)[0]
        assert row["record"] == "sub/casey.md"
        assert row["retired_as"] == "sub/casey.md.retired"
        assert row["uid"] == ABSORBED
        assert "c@example.org" not in json.dumps(row)
        # Reversal: rename back, drop the three stamped keys.
        retired.rename(record)
        restored = {
            k: v
            for k, v in _meta(record).items()
            if k not in (FOLDED_INTO_FIELD, pii.RETIRED_AT_FIELD, pii.RETIRED_REASON_FIELD)
        }
        assert restored == original

    def test_retired_records_are_outside_every_read(self, tmp_path: Path) -> None:
        root = tmp_path / "excluded"
        record = _record(root, "casey.md", ABSORBED, emails=["c@example.org"])

        pii.retire_contact_record(root, record, folded_into=CANON, reason="merge-fold")

        assert pii.iter_contact_records(root) == []
        assert pii.resolve_contact_record_for_uid(root, ABSORBED) is None
        assert pii.resolve_contact_record(root, "c@example.org") is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestRepairCli:
    def _store(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        wiki = tmp_path / "wiki"
        root = tmp_path / "excluded"
        _page(wiki, f"{CANON}-casey-lee.md", CANON, "Casey Lee")
        _record(root, f"{CANON}-casey-lee.md", CANON, emails=["c@example.org"])
        copy = _record(root, "casey-lee.md", CANON, emails=["c@example.org"])
        lonely = _record(root, f"{OTHER}.md", OTHER, emails=["l@example.org"])
        return root, copy, lonely

    def _run(self, *argv: str) -> int:
        from athenaeum.cli import main

        try:
            return main(list(argv))
        except SystemExit as exc:  # argparse
            return int(exc.code or 0)

    def test_dry_run_prints_counts_only_and_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, copy, _ = self._store(tmp_path)

        code = self._run(
            "repair",
            "--contact-records",
            "--knowledge-root",
            str(tmp_path),
            "--contacts-root",
            str(root),
        )

        out = capsys.readouterr().out
        assert code == 2
        assert "duplicate_copies_to_retire: 1" in out
        assert "pageless_no_merge_record: 1" in out
        assert "casey" not in out and "example.org" not in out and CANON not in out
        assert copy.exists()

    def test_apply_acts_and_a_rerun_is_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, copy, lonely = self._store(tmp_path)
        base = [
            "repair",
            "--contact-records",
            "--knowledge-root",
            str(tmp_path),
            "--contacts-root",
            str(root),
        ]

        assert self._run(*base, "--apply") == 0
        assert not copy.exists()
        assert lonely.exists()
        capsys.readouterr()
        # Only report-only populations remain, so the dry-run is clean.
        assert self._run(*base) == 0

    def test_apply_respects_the_run_lock(self, tmp_path: Path) -> None:
        from athenaeum._cli_shared import EXIT_LOCK_HELD
        from athenaeum.runlock import RunLock

        root, copy, _ = self._store(tmp_path)
        held = RunLock(tmp_path, wait=0)
        held.acquire()
        try:
            code = self._run(
                "repair",
                "--contact-records",
                "--knowledge-root",
                str(tmp_path),
                "--contacts-root",
                str(root),
                "--apply",
            )
        finally:
            held.release()

        assert code == EXIT_LOCK_HELD
        assert copy.exists()
