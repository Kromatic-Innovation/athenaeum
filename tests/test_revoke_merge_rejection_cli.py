# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum decisions revoke-merge-rejection`` (issue athenaeum#2546 AC3).

The never-merge link (``merge_rejected_with:``, athenaeum#715/athenaeum#2546) is
honoured by every merge path with NO expiry and no config gate — revoking it
is therefore an explicit operator action, not something that happens as a
side effect of any other command. This mirrors ``decisions
challenge-coordinate``'s shape (dry-run by default, ``--apply`` writes) but
targets the relationship frontmatter directly, since
``challenge-coordinate`` stale-marks comparator verdicts over a revoked
COORDINATE answer and has no concept of this field.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from athenaeum.cli import main as cli_main
from athenaeum.models import parse_frontmatter
from athenaeum.pending_merges import find_merge_rejection_conflict


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def _write(path: Path, *, name: str, rejected_with: list[str] | None = None) -> None:
    lines = ["---", f"name: {name}", "type: concept"]
    if rejected_with:
        quoted = ", ".join(f'"{n}"' for n in rejected_with)
        lines.append(f"merge_rejected_with: [{quoted}]")
    lines.append("---")
    lines.append("body\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_dry_run_reports_plan_and_writes_nothing(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page_a = wiki / "a.md"
    page_b = wiki / "b.md"
    _write(page_a, name="A", rejected_with=["B"])
    _write(page_b, name="B", rejected_with=["A"])

    rc, out = _run(
        [
            "decisions",
            "revoke-merge-rejection",
            "--path",
            str(tmp_path),
            "--json",
            "--a",
            "a",
            "--b",
            "b",
        ]
    )
    assert rc == 0
    plan = json.loads(out)
    assert plan == {
        "ok": True,
        "applied": False,
        "a": str(page_a),
        "b": str(page_b),
        "a_changed": True,
        "b_changed": True,
    }
    # Nothing written.
    a_meta, _ = parse_frontmatter(page_a.read_text(encoding="utf-8"))
    b_meta, _ = parse_frontmatter(page_b.read_text(encoding="utf-8"))
    assert a_meta.get("merge_rejected_with") == ["B"]
    assert b_meta.get("merge_rejected_with") == ["A"]


def test_apply_removes_the_link_from_both_sides(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page_a = wiki / "a.md"
    page_b = wiki / "b.md"
    _write(page_a, name="A", rejected_with=["B"])
    _write(page_b, name="B", rejected_with=["A"])

    rc, out = _run(
        [
            "decisions",
            "revoke-merge-rejection",
            "--path",
            str(tmp_path),
            "--json",
            "--a",
            "a",
            "--b",
            "b",
            "--apply",
        ]
    )
    assert rc == 0
    assert json.loads(out)["applied"] is True

    a_meta, _ = parse_frontmatter(page_a.read_text(encoding="utf-8"))
    b_meta, _ = parse_frontmatter(page_b.read_text(encoding="utf-8"))
    assert "merge_rejected_with" not in a_meta
    assert "merge_rejected_with" not in b_meta


def test_only_one_side_carries_the_link_still_revokes_it(tmp_path: Path) -> None:
    """athenaeum#715-era pages only ever got the link on ONE side — revoke
    must still clear that one side."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page_a = wiki / "a.md"
    page_b = wiki / "b.md"
    _write(page_a, name="A", rejected_with=["B"])
    _write(page_b, name="B")  # no link on B at all

    rc, out = _run(
        [
            "decisions",
            "revoke-merge-rejection",
            "--path",
            str(tmp_path),
            "--json",
            "--a",
            "a",
            "--b",
            "b",
            "--apply",
        ]
    )
    assert rc == 0
    plan = json.loads(out)
    assert plan["a_changed"] is True
    assert plan["b_changed"] is False
    a_meta, _ = parse_frontmatter(page_a.read_text(encoding="utf-8"))
    assert "merge_rejected_with" not in a_meta


def test_refuses_when_pair_carries_no_link(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page_a = wiki / "a.md"
    page_b = wiki / "b.md"
    _write(page_a, name="A")
    _write(page_b, name="B")

    rc, out = _run(
        [
            "decisions",
            "revoke-merge-rejection",
            "--path",
            str(tmp_path),
            "--json",
            "--a",
            "a",
            "--b",
            "b",
        ]
    )
    assert rc == 2
    assert json.loads(out)["ok"] is False


def test_revoked_pair_becomes_proposable_again(tmp_path: Path) -> None:
    """athenaeum#2546 AC3's own test: before revocation, propose-fold
    refuses the pair (test_refuses_rejected_pair in
    test_merges_propose_fold_cli.py); after revocation, the SAME pair is no
    longer flagged as a conflict at all.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page_a = wiki / "a.md"
    page_b = wiki / "b.md"
    _write(page_a, name="A", rejected_with=["B"])
    _write(page_b, name="B", rejected_with=["A"])

    assert find_merge_rejection_conflict([page_a, page_b]) is not None

    rc, _out = _run(
        [
            "decisions",
            "revoke-merge-rejection",
            "--path",
            str(tmp_path),
            "--a",
            "a",
            "--b",
            "b",
            "--apply",
        ]
    )
    assert rc == 0

    assert find_merge_rejection_conflict([page_a, page_b]) is None

    # And propose-fold, refused before revocation, now succeeds (dry-run).
    rc_fold, out_fold = _run(
        [
            "decisions",
            "propose-fold",
            "--path",
            str(tmp_path),
            "--json",
            "--into",
            "a",
            "--source",
            "b",
        ]
    )
    assert rc_fold == 0, out_fold
    assert json.loads(out_fold)["ok"] is True
