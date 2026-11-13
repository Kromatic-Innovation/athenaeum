# SPDX-License-Identifier: Apache-2.0
"""The oversized-page backlog in status and the run summary (issue
athenaeum#2536).

One class per acceptance criterion, named for it, so a reader can map a
failure back to the contract it broke:

- ``TestBacklogBlockInStatus`` -- AC1: one block, built from the
  athenaeum#2534 candidate scan, in the chars vocabulary of
  ``librarian.page_size_threshold_chars`` / ``librarian.oversize_hard_ceiling_chars``.
- ``TestBacklogCounts`` -- AC2: the fixture the issue specifies, and the
  retired-page exclusion.
- ``TestOneWalkNotTwo`` -- AC3: status reads each wiki page EXACTLY ONCE
  for page sizes, and the superseded byte-based line is marked deprecated
  in the output.
- ``TestNotAvailableNeverZero`` -- AC4.
- ``TestStatusJson`` -- AC5.
- ``TestRunSummaryEntry`` -- AC6.
- ``TestPageNamesAreVerboseOnly`` -- AC7.
- ``TestChangelog`` -- AC8.

Every fixture derives its paths from ``tmp_path``; none hard-codes an
absolute home directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import render_frontmatter
from athenaeum.run_summary_log import (
    PAGE_RECOMPILE_LEDGER_FIELDS,
    build_page_recompile_entry,
    build_run_summary_ledger_record,
    read_run_summary_ledger,
    write_run_summary_record,
)
from athenaeum.status import (
    PAGE_RECOMPILE_NOT_AVAILABLE,
    PAGE_RECOMPILE_PENDING_FIELDS,
    PAGE_SIZE_BYTES_REMOVAL_RELEASE,
    format_status,
    scan_page_backlog,
    status,
)

#: Small enough to keep fixture bodies readable; written into the fixture's
#: own ``athenaeum.yaml`` so the test also proves status reports the
#: RESOLVED knobs rather than the built-in defaults.
_THRESHOLD_CHARS = 200
_CEILING_CHARS = 800


def _write_page(wiki: Path, filename: str, *, body: str, **meta: object) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(render_frontmatter(dict(meta)) + body, encoding="utf-8")
    return path


def _seed(tmp_path: Path) -> Path:
    """The AC2 fixture: two flagged pages (one of them over the hard
    ceiling), one blocked page, one retired oversized page, and one
    ordinary small page. Returns the knowledge root."""
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (root / "raw" / "sessions").mkdir(parents=True)
    (root / "athenaeum.yaml").write_text(
        "librarian:\n"
        f"  page_size_threshold_chars: {_THRESHOLD_CHARS}\n"
        f"  oversize_hard_ceiling_chars: {_CEILING_CHARS}\n",
        encoding="utf-8",
    )

    # Flagged, over the threshold, UNDER the ceiling.
    _write_page(
        wiki,
        "aaaa1111-flagged-small.md",
        body="a" * (_THRESHOLD_CHARS + 50),
        uid="aaaa1111",
        type="reference",
        name="Flagged Small Page",
        needs_split=True,
    )
    # Flagged AND over the hard ceiling -- counts in BOTH subsets.
    _write_page(
        wiki,
        "bbbb2222-flagged-huge.md",
        body="b" * (_CEILING_CHARS + 50),
        uid="bbbb2222",
        type="reference",
        name="Flagged Huge Page",
        needs_split=True,
    )
    # Blocked: flagged but carries no uid, so the pass cannot act on it.
    (wiki / "cccc3333-no-uid.md").write_text(
        "---\nneeds_split: true\nname: Blocked No Uid Page\n---\n\nShort body.\n",
        encoding="utf-8",
    )
    # Retired AND oversized -- must not be counted at all.
    _write_page(
        wiki,
        "dddd4444-retired-huge.md",
        body="d" * (_CEILING_CHARS + 50),
        uid="dddd4444",
        type="reference",
        name="Retired Huge Page",
        retired=True,
        needs_split=True,
    )
    # An ordinary, healthy page.
    _write_page(
        wiki,
        "eeee5555-small.md",
        body="fine",
        uid="eeee5555",
        type="reference",
        name="Small Page",
    )
    return root


def _backlog(tmp_path: Path) -> dict[str, object]:
    return dict(status(_seed(tmp_path))["page_recompile"])


# ---------------------------------------------------------------------------
# AC1 / AC2
# ---------------------------------------------------------------------------


class TestBacklogCounts:
    def test_fixture_reports_two_flagged_one_over_ceiling_one_blocked(
        self, tmp_path: Path
    ) -> None:
        """The exact fixture AC2 specifies. ``flagged`` and ``over_ceiling``
        are deliberately NOT disjoint: the huge page is both."""
        block = _backlog(tmp_path)

        assert block["flagged"] == 2
        assert block["over_ceiling"] == 1
        assert block["blocked"] == 1
        assert block["blocked_by_reason"] == {"no_uid": 1}
        # Backlog = flagged OR over threshold, blocked pages excluded (the
        # pass cannot act on them, so they are not backlog it can drain).
        assert block["backlog"] == 2

    def test_a_retired_oversized_page_is_not_counted(self, tmp_path: Path) -> None:
        """A retired page over the hard ceiling contributes to NO count --
        not backlog, not over_ceiling, not blocked. Proven by name, not by
        arithmetic: the retired page must be absent from the name lists
        too, so a future off-by-one in the counts cannot pass this."""
        block = _backlog(tmp_path)

        named = [p["name"] for p in block["pages"]] + [
            p["name"] for p in block["blocked_pages"]
        ]
        assert "dddd4444-retired-huge.md" not in named
        # Positive control: the walk DID see a page of that size, so the
        # exclusion is the retired flag and not a threshold mistake.
        assert "bbbb2222-flagged-huge.md" in named


class TestBacklogBlockInStatus:
    def test_status_prints_one_block_with_the_counts(self, tmp_path: Path) -> None:
        info = status(_seed(tmp_path))
        out = format_status(info)

        assert out.count("Page recompile backlog") == 1
        block = out.split("Page recompile backlog")[1]
        assert "flagged (needs_split: true):         2" in block
        assert "over hard ceiling:                   1" in block
        assert "blocked:                             1" in block
        assert "no_uid: 1" in block

    def test_the_block_speaks_chars_and_names_both_knobs(
        self, tmp_path: Path
    ) -> None:
        """AC1's "in the chars vocabulary of..." -- the block names both
        config knobs and reports the RESOLVED values, so an operator never
        has to guess which thresholds produced the counts."""
        info = status(_seed(tmp_path))
        block = dict(info["page_recompile"])

        assert block["threshold_chars"] == _THRESHOLD_CHARS
        assert block["ceiling_chars"] == _CEILING_CHARS

        out = format_status(info)
        assert "Page recompile backlog (chars):" in out
        assert f"librarian.page_size_threshold_chars={_THRESHOLD_CHARS} chars" in out
        assert f"librarian.oversize_hard_ceiling_chars={_CEILING_CHARS} chars" in out

    def test_the_block_matches_the_standalone_candidate_scan(
        self, tmp_path: Path
    ) -> None:
        """AC1's "built from the candidate scan added in athenaeum#2534" --
        the block is not a second, drifting implementation: it agrees with
        ``find_recompile_candidates`` page for page."""
        from athenaeum.config import load_config
        from athenaeum.page_recompile import find_recompile_candidates

        root = _seed(tmp_path)
        scan = find_recompile_candidates(root / "wiki", load_config(root))
        block = dict(status(root)["page_recompile"])

        assert [p["name"] for p in block["pages"]] == [
            c.path.name for c in scan.candidates
        ]
        assert [p["name"] for p in block["blocked_pages"]] == [
            b.path.name for b in scan.blocked
        ]


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------


class TestOneWalkNotTwo:
    def test_each_wiki_page_is_read_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3, measured rather than argued: instrument ``Path.read_text``
        and assert the page-size walk reads each ``wiki/*.md`` page exactly
        once. Two scans over the same tree (the shape this AC forbids)
        would score 2 for every page."""
        root = _seed(tmp_path)
        reads: dict[str, int] = {}
        real_read_text = Path.read_text

        def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.suffix == ".md" and self.parent.name == "wiki":
                reads[self.name] = reads.get(self.name, 0) + 1
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", counting_read_text)
        scan = scan_page_backlog(
            root / "wiki",
            warn_bytes=8192,
            flag_bytes=16384,
            threshold_chars=_THRESHOLD_CHARS,
            ceiling_chars=_CEILING_CHARS,
        )

        assert reads, "positive control: the walk must have read something"
        assert set(reads.values()) == {1}, reads
        # And the one walk really did produce BOTH reports.
        assert scan.recompile.candidates
        assert scan.pages_warn == [] and scan.pages_flag == []

    def test_the_byte_based_line_is_marked_deprecated(self, tmp_path: Path) -> None:
        """AC3's other half: the superseded ``Oversized pages (warn/flag)``
        line says so in the OUTPUT, and names the removing release."""
        out = format_status(status(_seed(tmp_path)))

        line = next(
            ln for ln in out.splitlines() if ln.startswith("Oversized pages (warn/flag)")
        )
        assert "DEPRECATED" in line
        assert PAGE_SIZE_BYTES_REMOVAL_RELEASE in line

    def test_the_deprecation_release_is_documented(self) -> None:
        """...and in the docs: the generated configuration reference carries
        the same removing release for both byte knobs."""
        reference = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "reference"
            / "configuration.md"
        ).read_text(encoding="utf-8")

        for knob in ("page_warn_bytes", "page_flag_bytes"):
            section = reference.split(f"### `resolve_{knob}`")[1].split("### ")[0]
            assert "DEPRECATED" in section
            assert PAGE_SIZE_BYTES_REMOVAL_RELEASE in section


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------


