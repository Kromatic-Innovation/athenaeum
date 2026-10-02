# SPDX-License-Identifier: Apache-2.0
"""`athenaeum decompose-page` — the whole fixture-testable half of athenaeum#1914.

Everything here runs against `tests/fixtures/page_decompose/`, copied into
`tmp_path` per test; nothing in this module reads or writes `~/knowledge`.

The fixture wiki is built so that one dry run over one page produces all five
dispositions, in bullet order, with no test having to construct a special
case:

1. `attached`         — "Acme Corp", whose label `[^a]` has TWO conflicting
                        definitions, so the slug rule has to pick one.
2. `already-present`  — "Beacon Labs", whose target already cites the same
                        `drive/` source AND carries the bullet's quoted span.
3. `unresolved`       — "X Labs", where only a shorter entity "X" exists. The
                        bullet that proves the pass never prefix-matches.
4. `no-source`        — "Delta Freight", a bullet with no footnote ref at all.
5. `ambiguous-source` — "Cinder Works", whose label `[^e]` has two conflicting
                        definitions and whose slug matches neither.

A sixth label, `[^z]`, is defined and referenced by nothing — the orphan the
report counts.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import runlock
from athenaeum.cli import EXIT_LOCK_HELD, main
from athenaeum.footnote_markers import FOOTNOTE_DEF_RE, INLINE_MARKER_RE
from athenaeum.models import EntityIndex, IndexEntry, parse_frontmatter, render_frontmatter
from athenaeum.page_decompose import (
    DISPOSITIONS,
    BulletPlan,
    DecomposeError,
    _attach_to_target,
    apply_report,
    build_report,
    bullet_id,
    extract_subject,
    load_resolutions,
    resolve_subject,
    validate_rewrite,
)
from athenaeum.schemas import validate_wiki_meta

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "page_decompose"
SOURCE_UID = "src00001"
SOURCE_PAGE = "src00001-fixture-pipeline-tool.md"
#: The fixture page's sentence template. The REAL page's regex is supplied at
#: run time and deliberately never recorded in this repo (athenaeum#1914).
SUBJECT_UNTIL = r" reached the "
DESCRIPTION = "A fixture customer-relationship tracker used by the decompose-page tests."
#: Every bullet subject on the fixture page — the names the rewritten source
#: page must no longer contain anywhere.
FIXTURE_SUBJECTS = ("Acme Corp", "Beacon Labs", "X Labs", "Delta Freight", "Cinder Works")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A private copy of the fixture knowledge root."""
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    for page in sorted((FIXTURE_DIR / "wiki").glob("*.md")):
        (root / "wiki" / page.name).write_bytes(page.read_bytes())
    (root / "rewrite_body.md").write_bytes((FIXTURE_DIR / "rewrite_body.md").read_bytes())
    (root / "resolutions.json").write_bytes((FIXTURE_DIR / "resolutions.json").read_bytes())
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
        "--apply",
        "--resolutions",
        str(root / "resolutions.json"),
        "--rewrite-body",
        str(root / "rewrite_body.md"),
        "--description",
        DESCRIPTION,
        *extra,
    ]


class TestFixtureIntegrity:
    """The committed resolutions file is keyed by content hash — if a bullet's
    text is edited without regenerating it, every apply test fails for a
    reason that has nothing to do with the code. Say so here instead."""

    def test_committed_resolutions_still_match_the_fixture_page(self, workspace: Path) -> None:
        report = build_report(workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL)
        ruled = set(load_resolutions(FIXTURE_DIR / "resolutions.json"))
        blocking = {b.id for b in report.bullets if b.disposition in DISPOSITIONS[2:]}
        assert ruled == blocking, (
            "tests/fixtures/page_decompose/resolutions.json is keyed by bullet id "
            "(ordinal + sha256 of the bullet text) and no longer matches the fixture "
            "page — regenerate it after editing a bullet."
        )


