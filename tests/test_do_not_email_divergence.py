# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``do_not_email`` divergence report (issue athenaeum#960).

Structure mirrors ``tests/test_bounce_divergence.py`` (issue athenaeum#853),
whose report shape this module reuses — same four surface cases, same
empty-vs-unreadable distinction, same public-safety posture.

**Only one direction is a real divergence (issue athenaeum#1039).** The wiki
page is the sole authoring surface, and athenaeum#960's Out-of-scope forbids
backfilling marks onto the excluded surface — so a wiki-only mark
(``marked_on_wiki_not_excluded``) is the design's legal steady state, not a
defect to alert on. Before athenaeum#1039, this module's own predicate (and
``surface-divergence --field do_not_email``'s) alerted on EITHER direction,
which meant alerting on the design's only legal state.

The ``athenaeum do-not-email-divergence`` CLI subcommand this file's tests
once drove was removed by issue athenaeum#1111 (superseded by ``athenaeum
surface-divergence --field do_not_email``, which cron-fleet's nightly sweep
already used for this field). The former ``TestCli`` class — including its
anti-recurrence coverage for the athenaeum#960 exit-code criterion and the
athenaeum#1039 narrowing — is removed with it; that exact coverage already
exists at the CLI level in ``tests/test_surface_divergence.py``
(``TestCli::test_do_not_email_excluded_only_mark_exits_nonzero`` and
``TestCli::test_do_not_email_wiki_only_mark_exits_zero``, plus the
allowance-level ``TestAllowance::test_do_not_email_fails_on_excluded_only_mark``
/ ``test_do_not_email_tolerates_wiki_only_mark``), so nothing is lost. The
module-level tests below (``compute_do_not_email_divergence``,
``render_report``, ``report_as_dict``, the ``diverged`` property) remain in
place — ``athenaeum.surface_divergence`` still wraps them unchanged.

All fixtures are synthetic and built in ``tmp_path``; nothing reads a live
store.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.do_not_email_divergence import (
    SurfaceStatus,
    compute_do_not_email_divergence,
    render_report,
    report_as_dict,
)

PERSON_NAME = "Alex Example"


def _wiki_page(
    wiki_root: Path, *, uid: str = "19052", marked: bool | str | None = None
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: '{uid}'", "type: person", f"name: {PERSON_NAME}"]
    if marked is not None:
        lines.append(f"do_not_email: {marked}")
    lines += ["---", "", "An entity page.", ""]
    path = wiki_root / f"alex-example-{uid}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _contact_record(
    contacts_root: Path, *, uid: str = "19052", marked: bool | str | None = None
) -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: '{uid}'", "pii: true", "emails:", "  - alex@example.org"]
    if marked is not None:
        lines.append(f"do_not_email: {marked}")
    lines += ["---", "", "Archival contact data.", ""]
    path = contacts_root / f"{uid}-alex-example.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestDivergenceCases:
    """The surface cases the issue's anti-recurrence criterion covers."""

    def test_both_populated_and_agreeing(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=True)
        _contact_record(contacts_root, marked=True)

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.wiki.count == 1
        assert report.excluded.count == 1
        assert report.diverged is False
        assert report.marked_on_wiki_not_excluded == []
        assert report.marked_on_excluded_not_wiki == []

    def test_wiki_only_mark_is_the_live_shape_and_diverges(self, tmp_path: Path) -> None:
        """The shape all 4 live marks are in (athenaeum#960's own evidence):
        marked on the wiki page, with an excluded record present but
        carrying nothing."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=True)
        _contact_record(contacts_root)  # present, no mark

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.diverged is True
        assert len(report.marked_on_wiki_not_excluded) == 1
        assert report.marked_on_wiki_not_excluded[0].handle == "19052"
        assert report.marked_on_wiki_not_excluded[0].kind == "uid"
        assert report.marked_on_excluded_not_wiki == []

    def test_excluded_only_mark_diverges_the_other_direction(self, tmp_path: Path) -> None:
        """No production writer takes this path today, but the check must
        still see it — the residual for this field is zero either way."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root)  # present, no mark
        _contact_record(contacts_root, marked=True)

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.diverged is True
        assert report.marked_on_wiki_not_excluded == []
        assert len(report.marked_on_excluded_not_wiki) == 1
        assert report.marked_on_excluded_not_wiki[0].handle == "19052"

    def test_unreadable_surface_is_reported_as_such(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _contact_record(contacts_root, marked=True)
        # wiki_root never created — a missing surface is NOT an empty one.

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.complete is False
        assert report.wiki.status is SurfaceStatus.MISSING
        assert report.wiki.detail is not None
        assert report.excluded.status is SurfaceStatus.READ

    def test_clean_zero_report_is_not_an_error(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root)  # a page, but no do_not_email field
        _contact_record(contacts_root)  # a record, but no mark

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.complete is True
        assert report.clean_zero is True
        assert report.diverged is False

    def test_explicit_false_is_not_a_mark_on_either_surface(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=False)
        _contact_record(contacts_root, marked="false")

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.clean_zero is True

    def test_malformed_scalar_on_either_surface_counts_as_marked(self, tmp_path: Path) -> None:
        """Fail-closed: the divergence check must agree with the reader on
        what counts as marked, or it would miss exactly the case
        `_coerce_do_not_email_flag` exists to catch."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked="family request")
        _contact_record(contacts_root)

        report = compute_do_not_email_divergence(wiki_root, contacts_root)

        assert report.wiki.count == 1
        assert report.diverged is True


class TestEmptyIsNotUnreadable:
    def test_data_distinguishes_them(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        empty = compute_do_not_email_divergence(tmp_path / "empty", tmp_path / "empty")
        missing = compute_do_not_email_divergence(tmp_path / "gone", tmp_path / "absent")

        assert empty.wiki.status is SurfaceStatus.READ
        assert missing.wiki.status is SurfaceStatus.MISSING
        assert empty.complete is True and missing.complete is False

    def test_rendered_text_distinguishes_them(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        empty_text = render_report(
            compute_do_not_email_divergence(tmp_path / "empty", tmp_path / "empty")
        )
        missing_text = render_report(
            compute_do_not_email_divergence(tmp_path / "gone", tmp_path / "absent")
        )

        assert empty_text != missing_text
        assert "NOT READ" in missing_text
        assert "NOT READ" not in empty_text
        assert "INCOMPLETE" in missing_text


class TestOutputIsPublicSafe:
    def _diverged(self, tmp_path: Path) -> tuple[Path, Path]:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=True)
        _contact_record(contacts_root)
        return wiki_root, contacts_root

    def test_rendered_text_leaks_nothing(self, tmp_path: Path) -> None:
        wiki_root, contacts_root = self._diverged(tmp_path)
        text = render_report(compute_do_not_email_divergence(wiki_root, contacts_root))

        assert PERSON_NAME not in text
        assert "alex" not in text.lower()
        assert str(tmp_path) not in text

    def test_json_form_leaks_nothing(self, tmp_path: Path) -> None:
        wiki_root, contacts_root = self._diverged(tmp_path)
        payload = json.dumps(
            report_as_dict(compute_do_not_email_divergence(wiki_root, contacts_root))
        )

        assert PERSON_NAME not in payload
        assert "alex" not in payload.lower()
        assert str(tmp_path) not in payload

    # The former test_no_verbose_mode_exists_to_leak_detail asserted this at
    # the CLI-flag level via the now-deleted `_cmd_do_not_email_divergence`
    # module (issue athenaeum#1111 removed the `do-not-email-divergence`
    # subcommand); the module itself never grew a verbose/detail mode, so
    # nothing regresses.

