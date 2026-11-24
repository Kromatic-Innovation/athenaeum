# SPDX-License-Identifier: Apache-2.0
"""Tests for the attributable spend ledger (issue athenaeum#3146).

Three additive changes, each pinned here:

1. an optional ``account`` label on every spend record (schema v5), resolved
   env ``ATHENAEUM_SPEND_ACCOUNT`` > yaml ``spend.account`` > absent;
2. a distinct ``librarian-triggered`` run type for a fired
   ``athenaeum ingest --if-triggered``;
3. the external-reader contract — the fields a reader of ``spend.jsonl`` may
   rely on, documented in ``docs/reference/data-formats.md``.

Plus the compatibility bar: a mixed v4/v5 ledger reads through ``summarize``,
``reprice`` and ``athenaeum spend`` with the v4 rows treated exactly as before.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum import _cmd_lifecycle, drain_advisor, spend
from athenaeum.cli import main
from athenaeum.config import resolve_spend_account
from athenaeum.models import TokenUsage

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA_FORMATS_DOC = _REPO_ROOT / "docs" / "reference" / "data-formats.md"


@pytest.fixture(autouse=True)
def _hermetic_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ambient account / run-type / ledger-switch env leaks into a test,
    and ``athenaeum spend`` never opens a real default knowledge root."""
    for var in (
        "ATHENAEUM_SPEND_ACCOUNT",
        "ATHENAEUM_RUN_TYPE",
        "ATHENAEUM_SPEND_LEDGER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        _cmd_lifecycle, "DEFAULT_KNOWLEDGE_ROOT", tmp_path / "unused-default-root"
    )


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ledger" / "spend.jsonl"
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(path))
    return path


def _api_usage() -> TokenUsage:
    u = TokenUsage()
    u.add(10_000, 2_000, 0, 0, model="claude-haiku-4-5-20251001", knob="classify")
    return u


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. account label
# ---------------------------------------------------------------------------


class TestResolveSpendAccount:
    def test_absent_when_neither_is_set(self) -> None:
        assert resolve_spend_account(None) is None
        assert resolve_spend_account({}) is None
        assert resolve_spend_account({"spend": {}}) is None

    def test_yaml_only(self) -> None:
        assert resolve_spend_account({"spend": {"account": "personal"}}) == "personal"

    def test_env_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "team-api-key")
        assert resolve_spend_account(None) == "team-api-key"

    def test_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "from-env")
        assert resolve_spend_account({"spend": {"account": "from-yaml"}}) == "from-env"

    def test_blank_env_falls_through_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "   ")
        assert resolve_spend_account({"spend": {"account": "from-yaml"}}) == "from-yaml"

    @pytest.mark.parametrize(
        "value",
        ["a", "x" * 64, "team.api_key-2", "A1"],
    )
    def test_valid_tokens_are_kept(self, value: str) -> None:
        assert resolve_spend_account({"spend": {"account": value}}) == value

    @pytest.mark.parametrize(
        "value",
        ["has/slash", "x" * 65, "has space", "semi;colon", "ümlaut", 42, ["x"]],
    )
    def test_invalid_value_is_dropped_with_a_warning(
        self, value: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            assert resolve_spend_account({"spend": {"account": value}}) is None
        assert any("spend.account" in r.getMessage() for r in caplog.records)

    def test_invalid_env_is_dropped_not_echoed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The warning names the SOURCE, never the value: a mis-pasted
        # credential must not be copied into a log line.
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "sk-ant-not/a-label")
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            # Env is invalid -> dropped; it does NOT fall back to yaml.
            assert resolve_spend_account({"spend": {"account": "ok"}}) is None
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "ATHENAEUM_SPEND_ACCOUNT" in messages
        assert "sk-ant" not in messages


class TestBuildRecordAccount:
    def test_schema_version_is_5(self) -> None:
        assert spend.LEDGER_VERSION == 5
        rec = spend.build_record(_api_usage(), run_type="librarian", provider="api")
        assert rec["v"] == 5

    def test_account_key_absent_when_not_given(self) -> None:
        rec = spend.build_record(_api_usage(), run_type="librarian", provider="api")
        assert "account" not in rec

    def test_account_key_present_when_given(self) -> None:
        rec = spend.build_record(
            _api_usage(), run_type="librarian", provider="api", account="personal"
        )
        assert rec["account"] == "personal"


