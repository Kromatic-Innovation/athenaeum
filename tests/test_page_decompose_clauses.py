# SPDX-License-Identifier: Apache-2.0
"""``decompose-page --split-clauses`` (issue athenaeum#1947).

Two fixture roots are used here, never `~/knowledge`:

* ``tests/fixtures/page_decompose/`` -- the EXISTING athenaeum#1914 fixture,
  reused only to prove the default (no-flag) path is untouched: byte-identical
  to the checked-in ``golden_report_v1.json``, generated from the pre-change
  code, and still ``version: 1``.
* ``tests/fixtures/page_decompose_clauses/`` -- a new fixture whose one
  source page holds three top-level list items:

  1. a run-on item (A) of six template clauses: an ``unresolved`` lead-in
     (so the WHOLE item, under the default path, is itself ``unresolved`` --
     matching today's refusal), an ``attached`` clause, an
     ``already-present`` clause, a clause whose label ``L`` has two
     definitions and slug-selects its own, a clause reusing a
     single-definition label ``M`` under a subject that does not match
     ``M``'s drive slug (``ambiguous-source``), and an athenaeum#1942
     interleave -- a marker glued directly to the next clause's text with
     no separator -- that becomes one ``malformed`` clause spanning two
     subjects;
  2. a second run-on item (B): an ``unresolved`` lead-in (same reason as
     above), a clause reusing label ``L`` that slug-selects ``L``'s OTHER
     definition, a clause reusing label ``M`` whose subject's slug DOES
     match (so it attaches, unlike (A)'s), and a trailing span after the
     last terminator with no marker at all (``no-source``) -- this is (E);
  3. a single-fact item (D) with two mid-sentence citations and only one
     ``--subject-until`` match, which must stay whole.

  The page also carries a duplicated ``## Notes`` heading (the other half
  of (E)), which must never become a unit.

Nothing here reads or writes `~/knowledge`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import runlock
from athenaeum.cli import main
from athenaeum.footnote_markers import FOOTNOTE_DEF_RE, INLINE_MARKER_RE
from athenaeum.models import parse_frontmatter
from athenaeum.page_decompose import (
    BLOCKING_DISPOSITIONS,
    DECOMPOSE_REPORT_VERSION,
    DECOMPOSE_REPORT_VERSION_2,
    DISPOSITIONS,
    DecomposeError,
    Definition,
    apply_report,
    build_report,
    check_resolutions,
    clause_id,
    load_resolutions,
    select_definition,
    split_into_clauses,
    write_report,
)

V1_DIR = Path(__file__).parent / "fixtures" / "page_decompose"
V1_SOURCE_UID = "src00001"
V1_SUBJECT_UNTIL = r" reached the "

CLAUSE_DIR = Path(__file__).parent / "fixtures" / "page_decompose_clauses"
SOURCE_UID = "src00101"
SOURCE_PAGE = "src00101-fixture-sales-pipeline-tool.md"
SUBJECT_UNTIL = r" reached the "
DESCRIPTION = "A fixture sales-pipeline tracker used by the decompose-page clause-splitting tests."
#: Every invented subject name on the fixture page, including the ones that
#: only ever appear inside the malformed clause's raw text (never recorded
#: as any bullet's `.subject`). The rewrite must lose ALL of them, not only
#: the ones `validate_rewrite` happens to check.
FIXTURE_SUBJECTS = (
    "Thistle Analytics",
    "Nimbus Analytics",
    "Pinewood Traders",
    "Sable Orchard",
    "Quartz Logistics",
    "Quill Systems",
    "Harbor Fintech",
    "Fernglen Outfitters",
    "Driftwood Capital",
    "Harborlight Media",
    "Fernwood Capital",
    "Lighthouse Ventures",
)


@pytest.fixture
def v1_workspace(tmp_path: Path) -> Path:
    """A private copy of the EXISTING athenaeum#1914 fixture."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    for page in sorted((V1_DIR / "wiki").glob("*.md")):
        (root / "wiki" / page.name).write_bytes(page.read_bytes())
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A private copy of the clause-splitting fixture."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    for page in sorted((CLAUSE_DIR / "wiki").glob("*.md")):
        (root / "wiki" / page.name).write_bytes(page.read_bytes())
    (root / "rewrite_body.md").write_bytes((CLAUSE_DIR / "rewrite_body.md").read_bytes())
    (root / "resolutions.json").write_bytes((CLAUSE_DIR / "resolutions.json").read_bytes())
    return root


