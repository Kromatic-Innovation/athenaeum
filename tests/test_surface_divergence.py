# SPDX-License-Identifier: Apache-2.0
"""Tests for the generalized per-field surface-divergence guard (issue athenaeum#963).

Structure follows the issue's own acceptance criteria:

- ``TestRegistry`` — field-parameterization: adding ``bounced`` and
  ``do_not_email`` is a descriptor registration, and the ``bounced``
  descriptor's report is byte-for-byte what ``bounce_divergence`` produced
  directly (the AC protecting issue athenaeum#853's report from
  regressing).
- ``TestAllowance`` — each field's declared allowance, exercised at the
  boundary: a divergence WITHIN the declared allowance must not fail, and a
  divergence BEYOND it must. This is the anti-recurrence test for the
  built fixture: `bounced` tolerates a wiki-only entry but not a pii-mark-
  only entry; `do_not_email` tolerates neither direction.
- ``TestCli`` — ``athenaeum surface-divergence``'s exit-code contract: 0
  clean, 2 unreadable, 3 diverged-beyond-allowance, and ``--report-only``
  preserving the pre-athenaeum#963 exit-0-unless-unreadable contract.
- ``TestUnregisteredField`` — the check refuses to guess at an unregistered
  field's allowance.
- ``TestInstalledCli`` — reachable from a FRESH, non-editable install via
  the console-script entry point, not only `PYTHONPATH=src` — the literal
  gap issue athenaeum#963 reproduces against `bounce-divergence`.

All fixtures are synthetic and built in ``tmp_path``; nothing reads a live
store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum.bounce_divergence import (
    compute_divergence,
    render_report as bounce_render_report,
    report_as_dict as bounce_report_as_dict,
)
from athenaeum.cli import main
from athenaeum.pii import mark_bounced
from athenaeum.surface_divergence import (
    EXIT_DIVERGED,
    EXIT_SURFACE_UNREADABLE,
    field_names,
    get_field,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

ADDRESS = "alex@example.org"
PERSON_NAME = "Alex Example"


def _person_record(contacts_root: Path, *, uid: str = "19052") -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / f"{uid}-alex-example.md"
    path.write_text(
        "---\n"
        f"uid: '{uid}'\n"
        f"name: {PERSON_NAME} — contact record\n"
        f"contact_of: {PERSON_NAME}\n"
        "pii: true\n"
        "emails:\n"
        f"  - {ADDRESS}\n"
        "---\n\nArchival contact data.\n",
        encoding="utf-8",
    )
    return path


def _bounce_wiki_page(wiki_root: Path, *, uid: str = "19052", bounced: str | None = None) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: '{uid}'", "type: person", f"name: {PERSON_NAME}"]
    if bounced is not None:
        lines.append(f"bounced: {bounced}")
    lines += ["---", "", "An entity page.", ""]
    path = wiki_root / f"alex-example-{uid}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _mark_bounced(contacts_root: Path, identifier: str = ADDRESS) -> None:
    mark_bounced(
        contacts_root,
        identifier,
        diagnostic="550 5.1.1 user unknown",
        observed_at="2026-08-05",
        source="script:voltaire-bounce-relay",
    )


def _dne_wiki_page(
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


def _dne_contact_record(
    contacts_root: Path, *, uid: str = "19052", marked: bool | str | None = None
) -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: '{uid}'", "pii: true", "emails:", f"  - {ADDRESS}"]
    if marked is not None:
        lines.append(f"do_not_email: {marked}")
    lines += ["---", "", "Archival contact data.", ""]
    path = contacts_root / f"{uid}-alex-example.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestRegistry:
    """Field-parameterization: registering a field, not copying a module."""

    def test_both_fields_are_registered(self) -> None:
        assert field_names() == ["bounced", "do_not_email"]

    def test_bounced_report_is_unchanged_from_bounce_divergence_directly(
        self, tmp_path: Path
    ) -> None:
        """The AC: `bounced`'s numbers and JSON keys are unchanged."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        _bounce_wiki_page(wiki_root, bounced="MailboxDoesNotExist")
        _mark_bounced(contacts_root)

        direct = compute_divergence(wiki_root, contacts_root)
        spec = get_field("bounced")
        via_registry = spec.compute(wiki_root, contacts_root, None)

        assert spec.report_as_dict(via_registry) == bounce_report_as_dict(direct)
        assert spec.render_report(via_registry) == bounce_render_report(direct)

    def test_get_field_raises_for_unregistered_name(self) -> None:
        with pytest.raises(KeyError):
            get_field("nonexistent_field")


