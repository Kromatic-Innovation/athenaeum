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

    def _seed_scratch_tree(self, root: Path, meta: dict) -> tuple[Path, Path]:
        """Shared scaffolding for the §11 end-to-end tests: schema tables, one
        entity page, and a git-initialized ``knowledge_root`` (mirrors
        `test_11_2_bulk_relationship_graph`'s exact seeding pattern)."""
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text("# Types\n\n| Type |\n|------|\n| person |\n")
        (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )
        page = _write_page(wiki, "entity.md", meta)
        return wiki, page

    def _seed_git(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

    def test_11_1_delivery_status_flag_monotone_and_pii_routed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§11.1 end-to-end: a delivery-status monitor's `bounced` correction
        is BOTH the monotone case (§6.3) and the sensitivity-routed case
        (§7.1) -- it is set regardless of precedence AND lands on the PII
        surface rather than the entity page it named, even though the
        correction proposed entity frontmatter as the destination."""
        root = tmp_path / "knowledge"
        root.mkdir()
        meta = {"uid": "person-alex", "type": "person", "name": "Alex Doe"}
        wiki, page = self._seed_scratch_tree(root, meta)
        page_before = page.read_text()

        (root / "registry.json").write_text(
            json.dumps(
                {
                    "entities": {
                        "person-alex": {
                            "type": "person",
                            "handles": {"alt_emails": ["alex@example.org"]},
                        }
                    }
                }
            )
        )

        raw = root / "raw" / "delivery-monitor"
        raw.mkdir(parents=True)
        batch = raw / "20260806T140211Z-9f3ac1d2.jsonl"
        batch.write_text(
            _batch(
                {
                    "record": "correction",
                    "target": {"type": "person", "handle": {"alt_emails": "alex@example.org"}},
                    "op": "set",
                    "field": "bounced",
                    "value": "2026-08-06",
                    "note": "permanent delivery failure reported by the receiving server",
                },
                submitter="delivery-monitor",
                defaults={
                    "source": "api:delivery-monitor:2026-08-06",
                    "observed_at": "2026-08-06T14:01:55Z",
                },
            )
        )
        self._seed_git(root)

        config_path = root / "athenaeum.yaml"
        config_path.write_text(
            "librarian:\n"
            "  corrections:\n"
            "    fields:\n"
            "      bounced:\n"
            "        shape: scalar\n"
            "        writers: [delivery-monitor]\n"
            "        monotone: true\n"
            "    sensitive_fields:\n"
            "      bounced: pii\n"
            "storage:\n"
            "  mapping:\n"
            "    pii: excluded\n"
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        rc = run(raw_root=root / "raw", wiki_root=wiki, knowledge_root=root, max_runtime=30)
        assert rc == 0

        # The entity page named by the correction is untouched.
        assert page.read_text() == page_before

        # The fact landed on the PII surface instead -- as the SAME markdown
        # contact-record shape `classify_contact_value`/`iter_contact_records`
        # read and write (issue athenaeum#872), not a parallel `{uid}.json`.
        from athenaeum.models import parse_frontmatter

        surface_file = root / "excluded" / "person-alex.md"
        assert surface_file.exists()
        surface_meta, _ = parse_frontmatter(surface_file.read_text())
        assert surface_meta["bounced"] == "2026-08-06"

        ledger = wiki / "_corrections_applied.jsonl"
        record = json.loads(ledger.read_text().splitlines()[0])
        assert record["dispositions"].get("routed-elsewhere") == 1
        assert not batch.exists()  # retired

    def test_11_3_third_party_enrichment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§11.3: `api:` (rank 3) overwrites a `claude:`-sourced title (rank
        6) end-to-end through `run()`."""
        root = tmp_path / "knowledge"
        root.mkdir()
        meta = {"uid": "person-alex-doe-a1b2c3d4", "type": "person", "name": "Alex Doe"}
        wiki, page = self._seed_scratch_tree(root, meta)
        page.write_text(
            page.read_text().replace(
                "name: Alex Doe",
                "name: Alex Doe\ncurrent_title: Engineer\n"
                "field_sources:\n  current_title: 'claude:session-1'",
            )
        )

        raw = root / "raw" / "enrichment-service"
        raw.mkdir(parents=True)
        batch = raw / "20260806T060000Z-5e6f7a8b.jsonl"
        batch.write_text(
            _batch(
                {
                    "record": "correction",
                    "correction_id": "c3",
                    "target": {"uid": "person-alex-doe-a1b2c3d4"},
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                },
                submitter="enrichment-service",
                defaults={
                    "source": "api:enrichment-vendor:2026-08-06",
                    "observed_at": "2026-08-06T05:58:40Z",
                },
            )
        )
        self._seed_git(root)

        config_path = root / "athenaeum.yaml"
        config_path.write_text(
            "librarian:\n"
            "  corrections:\n"
            "    fields:\n"
            "      current_title:\n"
            "        shape: scalar\n"
            "        writers: [enrichment-service]\n"
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        rc = run(raw_root=root / "raw", wiki_root=wiki, knowledge_root=root, max_runtime=30)
        assert rc == 0

        assert "current_title: VP Engineering" in page.read_text()

        ledger = wiki / "_corrections_applied.jsonl"
        record = json.loads(ledger.read_text().splitlines()[0])
        assert record["dispositions"].get("applied") == 1
        assert not batch.exists()  # retired

    def test_11_4_rolled_up_activity_counters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§11.4: two rollup counters (not the underlying events, §12) applied
        end-to-end through `run()`, both hoisting `op` from envelope defaults
        and naming only `field`/`value` per record."""
        root = tmp_path / "knowledge"
        root.mkdir()
        meta = {"uid": "person-alex-doe-a1b2c3d4", "type": "person", "name": "Alex Doe"}
        wiki, page = self._seed_scratch_tree(root, meta)

        raw = root / "raw" / "cadence-tracker"
        raw.mkdir(parents=True)
        batch = raw / "20260806T070000Z-2c3d4e5f.jsonl"
        batch.write_text(
            _batch(
                {
                    "record": "correction",
                    "correction_id": "e5",
                    "target": {"uid": "person-alex-doe-a1b2c3d4"},
                    "field": "last_contacted_at",
                    "value": "2026-08-04",
                },
                {
                    "record": "correction",
                    "correction_id": "f6",
                    "target": {"uid": "person-alex-doe-a1b2c3d4"},
                    "field": "contact_count_90d",
                    "value": 7,
                },
                submitter="cadence-tracker",
                defaults={
                    "op": "set",
                    "source": "script:cadence-rollup",
                    "observed_at": "2026-08-06T07:00:00Z",
                },
            )
        )
        self._seed_git(root)

        config_path = root / "athenaeum.yaml"
        config_path.write_text(
            "librarian:\n"
            "  corrections:\n"
            "    fields:\n"
            "      last_contacted_at:\n"
            "        shape: scalar\n"
            "        writers: [cadence-tracker]\n"
            "      contact_count_90d:\n"
            "        shape: scalar\n"
            "        writers: [cadence-tracker]\n"
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        rc = run(raw_root=root / "raw", wiki_root=wiki, knowledge_root=root, max_runtime=30)
        assert rc == 0

        text = page.read_text()
        assert "last_contacted_at: '2026-08-04'" in text or "last_contacted_at: 2026-08-04" in text
        assert "contact_count_90d: 7" in text

        ledger = wiki / "_corrections_applied.jsonl"
        record = json.loads(ledger.read_text().splitlines()[0])
        assert record["dispositions"].get("applied") == 2
        assert not batch.exists()  # retired


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

    def test_cap_hit_emits_summary_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§10.2: hitting the escalation rate cap emits one summary line
        naming the submitter/field/count with the highest suppressed count —
        "the actionable signal anyway", not a silent drop."""
        wiki = tmp_path / "wiki"
        for uid in ("person-a", "person-b"):
            page = _write_page(wiki, f"{uid}.md", {"uid": uid, "type": "person", "name": uid})
            page.write_text(
                page.read_text().replace(
                    f"name: {uid}",
                    f"name: {uid}\ncurrent_title: CTO\n"
                    "field_sources:\n  current_title: 'api:apollo'",
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
                {
                    "record": "correction",
                    "target": {"uid": "person-b"},
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
                    },
                    "max_escalations_per_run": 1,
                }
            }
        }
        with caplog.at_level("WARNING", logger="athenaeum.librarian"):
            _run_correction_phase(_make_ctx(tmp_path, config))

        summary_lines = [
            r.message for r in caplog.records if "escalation rate cap" in r.message
        ]
        assert len(summary_lines) == 1
        assert "enrichment-service" in summary_lines[0]
        assert "current_title" in summary_lines[0]
        assert "1 suppressed" in summary_lines[0]


class TestSchemaAmendmentProposal:
    def test_propose_amendment_reaches_pending_questions(self, tmp_path: Path) -> None:
        """§7.2/§5.4 regression: `held-schema-proposal` is "a schema
        amendment... through the existing human-decision surface" and is
        "terminal once the question or proposal is recorded" -- it must
        actually reach `_pending_questions.md`, the same surface `escalated`
        uses, not merely report the disposition string with nothing recorded
        anywhere an operator can act on."""
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        raw = tmp_path / "raw" / "enrichment-service"
        raw.mkdir(parents=True)
        batch_path = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        batch_path.write_text(
            _batch(
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "set",
                    "field": "custom_attr",
                    "value": "some-value",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T00:00:00Z",
                },
                submitter="enrichment-service",
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {
                        "custom_attr": {"shape": "scalar", "writers": ["enrichment-service"]}
                    },
                    "schema_slots": {"custom_attr": {"propose_amendment": True}},
                }
            }
        }
        ctx = _make_ctx(tmp_path, config)
        _run_correction_phase(ctx)

        pending = wiki / "_pending_questions.md"
        assert pending.exists(), (
            "schema-amendment proposal never reached the pending-questions "
            "surface (§7.2/§5.4)"
        )
        text = pending.read_text()
        assert "schema-amendment" in text
        assert "custom_attr" in text

        ledger = wiki / "_corrections_applied.jsonl"
        record = json.loads(ledger.read_text().splitlines()[0])
        assert record["dispositions"].get("held-schema-proposal") == 1

        # Recorded and terminal on the first pass -- the batch is retired.
        assert not batch_path.exists()