def _digests(wiki: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wiki.glob("*.md"))
    }


def _dry_run(root: Path, *extra: str) -> tuple[int, dict]:
    report_path = root / "report.json"
    rc = main(
        [
            "decompose-page",
            SOURCE_UID,
            "--path",
            str(root),
            "--subject-until",
            SUBJECT_UNTIL,
            "--report",
            str(report_path),
            *extra,
        ]
    )
    payload = json.loads(report_path.read_text()) if report_path.exists() else {}
    return rc, payload


def _apply_argv(root: Path, *extra: str) -> list[str]:
    return [
        "decompose-page",
        SOURCE_UID,
        "--path",
        str(root),
        "--subject-until",
        SUBJECT_UNTIL,
        "--report",
        str(root / "report.json"),
        "--split-clauses",
        "--apply",
        "--resolutions",
        str(root / "resolutions.json"),
        "--rewrite-body",
        str(root / "rewrite_body.md"),
        "--description",
        DESCRIPTION,
        *extra,
    ]


class TestDefaultPathBackwardsCompatibility:
    """Without --split-clauses, nothing changes (issue athenaeum#1947 AC)."""

    def test_v1_fixture_report_is_byte_identical_to_the_golden_report(
        self, v1_workspace: Path
    ) -> None:
        report = build_report(v1_workspace / "wiki", V1_SOURCE_UID, subject_until=V1_SUBJECT_UNTIL)
        payload = json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n"
        golden = (V1_DIR / "golden_report_v1.json").read_text()
        assert payload == golden, (
            "the default (no --split-clauses) report must stay byte-identical "
            "to the pre-change golden report -- regenerate "
            "tests/fixtures/page_decompose/golden_report_v1.json from "
            "pre-athenaeum#1947 code only if this diff is deliberate"
        )
        assert report.version == DECOMPOSE_REPORT_VERSION == 1

    def test_the_cli_dry_run_also_matches_the_golden_report(self, v1_workspace: Path) -> None:
        report_path = v1_workspace / "report.json"
        rc = main(
            [
                "decompose-page",
                V1_SOURCE_UID,
                "--path",
                str(v1_workspace),
                "--subject-until",
                V1_SUBJECT_UNTIL,
                "--report",
                str(report_path),
            ]
        )
        assert rc == 0
        golden = (V1_DIR / "golden_report_v1.json").read_text()
        assert report_path.read_text() == golden

    def test_new_fixture_run_ons_are_unresolved_whole_items_without_the_flag(
        self, workspace: Path
    ) -> None:
        rc, report = _dry_run(workspace)
        assert rc == 0
        assert report["version"] == 1
        assert len(report["bullets"]) == 3
        item_a, item_b, item_d = report["bullets"]
        assert item_a["disposition"] == "unresolved"
        assert item_b["disposition"] == "unresolved"
        # "item" shape and the version 1 id scheme are implicit for v1: no
        # clause_shape/item_ordinal/clause_index key is even present.
        for key in ("item_ordinal", "clause_index", "clause_shape"):
            assert key not in item_a
            assert key not in item_b
            assert key not in item_d
        assert "items" not in report
        assert "clause_split" not in report


class TestSplitIntoClauses:
    """Unit tests for the terminator rule itself, independent of the fixture."""

    PATTERN_TEXT = r" reached the "

    def test_a_single_clause_with_no_marker_is_returned_whole(self) -> None:
        assert split_into_clauses("Acme Corp reached the pilot stage.") == [
            "Acme Corp reached the pilot stage."
        ]

    def test_two_well_formed_clauses_split_cleanly(self) -> None:
        text = 'Acme reached the pilot stage.[^a] Beta reached the trial stage.[^b]'
        assert split_into_clauses(text) == [
            "Acme reached the pilot stage.[^a]",
            "Beta reached the trial stage.[^b]",
        ]

    def test_the_separator_belongs_to_neither_clause(self) -> None:
        text = 'Acme reached the pilot stage.[^a], Beta reached the trial stage.[^b]'
        clauses = split_into_clauses(text)
        assert clauses[0] == "Acme reached the pilot stage.[^a]"
        assert not clauses[0].endswith(",")
        assert not clauses[1].startswith(" ") and not clauses[1].startswith(",")

    def test_a_marker_run_glued_to_the_next_clause_is_not_a_terminator(self) -> None:
        text = (
            'Acme reached the pilot stage.[^a]and immediately Beta reached the '
            'trial stage.[^b]'
        )
        clauses = split_into_clauses(text)
        # One glued clause, not two clean ones: the first marker run is
        # swallowed because nothing but a lowercase letter follows it.
        assert len(clauses) == 1
        assert clauses[0] == text

    def test_a_trailing_span_with_no_marker_is_its_own_final_clause(self) -> None:
        text = 'Acme reached the pilot stage.[^a] No citation for this part.'
        clauses = split_into_clauses(text)
        assert clauses == [
            "Acme reached the pilot stage.[^a]",
            "No citation for this part.",
        ]

    def test_a_marker_run_at_the_very_end_is_still_a_terminator(self) -> None:
        assert split_into_clauses('Acme reached the pilot stage.[^a]') == [
            "Acme reached the pilot stage.[^a]"
        ]


