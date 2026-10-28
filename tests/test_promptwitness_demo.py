# SPDX-License-Identifier: Apache-2.0
"""Tests for promptwitness.demo (athenaeum#2155).

Every path builds filesystem paths from ``tmp_path`` only -- never a
hardcoded machine-specific home-directory literal (the public-safe-lint
gate rejects that shape even in test fixtures).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from promptwitness import demo, verify


@pytest.fixture
def synthetic(tmp_path: Path) -> demo.SyntheticDemo:
    return demo.generate_synthetic(tmp_path / "ledger")


class TestGenerateSynthetic:
    def test_transcript_a_has_five_turns(self, synthetic: demo.SyntheticDemo) -> None:
        assert len(synthetic.transcript_a) == 5

    def test_transcript_b_has_six_rows(self, synthetic: demo.SyntheticDemo) -> None:
        assert len(synthetic.transcript_b) == 6

    def test_key_strength_is_software_file(self, synthetic: demo.SyntheticDemo) -> None:
        assert synthetic.key_provider.strength() == "software-file"

    def test_anchor_attestation_is_local_clock_and_complete(
        self, synthetic: demo.SyntheticDemo
    ) -> None:
        assert synthetic.anchor_attestation.source == "local-clock"
        assert synthetic.anchor_attestation.status == "complete"

    def test_chain_is_intact(self, synthetic: demo.SyntheticDemo) -> None:
        assert synthetic.ledger_index.chain_verification is not None
        assert synthetic.ledger_index.chain_verification.ok is True


class TestSyntheticVerdictsArePinned:
    """These two lists are the issue's own acceptance criteria, verbatim.
    Do not relax this test -- if the synthetic content changes, update the
    issue's AC and this test together, not just one of them."""

    def test_transcript_a_verdicts(self, synthetic: demo.SyntheticDemo) -> None:
        verdicts = verify.verify_transcript(synthetic.transcript_a, synthetic.ledger_index)
        assert [v.verdict for v in verdicts] == [
            "witnessed",
            "witnessed",
            "machine-authored",
            "witnessed",
            "witnessed",
        ]

    def test_transcript_b_verdicts(self, synthetic: demo.SyntheticDemo) -> None:
        verdicts = verify.verify_transcript(synthetic.transcript_b, synthetic.ledger_index)
        assert [v.verdict for v in verdicts] == [
            "witnessed",
            "witnessed",
            "machine-authored",
            "not-witnessed(inserted)",
            "digest-mismatch(edited)",
            "witnessed",
        ]


class TestRenderHtml:
    def _sections(self, synthetic: demo.SyntheticDemo) -> list[demo.DemoSection]:
        verdicts_a = verify.verify_transcript(synthetic.transcript_a, synthetic.ledger_index)
        verdicts_b = verify.verify_transcript(synthetic.transcript_b, synthetic.ledger_index)
        return [
            demo.DemoSection("Verified transcript", synthetic.transcript_a, verdicts_a),
            demo.DemoSection("Tampered transcript", synthetic.transcript_b, verdicts_b),
        ]

    def test_no_script_tag(self, synthetic: demo.SyntheticDemo) -> None:
        html_text = demo.render_html(
            self._sections(synthetic),
            mode="synthetic",
            key_strength=synthetic.key_provider.strength(),
            anchors=[("local-clock", "complete", "2026-01-01T00:00:00.000Z")],
        )
        assert "<script" not in html_text.lower()

    def test_both_section_titles_present(self, synthetic: demo.SyntheticDemo) -> None:
        html_text = demo.render_html(
            self._sections(synthetic),
            mode="synthetic",
            key_strength=synthetic.key_provider.strength(),
            anchors=(),
        )
        assert "Verified transcript" in html_text
        assert "Tampered transcript" in html_text

    def test_security_boundary_table_rendered_verbatim(
        self, synthetic: demo.SyntheticDemo
    ) -> None:
        html_text = demo.render_html(
            self._sections(synthetic),
            mode="synthetic",
            key_strength=synthetic.key_provider.strength(),
            anchors=(),
        )
        for mechanism, proves, not_proves in demo.SECURITY_BOUNDARY_TABLE:
            assert mechanism in html_text
            assert proves in html_text
            assert not_proves in html_text

    def test_badges_use_the_full_display_labels(self, synthetic: demo.SyntheticDemo) -> None:
        html_text = demo.render_html(
            self._sections(synthetic),
            mode="synthetic",
            key_strength=synthetic.key_provider.strength(),
            anchors=(),
        )
        assert "not witnessed (inserted)" in html_text
        assert "digest mismatch (edited)" in html_text
        assert "machine-authored (task-notification)" in html_text

    def test_html_escapes_prompt_text(self) -> None:
        verdict = verify.TurnVerdict(index=0, is_human=True, verdict=verify.VERDICT_WITNESSED)
        section = demo.DemoSection(
            "Section", [{"role": "human", "text": "<b>not html</b>"}], [verdict]
        )
        html_text = demo.render_html(
            [section], mode="synthetic", key_strength="software-file", anchors=()
        )
        assert "<b>not html</b>" not in html_text
        assert "&lt;b&gt;not html&lt;/b&gt;" in html_text

    def test_real_mode_banner_warns_about_real_prompt_text(
        self, synthetic: demo.SyntheticDemo
    ) -> None:
        html_text = demo.render_html(
            self._sections(synthetic),
            mode="real",
            key_strength=synthetic.key_provider.strength(),
            anchors=(),
        )
        assert "real prompt text" in html_text.lower()
        assert "do not publish" in html_text.lower()

    def test_no_network_markers(self, synthetic: demo.SyntheticDemo) -> None:
        """A static demo must carry no fetch/XHR/iframe-to-a-remote-origin --
        the closest a stdlib-only renderer can accidentally reintroduce
        network access."""
        html_text = demo.render_html(
            self._sections(synthetic),
            mode="synthetic",
            key_strength=synthetic.key_provider.strength(),
            anchors=(),
        )
        lowered = html_text.lower()
        for marker in ("fetch(", "xmlhttprequest", "<iframe", "http://", "https://"):
            assert marker not in lowered


class TestWriteDemoHtml:
    def test_synthetic_mode_writes_normally(self, tmp_path: Path) -> None:
        out = tmp_path / "demo.html"
        demo.write_demo_html(out, "<html></html>", restrict_mode=False)
        assert out.read_text(encoding="utf-8") == "<html></html>"

    def test_real_mode_writes_at_mode_0600(self, tmp_path: Path) -> None:
        out = tmp_path / "real-demo.html"
        demo.write_demo_html(out, "<html>real</html>", restrict_mode=True)
        assert out.read_text(encoding="utf-8") == "<html>real</html>"
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
