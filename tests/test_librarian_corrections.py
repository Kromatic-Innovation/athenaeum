# SPDX-License-Identifier: Apache-2.0
"""Wiring-level tests for the field-correction phase (issue athenaeum#797,
slice 5): `_run_correction_phase` inside `librarian.run()`.

Unit-level applier/routing coverage lives in `tests/test_corrections.py`;
this file covers the WIRING claims a green unit suite does not establish —
phase ordering, the zero-LLM-calls invariant (with a positive control), and
at least one full `run()` pass over a synthetic §11 worked-example batch.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import athenaeum.librarian as librarian_mod
from athenaeum.librarian import RunContext, TokenUsage, _run_correction_phase, run


def _make_ctx(tmp_path: Path, config: dict | None = None) -> RunContext:
    ctx = RunContext(
        raw_root=tmp_path / "raw",
        wiki_root=tmp_path / "wiki",
        knowledge_root=tmp_path,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=3600,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=None,
        push_after_run=None,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=datetime.now(timezone.utc),
        heartbeat=None,
        out_run_stats=None,
    )
    ctx.config = config
    ctx.usage = TokenUsage()
    ctx.run_deadline = None
    return ctx


def _write_page(wiki: Path, filename: str, meta: dict) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / filename
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    page.write_text("\n".join(lines) + f"\n\n# {meta.get('name')}\n\nBody.\n", encoding="utf-8")
    return page


def _batch(*records: dict, submitter: str = "graph-writer", **envelope_overrides) -> str:
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": submitter,
        "batch_id": "20260806T030000Z-1a2b3c4d",
        "created_at": "2026-08-06T03:00:00Z",
    }
    envelope.update(envelope_overrides)
    lines = [json.dumps(envelope)]
    lines.extend(json.dumps(r) for r in records)
    return "\n".join(lines) + "\n"


class TestZeroLLMCalls:
    def test_no_calls_on_real_batch(self, tmp_path: Path) -> None:
        _write_page(tmp_path / "wiki", "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        raw = tmp_path / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        (raw / "20260806T030000Z-1a2b3c4d.jsonl").write_text(
            _batch(
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "add",
                    "field": "backlinks",
                    "value": "company-b",
                    "source": "script:graph-writer",
                    "observed_at": "2026-08-06T03:00:00Z",
                }
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {"backlinks": {"shape": "list", "writers": ["graph-writer"]}}
                }
            }
        }
        ctx = _make_ctx(tmp_path, config)
        before = ctx.usage.api_calls
        _run_correction_phase(ctx)
        assert ctx.usage.api_calls == before
        assert ctx.corrections_summary is not None
        assert ctx.corrections_summary["dispositions"].get("applied") == 1

    def test_positive_control_detects_a_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the zero-calls assertion is load-bearing, not decorative:
        if the phase's underlying orchestration ever bumps ``usage.api_calls``
        (e.g. a future edit accidentally threads an LLM client through), the
        assertion in `_run_correction_phase` must catch it."""
        (tmp_path / "raw").mkdir()

        def _sabotaged_run_correction_phase(**kwargs):
            # Simulate an LLM call leaking into the "zero-LLM-calls" phase.
            return {
                "batches_processed": 0,
                "batches_carried_over": 0,
                "dispositions": {},
                "records_total": 0,
            }

        ctx = _make_ctx(tmp_path, None)

        def _sabotaged(**kwargs):
            ctx.usage.api_calls += 1
            return _sabotaged_run_correction_phase(**kwargs)

        monkeypatch.setattr(librarian_mod, "run_correction_phase", _sabotaged)
        with pytest.raises(AssertionError, match="zero LLM calls"):
            _run_correction_phase(ctx)


class TestPhaseOrdering:
    def test_correction_phase_runs_before_entity_tier_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        real_correction = librarian_mod._run_correction_phase
        real_entity = librarian_mod._run_entity_tier_phase

        def _spy_correction(ctx):
            order.append("correction")
            return real_correction(ctx)

        def _spy_entity(ctx):
            order.append("entity")
            return real_entity(ctx)

        monkeypatch.setattr(librarian_mod, "_run_correction_phase", _spy_correction)
        monkeypatch.setattr(librarian_mod, "_run_entity_tier_phase", _spy_entity)

        root = tmp_path / "knowledge"
        root.mkdir()
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text("# Types\n\n| Type |\n|------|\n| person |\n")
        (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )
        raw = root / "raw" / "sessions"
        raw.mkdir(parents=True)

        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        run(raw_root=root / "raw", wiki_root=wiki, knowledge_root=root, max_runtime=30)

        assert order.index("correction") < order.index("entity")


