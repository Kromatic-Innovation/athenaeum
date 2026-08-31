# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum explain-routing`` (issue athenaeum#1176).

The load-bearing assertion here is that the command's resolved provider,
model, and batch values for a given config are IDENTICAL to what the real
per-knob resolvers return for that same config -- not merely that the
command runs without raising. Each test below resolves the ground truth by
calling the underlying resolvers directly (:func:`athenaeum.provider.resolve_provider`,
each knob's own model getter, :func:`athenaeum.librarian.librarian_batch_knob`)
and compares against ``resolve_routing_table``'s output, so a future change
that lets the command's own values drift from what ``athenaeum run`` (or
``athenaeum drain``'s preflight, etc.) actually uses fails this suite.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum._cmd_explain_routing import cmd_explain_routing, resolve_routing_table
from athenaeum.config import load_config
from athenaeum.prompt_registry import KNOBS
from athenaeum.provider import resolve_provider


def _write_config(tmp_path: Path, yaml_text: str) -> Path:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "athenaeum.yaml").write_text(yaml_text, encoding="utf-8")
    return knowledge


class TestResolveRoutingTableMatchesRealResolvers:
    """AC: '--explain-routing ... matches what a real run actually uses.'"""

    def test_default_config_matches_real_resolvers(self, tmp_path: Path) -> None:
        knowledge = _write_config(tmp_path, "")
        config = load_config(knowledge)

        rows = {row["knob"]: row for row in resolve_routing_table(config)}

        assert set(rows) == set(KNOBS)

        from athenaeum.librarian import _resolve_run_models

        expected_models = dict(_resolve_run_models(config))
        for knob, expected_model in expected_models.items():
            assert rows[knob]["model"] == expected_model

        for knob in KNOBS:
            assert rows[knob]["provider"] == resolve_provider(config, knob=knob)

    def test_per_knob_yaml_overrides_are_reflected(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_WRITE_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ATHENAEUM_WRITE_MODEL", raising=False)
        knowledge = _write_config(
            tmp_path,
            """
llm:
  provider: claude-cli
  providers:
    write: api
models:
  write: claude-opus-5
""",
        )
        config = load_config(knowledge)
        rows = {row["knob"]: row for row in resolve_routing_table(config)}

        # The global default is claude-cli, but `write` has its own override.
        assert rows["write"]["provider"] == "api" == resolve_provider(config, knob="write")
        assert rows["write"]["model"] == "claude-opus-5"
        # A knob with no override inherits the global default.
        assert rows["classify"]["provider"] == "claude-cli" == resolve_provider(
            config, knob="classify"
        )

    def test_global_env_does_not_override_per_knob_yaml(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The precedence rule the new docs page states explicitly: a
        per-knob yaml setting is NOT overridden by a global env var."""
        monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
        monkeypatch.delenv("ATHENAEUM_WRITE_LLM_PROVIDER", raising=False)
        knowledge = _write_config(
            tmp_path,
            """
llm:
  providers:
    write: api
""",
        )
        config = load_config(knowledge)
        rows = {row["knob"]: row for row in resolve_routing_table(config)}

        assert rows["write"]["provider"] == "api"
        assert rows["write"]["provider"] == resolve_provider(config, knob="write")
        # Everything else falls through to the global env.
        assert rows["classify"]["provider"] == "claude-cli"

    def test_batch_eligibility_and_resolution_matches_librarian_batch_knob(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from athenaeum.librarian import BATCHABLE_KNOBS, librarian_batch_knob, librarian_batch_mode

        monkeypatch.delenv("ATHENAEUM_BATCH_MODE", raising=False)
        knowledge = _write_config(
            tmp_path,
            """
librarian:
  batch_mode: false
  batch:
    write: true
""",
        )
        config = load_config(knowledge)
        rows = {row["knob"]: row for row in resolve_routing_table(config)}

        run_default = librarian_batch_mode(config)
        for knob in KNOBS:
            expected_eligible = knob in BATCHABLE_KNOBS
            assert rows[knob]["batch_eligible"] == expected_eligible
            if expected_eligible:
                expected_batched = librarian_batch_knob(config, knob, default=run_default)
                assert rows[knob]["batched_this_run"] == expected_batched
            else:
                assert rows[knob]["batched_this_run"] is False

        assert rows["write"]["batched_this_run"] is True
        assert rows["classify"]["batched_this_run"] is False

    def test_price_matches_configured_pricing_table(self, tmp_path: Path) -> None:
        from athenaeum.models import _rates_for_model, configure_model_rates, model_has_price

        knowledge = _write_config(
            tmp_path,
            """
pricing:
  claude-opus-5: [7.0, 35.0]
models:
  resolve: claude-opus-5
""",
        )
        config = load_config(knowledge)
        rows = {row["knob"]: row for row in resolve_routing_table(config)}

        try:
            from athenaeum.config import resolve_model_rates

            configure_model_rates(resolve_model_rates(config))
            expected_rate = _rates_for_model("claude-opus-5")
            expected_has_price = model_has_price("claude-opus-5")
        finally:
            configure_model_rates(None)

        assert rows["resolve"]["price_input_usd_per_mtok"] == expected_rate[0]
        assert rows["resolve"]["price_output_usd_per_mtok"] == expected_rate[1]
        assert rows["resolve"]["price_is_blended_fallback"] == (not expected_has_price)


class TestCmdExplainRouting:
    def test_json_output_is_valid_and_covers_every_knob(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_config(tmp_path, "")
        args = argparse.Namespace(path=knowledge, json=True)
        rc = cmd_explain_routing(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert {row["knob"] for row in payload} == set(KNOBS)

    def test_text_output_lists_every_knob(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = _write_config(tmp_path, "")
        args = argparse.Namespace(path=knowledge, json=False)
        rc = cmd_explain_routing(args)
        assert rc == 0
        out = capsys.readouterr().out
        for knob in KNOBS:
            assert knob in out

    def test_registered_on_top_level_parser(self) -> None:
        from athenaeum.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["explain-routing", "--path", "/tmp/does-not-exist-athenaeum"])
        assert args.func is cmd_explain_routing
