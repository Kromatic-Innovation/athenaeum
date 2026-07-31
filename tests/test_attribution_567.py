# SPDX-License-Identifier: Apache-2.0
"""Issue #567 — attribute prompt + schema-fragment bytes.

The librarian's one-line ``librarian-run-summary`` and ``athenaeum status`` gain
byte-attribution so a classify/create regression can be pinned to *which* bytes a
run used — the operator's (possibly edited) schema fragments and the shipped
prompt set — from the log/status alone. Source of truth for the fragment state is
``tiers.schema_fragment_state`` (#563) and for the prompt bytes is
``prompt_registry.prompt_manifest`` (#561); this issue only *surfaces* them, it
re-implements no hashing.

Covers:

1. ``_render_schema_fragment_attribution`` / ``_render_run_summary`` — the head
   segment carries ``schema_fragments=`` (``default`` vs ``<sha8>`` per fragment)
   and one aggregate ``prompt_manifest=`` key; both omitted when their arg is
   ``None`` (byte-unchanged pre-#567 head).
2. ``prompt_registry.prompt_manifest_hash`` — one short aggregate digest, stable
   across dict order, sensitive to any prompt-byte change.
3. End-to-end: a run over untouched default fragments and a run over an edited
   fragment produce *visibly different* summary lines (the issue's proof AC),
   while exit code stays 0.
4. ``athenaeum status`` shows one divergence line per schema fragment.

All Anthropic calls are mocked / never made; no live API, no network.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import logging
import subprocess
from pathlib import Path

import pytest

from athenaeum.librarian import (
    _render_run_summary,
    _render_schema_fragment_attribution,
    run,
)
from athenaeum.prompt_registry import prompt_manifest, prompt_manifest_hash
from athenaeum.status import format_status, status

_ATTRIBUTED = ("observation-filter.md", "_entity-template.md")


def _bundled(fname: str) -> bytes:
    return (importlib.resources.files("athenaeum.schema") / fname).read_bytes()


def _seed_knowledge_root(tmp_path: Path, *, edit_entity_template: bool = False) -> Path:
    """Minimal knowledge root whose ``wiki/_schema`` holds the bundled defaults.

    With ``edit_entity_template=True`` the ``_entity-template.md`` copy is
    overwritten with operator-edited bytes (so it diverges from the default);
    everything else stays byte-identical to what the package ships.
    """
    root = tmp_path / "knowledge"
    root.mkdir(parents=True)
    schema = root / "wiki" / "_schema"
    schema.mkdir(parents=True)
    for fname in (
        "types.md",
        "tags.md",
        "access-levels.md",
        "observation-filter.md",
        "_entity-template.md",
    ):
        (schema / fname).write_bytes(_bundled(fname))
    if edit_entity_template:
        (schema / "_entity-template.md").write_bytes(
            b"## Entity template\n- operator's own edits only\n"
        )
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


def _summary_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [
        rec.message
        for rec in caplog.records
        if "librarian-run-summary" in rec.message
    ]
    assert len(lines) == 1, f"expected exactly one summary line, got {len(lines)}"
    return lines[0]


# ---------------------------------------------------------------------------
# 1. Head-segment rendering
# ---------------------------------------------------------------------------


class TestRenderAttribution:
    def test_fragment_attribution_default_vs_sha8(self) -> None:
        state = {
            "observation-filter.md": ("a" * 64, True),
            "_entity-template.md": ("deadbeef" + "0" * 56, False),
        }
        rendered = _render_schema_fragment_attribution(state)
        # ``.md`` stripped; default -> "default", edited -> first 8 hex chars;
        # order preserved from the state dict; comma-joined, no spaces.
        assert rendered == "observation-filter:default,_entity-template:deadbeef"

    def test_run_summary_head_carries_both_keys(self) -> None:
        state = {"observation-filter.md": ("f" * 64, False)}
        line = _render_run_summary(
            [("entity", 4.2, {"calls": 6})],
            schema_fragments=state,
            prompt_manifest_hash="9f8e7d6c",
        )
        # One line, stable prefix, keys ride the head right after total_secs,
        # before the first ``|`` phase segment.
        assert "\n" not in line
        assert line.startswith("librarian-run-summary total_secs=4.200")
        head = line.split(" | ", 1)[0]
        assert "schema_fragments=observation-filter:ffffffff" in head
        assert "prompt_manifest=9f8e7d6c" in head
        assert "entity secs=4.200 calls=6" in line

    def test_attribution_omitted_when_args_none(self) -> None:
        # Pure-formatting default (every pre-#567 caller): head byte-unchanged.
        line = _render_run_summary([("entity", 1.0, {})])
        assert line == "librarian-run-summary total_secs=1.000 | entity secs=1.000"
        assert "schema_fragments=" not in line
        assert "prompt_manifest=" not in line


# ---------------------------------------------------------------------------
# 2. prompt_manifest_hash
# ---------------------------------------------------------------------------


class TestPromptManifestHash:
    def test_default_length_and_hex(self) -> None:
        h = prompt_manifest_hash()
        assert len(h) == 8
        int(h, 16)  # is hex

    def test_stable_across_calls(self) -> None:
        assert prompt_manifest_hash() == prompt_manifest_hash()

    def test_matches_canonical_of_manifest(self) -> None:
        manifest = prompt_manifest()
        canonical = "\n".join(f"{n}={manifest[n]}" for n in sorted(manifest))
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
        assert prompt_manifest_hash() == expected

    def test_changes_when_a_prompt_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = prompt_manifest_hash()
        real = prompt_manifest()
        mutated = dict(real)
        first = next(iter(mutated))
        mutated[first] = hashlib.sha256(b"different bytes").hexdigest()
        monkeypatch.setattr(
            "athenaeum.prompt_registry.prompt_manifest", lambda: mutated
        )
        assert prompt_manifest_hash() != before

    def test_length_arg_truncates(self) -> None:
        assert len(prompt_manifest_hash(length=16)) == 16
        assert prompt_manifest_hash(length=16).startswith(prompt_manifest_hash())


# ---------------------------------------------------------------------------
# 3. End-to-end: default vs edited runs produce different summary lines
# ---------------------------------------------------------------------------


class TestRunSummaryAttribution:
    def _run_summary(
        self,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> str:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setattr(
            "athenaeum.librarian.discover_auto_memory_files", lambda *_a, **_k: []
        )
        caplog.set_level(logging.INFO, logger="athenaeum")
        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=100,
        )
        assert rc == 0  # attribution is observability-only; exit code unaffected
        return _summary_line(caplog)

    def test_default_run_marks_every_fragment_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        line = self._run_summary(root, monkeypatch, caplog)
        assert (
            "schema_fragments=observation-filter:default,_entity-template:default"
            in line
        )
        # The aggregate prompt-manifest key is present and equals the live hash.
        assert f"prompt_manifest={prompt_manifest_hash()}" in line

    def test_edited_fragment_run_differs_visibly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        default_root = _seed_knowledge_root(tmp_path / "d")
        default_line = self._run_summary(default_root, monkeypatch, caplog)
        caplog.clear()
        edited_root = _seed_knowledge_root(tmp_path / "e", edit_entity_template=True)
        edited_line = self._run_summary(edited_root, monkeypatch, caplog)

        assert default_line != edited_line, "edited fragment must change the summary"
        # observation-filter untouched in both -> still default; only the edited
        # fragment flips to a sha8 token.
        assert "observation-filter:default" in edited_line
        assert "_entity-template:default" not in edited_line
        edited_sha8 = hashlib.sha256(
            (edited_root / "wiki" / "_schema" / "_entity-template.md").read_bytes()
        ).hexdigest()[:8]
        assert f"_entity-template:{edited_sha8}" in edited_line


# ---------------------------------------------------------------------------
# 4. athenaeum status divergence line
# ---------------------------------------------------------------------------


class TestStatusDivergenceLine:
    def test_status_reports_default_and_edited(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path, edit_entity_template=True)
        info = status(root)
        assert set(info["schema_fragments"]) == set(_ATTRIBUTED)
        assert info["schema_fragments"]["observation-filter.md"][1] is True
        assert info["schema_fragments"]["_entity-template.md"][1] is False

        text = format_status(info)
        assert "Schema fragments:" in text
        # One line per fragment: the untouched one reads "default", the edited
        # one reads "edited (sha8 …)" naming its live byte-state.
        assert "observation-filter.md: default" in text
        edited_sha8 = info["schema_fragments"]["_entity-template.md"][0][:8]
        assert f"_entity-template.md: edited (sha8 {edited_sha8})" in text

    def test_status_all_default(self, tmp_path: Path) -> None:
        root = _seed_knowledge_root(tmp_path)
        text = format_status(status(root))
        assert "observation-filter.md: default" in text
        assert "_entity-template.md: default" in text
        assert "edited (sha8" not in text

    def test_format_status_backward_compatible_without_key(self) -> None:
        # A pre-#567 status dict (no ``schema_fragments`` key) still formats.
        legacy = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "pages_warn": [],
            "pages_flag": [],
            "drain_advisory": None,
        }
        text = format_status(legacy)  # type: ignore[arg-type]
        assert "Schema fragments:" not in text
