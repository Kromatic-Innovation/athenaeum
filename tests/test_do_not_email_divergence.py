# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``do_not_email`` divergence report (issue athenaeum#960).

Structure mirrors ``tests/test_bounce_divergence.py`` (issue athenaeum#853),
whose report shape this module reuses — same four surface cases, same
empty-vs-unreadable distinction, same public-safety posture. The one
deliberate difference this file exists to prove: unlike ``bounce-divergence``,
the CLI here exits non-zero on a real divergence, not only on an unreadable
surface — that is the anti-recurrence criterion issue athenaeum#960 requires,
and the class of defect ``bounce-divergence`` shipped without ("exits 0 even
when diverged"): ``TestCli::test_excluded_only_mark_exits_nonzero`` is the
test that fails against the ``bounce-divergence`` exit-code shape.

**Only one direction is a real divergence (issue athenaeum#1039).** The wiki
page is the sole authoring surface, and athenaeum#960's Out-of-scope forbids
backfilling marks onto the excluded surface — so a wiki-only mark
(``marked_on_wiki_not_excluded``) is the design's legal steady state, not a
defect to alert on. Before athenaeum#1039, this CLI's exit code (like
``surface-divergence --field do_not_email``'s predicate) alerted on EITHER
direction, which meant alerting on the design's only legal state.
``TestCli::test_wiki_only_mark_exits_zero`` is the regression test for that.

All fixtures are synthetic and built in ``tmp_path``; nothing reads a live
store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
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

    def test_no_verbose_mode_exists_to_leak_detail(self) -> None:
        import argparse

        from athenaeum._cmd_do_not_email_divergence import (
            add_do_not_email_divergence_subparser,
        )

        parser = argparse.ArgumentParser()
        add_do_not_email_divergence_subparser(parser.add_subparsers())
        with pytest.raises(SystemExit):
            parser.parse_args(["do-not-email-divergence", "--verbose"])


class TestCli:
    """The shipped surface takes a store root as a parameter, and is
    reachable from the installed CLI (issue athenaeum#960's own criterion)."""

    def test_installed_cli_exposes_the_subcommand(self) -> None:
        """Reachable from the INSTALLED CLI, not only via
        `PYTHONPATH=src python -m athenaeum.cli` — the exact gap athenaeum#960
        reports for `bounce-divergence`."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "from athenaeum.cli import main; main(['--help'])"],
            capture_output=True,
            text=True,
        )
        assert "do-not-email-divergence" in result.stdout

    def test_agreeing_store_exits_zero(self, tmp_path: Path, capsys) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=True)
        _contact_record(contacts_root, marked=True)

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "wiki `do_not_email:` pages: 1" in out
        assert "excluded-surface records: 1" in out

    def test_clean_zero_store_exits_zero(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "wiki").mkdir()
        (tmp_path / "contacts").mkdir()

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(tmp_path / "wiki"),
                "--contacts-root",
                str(tmp_path / "contacts"),
            ]
        )

        assert rc == 0
        assert "neither holds a do_not_email mark" in capsys.readouterr().out

    def test_wiki_only_mark_exits_zero(self, tmp_path: Path, capsys) -> None:
        """Issue athenaeum#1039's regression case: the design's only legal
        steady state — wiki carries a mark, excluded surface carries zero
        records (the live shape all production marks are in) — must exit 0,
        not `EXIT_DIVERGED`. Before athenaeum#1039 this asserted `rc != 0`,
        which was the exact defect the issue reports (observed live as 4
        wiki marks / 0 excluded records incorrectly exiting 3)."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=True)
        _contact_record(contacts_root)  # present, unmarked — the live shape

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "marked on wiki, not on excluded: 1" in out

    def test_excluded_only_mark_exits_nonzero(self, tmp_path: Path, capsys) -> None:
        """The anti-recurrence criterion itself: unlike `bounce-divergence`
        (which returns 0 whenever both surfaces were merely READABLE,
        regardless of whether they agree), this command's exit code reflects
        the real divergence — the excluded surface newly carrying the field —
        not just readability."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root)  # present, unmarked
        _contact_record(contacts_root, marked=True)

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert rc != 0
        assert rc not in (0,)
        out = capsys.readouterr().out
        assert "marked on excluded, not on wiki: 1" in out

    def test_unreadable_surface_exits_nonzero_and_differently_from_diverged(
        self, tmp_path: Path, capsys
    ) -> None:
        (tmp_path / "contacts").mkdir()

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(tmp_path / "gone"),
                "--contacts-root",
                str(tmp_path / "contacts"),
            ]
        )

        assert rc == 2  # distinct from 0 (agree), and from 3 (diverged)
        assert "NOT READ" in capsys.readouterr().out

    def test_json_output(self, tmp_path: Path, capsys) -> None:
        """Uses the real divergence direction (excluded-only mark) so `rc`
        reflects `EXIT_DIVERGED`, exercising the JSON payload shape on the
        one case this command still alerts on post-athenaeum#1039."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root)
        _contact_record(contacts_root, marked=True)

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
                "--json",
            ]
        )

        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["complete"] is True
        assert payload["diverged"] is True
        assert len(payload["marked_on_excluded_not_wiki"]) == 1

    def test_json_output_wiki_only_mark_diverged_but_exits_zero(
        self, tmp_path: Path, capsys
    ) -> None:
        """`diverged` in the JSON payload stays purely descriptive (the two
        surfaces DO differ) even though the exit code is 0 — only
        `marked_on_excluded_not_wiki` drives the exit code post-athenaeum#1039."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _wiki_page(wiki_root, marked=True)
        _contact_record(contacts_root)

        rc = main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
                "--json",
            ]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["complete"] is True
        assert payload["diverged"] is True
        assert len(payload["marked_on_wiki_not_excluded"]) == 1
        assert payload["marked_on_excluded_not_wiki"] == []

    def test_report_writes_to_neither_surface(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        record = _contact_record(contacts_root, marked=True)
        page = _wiki_page(wiki_root, marked=True)
        before = (record.read_text(encoding="utf-8"), page.read_text(encoding="utf-8"))

        main(
            [
                "do-not-email-divergence",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert (record.read_text(encoding="utf-8"), page.read_text(encoding="utf-8")) == before
