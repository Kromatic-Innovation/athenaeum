# SPDX-License-Identifier: Apache-2.0
"""Tests for ``scripts/rederive_recorded_fixture.py`` (issue athenaeum#1496).

Proves the tool's whole safety property in both directions, against
SYNTHETIC fixtures under ``tmp_path`` -- never the real
``tests/fixtures/recorded/`` tree, and never a real athenaeum call path (a
fake, injected driver stands in for the real per-layer drivers; see
:func:`scripts.rederive_recorded_fixture.rederive_case`'s ``driver``
parameter):

* a prompt that changed by EXACTLY the declared rename is re-derived: the
  fixture's ``prompt_hash``/``response_text`` are updated and a
  ``rederived`` provenance block is stamped, while ``recorded_at``/
  ``model``/``usage`` stay untouched.
* a prompt that changed by anything ELSE (here: extra wording alongside the
  same rename) is REFUSED: the fixture file on disk is byte-for-byte
  unchanged, so a genuinely stale fixture stays stale rather than being
  silently blessed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.evals.harness import RecordedResponse, prompt_hash, save_recorded

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "rederive_recorded_fixture.py"

_spec = importlib.util.spec_from_file_location("rederive_recorded_fixture", _SCRIPT)
assert _spec and _spec.loader
rederive_recorded_fixture = importlib.util.module_from_spec(_spec)
# Register before exec: the module defines a @dataclasses.dataclass, and
# dataclass field-type resolution looks the module up via
# sys.modules[cls.__module__] -- exec_module() alone does not register it.
sys.modules[_spec.name] = rederive_recorded_fixture
_spec.loader.exec_module(rederive_recorded_fixture)

rederive_case = rederive_recorded_fixture.rederive_case

_MODEL = "test-model"
_RENAME_PAIRS = [("Meridian's", "Thornhollow's"), ("Meridian", "Thornhollow")]


def _seed_fixture(layer: str, case_id: str, *, old_system: str, old_response: str) -> str:
    """Write a synthetic recorded fixture (pre-rename prompt) and return its
    original ``prompt_hash`` so a test can assert it is unchanged on refusal."""
    old_messages = [{"role": "user", "content": "describe the account"}]
    old_hash = prompt_hash(_MODEL, old_system, old_messages)
    save_recorded(
        RecordedResponse(
            case_id=case_id,
            layer=layer,
            model=_MODEL,
            prompt_hash=old_hash,
            response_text=old_response,
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            recorded_at="2026-08-02T18:05:05.955036+00:00",
            content_blocks=[{"type": "text", "text": old_response}],
        )
    )
    return old_hash


def _driver_returning(system: str) -> Any:
    """A fake per-layer driver: ignores case_id/tmp_dir, always returns the
    same (model, system, messages) -- standing in for a real driver's
    ``client.messages.create`` capture."""

    def _drive(case_id: str, tmp_dir: Path) -> dict[str, Any]:
        return {
            "model": _MODEL,
            "system": system,
            "messages": [{"role": "user", "content": "describe the account"}],
        }

    return _drive


@pytest.fixture
def _recorded_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "recorded"
    (root / "detector").mkdir(parents=True)
    monkeypatch.setattr("tests.evals.harness.RECORDED_ROOT", root)
    return root


class TestRederiveCase:
    def test_pure_rename_is_rederived(self, _recorded_root: Path, tmp_path: Path) -> None:
        old_hash = _seed_fixture(
            "detector",
            "synthetic_pure_rename",
            old_system="Client lead for Meridian's retainer.",
            old_response="Meridian's account is confirmed active.",
        )

        outcome = rederive_case(
            "detector",
            "synthetic_pure_rename",
            _RENAME_PAIRS,
            tmp_path,
            apply=True,
            driver=_driver_returning("Client lead for Thornhollow's retainer."),
        )

        assert outcome.status == "rederived", outcome.detail

        saved = json.loads((_recorded_root / "detector" / "synthetic_pure_rename.json").read_text())
        assert saved["prompt_hash"] != old_hash
        assert saved["response_text"] == "Thornhollow's account is confirmed active."
        assert saved["content_blocks"][0]["text"] == "Thornhollow's account is confirmed active."
        # Provenance: what changed and how, without disturbing the original
        # recording's own identity.
        assert saved["rederived"]["from_prompt_hash"] == old_hash
        assert saved["rederived"]["rename"] == ["Meridian's=Thornhollow's", "Meridian=Thornhollow"]
        assert saved["recorded_at"] == "2026-08-02T18:05:05.955036+00:00"
        assert saved["model"] == _MODEL
        assert saved["usage"]["input_tokens"] == 100

    def test_rename_plus_extra_wording_change_is_refused(
        self, _recorded_root: Path, tmp_path: Path
    ) -> None:
        """A prompt edit that is the declared rename PLUS something else
        (here: an added sentence) must be refused -- the tool cannot prove
        the diff is nothing but the rename, so it must not guess."""
        old_hash = _seed_fixture(
            "detector",
            "synthetic_extra_change",
            old_system="Client lead for Meridian's retainer.",
            old_response="Meridian's account is confirmed active.",
        )
        fixture_path = _recorded_root / "detector" / "synthetic_extra_change.json"
        before = fixture_path.read_text()

        outcome = rederive_case(
            "detector",
            "synthetic_extra_change",
            _RENAME_PAIRS,
            tmp_path,
            apply=True,
            # Renamed AND reworded -- not a pure substitution.
            driver=_driver_returning(
                "Client lead for Thornhollow's retainer, escalated to the partner."
            ),
        )

        assert outcome.status == "refused"
        assert "not provably just the declared rename" in outcome.detail
        # Refusal must not touch the file -- a stale fixture stays stale.
        assert fixture_path.read_text() == before
        assert json.loads(before)["prompt_hash"] == old_hash

    def test_unchanged_prompt_is_a_noop(self, _recorded_root: Path, tmp_path: Path) -> None:
        """A driver returning the SAME prompt the fixture already hashes
        against (nothing to rename) is reported unchanged, not rederived."""
        _seed_fixture(
            "detector",
            "synthetic_noop",
            old_system="Client lead for Bluewater's retainer.",
            old_response="Bluewater's account is confirmed active.",
        )
        fixture_path = _recorded_root / "detector" / "synthetic_noop.json"
        before = fixture_path.read_text()

        outcome = rederive_case(
            "detector",
            "synthetic_noop",
            _RENAME_PAIRS,
            tmp_path,
            apply=True,
            driver=_driver_returning("Client lead for Bluewater's retainer."),
        )

        assert outcome.status == "unchanged"
        assert fixture_path.read_text() == before
