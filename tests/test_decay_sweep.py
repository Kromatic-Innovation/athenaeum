# SPDX-License-Identifier: Apache-2.0
"""Tests for the deterministic decay sweep (issue athenaeum#904, AC6/AC7).

Mirrors ``tests/test_auto_memory_prune.py``'s structure closely — same
dry-run-report / apply split, same git-recoverability assertions — since
``athenaeum.decay_sweep`` deliberately follows that module's precedent.

Acceptance:
  - ``build_sweep_report`` finds exactly the EXPIRED ``bucket: daily`` pages
    (AC6) and leaves ``weekly``/``durable``/unbucketed/not-yet-expired pages
    alone;
  - ``apply_sweep`` archives ONLY the listed pages via a two-commit git
    sequence and refuses without a ``.git`` (AC6/AC7);
  - archived pages remain recoverable from git history (AC7);
  - the sweep makes zero LLM calls, structurally (no ``client``/model
    parameter anywhere in this module's public signatures).
"""

from __future__ import annotations

import inspect
import subprocess
from datetime import date
from pathlib import Path

import pytest

from athenaeum.decay_sweep import (
    apply_sweep,
    build_sweep_report,
    discover_daily_bucket_pages,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Decay Sweep Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed wiki")


def _page(*, name: str, bucket: str | None, valid_until: str | None, body: str) -> str:
    lines = ["---", f"name: {name}", "type: feedback"]
    if bucket is not None:
        lines.append(f"bucket: {bucket}")
    if valid_until is not None:
        lines.append(f"valid_until: '{valid_until}'")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


@pytest.fixture
def wiki_with_bucket_pages(tmp_path: Path) -> Path:
    knowledge_root = tmp_path / "knowledge"
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True)

    # Expired daily -> KILL.
    (wiki / "status-monday.md").write_text(
        _page(
            name="Monday status",
            bucket="daily",
            valid_until="2020-01-01",
            body="Monday's status, long expired.",
        ),
        encoding="utf-8",
    )
    # Daily, no valid_until -> RETAIN (fail-open, athenaeum#308 posture).
    (wiki / "status-open.md").write_text(
        _page(
            name="Open status",
            bucket="daily",
            valid_until=None,
            body="Daily status with no expiry set.",
        ),
        encoding="utf-8",
    )
    # Daily, not-yet-expired -> RETAIN.
    (wiki / "status-future.md").write_text(
        _page(
            name="Future status",
            bucket="daily",
            valid_until="2099-01-01",
            body="Daily status not yet expired.",
        ),
        encoding="utf-8",
    )
    # Expired weekly -> RETAIN (AC6: only daily is ever swept).
    (wiki / "weekly-report.md").write_text(
        _page(
            name="Weekly report",
            bucket="weekly",
            valid_until="2020-01-01",
            body="An expired WEEKLY page -- must never be swept.",
        ),
        encoding="utf-8",
    )
    # Expired durable -> RETAIN.
    (wiki / "durable-fact.md").write_text(
        _page(
            name="Durable fact",
            bucket="durable",
            valid_until="2020-01-01",
            body="An expired DURABLE page -- must never be swept.",
        ),
        encoding="utf-8",
    )
    # Expired, unbucketed -> RETAIN.
    (wiki / "unbucketed.md").write_text(
        _page(
            name="Unbucketed",
            bucket=None,
            valid_until="2020-01-01",
            body="Expired but no bucket at all -- not this module's business.",
        ),
        encoding="utf-8",
    )

    _git_init(knowledge_root)
    return knowledge_root


class TestDiscoverDailyBucketPages:
    def test_finds_only_daily_bucket_pages(self, wiki_with_bucket_pages: Path) -> None:
        wiki = wiki_with_bucket_pages / "wiki"
        found = {p.name for p in discover_daily_bucket_pages(wiki)}
        assert found == {"status-monday.md", "status-open.md", "status-future.md"}

    def test_missing_wiki_root_is_empty(self, tmp_path: Path) -> None:
        assert discover_daily_bucket_pages(tmp_path / "nonexistent") == []


class TestBuildSweepReport:
    def test_kill_and_retain_lists(self, wiki_with_bucket_pages: Path) -> None:
        wiki = wiki_with_bucket_pages / "wiki"
        report = build_sweep_report(wiki)
        kill_names = {c.path.name for c in report.kill}
        retain_names = {p.name for p, _ in report.retained}

        assert kill_names == {"status-monday.md"}
        assert "status-open.md" in retain_names
        assert "status-future.md" in retain_names
        # scanned only counts bucket:daily pages -- weekly/durable/unbucketed
        # are never even candidates.
        assert report.scanned == 3

    def test_every_kill_has_a_reason(self, wiki_with_bucket_pages: Path) -> None:
        wiki = wiki_with_bucket_pages / "wiki"
        report = build_sweep_report(wiki)
        for cand in report.kill:
            assert cand.reason

    def test_as_of_rewind(self, wiki_with_bucket_pages: Path) -> None:
        # Rewinding to a date before status-monday's valid_until means it
        # was NOT yet expired at that point in history.
        wiki = wiki_with_bucket_pages / "wiki"
        report = build_sweep_report(wiki, as_of=date(2019, 1, 1))
        assert report.kill == []

    def test_never_touches_weekly_or_durable_or_unbucketed(
        self, wiki_with_bucket_pages: Path
    ) -> None:
        wiki = wiki_with_bucket_pages / "wiki"
        report = build_sweep_report(wiki)
        kill_names = {c.path.name for c in report.kill}
        assert "weekly-report.md" not in kill_names
        assert "durable-fact.md" not in kill_names
        assert "unbucketed.md" not in kill_names