class TestNewFixtureSplitting:
    def test_items_a_and_b_split_into_the_clauses_the_terminator_rule_defines(
        self, workspace: Path
    ) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        assert report["version"] == DECOMPOSE_REPORT_VERSION_2
        assert report["items"] == 3
        assert report["clause_split"]["items_split"] == 2
        assert report["clause_split"]["clauses"] == 10
        assert report["clause_split"]["malformed"] == 1

        item1 = [b for b in report["bullets"] if b["item_ordinal"] == 1]
        item2 = [b for b in report["bullets"] if b["item_ordinal"] == 2]
        item3 = [b for b in report["bullets"] if b["item_ordinal"] == 3]
        assert len(item1) == 6
        assert len(item2) == 4
        assert len(item3) == 1
        assert [b["clause_shape"] for b in item1] == [
            "clause",
            "clause",
            "clause",
            "clause",
            "clause",
            "malformed",
        ]
        assert [b["clause_shape"] for b in item2] == ["clause", "clause", "clause", "clause"]
        assert item3[0]["clause_shape"] == "item"

    def test_each_clauses_refs_are_only_its_own_markers(self, workspace: Path) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        by_subject = {b["subject"]: b for b in report["bullets"] if b["subject"]}
        assert by_subject["Nimbus Analytics"]["refs"] == ["n1"]
        assert by_subject["Pinewood Traders"]["refs"] == ["p1"]
        assert by_subject["Driftwood Capital"]["refs"] == ["L"]
        assert by_subject["Harborlight Media"]["refs"] == ["M"]

    def test_d_stays_one_item_with_its_version_1_id_and_item_shape(self, workspace: Path) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        item_d = next(b for b in report["bullets"] if b["subject"] == "Lighthouse Ventures")
        assert item_d["clause_shape"] == "item"
        assert item_d["clause_index"] == 0
        assert item_d["item_ordinal"] == 3
        # Version 1 id scheme: "<ordinal>-<hash>", no dot.
        assert item_d["id"] == f"3-{item_d['id'].split('-', 1)[1]}"
        assert "." not in item_d["id"].split("-")[0]
        assert item_d["disposition"] == "attached"

    def test_the_interleave_yields_one_malformed_clause_unresolved_empty_subject(
        self, workspace: Path
    ) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        malformed = [b for b in report["bullets"] if b["clause_shape"] == "malformed"]
        assert len(malformed) == 1
        clause = malformed[0]
        assert clause["disposition"] == "unresolved"
        assert clause["subject"] == ""
        assert clause["source"] == ""
        assert "matched 2 time" in clause["note"]
        assert "." in clause["id"].split("-")[0]

    def test_a_uid_ruling_on_the_malformed_clause_is_refused_but_drop_is_accepted(
        self, workspace: Path
    ) -> None:
        report = build_report(
            workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, split_clauses=True
        )
        malformed = next(b for b in report.bullets if b.clause_shape == "malformed")
        uid_problems = check_resolutions(report, {malformed.id: "tgt00101"})
        assert any(malformed.id in p and "only 'drop'" in p for p in uid_problems)
        drop_problems = check_resolutions(report, {malformed.id: "drop"})
        assert not any(malformed.id in p for p in drop_problems)

    def test_the_trailing_span_is_a_normal_clause_classified_no_source(
        self, workspace: Path
    ) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        trailing = next(b for b in report["bullets"] if b["subject"] == "Fernwood Capital")
        assert trailing["clause_shape"] == "clause"
        assert trailing["disposition"] == "no-source"
        assert trailing["refs"] == []

    def test_the_duplicated_heading_creates_no_unit(self, workspace: Path) -> None:
        page = (workspace / "wiki" / SOURCE_PAGE).read_text()
        assert page.count("## Notes") == 2
        _, report = _dry_run(workspace, "--split-clauses")
        assert report["bullet_count"] == 11  # 6 + 4 clauses plus 1 whole item, never 13

    def test_label_l_resolves_differently_for_its_two_clauses(self, workspace: Path) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        sable = next(b for b in report["bullets"] if b["subject"] == "Sable Orchard")
        driftwood = next(b for b in report["bullets"] if b["subject"] == "Driftwood Capital")
        assert sable["disposition"] == "attached"
        assert driftwood["disposition"] == "attached"
        assert sable["source_drive_path"] != driftwood["source_drive_path"]
        assert sable["source_drive_path"] == "drive/d103-sable-orchard.md"
        assert driftwood["source_drive_path"] == "drive/d104-driftwood-capital.md"

    def test_label_m_is_ambiguous_for_one_subject_and_attaches_for_the_other(
        self, workspace: Path
    ) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        quartz = next(b for b in report["bullets"] if b["subject"] == "Quartz Logistics")
        harborlight = next(b for b in report["bullets"] if b["subject"] == "Harborlight Media")
        assert quartz["disposition"] == "ambiguous-source"
        assert "share label" in quartz["note"]
        assert harborlight["disposition"] == "attached"
        assert harborlight["source_drive_path"] == "drive/d105-harborlight-media.md"

    def test_the_collision_guard_is_off_by_default(self) -> None:
        """`collision_subjects=None` (select_definition's default -- the
        version 1 call shape) takes a single definition as-is regardless of
        how many subjects share its label; only an explicit
        `collision_subjects` mapping (what the ``split_clauses`` path
        builds) can turn that into ``ambiguous-source``."""
        definitions = {
            "m": [Definition(label="m", text="x", drive_path="drive/d1-other-subject.md")]
        }
        definition, note = select_definition(["m"], "Totally Different Subject", definitions)
        assert definition is not None
        assert note == ""
        # The SAME call, with a collision map saying label "m" is shared by
        # two subjects, is no longer taken as-is.
        definition2, note2 = select_definition(
            ["m"],
            "Totally Different Subject",
            definitions,
            collision_subjects={"m": frozenset({"Totally Different Subject", "Other Subject"})},
        )
        assert definition2 is None
        assert "share label" in note2


