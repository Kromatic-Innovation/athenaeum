# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#786 — the ``ingest-answers`` / ``reresolve-questions`` CLI
commands route their LLM client through the ``resolve`` knob.

These commands' only LLM call is ``resolutions``'s free-text proposer /
resolver (both tagged ``knob="resolve"``, see ``resolutions.py``), so their
one ``build_llm_client(cfg)`` call site each was updated to
``build_llm_client(cfg, knob="resolve")``. Covered narrowly here: the client
factory is called with the right knob, before any pending-sidecar content is
read (so an empty knowledge dir is enough — no live LLM, no lock contention).
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

        captured: dict = {}

        def _fake_build_llm_client(config, **kwargs):
            captured["config"] = config
            captured["kwargs"] = kwargs
            return None  # offline fallback -- ingest_answers degrades cleanly

        monkeypatch.setattr(provider, "build_llm_client", _fake_build_llm_client)

        rc = cmd_ingest_answers(_args(tmp_path))

        assert rc == 0
        assert captured["kwargs"].get("knob") == "resolve"


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
