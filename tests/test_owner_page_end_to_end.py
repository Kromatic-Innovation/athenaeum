# SPDX-License-Identifier: Apache-2.0
"""End to end: the owner page on a fresh install (athenaeum#2655).

A fresh knowledge root is initialised through the real CLI, the owner is set
through ``athenaeum owner set``, and the librarian compiles a first-person
preference through the LLM tiers driven by a stub backend (no network, no API
key). Recall in a fresh call then returns the fact from the owner page. With
no owner set, the same fact is visible in the drop ledger instead.

The fact is a communication-style preference: the shipped observation filter
lists preferences under "Always Capture" (food would be "Capture When
Reinforced" and is expected to be dropped on a single mention).
All fixtures are synthetic.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum.cli import main
from athenaeum.config import load_config, resolve_owner
from athenaeum.drops import DROP_STAGE_CLASSIFY, iter_drops
from athenaeum.mcp_server import recall_search
from athenaeum.models import parse_frontmatter

OWNER_NAME = "Riley Quill"
FACT_TEXT = "I prefer terse bullet-point status updates over long prose."
FACT_LINE = "- Prefers terse bullet-point status updates over long prose."
RAW_NAME = "20261101T120000Z-aabbccdd.md"


class _StubBackend:
    """Stands in for the Anthropic client; replies by prompt, offline."""

    def __init__(self, owner_uid: str | None) -> None:
        self.owner_uid = owner_uid
        self.calls: list[str] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        prompt = json.dumps(kwargs.get("messages", ""))
        self.calls.append(prompt)
        if "<existing_page>" in prompt:
            text = _merge_reply()  # tier-3 merge of the owner page
        elif self.owner_uid and f"{self.owner_uid}: {OWNER_NAME} (owner)" in prompt:
            text = json.dumps(  # tier-2 classify, owner offered as a candidate
                [
                    {
                        "candidate_uid": self.owner_uid,
                        "observations": "Preference: terse bullet-point status updates.",
                    }
                ]
            )
        elif "Fields to determine" in prompt:
            text = "{}"  # validity-window extraction: nothing to report
        else:
            text = "[]"  # classify with no owner: no entity worth a page
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.stop_reason = "end_turn"
        return response


def _merge_reply() -> str:
    return json.dumps(
        {
            "ops": [{"op": "append_section", "text": "## Preferences\n\n" + FACT_LINE}],
            "adds_new_claim": True,
        }
    )


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_owner: bool):
    import anthropic as anthropic_mod

    knowledge = tmp_path / "knowledge"
    cache = tmp_path / "cache"
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    assert main(["init", "--path", str(knowledge)]) == 0
    owner_uid: str | None = None
    if with_owner:
        assert main(["owner", "set", "--name", OWNER_NAME, "--path", str(knowledge)]) == 0
        owner = resolve_owner(load_config(knowledge))
        assert owner is not None
        owner_uid = owner["uid"]
    sessions = knowledge / "raw" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / RAW_NAME).write_text(FACT_TEXT + "\n", encoding="utf-8")
    # Commit the raw file so the librarian's cleanliness checks see a settled tree.
    subprocess.run(["git", "add", "-A"], cwd=knowledge, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed raw"], cwd=knowledge, check=True, capture_output=True
    )
    backend = _StubBackend(owner_uid)
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: backend)
    return knowledge, cache, owner_uid, backend


def _compile(knowledge: Path) -> None:
    from athenaeum.librarian import run

    run(raw_root=knowledge / "raw", wiki_root=knowledge / "wiki", knowledge_root=knowledge)


def test_owner_preference_compiles_and_recall_returns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge, cache, owner_uid, backend = _install(tmp_path, monkeypatch, with_owner=True)
    assert owner_uid is not None

    _compile(knowledge)

    pages = sorted((knowledge / "wiki").glob(f"{owner_uid}-*.md"))
    assert len(pages) == 1
    text = pages[0].read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    assert meta["access"] == "personal"
    assert FACT_LINE in body
    assert iter_drops(cache) == []

    # A fresh reindex and a fresh recall call, as a new session would do.
    assert (
        main(["reindex", "--path", str(knowledge), "--cache-dir", str(cache), "--backend", "fts5"])
        == 0
    )
    output = recall_search(
        knowledge / "wiki",
        "terse bullet-point status updates",
        top_k=5,
        search_backend="fts5",
        cache_dir=cache,
    )
    assert OWNER_NAME in output
    assert "terse bullet-point status updates" in output


def test_without_an_owner_the_same_fact_lands_in_the_drop_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge, cache, _, _ = _install(tmp_path, monkeypatch, with_owner=False)

    _compile(knowledge)

    rows = iter_drops(cache)
    assert len(rows) == 1
    assert rows[0].stage == DROP_STAGE_CLASSIFY
    assert rows[0].raw_ref == f"sessions/{RAW_NAME}"
    assert "terse bullet-point" in rows[0].excerpt
    # Nothing compiled onto any page.
    assert not [p for p in (knowledge / "wiki").glob("*.md") if not p.name.startswith("_")]
