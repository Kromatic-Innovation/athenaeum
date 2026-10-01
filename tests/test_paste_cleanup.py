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
from types import SimpleNamespace

import pytest

from athenaeum import spend
from athenaeum.paste_cleanup import (
    PasteCleanupReport,
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
    resume_verification,
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


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A spend ledger path isolated to tmp via ATHENAEUM_SPEND_LEDGER
    (mirrors ``tests/test_spend.py``'s ``ledger`` fixture) -- without this,
    ``spend.record_spend``/``spend.ceiling_tripped`` resolve to the real
    ``~/.cache/athenaeum/spend.jsonl``."""
    path = tmp_path / "cache" / "spend.jsonl"
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(path))
    for var in (
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN",
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY",
        "ATHENAEUM_SPEND_MAX_USD_PER_RUN",
        "ATHENAEUM_SPEND_MAX_USD_PER_DAY",
        "ATHENAEUM_SPEND_LEDGER_ENABLED",
        "ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT",
        "ATHENAEUM_SPEND_MAX_PCT_PER_DAY",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


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

    def test_thinking_only_response_becomes_error_not_crash(self, tmp_path: Path) -> None:
        """Issue athenaeum#1717: the 2026-09-25 slice-1 dry-run crash.
        ``response_text()`` deliberately falls back to
        ``response.content[0].text`` when no ``type == "text"`` block is
        found, which raises ``AttributeError`` on a thinking-only response
        (an SDK ``ThinkingBlock`` has no ``.text``). That must become this
        bullet's error verdict, not an uncaught exception."""
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="internal reasoning only")],
            usage=make_llm_usage(input_tokens=12, output_tokens=3),
        )
        client = FakeLLMClient(response=response)
        content = "A short paste."
        verdict = propose_page(
            client,
            uid="u5",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            meta={},
            content=content,
            model="claude-haiku-4-5-20251001",
        )
        assert verdict.verdict == "error"
        assert verdict.error is not None
        assert "parse error" in verdict.error


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

    def test_verifier_transport_error_holds_instead_of_proposer_verdict(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#1903 AC1: a proposer ``remove`` whose verifier
        call raises must not fall through to the unverified proposer
        verdict -- it must hold for a human."""
        verdict = ProposalVerdict(
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: A paste.",
            paste_text="A paste.",
            extraction_status="clean",
            verdict="remove",
            reason="off-topic",
            confidence="low",
            model="claude-haiku-4-5-20251001",
        )
        client = FakeLLMClient(raises=RuntimeError("network down"))
        verify_page(client, verdict, meta={}, model="claude-sonnet-5")
        assert verdict.verify_attempted is True
        assert verdict.error is not None
        assert verdict.final_verdict() == "hold"

    def test_verifier_unparseable_response_holds_instead_of_proposer_verdict(
        self, tmp_path: Path
    ) -> None:
        """Same AC1, the thinking-only-response variant (athenaeum#1889's
        ``response_text()`` fallback raising) hitting the VERIFIER call
        instead of the proposer call."""
        verdict = ProposalVerdict(
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: A paste.",
            paste_text="A paste.",
            extraction_status="clean",
            verdict="remove",
            reason="off-topic",
            confidence="low",
            model="claude-haiku-4-5-20251001",
        )
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="internal reasoning only")],
            usage=make_llm_usage(input_tokens=12, output_tokens=3),
        )
        client = FakeLLMClient(response=response)
        verify_page(client, verdict, meta={}, model="claude-sonnet-5")
        assert verdict.verify_attempted is True
        assert verdict.error is not None
        assert verdict.final_verdict() == "hold"

    def test_keep_never_selected_for_verification_is_unaffected(self) -> None:
        """A ``keep`` proposal never passed to :func:`verify_page` at all
        (the common ``sampled``-rule case) keeps ``verify_attempted is
        False`` and its own verdict as final -- the flag must not itself
        change behavior for the untouched majority."""
        verdict = ProposalVerdict(
            uid="u1",
            path=Path("p.md"),
            date="2026-01-01",
            raw_chunk="",
            paste_text="A paste.",
            extraction_status="clean",
            verdict="keep",
            reason="genuine fact",
            confidence="high",
        )
        assert verdict.verify_attempted is False
        assert verdict.error is None
        assert verdict.final_verdict() == "keep"


class TestMaxTokensDefaults:
    """Issue athenaeum#1903 AC2: the 2026-09-25 slice-1 dry run's 5.4%
    verifier-error rate (20 thinking-only, 44 truncated JSON) traced to the
    512-token budget on both calls; raise the default to 1024."""

    def test_propose_page_default_max_tokens_is_1024(self, tmp_path: Path) -> None:
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
            )
        )
        content = "A short on-topic paste about the subject."
        propose_page(
            client,
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            meta={},
            content=content,
            model="claude-haiku-4-5-20251001",
        )
        assert client.calls[0]["max_tokens"] == 1024

    def test_verify_page_default_max_tokens_is_1024(self, tmp_path: Path) -> None:
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
        )
        client = FakeLLMClient(
            text=json.dumps(
                {"verdict": "remove", "claim": "", "reason": "confirmed", "agree": True}
            )
        )
        verify_page(client, verdict, meta={}, model="claude-sonnet-5")
        assert client.calls[0]["max_tokens"] == 1024


class TestProposalVerdictRoundTrip:
    """Issue athenaeum#1903 AC3: ``--from-report`` reconstructs
    ``ProposalVerdict``/``PasteCleanupReport`` from a prior ``--json``
    report; ``from_dict(to_dict(v))`` must carry everything
    ``apply_paste_cleanup_report`` and ``final_claim`` need."""

    def test_to_dict_from_dict_round_trip_preserves_apply_fields(self, tmp_path: Path) -> None:
        verdict = ProposalVerdict(
            uid="u1",
            path=tmp_path / "p.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: A verifier-corrected paste.",
            paste_text="A verifier-corrected paste.",
            extraction_status="clean",
            verdict="remove",
            claim="",
            reason="off-topic",
            confidence="low",
            model="claude-haiku-4-5-20251001",
            verified=True,
            verifier_verdict="rewrite",
            verifier_agree=False,
            verifier_reason="actually on-topic",
            verifier_claim="Corrected claim (source: x).",
            verify_attempted=True,
        )
        restored = ProposalVerdict.from_dict(verdict.to_dict())
        assert restored.uid == verdict.uid
        assert restored.path == verdict.path
        assert restored.raw_chunk == verdict.raw_chunk
        assert restored.final_verdict() == verdict.final_verdict() == "rewrite"
        assert restored.final_claim() == verdict.final_claim() == "Corrected claim (source: x)."

    def test_report_from_dict_rejects_version_mismatch(self) -> None:
        payload = {"version": "paste-cleanup-v0-does-not-exist", "proposed": []}
        with pytest.raises(ValueError, match="version mismatch"):
            PasteCleanupReport.from_dict(payload)

    def test_report_from_dict_rejects_pre_1903_shape_missing_raw_chunk(self) -> None:
        """Sentry Seer finding on athenaeum#1904: a report shaped like the OLD
        ``to_dict()`` (no ``raw_chunk``/``claim``/etc.) must never reach
        ``ProposalVerdict.from_dict``'s field construction with a
        silently-defaulted ``raw_chunk`` -- that empty string would match
        every position in a live page body and corrupt it on apply. The
        version bump (v1 -> v2) is the primary guard; this asserts the
        version check actually fires for an old-shaped payload that still
        claims the CURRENT version (the case a stale/hand-edited caller
        could produce)."""
        old_shaped_verdict = {
            "uid": "person1",
            "path": "person1.md",
            "date": "2026-01-01",
            "extraction_status": "clean",
            "verdict": "remove",
            "confidence": "high",
            "reason": "off-topic",
            "model": "claude-haiku-4-5-20251001",
            "verified": False,
            "verifier_verdict": None,
            "verifier_agree": None,
            "final_verdict": "remove",
            "error": None,
            # No raw_chunk/claim/verifier_claim/verifier_reason/verify_attempted --
            # exactly the pre-athenaeum#1903 to_dict() shape.
        }
        with pytest.raises(KeyError):
            ProposalVerdict.from_dict(old_shaped_verdict)

    def test_report_round_trip_and_apply_writes_only_verified_rows(self, wiki: Path) -> None:
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
            verify_rule="all",
            length_threshold=10,
        )
        replayed = PasteCleanupReport.from_dict(json.loads(json.dumps(report.to_dict())))
        changed = apply_paste_cleanup_report(replayed, wiki)
        assert changed == 1
        after = (wiki / "person1.md").read_text(encoding="utf-8")
        assert content not in after


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


# --- spend wiring (issue athenaeum#1717 AC4) -------------------------------


class TestSpendWiring:
    def test_thinking_only_response_does_not_kill_the_pass(
        self, wiki: Path, ledger: Path
    ) -> None:
        """A text-less response on one page becomes that page's error verdict
        and the pass continues to process the next page (regression for the
        2026-09-25 slice-1 dry-run crash)."""
        content1 = "Off-topic internal retro content unrelated to the subject." * 5
        content2 = "A different off-topic paste about another person entirely." * 5
        _page(
            wiki,
            "person1.md",
            "uid: person1\nname: Person One\n",
            f"## Notes\n\n- 2026-01-01: {content1}\n",
        )
        _page(
            wiki,
            "person2.md",
            "uid: person2\nname: Person Two\n",
            f"## Notes\n\n- 2026-01-02: {content2}\n",
        )

        calls = {"n": 0}

        def _responder(**_kwargs: object) -> SimpleNamespace:
            calls["n"] += 1
            if calls["n"] == 1:
                # Thinking-only: no type == "text" block anywhere.
                return SimpleNamespace(
                    content=[SimpleNamespace(type="thinking", thinking="internal")],
                    usage=make_llm_usage(input_tokens=12, output_tokens=3),
                )
            if calls["n"] == 2:
                return make_llm_response(
                    json.dumps(
                        {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                    ),
                    usage=make_llm_usage(input_tokens=50, output_tokens=10),
                )
            # Any verify-pass call: agree.
            return make_llm_response(
                json.dumps({"verdict": "keep", "claim": "", "reason": "ok", "agree": True}),
                usage=make_llm_usage(input_tokens=20, output_tokens=5),
            )

        client = FakeLLMClient(responder=_responder)
        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="all",
            length_threshold=10,
        )

        assert len(report.proposed) == 2
        first, second = report.proposed
        assert first.verdict == "error"
        assert first.error is not None and "parse error" in first.error
        assert second.verdict == "keep"
        assert second.error is None
        assert report.ceiling_reason is None

    def test_spend_recorded_under_run_type_paste_cleanup(
        self, wiki: Path, ledger: Path
    ) -> None:
        content = "A short on-topic paste about the subject here for testing." * 3
        _page(
            wiki,
            "person1.md",
            "uid: person1\nname: Person One\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                ),
                usage=make_llm_usage(input_tokens=100, output_tokens=20),
            )
        )
        build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )

        assert ledger.is_file()
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        assert lines[0]["run_type"] == spend.RUN_TYPE_PASTE_CLEANUP
        assert lines[0]["run_type"] == "paste-cleanup"
        assert lines[0]["input_tokens"] >= 100
        assert lines[0]["api_calls"] >= 1

    def test_tripped_ceiling_stops_the_pass_cleanly(
        self, wiki: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Any single successful call costs far more than this at the blended
        # fallback rate ($1.50/M input + $7.50/M output), so the ceiling trips
        # on the check BEFORE the second candidate, not the first.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "0.0000001")
        for i in range(1, 4):
            content = f"An on-topic paste number {i} about the subject here for testing." * 3
            _page(
                wiki,
                f"person{i}.md",
                f"uid: person{i}\nname: Person {i}\n",
                f"## Notes\n\n- 2026-01-0{i}: {content}\n",
            )
        client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                ),
                usage=make_llm_usage(input_tokens=100, output_tokens=20),
            )
        )
        verify_client = FakeLLMClient(raises=AssertionError("verify must not be called"))

        report = build_paste_cleanup_report(
            wiki,
            client=client,
            verify_client=verify_client,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="all",
            length_threshold=10,
        )

        assert report.ceiling_reason is not None
        assert "ceiling" in report.ceiling_reason
        # First candidate processed before the trip; the rest left alone.
        assert len(report.proposed) == 1
        assert len(verify_client.calls) == 0
        assert "stopped early" in report.render_text()

        # What was actually spent (the one successful call) is still recorded.
        assert ledger.is_file()
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        assert lines[0]["run_type"] == spend.RUN_TYPE_PASTE_CLEANUP

    def test_mechanical_dry_run_records_no_spend(self, wiki: Path, ledger: Path) -> None:
        content = "A short on-topic paste about the subject here for testing." * 3
        _page(
            wiki,
            "person1.md",
            "uid: person1\nname: Person One\n",
            f"## Notes\n\n- 2026-01-01: {content}\n",
        )
        report = build_paste_cleanup_report(
            wiki,
            client=None,
            verify_client=None,
            model="claude-haiku-4-5-20251001",
            verify_model="claude-sonnet-5",
            verify_rule="sampled",
            length_threshold=10,
        )

        assert report.scanned == 1
        assert report.ceiling_reason is None
        assert not ledger.exists()


# --- verifier-pass resume (issue athenaeum#1923) ---------------------------


def _three_candidate_pages(wiki: Path) -> None:
    for i in range(1, 4):
        content = f"An on-topic paste number {i} about the subject here for testing." * 3
        _page(
            wiki,
            f"person{i}.md",
            f"uid: person{i}\nname: Person {i}\n",
            f"## Notes\n\n- 2026-01-0{i}: {content}\n",
        )


_PROPOSE_MODEL = "claude-haiku-4-5-20251001"  # matches rate prefix (1.0, 5.0) $/MTok
_VERIFY_MODEL = "claude-sonnet-5"  # matches rate (3.0, 15.0) $/MTok


class TestVerifierResume:
    """Issue athenaeum#1923: a verifier-pass ceiling trip must RESUME
    verification instead of leaving bullets ``verify_attempted: False``
    forever (AC1), and a trip during the PROPOSER pass must no longer skip
    the verifier pass entirely (AC2). Every test uses the ``ledger``
    fixture -- ``resume_verification`` calls ``spend.ceiling_tripped``/
    ``spend.record_spend`` exactly like ``build_paste_cleanup_report`` does.
    """

    def test_ceiling_trip_mid_verifier_batch_resumes_cleanly(
        self, wiki: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1, the headline: a ceiling trips mid-verifier-batch (3 bullets,
        ``verify_rule="all"``); some are left unattempted; clearing the
        ceiling and resuming verifies every selected bullet exactly once,
        with no re-verification of the one already done."""
        _three_candidate_pages(wiki)
        propose_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                ),
                usage=make_llm_usage(input_tokens=1000, output_tokens=0),
            )
        )
        verify_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps({"verdict": "keep", "claim": "", "reason": "ok", "agree": True}),
                usage=make_llm_usage(input_tokens=1000, output_tokens=0),
            )
        )
        # 3 proposer calls @ $0.001 each = $0.003, then verify calls @
        # $0.003 each: the 1st verify call brings the running total to
        # $0.006 -- past this $0.005 cap -- so the 2nd verify call's
        # pre-check trips before it is made.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "0.005")

        report = build_paste_cleanup_report(
            wiki,
            client=propose_client,
            verify_client=verify_client,
            model=_PROPOSE_MODEL,
            verify_model=_VERIFY_MODEL,
            verify_rule="all",
            length_threshold=10,
        )

        assert report.proposer_truncated is False
        assert len(report.proposed) == 3
        assert report.ceiling_reason is not None
        pending_before = report.pending_verification()
        assert pending_before == [1, 2]
        assert len(verify_client.calls) == 1

        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        resume_verification(report, wiki, verify_client=verify_client, config=None)

        assert report.pending_verification() == []
        assert all(v.verify_attempted for v in report.proposed)
        assert len(verify_client.calls) == 1 + len(pending_before)

    def test_proposer_trip_no_longer_skips_the_verifier_pass(
        self, wiki: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 / the issue title: a ceiling trip during the PROPOSER pass
        leaves ``proposer_truncated`` True and ``ceiling_reason`` set, but
        resuming still verifies the ONE proposal that WAS made -- and
        ``ceiling_reason`` stays set afterward because candidates 2 and 3
        were never proposed at all; a resume cannot fix that."""
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "0.0000001")
        _three_candidate_pages(wiki)
        propose_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                ),
                usage=make_llm_usage(input_tokens=100, output_tokens=20),
            )
        )
        verify_client = FakeLLMClient(raises=AssertionError("verify must not be called"))

        report = build_paste_cleanup_report(
            wiki,
            client=propose_client,
            verify_client=verify_client,
            model=_PROPOSE_MODEL,
            verify_model=_VERIFY_MODEL,
            verify_rule="all",
            length_threshold=10,
        )

        assert report.proposer_truncated is True
        assert len(report.proposed) == 1
        assert report.ceiling_reason is not None
        assert len(verify_client.calls) == 0
        assert report.pending_verification() == [0]

        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        resumed_verify_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps({"verdict": "keep", "claim": "", "reason": "ok", "agree": True}),
                usage=make_llm_usage(input_tokens=100, output_tokens=20),
            )
        )
        resume_verification(report, wiki, verify_client=resumed_verify_client, config=None)

        assert report.pending_verification() == []
        assert len(resumed_verify_client.calls) == 1
        assert report.ceiling_reason is not None

    def test_resume_clears_ceiling_reason_when_proposer_was_not_truncated(
        self, wiki: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A complete resume (no bullet left pending) clears
        ``ceiling_reason`` when the proposer loop ran to completion -- the
        "stopped early" banner must stop lying once the run is genuinely
        done."""
        _three_candidate_pages(wiki)
        propose_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                ),
                usage=make_llm_usage(input_tokens=1000, output_tokens=0),
            )
        )
        verify_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps({"verdict": "keep", "claim": "", "reason": "ok", "agree": True}),
                usage=make_llm_usage(input_tokens=1000, output_tokens=0),
            )
        )
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", "0.005")

        report = build_paste_cleanup_report(
            wiki,
            client=propose_client,
            verify_client=verify_client,
            model=_PROPOSE_MODEL,
            verify_model=_VERIFY_MODEL,
            verify_rule="all",
            length_threshold=10,
        )
        assert report.proposer_truncated is False
        assert report.ceiling_reason is not None

        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_USD_PER_RUN", raising=False)
        resume_verification(report, wiki, verify_client=verify_client, config=None)

        assert report.pending_verification() == []
        assert report.ceiling_reason is None

    def test_round_trip_resume_recovers_paste_text_for_the_prompt(
        self, tmp_path: Path, ledger: Path
    ) -> None:
        """The real ``--from-report`` shape: ``paste_text`` is lost on the
        ``to_dict``/``from_dict`` round trip. Without
        ``_paste_text_for_resume`` the resumed verify call would render its
        prompt with an EMPTY paste -- assert the paste text the fake client
        actually received, and that the bullet ends verified."""
        content = "An attributed paste about the subject that is reasonably long for testing."
        verdict = ProposalVerdict(
            uid="person1",
            path=tmp_path / "person1.md",
            date="2026-01-01",
            raw_chunk=f"- 2026-01-01: {content}",
            paste_text=content,
            extraction_status="clean",
            verdict="keep",
            reason="on-topic",
            confidence="high",
            model=_PROPOSE_MODEL,
        )
        report = PasteCleanupReport(
            proposed=[verdict], verify_rule="all", model=_PROPOSE_MODEL, verify_model=_VERIFY_MODEL
        )
        replayed = PasteCleanupReport.from_dict(json.loads(json.dumps(report.to_dict())))
        assert replayed.proposed[0].paste_text == ""  # lost on the round trip, as expected
        assert replayed.pending_verification() == [0]

        verify_client = FakeLLMClient(
            text=json.dumps({"verdict": "keep", "claim": "", "reason": "ok", "agree": True})
        )
        resume_verification(replayed, tmp_path, verify_client=verify_client, config=None)

        assert replayed.pending_verification() == []
        assert replayed.proposed[0].verify_attempted is True
        prompt = verify_client.calls[0]["messages"][0]["content"]
        assert content in prompt

    def test_no_op_resume_makes_no_calls_and_writes_no_ledger_row(
        self, tmp_path: Path, ledger: Path
    ) -> None:
        verdict = ProposalVerdict(
            uid="person1",
            path=tmp_path / "person1.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: On-topic paste text here.",
            paste_text="On-topic paste text here.",
            extraction_status="clean",
            verdict="keep",
            confidence="high",
            verify_attempted=True,
            verified=True,
            verifier_verdict="keep",
            verifier_agree=True,
        )
        report = PasteCleanupReport(
            proposed=[verdict], verify_rule="all", verify_model=_VERIFY_MODEL
        )
        assert report.pending_verification() == []

        client = FakeLLMClient(raises=AssertionError("must not be called on a no-op resume"))
        result = resume_verification(report, tmp_path, verify_client=client, config=None)

        assert result is report
        assert len(client.calls) == 0
        assert not ledger.exists()

    def test_resume_records_one_ledger_row_and_grows_usage_by_its_own_tokens_only(
        self, tmp_path: Path, ledger: Path
    ) -> None:
        verdict0 = ProposalVerdict(
            uid="person1",
            path=tmp_path / "person1.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: Already fully verified paste text here.",
            paste_text="Already fully verified paste text here.",
            extraction_status="clean",
            verdict="keep",
            confidence="high",
            model=_PROPOSE_MODEL,
            input_tokens=50,
            output_tokens=10,
            verify_attempted=True,
            verified=True,
            verifier_verdict="keep",
            verifier_agree=True,
        )
        verdict1 = ProposalVerdict(
            uid="person2",
            path=tmp_path / "person2.md",
            date="2026-01-02",
            raw_chunk="- 2026-01-02: Still-pending paste text here for the resume.",
            paste_text="Still-pending paste text here for the resume.",
            extraction_status="clean",
            verdict="remove",
            confidence="high",
            model=_PROPOSE_MODEL,
            input_tokens=30,
            output_tokens=5,
            verify_attempted=False,
        )
        report = PasteCleanupReport(
            proposed=[verdict0, verdict1], verify_rule="all", verify_model=_VERIFY_MODEL
        )
        report.usage.add_tokens(verdict0.input_tokens, verdict0.output_tokens, model=verdict0.model)
        report.usage.add_tokens(verdict1.input_tokens, verdict1.output_tokens, model=verdict1.model)
        assert report.pending_verification() == [1]

        verify_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "remove", "claim": "", "reason": "confirmed", "agree": True}
                ),
                usage=make_llm_usage(input_tokens=20, output_tokens=4),
            )
        )
        resume_verification(report, tmp_path, verify_client=verify_client, config=None)

        assert report.pending_verification() == []
        assert report.usage.input_tokens == 50 + 30 + 20
        assert report.usage.output_tokens == 10 + 5 + 4

        assert ledger.is_file()
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        assert lines[0]["run_type"] == spend.RUN_TYPE_PASTE_CLEANUP

    def test_bullet_rule_never_selected_is_not_pending_and_not_verified_by_resume(
        self, tmp_path: Path, ledger: Path
    ) -> None:
        """A bullet :func:`select_verify_sample` never selects under the
        ``sampled`` rule is correctly unverified BY RULE -- not a ceiling
        casualty -- so it is never in ``pending_verification()`` and a
        resume must never verify it."""
        selected_verdict = ProposalVerdict(
            uid="low1",
            path=tmp_path / "low1.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: A low-confidence paste that is always sampled.",
            paste_text="A low-confidence paste that is always sampled.",
            extraction_status="clean",
            verdict="keep",
            confidence="low",
        )
        # Confirmed offline: _stable_sample_fraction("high_not_selected:2026-01-01", 0.10)
        # is False -- this uid falls outside the stable 10% sample.
        not_selected_verdict = ProposalVerdict(
            uid="high_not_selected",
            path=tmp_path / "high_not_selected.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: A high-confidence paste that the sample rule skips.",
            paste_text="A high-confidence paste that the sample rule skips.",
            extraction_status="clean",
            verdict="keep",
            confidence="high",
        )
        report = PasteCleanupReport(
            proposed=[selected_verdict, not_selected_verdict],
            verify_rule="sampled",
            verify_model=_VERIFY_MODEL,
        )
        assert report.pending_verification() == [0]

        verify_client = FakeLLMClient(
            response=make_llm_response(
                json.dumps({"verdict": "keep", "claim": "", "reason": "ok", "agree": True}),
                usage=make_llm_usage(input_tokens=10, output_tokens=2),
            )
        )
        resume_verification(report, tmp_path, verify_client=verify_client, config=None)

        assert selected_verdict.verify_attempted is True
        assert not_selected_verdict.verify_attempted is False
        assert len(verify_client.calls) == 1

    def test_pending_verification_survives_round_trip(self, tmp_path: Path) -> None:
        pending_verdict = ProposalVerdict(
            uid="p1",
            path=tmp_path / "p1.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: Pending paste text.",
            paste_text="Pending paste text.",
            extraction_status="clean",
            verdict="keep",
            confidence="low",
        )
        done_verdict = ProposalVerdict(
            uid="p2",
            path=tmp_path / "p2.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: Already-verified paste text.",
            paste_text="Already-verified paste text.",
            extraction_status="clean",
            verdict="keep",
            confidence="low",
            verify_attempted=True,
            verified=True,
            verifier_verdict="keep",
            verifier_agree=True,
        )
        report = PasteCleanupReport(proposed=[pending_verdict, done_verdict], verify_rule="sampled")
        before = report.pending_verification()
        replayed = PasteCleanupReport.from_dict(json.loads(json.dumps(report.to_dict())))
        after = replayed.pending_verification()
        assert before == [0]
        assert after == [0]

    def test_resume_with_empty_model_raises(self, tmp_path: Path, ledger: Path) -> None:
        verdict = ProposalVerdict(
            uid="p1",
            path=tmp_path / "p1.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: Some paste text.",
            paste_text="Some paste text.",
            extraction_status="clean",
            verdict="keep",
            confidence="low",
        )
        report = PasteCleanupReport(proposed=[verdict], verify_rule="sampled", verify_model="")
        client = FakeLLMClient(raises=AssertionError("must not be called"))
        with pytest.raises(ValueError):
            resume_verification(report, tmp_path, verify_client=client, config=None)
        assert len(client.calls) == 0
