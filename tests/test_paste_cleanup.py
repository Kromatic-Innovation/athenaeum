# SPDX-License-Identifier: Apache-2.0
"""``athenaeum paste-cleanup`` — tier-0 attributed-paste cleanup pass
(issue athenaeum#1717).

Covers: bullet extraction; the no-LLM fusion-boundary heuristic
(:func:`extract_paste_span`) on invented fixtures shaped like the
2026-09-24 operator-review findings (never real corpus text); proposer/
verifier prompt+response round-trips against :class:`FakeLLMClient`;
verify-sample selection (low-confidence + stable 10% sample, or "all");
the >=90%% verify-rule threshold; agreement measurement; and the dry-run/
apply split (apply re-locates the exact bullet chunk, never trusts the
scan). All fixtures — no test reads or writes a live knowledge store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.paste_cleanup import (
    ProposalVerdict,
    apply_paste_cleanup_report,
    build_paste_cleanup_report,
    choose_verify_rule,
    extract_notes_bullets,
    extract_paste_span,
    measure_agreement,
    parse_propose_response,
    parse_verify_response,
    propose_page,
    select_verify_sample,
    verify_page,
)
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage


def _page(root: Path, name: str, frontmatter: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


# --- extraction ----------------------------------------------------------


class TestExtractNotesBullets:
    def test_single_bullet(self) -> None:
        body = "## Notes\n\n- 2026-01-01: A single short bullet.\n"
        bullets = extract_notes_bullets(body)
        assert len(bullets) == 1
        date, content, raw_chunk = bullets[0]
        assert date == "2026-01-01"
        assert content == "A single short bullet."
        assert raw_chunk.startswith("- 2026-01-01: ")

    def test_two_bullets_split_on_blank_line(self) -> None:
        body = "## Notes\n\n- 2026-02-01: Second, most recent.\n\n- 2026-01-01: First, older.\n"
        bullets = extract_notes_bullets(body)
        assert [d for d, _, _ in bullets] == ["2026-02-01", "2026-01-01"]

    def test_no_notes_section(self) -> None:
        assert extract_notes_bullets("# Just a page\n\nNo notes here.\n") == []


class TestExtractPasteSpan:
    def test_clean_short_paste_returned_whole(self) -> None:
        content = "Mark and Grant discussed the Q3 roadmap in passing." * 3
        paste, remainder, status = extract_paste_span(content)
        assert status == "clean"
        assert paste == content
        assert remainder == ""

    def test_fused_content_splits_at_legitimate_boundary(self) -> None:
        mural = "This board captured ideas from a workshop session. " * 40  # > 1700 chars
        legitimate = "\n\nCRM stage: qualified. Previously known as Acme Corp."
        content = mural + legitimate
        paste, remainder, status = extract_paste_span(content)
        assert status == "split"
        assert paste == mural.rstrip()
        assert remainder.startswith("CRM stage:")

    def test_confident_tier_workshop_summary_with_no_boundary_holds(self) -> None:
        # Long, opens like a workshop/mural summary, but carries none of the
        # evidence-derived fusion-tail markers anywhere -- extraction cannot
        # tell where a fused legitimate tail would start, so it must not guess.
        content = "This session covered many unrelated topics in long form. " * 60
        assert len(content) >= 2000
        paste, remainder, status = extract_paste_span(content)
        assert status == "hold"
        assert paste == content  # never trimmed on hold
        assert remainder == ""

    def test_short_workshop_opener_is_clean_not_held(self) -> None:
        content = "This board was prepared but stayed short."
        _paste, _remainder, status = extract_paste_span(content)
        assert status == "clean"


# --- response parsing ------------------------------------------------------


class TestParsePropose:
    def test_keep(self) -> None:
        parsed = parse_propose_response(
            json.dumps({"verdict": "keep", "claim": "", "reason": "on-topic", "confidence": "high"})
        )
        assert parsed == {
            "verdict": "keep",
            "claim": "",
            "reason": "on-topic",
            "confidence": "high",
        }

    def test_fenced_json_block(self) -> None:
        text = (
            '```json\n{"verdict": "remove", "claim": "", '
            '"reason": "off-topic", "confidence": "medium"}\n```'
        )
        parsed = parse_propose_response(text)
        assert parsed["verdict"] == "remove"

    def test_rewrite_requires_claim(self) -> None:
        with pytest.raises(ValueError):
            parse_propose_response(
                json.dumps({"verdict": "rewrite", "claim": "", "reason": "x", "confidence": "low"})
            )

    def test_invalid_verdict_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_propose_response(json.dumps({"verdict": "delete", "confidence": "high"}))

    def test_invalid_confidence_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_propose_response(json.dumps({"verdict": "keep", "confidence": "certain"}))


class TestParseVerify:
    def test_agree(self) -> None:
        parsed = parse_verify_response(
            json.dumps({"verdict": "remove", "claim": "", "reason": "x", "agree": True})
        )
        assert parsed["agree"] is True

    def test_disagree_with_new_claim(self) -> None:
        parsed = parse_verify_response(
            json.dumps(
                {"verdict": "rewrite", "claim": "Corrected claim.", "reason": "x", "agree": False}
            )
        )
        assert parsed["agree"] is False
        assert parsed["claim"] == "Corrected claim."


# --- propose_page / verify_page --------------------------------------------


class TestProposePage:
    def test_hold_short_circuits_before_any_llm_call(self, tmp_path: Path) -> None:
        content = "This board covered many unrelated topics in long form. " * 60
        client = FakeLLMClient(text="should never be called")
        verdict = propose_page(
            client,
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            meta={"name": "Test"},
            content=content,
            model="claude-haiku-4-5-20251001",
        )
        assert verdict.verdict == "hold"
        assert len(client.calls) == 0

    def test_keep_verdict_happy_path(self, tmp_path: Path) -> None:
        response_json = json.dumps(
            {"verdict": "keep", "claim": "", "reason": "genuine fact", "confidence": "high"}
        )
        client = FakeLLMClient(
            response=make_llm_response(
                response_json, usage=make_llm_usage(input_tokens=100, output_tokens=20)
            )
        )
        content = "A short on-topic paste about the subject."
        verdict = propose_page(
            client,
            uid="u2",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            meta={"name": "Test"},
            content=content,
            model="claude-haiku-4-5-20251001",
        )
        assert verdict.verdict == "keep"
        assert verdict.input_tokens == 100
        assert verdict.output_tokens == 20
        assert verdict.error is None

    def test_bad_json_becomes_error_not_crash(self, tmp_path: Path) -> None:
        client = FakeLLMClient(text="not json at all")
        content = "A short paste."
        verdict = propose_page(
            client,
            uid="u3",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            meta={},
            content=content,
            model="claude-haiku-4-5-20251001",
        )
        assert verdict.verdict == "error"
        assert verdict.error is not None

    def test_transport_failure_becomes_error_not_crash(self, tmp_path: Path) -> None:
        client = FakeLLMClient(raises=RuntimeError("network down"))
        content = "A short paste."
        verdict = propose_page(
            client,
            uid="u4",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            meta={},
            content=content,
            model="claude-haiku-4-5-20251001",
        )
        assert verdict.verdict == "error"
        assert "network down" in verdict.error


class TestVerifyPage:
    def test_agree_keeps_final_verdict(self, tmp_path: Path) -> None:
        verdict = ProposalVerdict(
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk="",
            paste_text="A paste.",
            extraction_status="clean",
            verdict="remove",
            reason="off-topic",
            confidence="medium",
            model="claude-haiku-4-5-20251001",
        )
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "confirmed", "agree": True}
            )
        )
        verify_page(client, verdict, meta={}, model="claude-sonnet-5")
        assert verdict.final_verdict() == "remove"
        assert verdict.verified is True

    def test_disagree_overrides_final_verdict_and_claim(self, tmp_path: Path) -> None:
        verdict = ProposalVerdict(
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk="",
            paste_text="A paste.",
            extraction_status="clean",
            verdict="remove",
            reason="off-topic",
            confidence="low",
            model="claude-haiku-4-5-20251001",
        )
        client = FakeLLMClient(
            text=json.dumps(
                {
                    "verdict": "rewrite",
                    "claim": "Corrected claim (source: x).",
                    "reason": "actually on-topic",
                    "agree": False,
                }
            )
        )
        verify_page(client, verdict, meta={}, model="claude-sonnet-5")
        assert verdict.final_verdict() == "rewrite"
        assert verdict.final_claim() == "Corrected claim (source: x)."


# --- verify-sample selection -------------------------------------------


def _verdict(uid: str, verdict: str, confidence: str | None) -> ProposalVerdict:
    return ProposalVerdict(
        uid=uid,
        path=Path(f"{uid}.md"),
        date="2026-01-01",
        raw_chunk="",
        paste_text="",
        extraction_status="clean",
        verdict=verdict,
        confidence=confidence,
    )


class TestSelectVerifySample:
    def test_all_rule_selects_every_eligible(self) -> None:
        verdicts = [
            _verdict("a", "keep", "high"),
            _verdict("b", "remove", "high"),
            _verdict("c", "hold", None),
        ]
        selected = select_verify_sample(verdicts, rule="all")
        assert selected == {0, 1}  # hold excluded

    def test_sampled_rule_always_includes_low_confidence(self) -> None:
        verdicts = [_verdict(f"low{i}", "keep", "low") for i in range(20)]
        selected = select_verify_sample(verdicts, rule="sampled")
        assert selected == set(range(20))

    def test_sampled_rule_is_deterministic_across_calls(self) -> None:
        verdicts = [_verdict(f"uid{i}", "keep", "high") for i in range(500)]
        first = select_verify_sample(verdicts, rule="sampled")
        second = select_verify_sample(verdicts, rule="sampled")
        assert first == second
        # roughly a 10% sample -- generous bounds to avoid flakiness
        assert 20 <= len(first) <= 100

    def test_unknown_rule_rejected(self) -> None:
        with pytest.raises(ValueError):
            select_verify_sample([], rule="bogus")


class TestChooseVerifyRule:
    def test_at_threshold_is_sampled(self) -> None:
        assert choose_verify_rule(0.90) == "sampled"

    def test_above_threshold_is_sampled(self) -> None:
        assert choose_verify_rule(1.0) == "sampled"

    def test_below_threshold_is_all(self) -> None:
        assert choose_verify_rule(0.8999) == "all"


# --- agreement measurement -----------------------------------------------


class TestMeasureAgreement:
    def test_full_agreement(self) -> None:
        rows = [
            {"proposed": "keep", "operator": "keep", "confidence": "high"},
            {"proposed": "remove", "operator": "remove", "confidence": "high"},
        ]
        report = measure_agreement(rows)
        assert report.agree == 2
        assert report.agreement_rate == 1.0

    def test_hold_rows_counted_separately_not_as_disagreement(self) -> None:
        rows = [
            {"proposed": "hold", "operator": "remove", "confidence": None},
            {"proposed": "keep", "operator": "keep", "confidence": "high"},
        ]
        report = measure_agreement(rows)
        assert report.hold == 1
        assert report.total == 2
        assert report.agree == 1
        assert report.agreement_rate == 1.0  # scored over non-hold rows only

    def test_confusion_matrix_and_confidence_buckets(self) -> None:
        rows = [
            {"proposed": "remove", "operator": "rewrite", "confidence": "medium"},
            {"proposed": "remove", "operator": "remove", "confidence": "high"},
        ]
        report = measure_agreement(rows)
        assert report.confusion[("remove", "rewrite")] == 1
        assert report.confusion[("remove", "remove")] == 1
        assert report.by_confidence["medium"] == {"total": 1, "agree": 0}
        assert report.by_confidence["high"] == {"total": 1, "agree": 1}


# --- dry-run report + apply integration ------------------------------------


class TestBuildAndApplyReport:
    def test_dry_run_writes_nothing(self, wiki: Path) -> None:
        content = "Off-topic internal retro content unrelated to the subject." * 10
        _page(
            wiki,
            "person1.md",
            "uid: person1\nname: Person One\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        before = (wiki / "person1.md").read_text(encoding="utf-8")
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "off-topic", "confidence": "high"}
            )
        )
        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )
        assert report.scanned == 1
        assert report.bullets_found == 1
        after = (wiki / "person1.md").read_text(encoding="utf-8")
        assert before == after

    def test_apply_removes_remove_verdict_bullet(self, wiki: Path) -> None:
        content = "Off-topic internal retro content unrelated to the subject." * 10
        _page(
            wiki,
            "person1.md",
            "uid: person1\nname: Person One\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "off-topic", "confidence": "high"}
            )
        )
        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person1.md").read_text(encoding="utf-8")
        assert content not in after

    def test_apply_rewrites_rewrite_verdict_bullet(self, wiki: Path) -> None:
        content = "A messy paste with a fact buried in it about the subject." * 5
        _page(
            wiki,
            "person2.md",
            "uid: person2\nname: Person Two\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        client = FakeLLMClient(
            text=json.dumps(
                {
                    "verdict": "rewrite",
                    "claim": "Tightened claim (source: raw).",
                    "reason": "buried fact",
                    "confidence": "high",
                }
            )
        )
        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person2.md").read_text(encoding="utf-8")
        assert "Tightened claim (source: raw)." in after
        assert content not in after

    def test_apply_never_writes_keep_or_hold(self, wiki: Path) -> None:
        content = "A genuinely on-topic short paste about the subject here." * 3
        _page(
            wiki,
            "person3.md",
            "uid: person3\nname: Person Three\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "keep", "claim": "", "reason": "on-topic", "confidence": "high"}
            )
        )
        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 0
        after = (wiki / "person3.md").read_text(encoding="utf-8")
        assert content in after

    def test_apply_skips_page_changed_since_scan(self, wiki: Path) -> None:
        content = "Off-topic internal retro content unrelated to the subject." * 10
        page = _page(
            wiki,
            "person4.md",
            "uid: person4\nname: Person Four\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "off-topic", "confidence": "high"}
            )
        )
        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )
        # Simulate a concurrent writer changing the page between scan and apply.
        page.write_text(
            "---\nuid: person4\nname: Person Four\n---\n## Notes\n\n"
            "- 2026-01-01: Something else entirely.\n",
            encoding="utf-8",
        )
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 0
