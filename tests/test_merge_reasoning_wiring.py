# SPDX-License-Identifier: Apache-2.0
"""The T1 reasoning-tier screen wired into the merge path (issue athenaeum#518).

Before this, `reasoning_tiers.run_reasoning_pipeline` had no production caller
(`DEFAULT_TIER_CHAIN = ()`). `reasoning_screens.t1_screen_rejects_merge_proposal`
is the wiring: at the merge-proposal seam, a confident T1 reject drops the proposal
before it reaches the human queue, gated behind the opt-in
`reasoning_tier_auditing_enabled` flag (default OFF), with the spend ceiling
(athenaeum#568) respected and the reject surfaced for the calibration audit loop (athenaeum#438).

Re-pointed by issue athenaeum#1257: the screen moved from ``athenaeum.merge`` to
``athenaeum.reasoning_screens`` (so retiring merge.py's C4 lane cannot orphan
it). Every assertion below is unchanged — this file proves the MOVE preserved
the behaviour verbatim, including the ``spend.ceiling_tripped`` degrade path
that used to be asserted through ``merge.spend``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum import reasoning_screens as screens_mod
from athenaeum.calibration import calibration_summary
from athenaeum.models import TokenUsage
from athenaeum.reasoning_screens import t1_screen_rejects_merge_proposal

# High sample rates so a T1 reject is always surfaced to the audit ledger.
_SAMPLE_CFG = {
    "librarian": {
        "audit_sample_rate_t1_rejects": 1.0,
        "audit_sample_rate_t2_approvals": 1.0,
    }
}


def _write_source(
    path: Path, *, name: str, memory_class: str | None, body: str = "A short body."
) -> str:
    lines = ["---", f"name: {name}"]
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return str(path)


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = resp
    return client


def _wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir(exist_ok=True)
    return w


def _cross_class_paths(tmp_path: Path) -> list[str]:
    """Two sources with distinct memory_class → a DETERMINISTIC T1 reject
    (no model call needed)."""
    return [
        _write_source(tmp_path / "a.md", name="Alpha", memory_class="fact"),
        _write_source(tmp_path / "b.md", name="Beta", memory_class="guideline"),
    ]


def _screen(tmp_path: Path, *, member_paths: list[str] | None = None, **overrides):
    if member_paths is None:
        member_paths = _cross_class_paths(tmp_path)
    kwargs = dict(
        member_paths=member_paths,
        merge_target_name="Alpha",
        cluster_id="c1",
        client=MagicMock(),
        usage=TokenUsage(),
        wiki_root=_wiki(tmp_path),
        config=_SAMPLE_CFG,
        provider="claude-cli",
        authority_manifest=None,
        enabled=True,
        dry_run=False,
    )
    kwargs.update(overrides)
    return t1_screen_rejects_merge_proposal(**kwargs), kwargs


class TestT1ScreenGuards:
    def test_disabled_is_a_noop(self, tmp_path: Path) -> None:
        client = MagicMock()
        dropped, _ = _screen(tmp_path, enabled=False, client=client)
        assert dropped is False
        client.messages.create.assert_not_called()

    def test_no_client_is_a_noop(self, tmp_path: Path) -> None:
        dropped, _ = _screen(tmp_path, client=None)
        assert dropped is False

    def test_dry_run_is_a_noop(self, tmp_path: Path) -> None:
        client = MagicMock()
        dropped, _ = _screen(tmp_path, dry_run=True, client=client)
        assert dropped is False
        client.messages.create.assert_not_called()

    def test_empty_members_is_a_noop(self, tmp_path: Path) -> None:
        dropped, _ = _screen(tmp_path, member_paths=[])
        assert dropped is False


class TestT1ScreenDecision:
    def test_deterministic_reject_drops_counts_and_samples(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(screens_mod.spend, "ceiling_tripped", lambda *a, **k: None)
        wiki = _wiki(tmp_path)
        usage = TokenUsage()
        client = MagicMock()
        dropped, _ = _screen(
            tmp_path, client=client, usage=usage, wiki_root=wiki
        )
        assert dropped is True
        # A cross-class reject is deterministic — no model call — but the
        # attempt is still counted against the run budget.
        client.messages.create.assert_not_called()
        assert usage.api_calls == 1
        # ...and surfaced for the human-audit calibration loop.
        assert calibration_summary(wiki)["T1"]["sampled"] == 1

    def test_model_reject_drops(self, tmp_path: Path) -> None:
        wiki = _wiki(tmp_path)
        # Same class → deterministic checks pass → model path; model rejects.
        paths = [
            _write_source(tmp_path / "a.md", name="Alpha", memory_class="fact"),
            _write_source(tmp_path / "b.md", name="Beta", memory_class="fact"),
        ]
        client = _mock_client('{"verdict": "reject", "reason": "different entities"}')
        dropped, _ = _screen(
            tmp_path, member_paths=paths, client=client, usage=None, wiki_root=wiki
        )
        assert dropped is True
        client.messages.create.assert_called_once()
        assert calibration_summary(wiki)["T1"]["sampled"] == 1

    def test_passup_flows_through_to_human_queue(self, tmp_path: Path) -> None:
        paths = [
            _write_source(tmp_path / "a.md", name="Alpha", memory_class="fact"),
            _write_source(tmp_path / "b.md", name="Alpha II", memory_class="fact"),
        ]
        client = _mock_client('{"verdict": "pass_up", "reason": "not confident"}')
        dropped, _ = _screen(
            tmp_path, member_paths=paths, client=client, usage=None
        )
        assert dropped is False  # a pass-up is written to the human queue as today
        client.messages.create.assert_called_once()

    def test_ceiling_trip_degrades_to_unscreened_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            screens_mod.spend, "ceiling_tripped", lambda *a, **k: "budget"
        )
        client = MagicMock()
        usage = TokenUsage()
        dropped, _ = _screen(tmp_path, client=client, usage=usage)
        assert dropped is False  # never blocks the queue — writes unscreened
        client.messages.create.assert_not_called()
        assert usage.api_calls == 0  # screen skipped before any spend