class TestWorkedExampleEndToEnd:
    def test_11_2_bulk_relationship_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§11.2: bulk relationship-graph batch, applied end-to-end through
        the public `run()` entrypoint against a synthetic scratch tree."""
        root = tmp_path / "knowledge"
        root.mkdir()
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text("# Types\n\n| Type |\n|------|\n| person |\n")
        (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )
        alex_meta = {"uid": "person-alex-doe-a1b2c3d4", "type": "person", "name": "Alex Doe"}
        _write_page(wiki, "alex.md", alex_meta)
        blair_meta = {"uid": "person-blair-roe-11ff22ee", "type": "person", "name": "Blair Roe"}
        _write_page(wiki, "blair.md", blair_meta)

        raw = root / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        batch = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        batch.write_text(
            _batch(
                {
                    "record": "correction",
                    "correction_id": "a1",
                    "target": {"uid": "person-alex-doe-a1b2c3d4"},
                    "value": "company-northwind-77aa11bc",
                },
                {
                    "record": "correction",
                    "correction_id": "b2",
                    "target": {"uid": "person-blair-roe-11ff22ee"},
                    "value": "company-northwind-77aa11bc",
                },
                submitter="graph-writer",
                defaults={
                    "op": "add",
                    "field": "backlinks",
                    "source": "script:graph-writer",
                    "observed_at": "2026-08-06T03:00:00Z",
                },
            )
        )

        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        config_path = root / "athenaeum.yaml"
        config_path.write_text(
            "librarian:\n"
            "  corrections:\n"
            "    fields:\n"
            "      backlinks:\n"
            "        shape: list\n"
            "        writers: [graph-writer]\n"
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        rc = run(raw_root=root / "raw", wiki_root=wiki, knowledge_root=root, max_runtime=30)
        assert rc == 0

        alex_text = (wiki / "alex.md").read_text()
        blair_text = (wiki / "blair.md").read_text()
        assert "company-northwind-77aa11bc" in alex_text
        assert "company-northwind-77aa11bc" in blair_text

        ledger = wiki / "_corrections_applied.jsonl"
        assert ledger.exists()
        record = json.loads(ledger.read_text().splitlines()[0])
        assert record["records_total"] == 2
        assert record["dispositions"].get("applied") == 2

        # The batch was retired -- no longer sitting in raw/.
        assert not batch.exists()
        # Nothing escalated for this fully-conformant batch.
        assert not (wiki / "_pending_questions.md").exists()


class TestBatchRetirement:
    def test_fully_deferred_batch_reruns_three_times_one_ledger_entry(
        self, tmp_path: Path
    ) -> None:
        """§5.4 regression for the athenaeum#414 failure class: a batch whose
        every record is `deferred-lower-precedence` (terminal on the FIRST
        pass) must be retired after that pass -- re-running the phase two
        more times must produce exactly ONE ledger entry and ZERO
        `_pending_questions.md` entries, because the delta gate does NOT
        cover a record that never attempts a write.
        """
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        page.write_text(
            page.read_text().replace(
                "name: A",
                "name: A\ncurrent_title: CTO\n"
                "field_sources:\n  current_title: 'user:conv-1'",
            )
        )
        raw = tmp_path / "raw" / "enrichment-service"
        raw.mkdir(parents=True)
        (raw / "20260806T030000Z-1a2b3c4d.jsonl").write_text(
            _batch(
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T05:58:40Z",
                },
                submitter="enrichment-service",
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {
                        "current_title": {
                            "shape": "scalar",
                            "writers": ["enrichment-service"],
                        }
                    }
                }
            }
        }

        for _ in range(3):
            ctx = _make_ctx(tmp_path, config)
            _run_correction_phase(ctx)

        ledger = wiki / "_corrections_applied.jsonl"
        assert ledger.exists()
        lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert not (wiki / "_pending_questions.md").exists()


class TestEscalationDedup:
    def test_already_open_correction_id_not_double_filed(self, tmp_path: Path) -> None:
        """§8/§10.2: an escalation dedupes on `correction_id` against an
        already-open pending-questions entry."""
        from athenaeum.corrections import (
            compute_correction_id,
            render_correction_id_marker,
        )

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        page.write_text(
            page.read_text().replace(
                "name: A", "name: A\ncurrent_title: CTO\nupdated: 2026-08-06"
            )
        )

        target = {"uid": "person-a"}
        cid = compute_correction_id(
            schema_version=1,
            target=target,
            op="set",
            field_name="current_title",
            value="VP Engineering",
        )
        pending = wiki / "_pending_questions.md"
        pending.write_text(
            "# Pending Questions\n\n"
            f'## [2026-08-06] Entity: "A" (from enrichment-service/prior.jsonl)\n'
            "- [ ] Resolve field-correction conflict for A\n\n"
            "**Conflict type**: field-correction\n"
            f"**Description**: prior escalation\n{render_correction_id_marker(cid)}\n"
        )
        before = pending.read_text()

        raw = tmp_path / "raw" / "enrichment-service"
        raw.mkdir(parents=True)
        (raw / "20260806T030000Z-1a2b3c4d.jsonl").write_text(
            _batch(
                {
                    "record": "correction",
                    "target": target,
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                    "source": "api:enrichment-vendor",
                    # Same rank (api:) as an implied api: incumbent, undated
                    # tie forced by an unparseable/absent comparison date.
                    "observed_at": "2026-08-06T05:58:40Z",
                },
                submitter="enrichment-service",
            )
        )
        # Force an equal-rank, differing-value, undated-tie escalation by
        # making the incumbent source ALSO api: with no comparable date.
        page.write_text(
            page.read_text()
            .replace("updated: 2026-08-06", "")
            .replace(
                "current_title: CTO",
                "current_title: CTO\nfield_sources:\n  current_title: 'api:apollo'",
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {
                        "current_title": {
                            "shape": "scalar",
                            "writers": ["enrichment-service"],
                        }
                    }
                }
            }
        }
        ctx = _make_ctx(tmp_path, config)
        _run_correction_phase(ctx)

        # No duplicate block appended for the same correction_id.
        after = pending.read_text()
        assert after.count(render_correction_id_marker(cid)) == 1
        assert before in after