class TestAllowance:
    """Each field's declared allowance, exercised at the boundary."""

    def test_bounced_tolerates_wiki_only_entry(self, tmp_path: Path) -> None:
        """Wiki-surface entry with no pii mark — the documented asymmetry."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir()
        _bounce_wiki_page(wiki_root, bounced="MailboxDoesNotExist")

        spec = get_field("bounced")
        report = spec.compute(wiki_root, contacts_root, None)

        assert report.on_wiki_not_marked  # diverges...
        assert not report.marked_not_on_wiki
        assert spec.exceeds_allowance(report) is False  # ...but is tolerated

    def test_bounced_fails_on_pii_mark_with_no_wiki_entry(self, tmp_path: Path) -> None:
        """Pii mark with no wiki entry — zero tolerance in this direction."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        wiki_root.mkdir()
        _mark_bounced(contacts_root)

        spec = get_field("bounced")
        report = spec.compute(wiki_root, contacts_root, None)

        assert report.marked_not_on_wiki
        assert spec.exceeds_allowance(report) is True

    def test_do_not_email_fails_on_wiki_only_mark(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _dne_wiki_page(wiki_root, marked=True)
        contacts_root.mkdir()

        spec = get_field("do_not_email")
        report = spec.compute(wiki_root, contacts_root, None)

        assert spec.exceeds_allowance(report) is True

    def test_do_not_email_tolerates_nothing(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _dne_wiki_page(wiki_root, marked=True)
        _dne_contact_record(contacts_root, marked=True)

        spec = get_field("do_not_email")
        report = spec.compute(wiki_root, contacts_root, None)

        assert spec.exceeds_allowance(report) is False


class TestCli:
    """``athenaeum surface-divergence``'s exit-code contract."""

    def test_clean_store_exits_zero(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "wiki").mkdir()
        (tmp_path / "contacts").mkdir()

        rc = main(
            [
                "surface-divergence",
                "--field",
                "do_not_email",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(tmp_path / "wiki"),
                "--contacts-root",
                str(tmp_path / "contacts"),
            ]
        )

        assert rc == 0

    def test_bounced_tolerated_divergence_exits_zero(self, tmp_path: Path, capsys) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir()
        _bounce_wiki_page(wiki_root, bounced="MailboxDoesNotExist")

        rc = main(
            [
                "surface-divergence",
                "--field",
                "bounced",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert rc == 0

    def test_bounced_untolerated_divergence_exits_nonzero(self, tmp_path: Path, capsys) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        wiki_root.mkdir()
        _mark_bounced(contacts_root)

        rc = main(
            [
                "surface-divergence",
                "--field",
                "bounced",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert rc == EXIT_DIVERGED

    def test_do_not_email_diverged_exits_nonzero(self, tmp_path: Path, capsys) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _dne_wiki_page(wiki_root, marked=True)
        contacts_root.mkdir()

        rc = main(
            [
                "surface-divergence",
                "--field",
                "do_not_email",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
            ]
        )

        assert rc == EXIT_DIVERGED

    def test_unreadable_surface_exits_distinctly(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "contacts").mkdir()

        rc = main(
            [
                "surface-divergence",
                "--field",
                "do_not_email",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(tmp_path / "gone"),
                "--contacts-root",
                str(tmp_path / "contacts"),
            ]
        )

        assert rc == EXIT_SURFACE_UNREADABLE
        assert rc not in (0, EXIT_DIVERGED)

    def test_report_only_preserves_old_exit_zero_on_divergence(
        self, tmp_path: Path, capsys
    ) -> None:
        """``--report-only`` is the ONLY way to get the pre-athenaeum#963
        exit-0-on-divergence contract from this command."""
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _dne_wiki_page(wiki_root, marked=True)
        contacts_root.mkdir()

        rc = main(
            [
                "surface-divergence",
                "--field",
                "do_not_email",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(wiki_root),
                "--contacts-root",
                str(contacts_root),
                "--report-only",
            ]
        )

        assert rc == 0

    def test_report_only_still_fails_on_unreadable_surface(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "contacts").mkdir()

        rc = main(
            [
                "surface-divergence",
                "--field",
                "do_not_email",
                "--path",
                str(tmp_path),
                "--wiki-root",
                str(tmp_path / "gone"),
                "--contacts-root",
                str(tmp_path / "contacts"),
                "--report-only",
            ]
        )

        assert rc == EXIT_SURFACE_UNREADABLE

    def test_json_output(self, tmp_path: Path, capsys) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _dne_wiki_page(wiki_root, marked=True)
        _dne_contact_record(contacts_root, marked=True)

        rc = main(
            [
                "surface-divergence",
                "--field",
                "do_not_email",
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
        assert payload["diverged"] is False

    def test_invalid_field_choice_is_rejected_by_argparse(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            main(["surface-divergence", "--field", "nonexistent_field", "--path", str(tmp_path)])


class TestUnregisteredField:
    def test_get_field_error_names_registered_fields(self) -> None:
        with pytest.raises(KeyError, match="bounced"):
            get_field("nope")


class TestInstalledCli:
    """Reachable from a fresh, non-editable install (issue athenaeum#963's
    own criterion — the same gap issue athenaeum#963 reproduces for
    `bounce-divergence`: `PYTHONPATH=src python -m athenaeum.cli` works, the
    installed console script did not)."""

    def test_installed_cli_lists_the_subcommand(self) -> None:
        """Cheap smoke check that does not need a fresh venv: the subcommand
        is registered on the parser reachable via a plain subprocess
        import (mirrors the precedent in
        tests/test_do_not_email_divergence.py)."""
        result = subprocess.run(
            [sys.executable, "-c", "from athenaeum.cli import main; main(['--help'])"],
            capture_output=True,
            text=True,
        )
        assert "surface-divergence" in result.stdout

    def test_fresh_install_console_script_reaches_surface_divergence(
        self, tmp_path: Path
    ) -> None:
        """The real acceptance bar: build a wheel, install it (--no-deps,
        --system-site-packages so runtime deps come from the environment
        already provisioned by CI's `pip install -e ".[dev,vector]"` rather
        than a second network fetch — this repo's suite never hits the
        network, see the `eval` marker note in pyproject.toml), and run the
        INSTALLED console script with PYTHONPATH stripped. This is the exact
        failure issue athenaeum#963 reports for `bounce-divergence`:
        reachable only via `PYTHONPATH=src python -m athenaeum.cli`, not the
        shipped `athenaeum` command.
        """
        pytest.importorskip("build", reason="`build` not installed; `pip install athenaeum[dev]`")

        outdir = tmp_path / "dist"
        outdir.mkdir()
        build_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(outdir),
                str(_REPO_ROOT),
            ],
            capture_output=True,
            text=True,
        )
        assert build_result.returncode == 0, (
            f"wheel build failed:\nstdout:\n{build_result.stdout}\n"
            f"stderr:\n{build_result.stderr}"
        )
        wheels = list(outdir.glob("*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"

        venv_dir = tmp_path / "venv"
        venv_result = subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
            capture_output=True,
            text=True,
        )
        assert venv_result.returncode == 0, venv_result.stderr

        pip_bin = venv_dir / "bin" / "pip"
        install_result = subprocess.run(
            [str(pip_bin), "install", "--no-deps", "--ignore-requires-python", str(wheels[0])],
            capture_output=True,
            text=True,
        )
        assert install_result.returncode == 0, install_result.stderr

        athenaeum_bin = venv_dir / "bin" / "athenaeum"
        assert athenaeum_bin.is_file(), "console-script entry point was not installed"

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        run_result = subprocess.run(
            [str(athenaeum_bin), "surface-divergence", "--help"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert run_result.returncode == 0, (
            f"installed console script could not reach surface-divergence:\n"
            f"stdout:\n{run_result.stdout}\nstderr:\n{run_result.stderr}"
        )
        assert "--field" in run_result.stdout