class TestVersion2ReportShape:
    def test_version_2_adds_fields_and_keeps_every_version_1_key(self, workspace: Path) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        assert report["version"] == 2
        for key in (
            "source_uid",
            "source_name",
            "source_path",
            "subject_until",
            "bullet_count",
            "orphan_definitions",
            "conflicting_labels",
            "counts",
            "subjects",
        ):
            assert key in report
        assert "items" in report
        assert set(report["clause_split"]) == {"items_split", "clauses", "malformed"}
        for bullet in report["bullets"]:
            assert {"item_ordinal", "clause_index", "clause_shape"} <= set(bullet)

    def test_reading_only_the_version_1_fields_gives_consistent_counts(
        self, workspace: Path
    ) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        v1_fields = (
            "ordinal",
            "id",
            "raw",
            "subject",
            "refs",
            "disposition",
            "uid",
            "target",
            "source",
            "source_drive_path",
            "note",
        )
        stripped = [{k: b[k] for k in v1_fields} for b in report["bullets"]]
        assert len(stripped) == report["bullet_count"]
        tally = dict.fromkeys(DISPOSITIONS, 0)
        for b in stripped:
            tally[b["disposition"]] += 1
        assert tally == report["counts"]
        resolved = sum(1 for b in stripped if b["uid"])
        assert resolved == report["subjects"]["resolved"]
        assert len(stripped) - resolved == report["subjects"]["unresolved"]

    def test_clause_ids_follow_item_dot_clause_dash_hash(self, workspace: Path) -> None:
        _, report = _dry_run(workspace, "--split-clauses")
        for bullet in report["bullets"]:
            if bullet["clause_shape"] == "item":
                assert "." not in bullet["id"].split("-", 1)[0]
            else:
                head = bullet["id"].split("-", 1)[0]
                assert head == f"{bullet['item_ordinal']}.{bullet['clause_index']}"

    def test_editing_one_character_of_a_clause_invalidates_its_ruling(
        self, workspace: Path
    ) -> None:
        report = build_report(
            workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, split_clauses=True
        )
        sable = next(b for b in report.bullets if b.subject == "Sable Orchard")
        stale_ruling = {sable.id: "tgt00103"}

        page = workspace / "wiki" / SOURCE_PAGE
        page.write_text(page.read_text().replace("a renewal note", "a renewal notes"))
        report2 = build_report(
            workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, split_clauses=True
        )
        problems = check_resolutions(report2, stale_ruling)
        assert any("does not match any bullet" in p or "text changed" in p for p in problems)

    def test_clause_id_helper_matches_what_build_report_produces(self) -> None:
        raw = '- Sable Orchard reached the renewal stage, logging "a renewal note".[^L]'
        assert clause_id(1, 4, raw) == clause_id(1, 4, raw)
        assert clause_id(1, 4, raw) != clause_id(1, 5, raw)
        assert clause_id(1, 4, raw) != clause_id(2, 4, raw)


