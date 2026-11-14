# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#786 — the ``ingest-answers`` / ``reresolve-questions`` CLI
commands route their LLM client through the ``resolve`` knob.

These commands' only LLM call is ``resolutions``'s free-text proposer /
resolver (both tagged ``knob="resolve"``, see ``resolutions.py``), so their
one ``build_llm_client(cfg)`` call site each was updated to
``build_llm_client(cfg, knob="resolve")``. Covered narrowly here: the client
factory is called with the right knob, before any pending-sidecar content is
read (so an empty knowledge dir is enough — no live LLM, no lock contention).

Issue athenaeum#2515: ``cmd_ingest_answers`` now builds a SECOND client on the
``classify`` knob too, for the one narrow scope-sameness judgement a
``coordinate`` decision answer can trigger
(:func:`athenaeum.decision_answers._apply_coordinate_answer`) — the
``TestIngestAnswersProviderRouting`` test below now captures every call
rather than just the last one, since there are two.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from athenaeum import provider


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(path=tmp_path)


class TestIngestAnswersProviderRouting:
    def test_build_llm_client_called_with_resolve_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum._cmd_pending import cmd_ingest_answers

        calls: list[dict] = []

        def _fake_build_llm_client(config, **kwargs):
            calls.append({"config": config, "kwargs": kwargs})
            return None  # offline fallback -- ingest_answers degrades cleanly

        monkeypatch.setattr(provider, "build_llm_client", _fake_build_llm_client)

        rc = cmd_ingest_answers(_args(tmp_path))

        assert rc == 0
        knobs = [call["kwargs"].get("knob") for call in calls]
        assert "resolve" in knobs

    def test_build_llm_client_also_called_with_classify_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue athenaeum#2515: the coordinate-answer scope-sameness judge
        rides the ``classify`` knob — this is the wiring that makes a real
        client reach :func:`athenaeum.decision_answers._apply_coordinate_answer`
        in production instead of it always seeing ``client=None``."""
        from athenaeum._cmd_pending import cmd_ingest_answers

        calls: list[dict] = []

        def _fake_build_llm_client(config, **kwargs):
            calls.append({"config": config, "kwargs": kwargs})
            return None

        monkeypatch.setattr(provider, "build_llm_client", _fake_build_llm_client)

        rc = cmd_ingest_answers(_args(tmp_path))

        assert rc == 0
        knobs = [call["kwargs"].get("knob") for call in calls]
        assert "classify" in knobs


class TestReresolveQuestionsProviderRouting:
    def test_build_llm_client_called_with_resolve_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum._cmd_pending import cmd_reresolve_questions

        captured: dict = {}

        def _fake_build_llm_client(config, **kwargs):
            captured["config"] = config
            captured["kwargs"] = kwargs
            return None

        monkeypatch.setattr(provider, "build_llm_client", _fake_build_llm_client)

        rc = cmd_reresolve_questions(_args(tmp_path))

        assert rc == 0
        assert captured["kwargs"].get("knob") == "resolve"