class TestApplySweep:
    def test_apply_archives_only_expired_daily(self, wiki_with_bucket_pages: Path) -> None:
        knowledge_root = wiki_with_bucket_pages
        wiki = knowledge_root / "wiki"
        retained_bytes = (wiki / "status-open.md").read_bytes()

        report = build_sweep_report(wiki)
        report = apply_sweep(knowledge_root, report)

        assert report.committed is True
        assert not (wiki / "status-monday.md").exists()
        # Retained pages are byte-identical and untouched.
        assert (wiki / "status-open.md").read_bytes() == retained_bytes
        assert (wiki / "status-future.md").exists()
        assert (wiki / "weekly-report.md").exists()
        assert (wiki / "durable-fact.md").exists()
        assert (wiki / "unbucketed.md").exists()

    def test_removal_is_git_recoverable(self, wiki_with_bucket_pages: Path) -> None:
        knowledge_root = wiki_with_bucket_pages
        wiki = knowledge_root / "wiki"
        report = build_sweep_report(wiki)
        apply_sweep(knowledge_root, report)

        # The page is gone from HEAD (the archive commit) but recoverable
        # from the commit immediately before it -- no Commit A snapshot was
        # needed here (the fixture's initial commit already covers this
        # page byte-for-byte), so HEAD~1 is that initial commit.
        assert not (wiki / "status-monday.md").exists()
        show = _git(knowledge_root, "show", "HEAD~1:wiki/status-monday.md")
        assert "Monday" in show.stdout

    def test_commit_is_scoped_to_kill_list(self, wiki_with_bucket_pages: Path) -> None:
        knowledge_root = wiki_with_bucket_pages
        wiki = knowledge_root / "wiki"
        unrelated = knowledge_root / "unrelated.md"
        unrelated.write_text("pre-staged work\n", encoding="utf-8")
        _git(knowledge_root, "add", "unrelated.md")

        report = build_sweep_report(wiki)
        report = apply_sweep(knowledge_root, report)
        assert report.committed is True

        names = _git(knowledge_root, "show", "--name-only", "--format=", "HEAD")
        assert "unrelated.md" not in names.stdout
        assert "wiki/status-monday.md" in names.stdout
        staged = _git(knowledge_root, "diff", "--cached", "--name-only")
        assert "unrelated.md" in staged.stdout

    def test_empty_kill_list_is_noop(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "status-future.md").write_text(
            _page(name="x", bucket="daily", valid_until="2099-01-01", body="not expired"),
            encoding="utf-8",
        )
        _git_init(knowledge_root)
        head_before = _git(knowledge_root, "rev-parse", "HEAD").stdout.strip()

        report = build_sweep_report(wiki)
        report = apply_sweep(knowledge_root, report)

        assert report.kill == []
        assert report.committed is False
        head_after = _git(knowledge_root, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after

    def test_apply_without_git_refuses(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "status-monday.md").write_text(
            _page(name="x", bucket="daily", valid_until="2020-01-01", body="expired"),
            encoding="utf-8",
        )
        report = build_sweep_report(wiki)
        report = apply_sweep(knowledge_root, report)

        assert report.committed is False
        assert report.errors  # refused: no git repo
        assert (wiki / "status-monday.md").exists()  # nothing removed

    def test_uncommitted_page_is_snapshotted_before_removal(
        self, wiki_with_bucket_pages: Path
    ) -> None:
        # A page written/edited since its last commit must still be
        # recoverable -- the provenance-snapshot commit (Commit A) catches
        # this, not just Commit B's git rm.
        knowledge_root = wiki_with_bucket_pages
        wiki = knowledge_root / "wiki"
        stale_page = wiki / "status-monday.md"
        stale_page.write_text(
            _page(
                name="Monday status",
                bucket="daily",
                valid_until="2020-01-01",
                body="EDITED after the initial commit, never committed.",
            ),
            encoding="utf-8",
        )

        report = build_sweep_report(wiki)
        report = apply_sweep(knowledge_root, report)
        assert report.committed is True

        # Recoverable from the immediately-preceding commit (the snapshot),
        # carrying the EDITED content -- not the stale initial-commit body.
        show = _git(knowledge_root, "show", "HEAD~1:wiki/status-monday.md")
        assert "EDITED after the initial commit" in show.stdout


class TestNoLLMCalls:
    """Structural assertion (issue athenaeum#904 AC6): no function in this module
    accepts an LLM client/provider/model parameter -- there is nothing for an
    LLM call to hang off of."""

    def test_build_sweep_report_has_no_client_param(self) -> None:
        params = inspect.signature(build_sweep_report).parameters
        assert not any(
            name in ("client", "provider", "model", "llm") for name in params
        )

    def test_apply_sweep_has_no_client_param(self) -> None:
        params = inspect.signature(apply_sweep).parameters
        assert not any(
            name in ("client", "provider", "model", "llm") for name in params
        )

    def test_discover_has_no_client_param(self) -> None:
        params = inspect.signature(discover_daily_bucket_pages).parameters
        assert not any(
            name in ("client", "provider", "model", "llm") for name in params
        )