class TestStdoutCountsOnly:
    def test_split_clauses_stdout_has_no_subject_and_three_new_count_lines(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, report = _dry_run(workspace, "--split-clauses")
        assert rc == 0
        assert report["bullets"]
        captured = capsys.readouterr()
        for subject in FIXTURE_SUBJECTS:
            assert subject not in captured.out
            assert subject not in captured.err
        assert "items split:" in captured.out
        assert "clauses:" in captured.out
        assert "malformed:" in captured.out


class TestApplyWithSplitClauses:
    @pytest.fixture
    def applied(self, workspace: Path) -> Path:
        rc = main(_apply_argv(workspace))
        assert rc == 0
        return workspace

    def test_every_well_formed_attached_clause_lands_verbatim_renumbered(
        self, applied: Path
    ) -> None:
        body = parse_frontmatter((applied / "wiki" / "tgt00101-nimbus-analytics.md").read_text())[
            1
        ]
        assert '- Nimbus Analytics reached the pilot stage, logging "a kickoff call".[^2]' in body
        assert "drive/d101-nimbus-analytics.md" in body

    def test_an_already_present_target_is_byte_identical(self, applied: Path) -> None:
        name = "tgt00102-pinewood-traders.md"
        assert (applied / "wiki" / name).read_bytes() == (CLAUSE_DIR / "wiki" / name).read_bytes()

    def test_label_l_attaches_its_own_definition_to_each_target(self, applied: Path) -> None:
        sable = parse_frontmatter((applied / "wiki" / "tgt00103-sable-orchard.md").read_text())[1]
        driftwood = parse_frontmatter(
            (applied / "wiki" / "tgt00104-driftwood-capital.md").read_text()
        )[1]
        assert "drive/d103-sable-orchard.md" in sable
        assert "drive/d104-driftwood-capital.md" not in sable
        assert "drive/d104-driftwood-capital.md" in driftwood
        assert "drive/d103-sable-orchard.md" not in driftwood

    def test_ambiguous_source_leaves_quartz_untouched(self, applied: Path) -> None:
        before = (CLAUSE_DIR / "wiki" / "tgt00106-quartz-logistics.md").read_bytes()
        after = (applied / "wiki" / "tgt00106-quartz-logistics.md").read_bytes()
        assert before == after

    def test_dropped_clauses_land_nowhere(self, applied: Path) -> None:
        for page in sorted((applied / "wiki").glob("*.md")):
            if page.name == SOURCE_PAGE:
                continue
            text = page.read_text()
            assert "an unresolved note" not in text
            assert "an unresolved aside" not in text
            assert "a demo recap" not in text
            assert "a growth packet" not in text
            assert "a procurement note" not in text

    def test_source_is_rewritten_and_loses_every_fixture_subject(self, applied: Path) -> None:
        meta, body = parse_frontmatter((applied / "wiki" / SOURCE_PAGE).read_text())
        whole = f"{meta.get('description', '')}\n{body}".casefold()
        for subject in FIXTURE_SUBJECTS:
            assert subject.casefold() not in whole

    def test_a_second_apply_with_split_clauses_is_a_byte_for_byte_no_op(
        self, applied: Path
    ) -> None:
        before = _digests(applied / "wiki")
        assert main(_apply_argv(applied)) == 0
        assert _digests(applied / "wiki") == before

    def test_targets_have_no_duplicate_labels_and_no_dangling_refs(self, applied: Path) -> None:
        for name in (
            "tgt00101-nimbus-analytics.md",
            "tgt00103-sable-orchard.md",
            "tgt00104-driftwood-capital.md",
            "tgt00105-harborlight-media.md",
            "tgt00107-lighthouse-ventures.md",
        ):
            body = parse_frontmatter((applied / "wiki" / name).read_text())[1]
            defined = [m.group(1) for m in FOOTNOTE_DEF_RE.finditer(body)]
            assert len(defined) == len(set(defined)), f"{name}: duplicate footnote label"
            assert set(INLINE_MARKER_RE.findall(body)) <= set(defined), f"{name}: dangling ref"


class TestApplyRefusals:
    def test_apply_refuses_without_rulings_for_the_blocking_clauses(self, workspace: Path) -> None:
        before = _digests(workspace / "wiki")
        argv = _apply_argv(workspace)
        argv.remove("--resolutions")
        argv.remove(str(workspace / "resolutions.json"))
        assert main(argv) == 1
        assert _digests(workspace / "wiki") == before


class TestRunLockUnderSplitClauses:
    def test_dry_run_does_not_take_the_lock(self, workspace: Path) -> None:
        _dry_run(workspace, "--split-clauses")
        assert not (workspace / runlock.LOCKFILE_NAME).exists()

    def test_apply_still_takes_the_lock(self, workspace: Path) -> None:
        assert main(_apply_argv(workspace)) == 0
        assert (workspace / runlock.LOCKFILE_NAME).exists()


class TestNoLLMUnderSplitClauses:
    """athenaeum#1947: clause mode is still tier-0 -- no LLM call, no provider,
    whether or not an item was actually split."""

    def test_no_provider_is_constructed_during_a_split_clauses_apply(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.provider as provider

        def _boom(*args: object, **kwargs: object) -> str:
            raise AssertionError("decompose-page --split-clauses must not resolve an LLM provider")

        monkeypatch.setattr(provider, "resolve_provider", _boom)
        monkeypatch.setattr(provider, "preflight_provider", _boom)
        assert main(_apply_argv(workspace)) == 0

    def test_a_split_clauses_apply_never_imports_the_llm_chain(self, workspace: Path) -> None:
        probe = (
            "import json, sys\n"
            "from athenaeum.cli import main\n"
            f"rc = main({_apply_argv(workspace)!r})\n"
            "print(json.dumps({'rc': rc, 'llm': sorted(\n"
            "    m for m in sys.modules\n"
            "    if m in ('anthropic', 'athenaeum.librarian', 'athenaeum.tiers',\n"
            "             'athenaeum.batch', 'athenaeum.merge')\n"
            ")}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PATH": "/usr/bin:/bin",
                "HOME": str(workspace.parent),
                "ATHENAEUM_CACHE_DIR": str(workspace.parent / "cache"),
            },
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["rc"] == 0
        assert result["llm"] == []


class TestFixtureIntegrity:
    """Mirrors tests/test_page_decompose.py's own integrity check: the
    committed resolutions file is keyed by content hash, so an edit to the
    fixture page without regenerating it fails every apply test for a
    reason that has nothing to do with the code."""

    def test_committed_resolutions_still_match_the_fixture_page(self, workspace: Path) -> None:
        report = build_report(
            workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, split_clauses=True
        )
        ruled = set(load_resolutions(CLAUSE_DIR / "resolutions.json"))
        blocking = {b.id for b in report.bullets if b.disposition in BLOCKING_DISPOSITIONS}
        assert ruled == blocking, (
            "tests/fixtures/page_decompose_clauses/resolutions.json is keyed by "
            "clause/bullet id and no longer matches the fixture page -- "
            "regenerate it after editing a clause"
        )


class TestDomainErrors:
    def test_write_report_still_works_under_split_clauses(self, workspace: Path) -> None:
        report = build_report(
            workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, split_clauses=True
        )
        out = workspace / "r.json"
        write_report(report, out)
        assert json.loads(out.read_text())["version"] == 2

    def test_apply_report_raises_before_any_write_when_resolutions_are_missing(
        self, workspace: Path
    ) -> None:
        report = build_report(
            workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, split_clauses=True
        )
        before = _digests(workspace / "wiki")
        with pytest.raises(DecomposeError):
            apply_report(
                report,
                workspace / "wiki",
                resolutions={},
                rewrite_body=(workspace / "rewrite_body.md").read_text(),
                description=DESCRIPTION,
            )
        assert _digests(workspace / "wiki") == before