class TestDryRun:
    def test_dry_run_writes_nothing(self, workspace: Path) -> None:
        before = _digests(workspace / "wiki")
        rc, _ = _dry_run(workspace)
        assert rc == 0
        assert _digests(workspace / "wiki") == before
        assert not (workspace / runlock.LOCKFILE_NAME).exists()

    def test_every_bullet_gets_exactly_one_known_disposition(self, workspace: Path) -> None:
        _, report = _dry_run(workspace)
        assert report["bullet_count"] == 5
        for bullet in report["bullets"]:
            assert bullet["disposition"] in DISPOSITIONS

    def test_the_five_dispositions_land_in_bullet_order(self, workspace: Path) -> None:
        _, report = _dry_run(workspace)
        assert [b["disposition"] for b in report["bullets"]] == [
            "attached",
            "already-present",
            "unresolved",
            "no-source",
            "ambiguous-source",
        ]

    def test_report_carries_id_subject_and_uid_per_bullet(self, workspace: Path) -> None:
        _, report = _dry_run(workspace)
        first = report["bullets"][0]
        assert first["subject"] == "Acme Corp"
        assert first["uid"] == "tgt00001"
        assert first["id"] == bullet_id(1, first["raw"])
        ordinal, _, digest = first["id"].partition("-")
        assert ordinal == "1"
        assert len(digest) == 12 and int(digest, 16) >= 0

    def test_report_counts_orphan_definitions(self, workspace: Path) -> None:
        _, report = _dry_run(workspace)
        assert report["orphan_definitions"] == 1
        assert report["conflicting_labels"] == 2

    def test_report_counts_resolved_and_unresolved_subjects(self, workspace: Path) -> None:
        _, report = _dry_run(workspace)
        assert report["subjects"]["resolved"] == 4
        assert report["subjects"]["unresolved"] == 1
        assert report["subjects"]["resolved"] + report["subjects"]["unresolved"] == 5

    def test_report_goes_to_the_report_path_not_stdout(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, report = _dry_run(workspace)
        assert rc == 0
        assert report["bullets"], "the report file must carry the per-bullet detail"
        out = capsys.readouterr().out
        for subject in FIXTURE_SUBJECTS:
            assert subject not in out, "stdout must never carry corpus content"
        for page in ("tgt00001-acme-corp.md", "drive/d001-acme-corp.md"):
            assert page not in out
        assert "bullets:" in out and "orphan definitions:" in out


class TestSubjectExtraction:
    def test_subject_is_the_span_before_the_first_regex_match(self) -> None:
        raw = '- Acme Corp reached the pilot stage, logging "two calls".[^a]'
        assert extract_subject(raw, re.compile(SUBJECT_UNTIL)) == "Acme Corp"

    def test_only_the_FIRST_match_bounds_the_subject(self) -> None:
        raw = "- Acme Corp reached the stage it reached the year before."
        assert extract_subject(raw, re.compile(SUBJECT_UNTIL)) == "Acme Corp"

    def test_a_bullet_the_regex_misses_has_an_empty_subject(self) -> None:
        raw = "- Acme Corp was archived in 2026."
        assert extract_subject(raw, re.compile(SUBJECT_UNTIL)) == ""

    def test_a_bullet_the_regex_misses_is_unresolved(self, workspace: Path) -> None:
        page = workspace / "wiki" / SOURCE_PAGE
        # Bullet 1, not bullet 4: bullet 4 has no footnote ref, so `no-source`
        # would decide it first and the subject would never be consulted.
        page.write_text(
            page.read_text().replace(
                "- Acme Corp reached the pilot stage,",
                "- Acme Corp was archived after,",
            )
        )
        _, report = _dry_run(workspace)
        offending = report["bullets"][0]
        assert offending["subject"] == ""
        assert offending["disposition"] == "unresolved"
        assert offending["uid"] == ""

    def test_a_longer_subject_never_falls_back_to_a_shorter_entity(
        self, workspace: Path
    ) -> None:
        """"X Labs" must not attach to the existing entity "X" (athenaeum#1914)."""
        _, report = _dry_run(workspace)
        x_labs = report["bullets"][2]
        assert x_labs["subject"] == "X Labs"
        assert x_labs["disposition"] == "unresolved"
        assert x_labs["uid"] == ""


class TestResolutionOrder:
    def test_exact_lookup_wins_and_is_type_scoped(self, workspace: Path) -> None:
        index = EntityIndex(workspace / "wiki")
        uid, page, _ = resolve_subject("Acme Corp", index, SOURCE_UID)
        assert (uid, page) == ("tgt00001", "tgt00001-acme-corp.md")

    def test_a_non_company_non_person_page_never_resolves(self, workspace: Path) -> None:
        index = EntityIndex(workspace / "wiki")
        uid, _, note = resolve_subject("Pipeline Directory", index, SOURCE_UID)
        assert uid == ""
        assert "no company/person page" in note

    def test_the_source_page_never_resolves_to_itself(self, workspace: Path) -> None:
        index = EntityIndex(workspace / "wiki")
        assert resolve_subject("Fixture Pipeline Tool", index, SOURCE_UID)[0] == ""

    def test_loose_match_resolves_only_when_unique(self, workspace: Path) -> None:
        index = EntityIndex(workspace / "wiki")
        # "Acme Corp 🦁" differs from the indexed key only by a symbol, which
        # `normalize_name` strips — exactly one candidate, so it resolves.
        uid, _, _ = resolve_subject("Acme Corp \N{LION FACE}", index, SOURCE_UID)
        assert uid == "tgt00001"

    def test_two_loose_candidates_are_unresolved_never_a_guess(self, workspace: Path) -> None:
        twin = workspace / "wiki" / "tgt00099-acme-corp-twin.md"
        twin.write_text(
            "---\nuid: tgt00099\ntype: company\n"
            "name: 'Acme Corp \N{BLACK STAR}'\n---\n\nTwin.\n"
        )
        index = EntityIndex(workspace / "wiki")
        uid, _, note = resolve_subject("Acme Corp \N{LION FACE}", index, SOURCE_UID)
        assert uid == ""
        assert "loose candidates" in note


class TestConflictingLabelRule:
    def test_the_slug_selected_definition_is_the_one_reported(self, workspace: Path) -> None:
        _, report = _dry_run(workspace)
        acme = report["bullets"][0]
        # `[^a]` is defined twice: d001-acme-corp and d002-beacon-labs. Only
        # the first one's slug equals slugify("Acme Corp").
        assert acme["source_drive_path"] == "drive/d001-acme-corp.md"
        assert "d002-beacon-labs" not in acme["source"]

    def test_no_matching_slug_is_ambiguous_not_an_attach_with_everything(
        self, workspace: Path
    ) -> None:
        _, report = _dry_run(workspace)
        cinder = report["bullets"][4]
        assert cinder["disposition"] == "ambiguous-source"
        assert cinder["source"] == ""
        assert cinder["source_drive_path"] == ""


class TestApplyRefusals:
    def test_apply_refuses_and_writes_nothing_without_rulings(self, workspace: Path) -> None:
        before = _digests(workspace / "wiki")
        argv = _apply_argv(workspace)
        argv.remove("--resolutions")
        argv.remove(str(workspace / "resolutions.json"))
        rc = main(argv)
        assert rc == 1
        assert _digests(workspace / "wiki") == before

    def test_apply_refuses_a_ruling_whose_hash_no_longer_matches(
        self, workspace: Path
    ) -> None:
        rulings = json.loads((workspace / "resolutions.json").read_text())
        stale = {f"{k.split('-')[0]}-000000000000": v for k, v in rulings.items()}
        (workspace / "resolutions.json").write_text(json.dumps(stale))
        before = _digests(workspace / "wiki")
        assert main(_apply_argv(workspace)) == 1
        assert _digests(workspace / "wiki") == before

    def test_apply_refuses_a_uid_ruling_for_a_sourceless_bullet(
        self, workspace: Path
    ) -> None:
        rulings = json.loads((workspace / "resolutions.json").read_text())
        for key, verdict in list(rulings.items()):
            if verdict == "drop":
                rulings[key] = "tgt00005"
        (workspace / "resolutions.json").write_text(json.dumps(rulings))
        before = _digests(workspace / "wiki")
        assert main(_apply_argv(workspace)) == 1
        assert _digests(workspace / "wiki") == before

    def test_apply_requires_rewrite_body_and_description(self, workspace: Path) -> None:
        before = _digests(workspace / "wiki")
        rc = main(
            [
                "decompose-page",
                SOURCE_UID,
                "--path",
                str(workspace),
                "--subject-until",
                SUBJECT_UNTIL,
                "--report",
                str(workspace / "report.json"),
                "--apply",
            ]
        )
        assert rc == 1
        assert _digests(workspace / "wiki") == before

    def test_an_unknown_uid_is_refused(self, workspace: Path) -> None:
        rc, _ = _dry_run(workspace)
        assert rc == 0
        assert (
            main(
                [
                    "decompose-page",
                    "nosuchuid",
                    "--path",
                    str(workspace),
                    "--subject-until",
                    SUBJECT_UNTIL,
                    "--report",
                    str(workspace / "r2.json"),
                ]
            )
            == 1
        )


class TestApply:
    @pytest.fixture
    def applied(self, workspace: Path) -> Path:
        assert main(_apply_argv(workspace)) == 0
        return workspace

    def test_target_gains_the_bullet_before_the_first_definition(
        self, applied: Path
    ) -> None:
        text = (applied / "wiki" / "tgt00001-acme-corp.md").read_text()
        _, body = parse_frontmatter(text)
        lines = body.split("\n")
        bullet_at = next(
            i for i, line in enumerate(lines) if line.startswith("- Acme Corp reached the")
        )
        first_def_at = next(i for i, line in enumerate(lines) if FOOTNOTE_DEF_RE.match(line))
        assert bullet_at < first_def_at

    def test_target_gains_the_bullet_verbatim_but_renumbered(self, applied: Path) -> None:
        body = parse_frontmatter((applied / "wiki" / "tgt00001-acme-corp.md").read_text())[1]
        assert '- Acme Corp reached the pilot stage, logging "two discovery calls".[^3]' in body

    def test_exactly_one_definition_comes_with_it_and_it_is_the_slug_selected_one(
        self, applied: Path
    ) -> None:
        body = parse_frontmatter((applied / "wiki" / "tgt00001-acme-corp.md").read_text())[1]
        assert body.count("drive/d001-acme-corp.md") == 1
        assert "drive/d002-beacon-labs.md" not in body

    def test_target_has_no_duplicate_labels_and_no_dangling_refs(
        self, applied: Path
    ) -> None:
        for name in ("tgt00001-acme-corp.md", "tgt00004-x.md"):
            body = parse_frontmatter((applied / "wiki" / name).read_text())[1]
            defined = [m.group(1) for m in FOOTNOTE_DEF_RE.finditer(body)]
            assert len(defined) == len(set(defined)), f"{name}: duplicate footnote label"
            assert set(INLINE_MARKER_RE.findall(body)) <= set(defined), f"{name}: dangling ref"

    def test_an_already_present_target_is_byte_identical(self, applied: Path) -> None:
        name = "tgt00002-beacon-labs.md"
        assert (applied / "wiki" / name).read_bytes() == (
            FIXTURE_DIR / "wiki" / name
        ).read_bytes()

    def test_a_ruled_bullet_is_attached_to_the_ruled_uid(self, applied: Path) -> None:
        body = parse_frontmatter((applied / "wiki" / "tgt00004-x.md").read_text())[1]
        assert '- X Labs reached the pilot stage, logging "one intro email".[^2]' in body
        assert "[^2]: **Source:** `drive/d004-x-labs.md`" in body

    def test_dropped_count_is_not_stale_on_a_second_apply(self, applied: Path) -> None:
        # Sentry LOW finding (athenaeum#1914): re-running apply on an
        # already-decomposed page (its source now has zero bullets) must
        # not double-count the SAME resolutions-file "drop" rulings as new
        # drops. dropped must reflect only bullets present in THIS report.
        index = EntityIndex(applied / "wiki")
        report2 = build_report(
            applied / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL, index=index
        )
        assert report2.bullets == []
        resolutions = load_resolutions(applied / "resolutions.json")
        rewrite_body = (applied / "rewrite_body.md").read_text(encoding="utf-8")
        result2 = apply_report(
            report2,
            applied / "wiki",
            resolutions=resolutions,
            rewrite_body=rewrite_body,
            description=DESCRIPTION,
            index=index,
        )
        assert result2.dropped == 0

    def test_a_dropped_bullet_lands_nowhere(self, applied: Path) -> None:
        for page in sorted((applied / "wiki").glob("*.md")):
            if page.name == SOURCE_PAGE:
                continue
            assert "Delta Freight reached the closed stage" not in page.read_text()
            assert "Cinder Works reached the trial stage" not in page.read_text()

    def test_written_targets_validate_and_bump_only_updated(self, applied: Path) -> None:
        for name in ("tgt00001-acme-corp.md", "tgt00004-x.md"):
            before, _ = parse_frontmatter((FIXTURE_DIR / "wiki" / name).read_text())
            after, _ = parse_frontmatter((applied / "wiki" / name).read_text())
            validate_wiki_meta(dict(after))
            assert after["updated"] != before["updated"]
            assert {k: v for k, v in after.items() if k != "updated"} == {
                k: v for k, v in before.items() if k != "updated"
            }

    def test_untouched_targets_are_byte_identical(self, applied: Path) -> None:
        for name in (
            "tgt00002-beacon-labs.md",
            "tgt00003-cinder-works.md",
            "tgt00005-delta-freight.md",
            "tgt00006-pipeline-directory.md",
        ):
            assert (applied / "wiki" / name).read_bytes() == (
                FIXTURE_DIR / "wiki" / name
            ).read_bytes()

    def test_source_is_rewritten_small_and_link_clean(self, applied: Path) -> None:
        meta, body = parse_frontmatter((applied / "wiki" / SOURCE_PAGE).read_text())
        assert len(body.encode("utf-8")) < 2048
        index = EntityIndex(applied / "wiki")
        assert validate_rewrite(body, str(meta.get("description", "")), [], index) == []

    def test_no_fixture_subject_name_survives_anywhere_on_the_source(
        self, applied: Path
    ) -> None:
        whole_file = (applied / "wiki" / SOURCE_PAGE).read_text().casefold()
        for subject in FIXTURE_SUBJECTS:
            assert subject.casefold() not in whole_file

    def test_source_identity_fields_are_unchanged(self, applied: Path) -> None:
        before, _ = parse_frontmatter((FIXTURE_DIR / "wiki" / SOURCE_PAGE).read_text())
        after, _ = parse_frontmatter((applied / "wiki" / SOURCE_PAGE).read_text())
        for key in ("uid", "type", "name"):
            assert after[key] == before[key]
        assert after["description"] == DESCRIPTION

    def test_a_second_apply_is_a_no_op(self, applied: Path) -> None:
        before = _digests(applied / "wiki")
        assert main(_apply_argv(applied)) == 0
        assert _digests(applied / "wiki") == before


class TestAttachNoSubstringFallback:
    """Sentry HIGH finding (athenaeum#1914): ``_attach_to_target``'s old
    duplicate-detection fallback compared the bullet's stripped text as a
    bare substring of the WHOLE target body, so a new fact that happened to
    be a substring of a longer, unrelated existing bullet was silently
    skipped. The only duplicate test now is ``already_present`` — the
    source-citation-AND-quoted-span conjunction the issue specifies.
    """

    def test_a_fact_that_is_a_substring_of_a_longer_bullet_still_attaches(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target.md"
        target.write_text(
            "---\n"
            "uid: tgt-substr\n"
            "type: company\n"
            "name: Substr Co\n"
            "created: 2026-01-01\n"
            "updated: 2026-01-01\n"
            "---\n\n"
            "## Notes\n\n"
            "- Substr Co reached the pilot stage after a long engagement.[^1]\n\n"
            "[^1]: **Source:** `drive/d900-substr-co.md`\n",
            encoding="utf-8",
        )
        # This bullet's stripped text ("- Substr Co reached the pilot
        # stage") is a literal substring of the existing bullet above, but
        # it cites a DIFFERENT drive source and carries no quoted span the
        # target already has — already_present() correctly says "not
        # present", so it must attach.
        plan = BulletPlan(
            ordinal=1,
            id="1-deadbeefcafe",
            raw="- Substr Co reached the pilot stage[^9]",
            subject="Substr Co",
            refs=["9"],
            disposition="attached",
        )
        # definition_text is the footnote TEXT only, no "[^label]: " prefix
        # — _attach_to_target builds that prefix itself (see the X Labs
        # assertion in TestApply for the same shape).
        wrote = _attach_to_target(
            target, plan, "**Source:** `drive/d901-other-source.md`", "2026-10-02"
        )
        assert wrote is True
        body = parse_frontmatter(target.read_text(encoding="utf-8"))[1]
        assert "- Substr Co reached the pilot stage[^2]" in body
        assert "drive/d901-other-source.md" in body


class TestApplyAllOrNothing:
    """Sentry HIGH finding (athenaeum#1914): apply_report -> _attach_to_target
    -> _bump_and_render -> validate_wiki_meta could raise an uncaught
    pydantic.ValidationError mid-run, leaving the wiki partially modified.
    apply_report now validates every target's rendered frontmatter in a
    pre-pass before any write, so a failure refuses the whole apply.
    """

    def test_a_second_target_failing_validation_leaves_the_first_untouched(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bullet order on the fixture page attaches to tgt00001 (Acme Corp)
        # first and tgt00004 (X) second. Corrupt tgt00004's ``field_sources``
        # (schema requires a dict; EntityIndex never reads this field at
        # all) so the page still resolves by uid — the refusal must come
        # from the frontmatter-validation pre-pass, not from an uid lookup
        # failure. Then assert the apply refuses and tgt00001 is
        # byte-identical to its state BEFORE the apply ran — not just
        # "unwritten by this bullet", but never touched at all.
        target4 = workspace / "wiki" / "tgt00004-x.md"
        meta, body = parse_frontmatter(target4.read_text(encoding="utf-8"))
        meta["field_sources"] = ["not-a-dict"]
        target4.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
        assert EntityIndex(workspace / "wiki").get_by_uid("tgt00004") is not None

        before = _digests(workspace / "wiki")
        rc = main(_apply_argv(workspace))
        assert rc == 1
        assert _digests(workspace / "wiki") == before
        stderr = capsys.readouterr().err
        assert "frontmatter validation" in stderr
        assert "tgt00004-x.md" in stderr
        assert "not-a-dict" not in stderr


class TestRewriteValidation:
    def _index(self, workspace: Path) -> EntityIndex:
        return EntityIndex(workspace / "wiki")

    def test_an_oversize_body_is_refused(self, workspace: Path) -> None:
        problems = validate_rewrite("x" * 3000, DESCRIPTION, [], self._index(workspace))
        assert any("over the" in p for p in problems)

    def test_a_link_to_a_missing_page_is_refused(self, workspace: Path) -> None:
        problems = validate_rewrite(
            "See [[No Such Page]].", DESCRIPTION, [], self._index(workspace)
        )
        assert any("not a wiki page" in p for p in problems)

    def test_a_markdown_link_to_a_missing_file_is_refused(self, workspace: Path) -> None:
        problems = validate_rewrite(
            "See [here](tgt99999-nope.md).", DESCRIPTION, [], self._index(workspace)
        )
        assert any("not a file in the wiki" in p for p in problems)

    def test_a_surviving_subject_name_in_the_body_is_refused(self, workspace: Path) -> None:
        problems = validate_rewrite(
            "Still about Acme Corp.", DESCRIPTION, ["Acme Corp"], self._index(workspace)
        )
        assert any("still names a decomposed subject" in p for p in problems)

    def test_a_surviving_subject_name_in_the_description_is_refused(
        self, workspace: Path
    ) -> None:
        problems = validate_rewrite(
            "Clean body.",
            "Pipeline history for Acme Corp.",
            ["Acme Corp"],
            self._index(workspace),
        )
        assert any("still names a decomposed subject" in p for p in problems)

    def test_apply_refuses_a_rewrite_that_still_names_a_subject(
        self, workspace: Path
    ) -> None:
        before = _digests(workspace / "wiki")
        rc = main(_apply_argv(workspace)[:-1] + ["Pipeline history for Acme Corp."])
        assert rc == 1
        assert _digests(workspace / "wiki") == before


class TestRunLock:
    def test_dry_run_does_not_take_the_lock(self, workspace: Path) -> None:
        _dry_run(workspace)
        assert not (workspace / runlock.LOCKFILE_NAME).exists()

    def test_apply_takes_the_lock(self, workspace: Path) -> None:
        assert main(_apply_argv(workspace)) == 0
        assert (workspace / runlock.LOCKFILE_NAME).exists()

    def test_apply_refuses_and_writes_nothing_while_the_lock_is_held(
        self, workspace: Path
    ) -> None:
        before = _digests(workspace / "wiki")
        holder = runlock.RunLock(workspace)
        holder.acquire()
        try:
            assert main(_apply_argv(workspace)) == EXIT_LOCK_HELD
        finally:
            holder.release()
        assert _digests(workspace / "wiki") == before


class TestNoLLM:
    """athenaeum#1914: the pass is tier-0. It must not reach the LLM chain at
    all — not by constructing a provider, and not by dragging the librarian or
    the tiers into the process to get there."""

    def test_no_provider_is_constructed_during_an_apply(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.provider as provider

        def _boom(*args: object, **kwargs: object) -> str:
            raise AssertionError("decompose-page must not resolve an LLM provider")

        monkeypatch.setattr(provider, "resolve_provider", _boom)
        monkeypatch.setattr(provider, "preflight_provider", _boom)
        assert main(_apply_argv(workspace)) == 0

    def test_an_apply_never_imports_the_llm_chain(self, workspace: Path) -> None:
        """A subprocess, because the in-process suite has already imported
        half the tree — only a clean interpreter can tell what THIS command
        pulls in."""
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
                # athenaeum#791: a hand-built env dict that redirects HOME must
                # pin ATHENAEUM_CACHE_DIR too, so nothing can fall through to
                # the operator's real ~/.cache/athenaeum.
                "ATHENAEUM_CACHE_DIR": str(workspace.parent / "cache"),
            },
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["rc"] == 0
        assert result["llm"] == []


class TestDomainErrors:
    def test_build_report_raises_on_an_unknown_uid(self, workspace: Path) -> None:
        with pytest.raises(DecomposeError):
            build_report(workspace / "wiki", "nope", subject_until=SUBJECT_UNTIL)

    def test_load_resolutions_rejects_a_non_object(self, workspace: Path) -> None:
        path = workspace / "bad.json"
        path.write_text("[1, 2]")
        with pytest.raises(DecomposeError):
            load_resolutions(path)

    def test_load_resolutions_rejects_invalid_json(self, workspace: Path) -> None:
        path = workspace / "bad.json"
        path.write_text("{nope")
        with pytest.raises(DecomposeError):
            load_resolutions(path)

    def test_apply_report_refuses_a_ruling_naming_no_page(self, workspace: Path) -> None:
        report = build_report(workspace / "wiki", SOURCE_UID, subject_until=SUBJECT_UNTIL)
        rulings = load_resolutions(workspace / "resolutions.json")
        unresolved = next(b for b in report.bullets if b.disposition == "unresolved")
        rulings[unresolved.id] = "tgt99999"
        before = _digests(workspace / "wiki")
        with pytest.raises(DecomposeError, match="names no wiki page"):
            apply_report(
                report,
                workspace / "wiki",
                resolutions=rulings,
                rewrite_body=(workspace / "rewrite_body.md").read_text(),
                description=DESCRIPTION,
            )
        assert _digests(workspace / "wiki") == before

    def test_index_entry_without_a_type_never_resolves(self, workspace: Path) -> None:
        index = EntityIndex(workspace / "wiki")
        index._by_name["typeless"] = IndexEntry("tgt00098", workspace / "wiki" / "x.md", None)
        assert resolve_subject("typeless", index, SOURCE_UID)[0] == ""


class TestCliRefusals:
    def test_a_missing_wiki_root_is_refused(self, tmp_path: Path) -> None:
        assert (
            main(
                [
                    "decompose-page",
                    SOURCE_UID,
                    "--path",
                    str(tmp_path / "nowhere"),
                    "--subject-until",
                    SUBJECT_UNTIL,
                    "--report",
                    str(tmp_path / "report.json"),
                ]
            )
            == 1
        )

    def test_an_invalid_subject_until_regex_is_refused(self, workspace: Path) -> None:
        rc = main(
            [
                "decompose-page",
                SOURCE_UID,
                "--path",
                str(workspace),
                "--subject-until",
                "(unclosed",
                "--report",
                str(workspace / "report.json"),
            ]
        )
        assert rc == 1
        assert not (workspace / "report.json").exists()

    def test_a_missing_resolutions_file_is_refused_before_the_lock(
        self, workspace: Path
    ) -> None:
        before = _digests(workspace / "wiki")
        argv = _apply_argv(workspace)
        argv[argv.index("--resolutions") + 1] = str(workspace / "absent.json")
        assert main(argv) == 1
        assert _digests(workspace / "wiki") == before
        assert not (workspace / runlock.LOCKFILE_NAME).exists()

    def test_a_missing_rewrite_body_file_is_refused(self, workspace: Path) -> None:
        before = _digests(workspace / "wiki")
        argv = _apply_argv(workspace)
        argv[argv.index("--rewrite-body") + 1] = str(workspace / "absent.md")
        assert main(argv) == 1
        assert _digests(workspace / "wiki") == before