class TestNotAvailableNeverZero:
    def test_the_pending_fields_are_none_not_zero(self, tmp_path: Path) -> None:
        block = _backlog(tmp_path)

        for field in PAGE_RECOMPILE_PENDING_FIELDS:
            assert field in block, field
            assert block[field] is None, field
            assert block[field] != 0, field

    def test_they_render_as_not_available(self, tmp_path: Path) -> None:
        out = format_status(status(_seed(tmp_path)))
        block = out.split("Page recompile backlog")[1].split("Oversized pages")[0]

        for label in (
            "plans awaiting approval",
            "applied in the last 7 days",
            "nights since progress",
        ):
            line = next(ln for ln in block.splitlines() if label in ln)
            assert line.strip().endswith(PAGE_RECOMPILE_NOT_AVAILABLE), line
            assert not line.strip().endswith("0"), line


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------


class TestStatusJson:
    def test_status_json_carries_the_block_under_one_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)

        rc = main(["status", "--path", str(root), "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        block = payload["page_recompile"]
        assert block["backlog"] == 2
        assert block["flagged"] == 2
        assert block["over_ceiling"] == 1
        assert block["blocked"] == 1
        assert block["threshold_chars"] == _THRESHOLD_CHARS
        assert block["ceiling_chars"] == _CEILING_CHARS

    def test_the_json_numbers_are_the_same_numbers_the_text_block_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """"the SAME numbers": derived from one snapshot, not two
        independently-computed surfaces."""
        root = _seed(tmp_path)
        main(["status", "--path", str(root), "--json"])
        payload = json.loads(capsys.readouterr().out)

        text = format_status(status(root))
        block = text.split("Page recompile backlog")[1]
        json_block = payload["page_recompile"]
        assert (
            f"backlog (flagged or over threshold): {json_block['backlog']}" in block
        )
        assert f"blocked:                             {json_block['blocked']}" in block


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------


class TestRunSummaryEntry:
    def test_the_ledger_record_carries_page_recompile_with_backlog(
        self, tmp_path: Path
    ) -> None:
        ledger_path = tmp_path / "run_summary.jsonl"
        entry = build_page_recompile_entry(_backlog(tmp_path))
        assert entry is not None

        write_run_summary_record(
            [("entity", 1.0, {"reason": "completed"})],
            ledger_path=ledger_path,
            page_recompile=entry,
        )

        records = read_run_summary_ledger(ledger_path)
        assert len(records) == 1
        written = records[0]["page_recompile"]
        assert written["backlog"] == 2
        assert written["flagged"] == 2
        assert written["over_ceiling"] == 1
        assert written["blocked"] == 1

    def test_the_entry_is_counts_only(self, tmp_path: Path) -> None:
        """The durable per-run ledger carries no page NAMES -- same posture
        as ``athenaeum recompile candidates`` (athenaeum#2534 AC8)."""
        entry = build_page_recompile_entry(_backlog(tmp_path))

        assert entry is not None
        assert set(entry) <= set(PAGE_RECOMPILE_LEDGER_FIELDS)
        assert "pages" not in entry and "blocked_pages" not in entry
        assert "Flagged Huge Page" not in json.dumps(entry)

    def test_an_unmeasured_backlog_omits_the_key_rather_than_writing_zero(
        self,
    ) -> None:
        """A run whose scan never happened must not look like a run with an
        empty backlog."""
        assert build_page_recompile_entry(None) is None
        assert build_page_recompile_entry({}) is None
        assert build_page_recompile_entry({"flagged": 3}) is None

        record = build_run_summary_ledger_record(
            [("entity", 1.0, {"reason": "completed"})], page_recompile=None
        )
        assert "page_recompile" not in record

    def test_the_librarian_threads_its_measured_backlog_into_the_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wiring, not just the shape: ``emit_run_summary`` passes the
        backlog the athenaeum#310 guardrail measured (``ctx.page_recompile_backlog``)
        straight through to the durable record."""
        from athenaeum import librarian as librarian_mod
        from tests.test_librarian_run_phases import _make_ctx

        captured: dict[str, object] = {}

        def fake_write(profile: object, **kwargs: object) -> bool:
            captured.update(kwargs)
            return True

        monkeypatch.setattr(librarian_mod, "write_run_summary_record", fake_write)

        ctx = _make_ctx(tmp_path)
        ctx.page_recompile_backlog = _backlog(tmp_path)
        ctx.emit_run_summary()

        assert captured["page_recompile"] is not None
        assert captured["page_recompile"]["backlog"] == 2


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------


class TestPageNamesAreVerboseOnly:
    #: Every page name the fixture could leak.
    _NAMES = (
        "aaaa1111-flagged-small.md",
        "bbbb2222-flagged-huge.md",
        "cccc3333-no-uid.md",
    )

    def test_default_block_prints_no_page_name(self, tmp_path: Path) -> None:
        info = status(_seed(tmp_path))
        block = format_status(info).split("Page recompile backlog")[1].split(
            "Oversized pages"
        )[0]

        for name in self._NAMES:
            assert name not in block, name

    def test_verbose_lists_them(self, tmp_path: Path) -> None:
        info = status(_seed(tmp_path))
        block = format_status(info, verbose=True).split("Page recompile backlog")[
            1
        ].split("Oversized pages")[0]

        for name in self._NAMES:
            assert name in block, name
        assert "blocked pages:" in block

    def test_the_cli_verbose_flag_reaches_the_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed(tmp_path)

        assert main(["status", "--path", str(root)]) == 0
        quiet = capsys.readouterr().out
        assert main(["status", "--path", str(root), "--verbose"]) == 0
        loud = capsys.readouterr().out

        assert "bbbb2222-flagged-huge.md" not in quiet.split("Oversized pages")[0]
        assert "bbbb2222-flagged-huge.md" in loud


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------


class TestChangelog:
    def test_unreleased_names_the_backlog_and_the_deprecation(self) -> None:
        changelog = (
            Path(__file__).resolve().parent.parent / "CHANGELOG.md"
        ).read_text(encoding="utf-8")
        unreleased = changelog.split("## [Unreleased]")[1].split("\n## [")[0]

        assert "athenaeum#2536" in unreleased
        assert "page-recompile backlog" in unreleased
        assert PAGE_SIZE_BYTES_REMOVAL_RELEASE in unreleased