class TestRecordSpendAccount:
    def test_env_label_lands_on_the_record(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "x")
        assert spend.record_spend(_api_usage(), run_type="librarian", provider="api")
        (rec,) = _read(ledger)
        assert rec["account"] == "x"
        assert rec["v"] == 5

    def test_yaml_label_lands_on_the_record(self, ledger: Path) -> None:
        assert spend.record_spend(
            _api_usage(),
            run_type="librarian",
            provider="api",
            config={"spend": {"account": "x"}},
        )
        (rec,) = _read(ledger)
        assert rec["account"] == "x"

    def test_env_beats_yaml_on_the_record(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "env-wins")
        spend.record_spend(
            _api_usage(),
            run_type="librarian",
            provider="api",
            config={"spend": {"account": "yaml-loses"}},
        )
        (rec,) = _read(ledger)
        assert rec["account"] == "env-wins"

    def test_no_label_means_no_key_not_null(self, ledger: Path) -> None:
        spend.record_spend(_api_usage(), run_type="librarian", provider="api")
        line = ledger.read_text()
        assert '"account"' not in line
        (rec,) = _read(ledger)
        assert "account" not in rec
        assert rec["v"] == 5

    def test_invalid_label_still_records_spend(
        self,
        ledger: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "bad/label")
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            assert spend.record_spend(
                _api_usage(), run_type="librarian", provider="api"
            )
        (rec,) = _read(ledger)
        assert "account" not in rec
        assert rec["input_tokens"] == 10_000
        assert any(
            "ATHENAEUM_SPEND_ACCOUNT" in r.getMessage() for r in caplog.records
        )

    def test_overlong_label_still_records_spend(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "x" * 65)
        assert spend.record_spend(_api_usage(), run_type="librarian", provider="api")
        (rec,) = _read(ledger)
        assert "account" not in rec

    def test_per_knob_provider_split_labels_every_row(
        self, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SPEND_ACCOUNT", "shared-key")
        usage = TokenUsage()
        usage.add(1_000, 100, 0, 0, model="claude-haiku-4-5-20251001", knob="classify")
        usage.add(2_000, 200, 0, 0, model="claude-sonnet-4-6", knob="write")
        assert spend.record_spend_per_knob_provider(
            usage,
            {"classify": "claude-cli", "write": "api"},
            {"classify": "claude-haiku-4-5-20251001", "write": "claude-sonnet-4-6"},
            run_type="librarian",
            default_provider="api",
        )
        rows = _read(ledger)
        assert {r["billing_mode"] for r in rows} == {"api", "subscription"}
        assert [r["account"] for r in rows] == ["shared-key", "shared-key"]
        assert all(r["v"] == 5 for r in rows)

    def test_per_knob_provider_split_without_label_has_no_key(
        self, ledger: Path
    ) -> None:
        usage = TokenUsage()
        usage.add(1_000, 100, 0, 0, model="claude-haiku-4-5-20251001", knob="classify")
        usage.add(2_000, 200, 0, 0, model="claude-sonnet-4-6", knob="write")
        spend.record_spend_per_knob_provider(
            usage,
            {"classify": "claude-cli", "write": "api"},
            {"classify": "claude-haiku-4-5-20251001", "write": "claude-sonnet-4-6"},
            run_type="librarian",
            default_provider="api",
        )
        rows = _read(ledger)
        assert len(rows) == 2
        assert all("account" not in r for r in rows)


# ---------------------------------------------------------------------------
# 2. librarian-triggered run type
# ---------------------------------------------------------------------------


class TestLibrarianTriggeredRunType:
    def test_constant_value(self) -> None:
        assert spend.RUN_TYPE_LIBRARIAN_TRIGGERED == "librarian-triggered"

    @pytest.mark.parametrize(
        ("run_type", "expected"),
        [
            (spend.RUN_TYPE_LIBRARIAN, True),
            (spend.RUN_TYPE_LIBRARIAN_NIGHTLY, True),
            (spend.RUN_TYPE_LIBRARIAN_TRIGGERED, True),
            ("librarian-triggered", True),
            (spend.RUN_TYPE_ANSWERS, False),
            (spend.RUN_TYPE_QUERY_TOPICS, False),
            ("not-librarian", False),
            (None, False),
        ],
    )
    def test_family_membership(self, run_type: object, expected: bool) -> None:
        assert spend.is_librarian_run_type(run_type) is expected

    def test_drain_advisor_counts_a_triggered_row(self) -> None:
        records = [
            {
                "v": 5,
                "ts": "2026-11-20T02:00:00Z",
                "run_type": spend.RUN_TYPE_LIBRARIAN_TRIGGERED,
                "provider": "anthropic",
                "files_processed": 12,
                "input_tokens": 1_000,
                "output_tokens": 100,
                "api_calls": 3,
            },
            {
                "v": 5,
                "ts": "2026-11-20T03:00:00Z",
                "run_type": spend.RUN_TYPE_ANSWERS,
                "provider": "anthropic",
                "files_processed": 99,
            },
        ]
        rate, source = drain_advisor.estimate_files_per_night(records)
        assert source == "ledger"
        assert rate == 12.0


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _seed_knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    schema = root / "wiki" / "_schema"
    schema.mkdir(parents=True)
    (schema / "types.md").write_text("# Types\n\n| Type |\n|------|\n| person |\n")
    (schema / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
    (schema / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    (sessions / "20240410T120000Z-aabbccdd.md").write_text(
        "---\nuid: p-0001\ntype: person\nname: Alice\ntags: [active]\n"
        "access: internal\n---\n\nNotes about Alice.\n"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def stub_llm_spend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A stub LLM that "spends" tokens during the real run.

    The seeded raw file is tier-0 (pre-structured), so the real compile makes
    no model call and a real run would record no spend at all. This stands
    in for the model calls a real compile makes: it charges a fixed token
    bucket to the run's own accumulator just before the REAL finalize phase
    writes the ledger record. Everything that decides the record's run type
    — the CLI, ``ingest()``, ``run()``'s ``run_type`` argument, the env
    resolution, the finalize write — is the production path.
    """
    import anthropic as anthropic_mod

    from athenaeum import librarian

    client = MagicMock()
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key-not-real")

    real_finalize = librarian._run_finalize_phase

    def _finalize_after_spending(ctx: Any) -> int:
        ctx.usage.add(4_000, 500, 0, 0, model="claude-haiku-4-5-20251001")
        return real_finalize(ctx)

    monkeypatch.setattr(librarian, "_run_finalize_phase", _finalize_after_spending)
    return client


def _ingest_args(root: Path, cache: Path, *extra: str) -> list[str]:
    return ["ingest", *extra, "--path", str(root), "--cache-dir", str(cache)]


class TestTriggeredIngestRunType:
    def test_fired_trigger_records_librarian_triggered(
        self,
        tmp_path: Path,
        ledger: Path,
        stub_llm_spend: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        rc = main(_ingest_args(root, tmp_path / "cache", "--if-triggered"))
        assert rc == 0
        summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert summary["trigger"] == "backlog-files"
        (rec,) = _read(ledger)
        assert rec["run_type"] == "librarian-triggered"
        assert spend.is_librarian_run_type(rec["run_type"])

    def test_env_run_type_overrides_the_triggered_tag(
        self,
        tmp_path: Path,
        ledger: Path,
        stub_llm_spend: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RUN_TYPE", "librarian-nightly")
        root = _seed_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        assert main(_ingest_args(root, tmp_path / "cache", "--if-triggered")) == 0
        (rec,) = _read(ledger)
        assert rec["run_type"] == "librarian-nightly"

    def test_non_firing_evaluation_writes_no_spend_record(
        self,
        tmp_path: Path,
        ledger: Path,
        stub_llm_spend: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        # First call fires the nightly backstop (no stamp yet) and compiles.
        assert main(_ingest_args(root, cache, "--if-triggered")) == 0
        assert len(_read(ledger)) == 1
        capsys.readouterr()
        # Second call: fresh stamp, nothing configured -> does not fire.
        assert main(_ingest_args(root, cache, "--if-triggered")) == 0
        summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert summary["trigger"] == "none"
        assert len(_read(ledger)) == 1  # nothing appended

    def test_plain_ingest_keeps_the_librarian_tag(
        self,
        tmp_path: Path,
        ledger: Path,
        stub_llm_spend: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        assert main(_ingest_args(root, tmp_path / "cache")) == 0
        (rec,) = _read(ledger)
        assert rec["run_type"] == "librarian"


# ---------------------------------------------------------------------------
# 3. external-reader contract
# ---------------------------------------------------------------------------


def _documented_reader_fields() -> list[str]:
    """The field names in the data-formats doc's reader-contract table."""
    text = _DATA_FORMATS_DOC.read_text(encoding="utf-8")
    start = text.index("**Fields an external reader may rely on.**")
    end = text.index("**Rules a reader must follow.**", start)
    fields = re.findall(r"^\| `([a-z_]+)` \|", text[start:end], flags=re.MULTILINE)
    assert fields, "no reader-contract fields found in the doc table"
    return fields


class TestReaderContract:
    def test_doc_lists_exactly_the_pinned_fields(self) -> None:
        assert _documented_reader_fields() == list(spend.READER_CONTRACT_FIELDS)

    def test_issue_named_fields_are_all_pinned(self) -> None:
        # The list the issue specified, verbatim.
        expected = {
            "ts", "v", "provider", "billing_mode", "run_type", "models",
            "api_calls", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "tokens_by_model", "estimated_cost_usd", "notional_usd", "account",
        }
        assert set(spend.READER_CONTRACT_FIELDS) == expected

    @pytest.mark.parametrize("provider", ["api", "claude-cli"])
    def test_every_documented_field_is_a_member_of_a_built_record(
        self, provider: str
    ) -> None:
        # Membership, not a key count: adding a field to the record must
        # never break this test.
        rec = spend.build_record(
            _api_usage(), run_type="librarian", provider=provider, account="x"
        )
        for field in _documented_reader_fields():
            assert field in rec, field

    def test_every_documented_field_but_account_is_always_written(self) -> None:
        rec = spend.build_record(_api_usage(), run_type="librarian", provider="api")
        missing = [f for f in _documented_reader_fields() if f not in rec]
        assert missing == ["account"]

    def test_doc_states_the_money_and_volume_rules(self) -> None:
        text = _DATA_FORMATS_DOC.read_text(encoding="utf-8")
        assert 'Money only on `billing_mode: "api"` rows' in text
        assert "**never** money" in text
        assert "One record is one **run**, not one model call." in text


# ---------------------------------------------------------------------------
# mixed v4/v5 ledger
# ---------------------------------------------------------------------------


def _v4_rows() -> list[dict[str, Any]]:
    """Two v4-shaped rows (one per billing path), built by the real writer
    and then stamped down to v4 with no ``account`` key."""
    api = spend.build_record(_api_usage(), run_type="librarian", provider="api")
    sub_usage = TokenUsage()
    sub_usage.subscription_covered = True
    sub_usage.add(3_000, 300, 0, 500, model="claude-sonnet-4-6", knob="write")
    sub = spend.build_record(
        sub_usage, run_type="librarian-nightly", provider="claude-cli"
    )
    rows = [api, sub]
    for row in rows:
        row["v"] = 4
        row.pop("account", None)
    return rows


def _as_v5(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(rows)
    for row in out:
        row["v"] = 5
        row["account"] = "team-api-key"
    return out


class TestMixedV4V5Ledger:
    def test_summarize_treats_v4_rows_exactly_as_before(self) -> None:
        v4 = _v4_rows()
        v5 = _as_v5(v4)
        for kwargs in (
            {},
            {"by_model": True, "by_provider": True, "by_knob": True, "by_surface": True},
        ):
            alone = spend.summarize(copy.deepcopy(v4), **kwargs)
            # The account label is not a cost dimension: v5 copies of the
            # same runs summarise identically to the v4 originals...
            assert spend.summarize(copy.deepcopy(v5), **kwargs) == alone
            mixed = spend.summarize(copy.deepcopy(v4) + copy.deepcopy(v5), **kwargs)
            # ...and a mixed ledger is exactly the sum of its halves.
            for path in ("api", "subscription"):
                for key in ("total_tokens", "estimated_cost_usd", "input_tokens"):
                    if key in alone[path]:
                        assert mixed[path][key] == pytest.approx(
                            2 * alone[path][key]
                        ), (path, key)

    def test_reprice_treats_v4_rows_exactly_as_before(self) -> None:
        v4 = _v4_rows()
        v5 = _as_v5(v4)
        alone = spend.reprice(copy.deepcopy(v4))
        assert spend.reprice(copy.deepcopy(v5)) == alone
        mixed = spend.reprice(copy.deepcopy(v4) + copy.deepcopy(v5))
        assert mixed["api"]["repriced_usd"] == pytest.approx(
            2 * alone["api"]["repriced_usd"]
        )
        assert mixed["api"]["stored_usd"] == pytest.approx(
            2 * alone["api"]["stored_usd"]
        )

    def test_spend_command_reads_a_mixed_ledger(
        self, ledger: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        v4 = _v4_rows()
        # Fresh timestamps so --since keeps every row.
        from athenaeum.store import now_iso

        stamp = now_iso()
        for row in v4:
            row["ts"] = stamp
        v5 = _as_v5(v4)

        def _write(rows: list[dict[str, Any]]) -> None:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(
                "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
            )

        _write(v4)
        assert main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)]) == 0
        v4_only = json.loads(capsys.readouterr().out)

        _write(v4 + v5)
        assert main(["spend", "--since", "30d", "--json", "--ledger", str(ledger)]) == 0
        mixed = json.loads(capsys.readouterr().out)
        assert mixed["record_count"] == 2 * v4_only["record_count"]
        assert mixed["api"]["estimated_cost_usd"] == pytest.approx(
            2 * v4_only["api"]["estimated_cost_usd"]
        )
        assert mixed["subscription"]["total_tokens"] == (
            2 * v4_only["subscription"]["total_tokens"]
        )

        assert (
            main(
                ["spend", "--since", "30d", "--json", "--reprice", "--ledger", str(ledger)]
            )
            == 0
        )
        json.loads(capsys.readouterr().out)  # parses; no error


# ---------------------------------------------------------------------------
# no foreign ledger
# ---------------------------------------------------------------------------


def test_no_fleet_cost_ledger_hook_exists() -> None:
    """The withdrawn outside-ledger hook is not implemented anywhere in src/."""
    src = _REPO_ROOT / "src" / "athenaeum"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "FLEET_COST_LEDGER" not in text, path
        assert "FLEET_COST_LABEL" not in text, path
