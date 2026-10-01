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
    chunk_identity_hash,
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


# --- operator-corrected claim override (issue athenaeum#1924) -------------


def _page_with_bullet(
    wiki: Path, name: str, content: str, *, date: str = "2026-01-01"
) -> tuple[Path, str]:
    """A one-bullet page plus the exact ``raw_chunk`` apply must re-locate --
    the minimal fixture shape every test below builds a :class:`ProposalVerdict`
    against (mirrors ``_page``, but also hands back ``raw_chunk`` so a test
    never has to reconstruct the ``- date: content`` head itself)."""
    page = _page(wiki, name, f"uid: {name}\nname: Test\n", f"## Notes\n\n- {date}: {content}\n")
    return page, f"- {date}: {content}"


class TestOperatorClaimOverride:
    """Issue athenaeum#1924: an operator's hand-corrected claim, carried on
    a report entry, must be written VERBATIM by ``--apply`` in preference
    to the model's own claim AND verdict (AC1) -- including over a `remove`
    verdict that would otherwise delete the bullet outright, the literal
    defect observed twice on athenaeum#1717 (phases B1 and B2). AC2 (no
    override fields present -> no behaviour change) is its own explicit
    regression test below, not just an absence of failures elsewhere.
    """

    def test_override_wins_over_model_rewrite(self, wiki: Path) -> None:
        content = "A messy paste with a fact buried in it about the subject." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person1.md", content)
        v = ProposalVerdict(
            uid="person1",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="rewrite",
            claim="Model's own tightened claim.",
            operator_claim="Operator's corrected claim text.",
        )
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person1.md").read_text(encoding="utf-8")
        assert "Operator's corrected claim text." in after
        assert "Model's own tightened claim." not in after

    def test_override_wins_over_model_remove_the_headline_regression(self, wiki: Path) -> None:
        """Without the override this `remove` verdict would delete the
        bullet outright -- the exact silent-discard defect athenaeum#1924
        exists to fix. With it, the bullet survives, rewritten to the
        operator's own wording."""
        content = "Off-topic internal retro content unrelated to the subject." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person2.md", content)
        v = ProposalVerdict(
            uid="person2",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="remove",
            operator_claim="Operator's corrected claim text.",
        )
        report = PasteCleanupReport(proposed=[v])
        assert v.final_verdict() == "rewrite"  # not "remove"
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person2.md").read_text(encoding="utf-8")
        assert content not in after
        assert "- 2026-01-01: Operator's corrected claim text." in after

    def test_override_wins_over_model_keep(self, wiki: Path) -> None:
        content = "A genuinely on-topic short paste about the subject here." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person3.md", content)
        v = ProposalVerdict(
            uid="person3",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="keep",
            operator_claim="Operator's corrected claim text.",
        )
        report = PasteCleanupReport(proposed=[v])
        assert v.final_verdict() == "rewrite"
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person3.md").read_text(encoding="utf-8")
        assert "Operator's corrected claim text." in after
        assert content not in after

    def test_override_wins_over_verifier_override(self, wiki: Path) -> None:
        content = "A paste that needs correction about the subject here for testing." * 2
        page, raw_chunk = _page_with_bullet(wiki, "person4.md", content)
        v = ProposalVerdict(
            uid="person4",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="remove",
            verified=True,
            verifier_verdict="rewrite",
            verifier_agree=False,
            verifier_claim="Verifier's own corrected claim.",
            operator_claim="Operator's corrected claim text.",
        )
        report = PasteCleanupReport(proposed=[v])
        assert v.final_claim() == "Operator's corrected claim text."
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person4.md").read_text(encoding="utf-8")
        assert "Operator's corrected claim text." in after
        assert "Verifier's own corrected claim." not in after

    def test_override_wins_over_error_hold(self, wiki: Path) -> None:
        """An ``error``-bearing verdict would otherwise hold for a human
        (:meth:`ProposalVerdict.final_verdict`'s own ``error is not None``
        branch) -- the operator override must still win over it."""
        content = "A paste that needs correction about the subject here for testing." * 2
        page, raw_chunk = _page_with_bullet(wiki, "person5.md", content)
        v = ProposalVerdict(
            uid="person5",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="remove",
            error="network down",
            operator_claim="Operator's corrected claim text.",
        )
        assert v.final_verdict() == "rewrite"  # not "hold"
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person5.md").read_text(encoding="utf-8")
        assert "Operator's corrected claim text." in after

    def test_override_written_verbatim_no_normalisation(self, wiki: Path) -> None:
        """Pins "no silent normalisation": leading/trailing whitespace and
        internal double spaces in the override survive into the written
        bullet EXACTLY, asserted against the full post-apply body."""
        content = "A messy paste with a fact buried in it about the subject." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person6.md", content)
        messy_claim = "  Leading and trailing  space,   double   internal space.  "
        v = ProposalVerdict(
            uid="person6",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="rewrite",
            claim="Model's own claim.",
            operator_claim=messy_claim,
        )
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person6.md").read_text(encoding="utf-8")
        expected = (
            f"---\nuid: person6.md\nname: Test\n---\n\n## Notes\n\n- 2026-01-01: {messy_claim}\n"
        )
        assert after == expected

    def test_ac2_no_override_fields_is_byte_identical_to_pre_1924_behaviour(
        self, wiki: Path
    ) -> None:
        """AC2, the hard back-compat requirement: a verdict with every new
        field at its default (absent, in report terms) must resolve and
        apply EXACTLY as it did before this issue -- same final_verdict,
        same final_claim, same written body."""
        content = "Off-topic internal retro content unrelated to the subject." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person7.md", content)
        v = ProposalVerdict(
            uid="person7",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="remove",
            reason="off-topic",
            confidence="high",
        )
        assert v.operator_override_active() is False
        assert v.final_verdict() == "remove"
        assert v.final_claim() == ""
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person7.md").read_text(encoding="utf-8")
        assert content not in after
        assert after == "---\nuid: person7.md\nname: Test\n---\n\n## Notes\n"

    def test_hash_mismatch_refuses_override_and_tallies_skip(self, wiki: Path) -> None:
        content = "A genuinely on-topic short paste about the subject here." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person8.md", content)
        real_hash = chunk_identity_hash(raw_chunk)
        wrong_hash = ("0" if real_hash[0] != "0" else "1") + real_hash[1:]
        assert wrong_hash != real_hash
        v = ProposalVerdict(
            uid="person8",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="keep",
            chunk_hash=real_hash,
            operator_claim="Operator's claim labelled against the wrong run.",
            operator_claim_chunk_hash=wrong_hash,
        )
        assert v.operator_override_active() is False
        assert v.final_verdict() == "keep"  # falls back to the model's own verdict
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 0
        after = (wiki / "person8.md").read_text(encoding="utf-8")
        assert content in after  # untouched -- the refused override wrote nothing
        assert report.apply_skips["override_hash_mismatch"] == 1

    def test_hash_match_applies_override(self, wiki: Path) -> None:
        content = "A genuinely on-topic short paste about the subject here." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person9.md", content)
        real_hash = chunk_identity_hash(raw_chunk)
        v = ProposalVerdict(
            uid="person9",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="clean",
            verdict="keep",
            chunk_hash=real_hash,
            operator_claim="Operator's claim, correctly keyed.",
            operator_claim_chunk_hash=real_hash,
        )
        assert v.operator_override_active() is True
        assert v.final_verdict() == "rewrite"
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        after = (wiki / "person9.md").read_text(encoding="utf-8")
        assert "Operator's claim, correctly keyed." in after
        assert report.apply_skips.get("override_hash_mismatch", 0) == 0

    def test_extraction_hold_still_refuses_override(self, wiki: Path) -> None:
        """The fusion-boundary hold is a BOUNDARY-safety hold, not a verdict
        preference (see :meth:`ProposalVerdict.final_verdict`'s own
        comment) -- an operator override must not unstick it either."""
        content = "A fusion-suspect paste that extraction could not cleanly split." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person10.md", content)
        v = ProposalVerdict(
            uid="person10",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_chunk,
            paste_text=content,
            extraction_status="hold",
            verdict="hold",
            operator_claim="An override that must still be refused.",
        )
        assert v.final_verdict() == "hold"
        report = PasteCleanupReport(proposed=[v])
        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 0
        after = (wiki / "person10.md").read_text(encoding="utf-8")
        assert content in after
        assert report.apply_skips["extraction_hold"] == 1

    def test_round_trip_preserves_new_fields_and_override_active_agrees(
        self, tmp_path: Path
    ) -> None:
        v = ProposalVerdict(
            uid="p1",
            path=tmp_path / "p1.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: Some content here.",
            paste_text="Some content here.",
            extraction_status="clean",
            verdict="keep",
            chunk_hash=chunk_identity_hash("- 2026-01-01: Some content here."),
            operator_claim="Operator's claim.",
            operator_claim_chunk_hash=chunk_identity_hash("- 2026-01-01: Some content here."),
            operator_reason="Model missed a correction the operator caught.",
        )
        before_active = v.operator_override_active()
        restored = ProposalVerdict.from_dict(json.loads(json.dumps(v.to_dict())))
        assert restored.chunk_hash == v.chunk_hash
        assert restored.operator_claim == v.operator_claim
        assert restored.operator_claim_chunk_hash == v.operator_claim_chunk_hash
        assert restored.operator_reason == v.operator_reason
        assert restored.operator_override_active() == before_active is True

    def test_chunk_hash_and_ordinal_populated_by_a_real_build_pass(self, wiki: Path) -> None:
        c1 = "First bullet content long enough to pass threshold here for sure." * 2
        c2 = "Second bullet content long enough to pass threshold here for sure." * 2
        c3 = "Third bullet content long enough to pass threshold here for sure." * 2
        _page(
            wiki,
            "person11.md",
            "uid: person11\nname: Person Eleven\n",
            "## Notes\n\n"
            f"- 2026-01-01: {c1}\n\n"
            f"- 2026-01-02: {c2}\n\n"
            f"- 2026-01-03: {c3}\n",
        )
        client = FakeLLMClient(
            response=make_llm_response(
                json.dumps(
                    {"verdict": "keep", "claim": "", "reason": "fine", "confidence": "high"}
                ),
                usage=make_llm_usage(input_tokens=10, output_tokens=2),
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
        assert len(report.proposed) == 3
        for v in report.proposed:
            assert v.chunk_hash == chunk_identity_hash(v.raw_chunk)
        assert sorted(v.ordinal for v in report.proposed) == [0, 1, 2]

    def test_skip_tally_exact_mix_and_reset_not_accumulated(self, wiki: Path) -> None:
        content_keep = "A genuinely on-topic short paste about the subject here." * 3
        content_rewrite = "A messy paste with a fact buried in it about the subject." * 3
        content_cnf = "Off-topic internal retro content unrelated to the subject." * 3

        page_keep, raw_keep = _page_with_bullet(wiki, "keep.md", content_keep)
        page_rewrite, raw_rewrite = _page_with_bullet(wiki, "rewrite.md", content_rewrite)
        page_cnf, raw_cnf = _page_with_bullet(wiki, "cnf.md", content_cnf)
        # Simulate a concurrent writer changing this page between scan and
        # apply -- raw_cnf will no longer be found in the live body.
        page_cnf.write_text(
            "---\nuid: cnf.md\nname: Test\n---\n## Notes\n\n- 2026-01-01: Something else.\n",
            encoding="utf-8",
        )

        v_missing = ProposalVerdict(
            uid="ghost",
            path=wiki / "ghost.md",  # never written -- unreadable at apply time
            date="2026-01-01",
            raw_chunk="- 2026-01-01: ghost content",
            paste_text="ghost content",
            extraction_status="clean",
            verdict="remove",
        )
        v_cnf = ProposalVerdict(
            uid="cnf",
            path=page_cnf,
            date="2026-01-01",
            raw_chunk=raw_cnf,
            paste_text=content_cnf,
            extraction_status="clean",
            verdict="remove",
        )
        v_keep = ProposalVerdict(
            uid="keep",
            path=page_keep,
            date="2026-01-01",
            raw_chunk=raw_keep,
            paste_text=content_keep,
            extraction_status="clean",
            verdict="keep",
        )
        v_rewrite = ProposalVerdict(
            uid="rewrite",
            path=page_rewrite,
            date="2026-01-01",
            raw_chunk=raw_rewrite,
            paste_text=content_rewrite,
            extraction_status="clean",
            verdict="rewrite",
            claim="Tightened claim.",
        )
        report = PasteCleanupReport(proposed=[v_missing, v_cnf, v_keep, v_rewrite])

        changed = apply_paste_cleanup_report(report, wiki)
        assert changed == 1
        assert report.apply_skips == {
            "missing_page": 1,
            "chunk_not_found": 1,
            "not_writable_verdict": 1,
        }

        # Reset-not-accumulated: poke a sentinel reason onto the report as
        # if it were a leftover from some earlier call, then apply again --
        # a fresh call must start from {} every time, never carry a prior
        # call's tally forward.
        report.apply_skips["sentinel_from_a_different_call"] = 1
        apply_paste_cleanup_report(report, wiki)
        assert "sentinel_from_a_different_call" not in report.apply_skips

    def test_three_bullet_page_with_duplicate_chunk_pins_actual_replace_behaviour(
        self, wiki: Path
    ) -> None:
        """Retro #3: every pre-athenaeum#1924 apply test built a page with ONE
        bullet; the 2026-10-01 apply hit a 3-bullet page with a duplicate
        chunk and one rewrite silently skipped. This pins (does NOT fix --
        that is a separate issue) ``apply_paste_cleanup_report``'s actual
        ``body.replace(..., 1)`` behaviour on a page with two
        BYTE-IDENTICAL bullets (same date, same content) and a distinct
        middle bullet, under mixed verdicts remove/rewrite/keep.

        The real (verified, not guessed) behaviour is surprising and, by
        this author's reading, WRONG: the first bullet's ``remove`` is
        implemented as two sequential ``body.replace(needle, "", 1)``
        calls -- one targeting ``"\\n\\n" + raw_chunk`` (removes bullet 1
        whole, including its own leading blank line), one targeting the
        bare ``raw_chunk`` text as a safety net for a bullet with no
        leading ``"\\n\\n"``. Because bullet 3's raw_chunk is a byte-for-byte
        duplicate of bullet 1's, that SECOND plain-text replace -- having
        already found and consumed bullet 1's own occurrence via the first
        call -- matches and deletes bullet 3's occurrence of the text
        instead (the only one left), as collateral damage of bullet 1's
        removal. Bullet 3's own verdict is ``keep`` -- it should never have
        been touched at all. It is left with no detectable chunk afterward,
        so the loop reports it as ``chunk_not_found`` (a page-changed-since-
        scan symptom) rather than anything describing what actually
        happened to it (silently deleted by a sibling bullet's apply).
        """
        dup_content = (
            "Duplicate paste content shared across two bullets for testing fidelity here."
        )
        mid_content = "A distinct middle bullet with its own unique content here for testing."
        page = _page(
            wiki,
            "person12.md",
            "uid: person12\nname: Person Twelve\n",
            "## Notes\n\n"
            f"- 2026-01-01: {dup_content}\n\n"
            f"- 2026-01-02: {mid_content}\n\n"
            f"- 2026-01-01: {dup_content}\n",
        )
        raw_dup = f"- 2026-01-01: {dup_content}"
        raw_mid = f"- 2026-01-02: {mid_content}"

        v_remove = ProposalVerdict(
            uid="person12",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_dup,
            paste_text=dup_content,
            extraction_status="clean",
            verdict="remove",
        )
        v_rewrite = ProposalVerdict(
            uid="person12",
            path=page,
            date="2026-01-02",
            raw_chunk=raw_mid,
            paste_text=mid_content,
            extraction_status="clean",
            verdict="rewrite",
            claim="Tightened middle claim.",
        )
        v_keep = ProposalVerdict(
            uid="person12",
            path=page,
            date="2026-01-01",
            raw_chunk=raw_dup,
            paste_text=dup_content,
            extraction_status="clean",
            verdict="keep",
        )
        report = PasteCleanupReport(proposed=[v_remove, v_rewrite, v_keep])

        changed = apply_paste_cleanup_report(report, wiki)

        assert changed == 1
        # The "keep" bullet's own text is gone -- NOT preserved, despite its
        # verdict -- pinning the collateral-damage behaviour described above.
        after = page.read_text(encoding="utf-8")
        expected = (
            "---\nuid: person12\nname: Person Twelve\n---\n\n"
            "## Notes\n\n- 2026-01-02: Tightened middle claim.\n\n\n"
        )
        assert after == expected
        assert dup_content not in after
        # Tallied under the ordinary "nothing to write" bucket, because a
        # `keep` verdict never reaches the chunk-presence check at all --
        # so NOTHING in the tally names, or could name, the true cause
        # (deleted by a sibling bullet's own remove). That blind spot is
        # the point of the xfail test below.
        assert report.apply_skips == {"not_writable_verdict": 1}

    def test_render_text_includes_skip_tally_when_present_and_omits_when_empty(
        self, wiki: Path
    ) -> None:
        content = "Off-topic internal retro content unrelated to the subject." * 3
        page, raw_chunk = _page_with_bullet(wiki, "person13.md", content)

        # Empty case: a dry-run-shaped report with no apply ever run.
        empty_report = PasteCleanupReport(scanned=1, bullets_found=1)
        assert "apply skips:" not in empty_report.render_text()

        # Non-empty case: a real apply with one skip.
        v_ghost = ProposalVerdict(
            uid="ghost",
            path=wiki / "does-not-exist.md",
            date="2026-01-01",
            raw_chunk="- 2026-01-01: ghost",
            paste_text="ghost",
            extraction_status="clean",
            verdict="remove",
        )
        report = PasteCleanupReport(proposed=[v_ghost])
        apply_paste_cleanup_report(report, wiki)
        text = report.render_text()
        assert "apply skips:" in text
        assert "  missing_page: 1" in text


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

    def test_malformed_raw_chunk_is_marked_attempted_and_does_not_stay_pending(
        self, tmp_path: Path, ledger: Path
    ) -> None:
        """A ``raw_chunk`` whose ``- YYYY-MM-DD: `` head does not parse cannot
        have its paste text re-derived, so no verifier call can be made for it
        (Sentry Seer finding on athenaeum#1927). It must still be marked
        ``verify_attempted`` -- the athenaeum#1903 meaning of that flag is "the
        verifier was reached for this verdict", which it was. Leaving it False
        would keep the bullet in ``pending_verification()`` forever, so
        ``ceiling_reason`` could never clear and every later resume would
        re-attempt a bullet that can never succeed. The recorded ``error``
        still routes ``final_verdict()`` to ``hold``, so apply never writes it.
        """
        verdict = ProposalVerdict(
            uid="broken1",
            path=tmp_path / "broken1.md",
            date="2026-01-01",
            raw_chunk="this chunk has no dated bullet head at all",
            paste_text="",
            extraction_status="clean",
            verdict="remove",
            confidence="low",
        )
        report = PasteCleanupReport(
            proposed=[verdict], verify_rule="all", verify_model=_VERIFY_MODEL
        )
        report.ceiling_reason = "per-day token ceiling reached (fixture)"
        assert report.pending_verification() == [0]

        client = FakeLLMClient(raises=AssertionError("no verifier call is possible"))
        resume_verification(report, tmp_path, verify_client=client, config=None)

        assert len(client.calls) == 0
        assert verdict.verify_attempted is True
        assert verdict.error is not None and verdict.error.startswith("resume:")
        assert verdict.final_verdict() == "hold"
        # The whole point: it is no longer pending, so the run can complete and
        # a second resume does not re-attempt it.
        assert report.pending_verification() == []
        assert report.ceiling_reason is None
        resume_verification(report, tmp_path, verify_client=client, config=None)
        assert len(client.calls) == 0

class TestDuplicateChunkCollateralDeletion:
    """The defect the retro #3 fixture above UNCOVERED, recorded as a defect
    rather than only pinned (see
    ``TestOperatorClaimOverride.test_three_bullet_page_with_duplicate_chunk_pins_actual_replace_behaviour``
    for the pinning test, which asserts today's exact bytes).

    ``apply_paste_cleanup_report``'s ``remove`` branch runs BOTH of its
    replaces unconditionally::

        body = body.replace(f"\\n\\n{v.raw_chunk}", "", 1)
        body = body.replace(v.raw_chunk, "", 1)

    The second is meant as a fallback for a bullet with no leading blank
    line, but it fires even when the first already succeeded -- so on a page
    with two byte-identical bullets it deletes the OTHER one too, whatever
    that bullet's own verdict says. Verified reproducible on the pre-athenaeum#1923
    base commit, so this is long-standing and not introduced by athenaeum#1923/athenaeum#1924.

    Marked ``xfail(strict=True)`` deliberately: it asserts the CORRECT
    invariant, so it reports as XFAIL while the defect stands and turns into
    a hard FAILURE the moment someone fixes it -- which is the signal to
    delete the mark and keep the assertion. Pinning the broken bytes alone
    would have quietly blessed data loss as intended behaviour.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="athenaeum#1924 finding: a remove verdict's unconditional second "
        "body.replace() deletes a byte-identical sibling bullet regardless of "
        "that sibling's own verdict. Filed separately; fix flips this to pass.",
    )
    def test_a_keep_bullet_survives_an_identical_siblings_remove(self, wiki: Path) -> None:
        dup = "Duplicate paste content shared across two bullets for testing fidelity here."
        other = "A distinct middle bullet with its own unique content here for testing."
        page = _page(
            wiki,
            "person_dup.md",
            "uid: person_dup\nname: Person Dup\n",
            "## Notes\n\n"
            f"- 2026-01-01: {dup}\n\n"
            f"- 2026-01-02: {other}\n\n"
            f"- 2026-01-01: {dup}\n",
        )

        def _v(date: str, content: str, verdict: str) -> ProposalVerdict:
            return ProposalVerdict(
                uid="person_dup",
                path=page,
                date=date,
                raw_chunk=f"- {date}: {content}",
                paste_text=content,
                extraction_status="clean",
                verdict=verdict,
            )

        report = PasteCleanupReport(
            proposed=[
                _v("2026-01-01", dup, "remove"),
                _v("2026-01-02", other, "keep"),
                _v("2026-01-01", dup, "keep"),
            ]
        )
        apply_paste_cleanup_report(report, wiki)
        after = page.read_text(encoding="utf-8")

        # Exactly ONE of the two identical bullets was marked `remove`, so
        # exactly ONE must be gone and one must remain.
        assert after.count(f"- 2026-01-01: {dup}") == 1, (
            "a `keep` bullet was deleted as collateral damage of a byte-identical "
            "sibling's `remove`"
        )