class TestHandoffIdempotency:
    def test_handoff_not_duplicated_when_batch_carries_over_for_other_reason(
        self, tmp_path: Path
    ) -> None:
        """§8.1 regression: a batch carrying BOTH a raised-tier record and an
        escalated record that hits the §10.2 rate cap is NOT retired after
        the first pass (`escalations_recorded=False`), so the SAME
        raised-tier record is recomputed on every subsequent pass. Without
        the `previously_handed_off_correction_ids` guard,
        `write_correction_handoff` is called again each pass, writing a
        duplicate `.md` handoff file for a record already handed over --
        exactly the non-idempotency AC5 forbids ("idempotent on (batch_id,
        sorted correction_id set) across re-runs"). Exactly ONE handoff file
        must exist after two passes.
        """
        wiki = tmp_path / "wiki"
        for uid in ("person-a", "person-b", "person-c"):
            page = _write_page(wiki, f"{uid}.md", {"uid": uid, "type": "person", "name": uid})
            page.write_text(
                page.read_text().replace(
                    f"name: {uid}",
                    f"name: {uid}\ncurrent_title: CTO\n"
                    "field_sources:\n  current_title: 'api:apollo'",
                )
            )
        raw = tmp_path / "raw" / "enrichment-service"
        raw.mkdir(parents=True)
        batch_path = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        batch_path.write_text(
            _batch(
                {
                    # Equal rank (api:), differing value, no comparable date
                    # anywhere -- undated tie -> escalated (§6.2). Three
                    # distinct targets so, with a per-run cap of 1, at least
                    # one is still capped on EVERY pass across two runs (a
                    # 1-per-run budget clears at most 2 of 3 in two passes).
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T05:58:40Z",
                },
                {
                    "record": "correction",
                    "target": {"uid": "person-b"},
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T05:58:40Z",
                },
                {
                    "record": "correction",
                    "target": {"uid": "person-c"},
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T05:58:40Z",
                },
                {
                    # Attribute not on the allowlist -> raised-tier (§8).
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "set",
                    "field": "not_allowlisted_attr",
                    "value": "x",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T00:00:00Z",
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
                    },
                    # A cap of 1/run lets one of the three escalations
                    # record per pass, forcing `escalations_recorded=False`
                    # (and the batch NOT retired) on every pass for at least
                    # two runs -- exactly the "carried over for an unrelated
                    # reason" scenario §8.1's idempotency guard must cover.
                    "max_escalations_per_run": 1,
                }
            }
        }

        _run_correction_phase(_make_ctx(tmp_path, config))
        assert batch_path.exists(), "batch should have carried over, not retired"
        handoff_files_pass_1 = sorted(raw.glob("*.md"))
        assert len(handoff_files_pass_1) == 1

        _run_correction_phase(_make_ctx(tmp_path, config))
        assert batch_path.exists(), "batch should still be carried over after pass 2"
        handoff_files_pass_2 = sorted(raw.glob("*.md"))
        assert len(handoff_files_pass_2) == 1, (
            "handoff file duplicated on a carried-over re-run -- §8.1 "
            "idempotency on (batch_id, sorted correction_id set) was violated"
        )
        assert handoff_files_pass_1 == handoff_files_pass_2
