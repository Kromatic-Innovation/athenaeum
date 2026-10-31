# SPDX-License-Identifier: Apache-2.0
"""Attribution escalation, the verdict memo and shadow mode (athenaeum#2212).

``docs/design/attribution-verification.md`` §6 to §9 is the normative spec.
This file covers the ESCALATION half of child E; the placement executor it
hands verdicts to is covered in ``tests/test_contact_value_placement.py``.

Every model call is mocked and every fixture is synthetic
(``example.org``/``example.com``). Two deliberate test doubles carry the
load:

- ``_NoCallClient`` raises on ``.messages.create``, so "makes no model call"
  is asserted by construction rather than by counting.
- ``_entry`` / ``_patch_record`` install a SYNTHETIC passing calibration
  record. The shipped one ships ``passes: false`` with ``bins: []`` for
  every class (by design, until a live eval runs), so no above-threshold
  path is reachable through it — which is itself asserted, in
  ``TestShippedRecordIsNotAutoFiling``.

Cases:

- ``TestCalibratedProbability`` — the bin mapping and every documented edge.
- ``TestThresholdForClass`` — record, override, and the ``None`` cases.
- ``TestQuestionClassDrivesModelAndThreshold`` — per-class resolution.
- ``TestSuspectOnly`` — ``pass``/``non-personal`` make no call.
- ``TestMemo`` — a repeat makes none either; a changed sha or model re-asks.
- ``TestHumanQueueTriggers`` — every design §8.1 trigger, one test each,
  plus the two malformed-answer guards.
- ``TestQueueItemContents`` — what the description must name.
- ``TestRawValueNeverReachesPendingQuestions`` — the explicit AC, through a
  real ``tier4_escalate`` write.
- ``TestPlacementHandoff`` — at or above threshold, and the re-check path.
- ``TestNeverGrantsOutreachEligibility`` — end to end from escalation.
- ``TestSuspectJudgementRecord`` — the suspect-file stamp and its
  interaction with ``park_suspect``'s idempotency.
- ``TestReadSuspect`` — the one-way-out read and its fail-closed gate.
- ``TestShadowSweepObserveMode`` — the pipeline hook files nothing and the
  writers' output is byte-identical.
- ``TestLibrarianShadowPhase`` — the wiring, its default-off gate and its
  per-run cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii, pii_attribution
from athenaeum.models import TokenUsage, parse_frontmatter, render_frontmatter
from athenaeum.tiers import tier4_escalate
from tests.conftest import FakeLLMClient

EXCLUDED_CONFIG: dict[str, Any] = {"storage": {"mapping": {"pii": "excluded"}}}

_VALUE = "introducer@example.org"
_TARGET_UID = "uid-target"
_TARGET_NAME = "Target Person"
_HOLDER_UID = "uid-holder"
_HOLDER_NAME = "Holder Person"
_MODEL = "synthetic-attribution-model"


# ---------------------------------------------------------------------------
# Doubles and fixture builders
# ---------------------------------------------------------------------------


class _NoCallClient:
    """Raises if any model call is attempted.

    Shaped like the anthropic client slice ``judge_contact_attribution``
    uses (``client.messages.create``). Asserting "no call" with a double
    that cannot be called is stronger than asserting a call count: a future
    edit that adds a second call site fails here immediately.
    """

    def __init__(self) -> None:
        self.messages = self

    def create(self, **_kwargs: Any) -> Any:
        raise AssertionError("a model call was made where none was expected")


def _judge_client(**payload: Any) -> FakeLLMClient:
    """A client answering with one strict-JSON judgement payload."""
    return FakeLLMClient(text=json.dumps(payload))


def _entry(
    question_class: str = pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE,
    *,
    signal: str = pii_attribution.SIGNAL_SELF_REPORTED,
    bins: tuple[tuple[float, float, float], ...] = (
        (0.0, 0.5, 0.20),
        (0.5, 0.9, 0.70),
        (0.9, 1.0, 0.98),
    ),
    threshold: float = 0.90,
    passes: bool = True,
    model: str = _MODEL,
) -> pii_attribution.CalibrationEntry:
    return pii_attribution.CalibrationEntry(
        question_class=question_class,
        model=model,
        prompt_sha256="synthetic-prompt-sha",
        signal=signal,
        bins=bins,
        threshold=threshold,
        n_fit=60,
        n_holdout=40,
        holdout_accuracy_above=0.98,
        holdout_wilson_lb=0.95,
        abstain_share=0.18,
        dataset_sha256="synthetic-dataset-sha",
        measured_at="2026-10-31",
        passes=passes,
        ece=0.03,
        auroc=0.81,
    )


def _patch_record(
    monkeypatch: pytest.MonkeyPatch,
    *entries: pii_attribution.CalibrationEntry,
) -> None:
    record = {e.question_class: e for e in entries}
    monkeypatch.setattr(pii_attribution, "load_calibration_record", lambda: record)


def _write_page(
    wiki_root: Path,
    *,
    uid: str,
    name: str,
    type_: str = "person",
    aliases: list[str] | None = None,
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"uid": uid, "name": name, "type": type_}
    if aliases:
        meta["aliases"] = aliases
    path = wiki_root / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(render_frontmatter(meta) + "\nSynthetic page.\n", encoding="utf-8")
    return path


def _write_contact_record(
    contacts_root: Path, filename: str, *, uid: str, emails: list[str]
) -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"uid": uid, "pii": True, "emails": list(emails)}
    path = contacts_root / filename
    path.write_text(
        render_frontmatter(meta) + "\nSynthetic contact record.\n", encoding="utf-8"
    )
    return path


def _context(
    tmp_path: Path, *, pages: bool = True
) -> pii_attribution.AttributionContext:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    if pages:
        _write_page(wiki, uid=_TARGET_UID, name=_TARGET_NAME)
        _write_page(wiki, uid=_HOLDER_UID, name=_HOLDER_NAME, aliases=["H. Person"])
    return pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)


def _suspect_verdict(
    context: pii_attribution.AttributionContext,
    *,
    value: str = _VALUE,
    checks: tuple[pii_attribution.CheckEvidence, ...] | None = None,
    candidates: tuple[str, ...] = (_HOLDER_UID,),
) -> pii_attribution.AttributionVerdict:
    if checks is None:
        checks = (
            pii_attribution.CheckEvidence(
                check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE, uids=(_HOLDER_UID,)
            ),
        )
    return pii_attribution.AttributionVerdict(
        disposition=pii_attribution.DISPOSITION_SUSPECT,
        checks=checks,
        candidates=candidates,
        value_key=pii_attribution.value_key_for(context, value),
    )


def _escalate(
    context: pii_attribution.AttributionContext,
    verdict: pii_attribution.AttributionVerdict,
    *,
    value: str = _VALUE,
    client: Any = None,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> pii_attribution.EscalationOutcome:
    return pii_attribution.escalate_contact_attribution_suspect(
        context,
        value,
        verdict,
        target_uid=kwargs.pop("target_uid", _TARGET_UID),
        target_name=kwargs.pop("target_name", _TARGET_NAME),
        target_type=kwargs.pop("target_type", "person"),
        source_ref=kwargs.pop("source_ref", "raw/intake/note.md"),
        writer=kwargs.pop("writer", pii_attribution.WRITER_OBSERVED_CONTACT),
        client=client,
        config=config if config is not None else EXCLUDED_CONFIG,
        **kwargs,
    )


def _snapshot(root: Path, *, skip_attribution: bool = True) -> dict[str, bytes]:
    """Byte snapshot of every file under *root*.

    ``skip_attribution`` drops the ``_attribution*`` namespace -- the log,
    the memo and the suspect files. Observe mode is DEFINED as writing
    exactly there (design §4.3, §9.2) and nowhere else, so those paths are
    excluded to make the real assertion ("nothing a writer produced moved")
    testable rather than trivially false.
    """
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if skip_attribution and "_attribution" in rel:
            continue
        out[rel] = path.read_bytes()
    return out


# ---------------------------------------------------------------------------
# B1 -- the calibrated mapping
# ---------------------------------------------------------------------------


class TestCalibratedProbability:
    def test_score_maps_through_its_bin(self) -> None:
        entry = _entry()
        assert pii_attribution.calibrated_probability(entry, 0.1) == 0.20
        assert pii_attribution.calibrated_probability(entry, 0.6) == 0.70
        assert pii_attribution.calibrated_probability(entry, 0.95) == 0.98

    def test_lower_bound_is_inclusive_and_upper_bound_exclusive(self) -> None:
        entry = _entry()
        # 0.5 is the first bin's `hi` AND the second bin's `lo`: it must land
        # in the SECOND bin, which is the only reading under which the
        # design's shared-endpoint bins partition.
        assert pii_attribution.calibrated_probability(entry, 0.5) == 0.70
        assert pii_attribution.calibrated_probability(entry, 0.9) == 0.98

    def test_last_bin_is_closed_at_its_upper_bound(self) -> None:
        entry = _entry()
        assert pii_attribution.calibrated_probability(entry, 1.0) == 0.98

    def test_score_below_the_first_bin_is_unmappable(self) -> None:
        entry = _entry(bins=((0.2, 0.6, 0.5), (0.6, 1.0, 0.9)))
        assert pii_attribution.calibrated_probability(entry, 0.1) == 0.0

    def test_score_above_the_last_bin_is_unmappable(self) -> None:
        entry = _entry(bins=((0.0, 0.4, 0.3),))
        assert pii_attribution.calibrated_probability(entry, 0.9) == 0.0

    def test_score_in_a_gap_between_bins_is_unmappable(self) -> None:
        entry = _entry(bins=((0.0, 0.3, 0.2), (0.7, 1.0, 0.95)))
        assert pii_attribution.calibrated_probability(entry, 0.5) == 0.0

    def test_empty_bins_mean_no_measured_mapping(self) -> None:
        entry = _entry(bins=())
        assert pii_attribution.calibrated_probability(entry, 0.99) == 0.0

    def test_unmappable_can_never_clear_a_real_threshold(self) -> None:
        """The fail-safe property, stated as an assertion rather than prose."""
        entry = _entry(bins=())
        probability = pii_attribution.calibrated_probability(entry, 1.0)
        assert probability == 0.0
        assert probability < _entry().threshold

    def test_class_level_signal_bypasses_the_bins_entirely(self) -> None:
        passing = _entry(signal=pii_attribution.SIGNAL_CLASS_LEVEL, bins=(), passes=True)
        failing = _entry(signal=pii_attribution.SIGNAL_CLASS_LEVEL, bins=(), passes=False)
        # The score is a constant for this signal, so it cannot change the answer.
        assert pii_attribution.calibrated_probability(passing, 0.0) == 1.0
        assert pii_attribution.calibrated_probability(passing, 1.0) == 1.0
        assert pii_attribution.calibrated_probability(failing, 1.0) == 0.0

    def test_non_numeric_score_is_unmappable(self) -> None:
        entry = _entry()
        assert pii_attribution.calibrated_probability(entry, float("nan")) == 0.0

    def test_for_class_returns_zero_without_a_valid_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(passes=False))
        assert (
            pii_attribution.calibrated_probability_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, 0.99
            )
            == 0.0
        )

    def test_for_class_maps_through_a_valid_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        assert (
            pii_attribution.calibrated_probability_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, 0.95
            )
            == 0.98
        )


class TestThresholdForClass:
    def test_reads_the_calibration_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.93))
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE
            )
            == 0.93
        )

    def test_config_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.93))
        config = {"librarian": {"attribution": {"thresholds": {"held-elsewhere": 0.55}}}}
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, config
            )
            == 0.55
        )

    def test_override_is_per_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(
            monkeypatch,
            _entry(pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, threshold=0.93),
            _entry(pii_attribution.QUESTION_CLASS_NAMES_OTHER, threshold=0.91),
        )
        config = {"librarian": {"attribution": {"thresholds": {"names-other": 0.42}}}}
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, config
            )
            == 0.93
        )
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_NAMES_OTHER, config
            )
            == 0.42
        )

    def test_none_when_no_entry_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch)
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE
            )
            is None
        )

    def test_a_non_passing_entry_yields_none_not_its_stored_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `passes: false` row must never auto-file.

        The shipped record carries `threshold: 0.0` with `passes: false`, and
        a 0.0 threshold would clear for every probability -- the exact
        inversion this must not allow.
        """
        _patch_record(monkeypatch, _entry(threshold=0.0, passes=False))
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE
            )
            is None
        )

    def test_malformed_override_is_ignored_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.93))
        config = {
            "librarian": {
                "attribution": {"thresholds": {"held-elsewhere": "very confident"}}
            }
        }
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, config
            )
            == 0.93
        )

    def test_out_of_range_override_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.93))
        config = {"librarian": {"attribution": {"thresholds": {"held-elsewhere": 42}}}}
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, config
            )
            == 0.93
        )


class TestShippedRecordIsNotAutoFiling:
    """The shipped record must not auto-file anything, for any class."""

    def test_every_shipped_class_has_no_threshold_and_no_model(self) -> None:
        record = pii_attribution.load_calibration_record()
        assert set(record) == set(pii_attribution.QUESTION_CLASSES)
        for question_class in pii_attribution.QUESTION_CLASSES:
            assert pii_attribution.threshold_for_class(question_class) is None
            assert pii_attribution.recommended_model_for_class(question_class) is None
            assert (
                pii_attribution.calibrated_probability_for_class(question_class, 1.0)
                == 0.0
            )


@pytest.mark.parametrize(
    ("check_ids", "expected"),
    [
        ((pii_attribution.CHECK_D2_HELD_ELSEWHERE,), "held-elsewhere"),
        ((pii_attribution.CHECK_D4_FAN_OUT,), "held-elsewhere"),
        ((pii_attribution.CHECK_C1_INLINE_ELSEWHERE,), "held-elsewhere"),
        ((pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER,), "names-other"),
        ((pii_attribution.CHECK_C3_SOURCE_CONTEXT,), "source-context"),
        ((pii_attribution.CHECK_D3_NOT_A_PERSON,), "non-person-target"),
        ((pii_attribution.CHECK_D1_ROLE,), "role-unmatched"),
        # Design §6.6: "When several classes fire, the class is the first one
        # in this table's order." Every pair below is deliberately listed
        # with the LOWER-ranked check first, so citation order cannot be
        # what produces the answer.
        (
            (
                pii_attribution.CHECK_D1_ROLE,
                pii_attribution.CHECK_D3_NOT_A_PERSON,
                pii_attribution.CHECK_C3_SOURCE_CONTEXT,
                pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER,
                pii_attribution.CHECK_D2_HELD_ELSEWHERE,
            ),
            "held-elsewhere",
        ),
        (
            (
                pii_attribution.CHECK_D1_ROLE,
                pii_attribution.CHECK_D3_NOT_A_PERSON,
                pii_attribution.CHECK_C3_SOURCE_CONTEXT,
                pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER,
            ),
            "names-other",
        ),
        (
            (
                pii_attribution.CHECK_D1_ROLE,
                pii_attribution.CHECK_D3_NOT_A_PERSON,
                pii_attribution.CHECK_C3_SOURCE_CONTEXT,
            ),
            "source-context",
        ),
        (
            (pii_attribution.CHECK_D1_ROLE, pii_attribution.CHECK_D3_NOT_A_PERSON),
            "non-person-target",
        ),
    ],
)
def test_question_class_table_order(check_ids: tuple[str, ...], expected: str) -> None:
    checks = tuple(pii_attribution.CheckEvidence(check_id=c) for c in check_ids)
    assert pii_attribution.question_class_for(checks) == expected


class TestQuestionClassDrivesModelAndThreshold:
    def test_model_and_threshold_both_resolve_per_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(
            monkeypatch,
            _entry(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE,
                model="model-held-elsewhere",
                threshold=0.93,
            ),
            _entry(
                pii_attribution.QUESTION_CLASS_NAMES_OTHER,
                model="model-names-other",
                threshold=0.81,
            ),
        )
        monkeypatch.delenv("ATHENAEUM_ATTRIBUTION_MODEL", raising=False)
        held_model, held_sha = pii_attribution.attribution_call_identity(
            pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, EXCLUDED_CONFIG
        )
        names_model, names_sha = pii_attribution.attribution_call_identity(
            pii_attribution.QUESTION_CLASS_NAMES_OTHER, EXCLUDED_CONFIG
        )
        assert held_model == "model-held-elsewhere"
        assert names_model == "model-names-other"
        # One prompt, so one sha for both classes (design §6.6).
        assert held_sha == names_sha
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, EXCLUDED_CONFIG
            )
            == 0.93
        )
        assert (
            pii_attribution.threshold_for_class(
                pii_attribution.QUESTION_CLASS_NAMES_OTHER, EXCLUDED_CONFIG
            )
            == 0.81
        )

    def test_the_escalation_calls_the_class_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(model="model-held-elsewhere"))
        monkeypatch.delenv("ATHENAEUM_ATTRIBUTION_MODEL", raising=False)
        context = _context(tmp_path)
        client = _judge_client(verdict="own", score=0.95)
        outcome = _escalate(context, _suspect_verdict(context), client=client)
        assert client.calls[0]["model"] == "model-held-elsewhere"
        assert outcome.judgement is not None
        assert outcome.judgement.model == "model-held-elsewhere"


# ---------------------------------------------------------------------------
# B3 -- suspect only (issue AC 1)
# ---------------------------------------------------------------------------


class TestSuspectOnly:
    @pytest.mark.parametrize(
        "disposition",
        [pii_attribution.DISPOSITION_PASS, pii_attribution.DISPOSITION_NON_PERSONAL],
    )
    def test_non_suspect_makes_no_model_call(
        self, tmp_path: Path, disposition: str
    ) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.AttributionVerdict(
            disposition=disposition,
            value_key=pii_attribution.value_key_for(context, _VALUE),
        )
        outcome = _escalate(context, verdict, client=_NoCallClient())
        assert outcome.escalated is False
        assert outcome.judgement is None
        assert outcome.queue_items == ()
        assert outcome.placement is None
        assert disposition in outcome.reason

    def test_non_suspect_writes_nothing_at_all(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        before = _snapshot(tmp_path, skip_attribution=False)
        verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_PASS,
            value_key=pii_attribution.value_key_for(context, _VALUE),
        )
        _escalate(context, verdict, client=_NoCallClient())
        assert _snapshot(tmp_path, skip_attribution=False) == before


# ---------------------------------------------------------------------------
# B2 -- the memo
# ---------------------------------------------------------------------------


class TestMemo:
    def test_memo_path_is_on_the_excluded_surface(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        path = pii_attribution.default_attribution_memo_path(context.contacts_root)
        assert path.parent == context.contacts_root
        assert path.name == "_attribution_memo.jsonl"
        assert pii_attribution.ATTRIBUTION_MEMO_FILENAME == "_attribution_memo.jsonl"

    def test_key_is_built_from_exactly_the_five_components(self) -> None:
        base = {
            "value_key": "vk",
            "target_uid": "uid-a",
            "source_ref": "raw/x.md",
            "prompt_sha256": "sha",
            "model": "m",
        }
        baseline = pii_attribution.attribution_memo_key(**base)
        for field, changed in (
            ("value_key", "vk2"),
            ("target_uid", "uid-b"),
            ("source_ref", "raw/y.md"),
            ("prompt_sha256", "sha2"),
            ("model", "m2"),
        ):
            assert (
                pii_attribution.attribution_memo_key(**{**base, field: changed})
                != baseline
            ), field

    def test_dict_source_ref_is_key_order_independent(self) -> None:
        a = pii_attribution.attribution_memo_key(
            value_key="vk",
            target_uid="uid-a",
            source_ref={"path": "raw/x.md", "ref": "x"},
            prompt_sha256="sha",
            model="m",
        )
        b = pii_attribution.attribution_memo_key(
            value_key="vk",
            target_uid="uid-a",
            source_ref={"ref": "x", "path": "raw/x.md"},
            prompt_sha256="sha",
            model="m",
        )
        assert a == b

    def test_key_never_contains_the_raw_value(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        key = pii_attribution.attribution_memo_key(
            value_key=pii_attribution.value_key_for(context, _VALUE),
            target_uid=_TARGET_UID,
            source_ref="raw/x.md",
            prompt_sha256="sha",
            model="m",
        )
        assert _VALUE not in key
        assert "introducer" not in key

    def test_a_repeat_makes_no_model_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        client = _judge_client(verdict="other-known", owner_uid=_HOLDER_UID, score=0.95)
        first = _escalate(context, verdict, client=client)
        assert first.memo_hit is False
        assert len(client.calls) == 1

        second = _escalate(context, verdict, client=_NoCallClient())
        assert second.memo_hit is True
        assert second.judgement is not None
        assert second.judgement.verdict == pii_attribution.JUDGEMENT_OTHER_KNOWN
        assert second.judgement.owner_uid == _HOLDER_UID

    def test_a_changed_prompt_sha_re_asks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        client = _judge_client(verdict="own", score=0.95)
        _escalate(context, verdict, client=client)
        assert len(client.calls) == 1

        monkeypatch.setattr(
            pii_attribution,
            "CONTACT_ATTRIBUTION_JUDGE_SYSTEM",
            pii_attribution.CONTACT_ATTRIBUTION_JUDGE_SYSTEM + "\nAn edited instruction.\n",
        )
        outcome = _escalate(context, verdict, client=client)
        assert outcome.memo_hit is False
        assert len(client.calls) == 2

    def test_a_changed_model_re_asks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        client = _judge_client(verdict="own", score=0.95)
        monkeypatch.setenv("ATHENAEUM_ATTRIBUTION_MODEL", "model-one")
        _escalate(context, verdict, client=client)
        assert len(client.calls) == 1

        monkeypatch.setenv("ATHENAEUM_ATTRIBUTION_MODEL", "model-two")
        outcome = _escalate(context, verdict, client=client)
        assert outcome.memo_hit is False
        assert len(client.calls) == 2
        assert client.calls[1]["model"] == "model-two"

    def test_a_malformed_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        path = pii_attribution.default_attribution_memo_path(context.contacts_root)
        good = {
            "key": "k-good",
            "verdict": "own",
            "score": 0.5,
            "model": "m",
            "prompt_sha256": "s",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "not json at all\n"
            + json.dumps(["a", "list", "not", "an", "object"])
            + "\n"
            + json.dumps({"verdict": "own"})  # keyless
            + "\n"
            + json.dumps({"key": "k-bad", "verdict": "not-a-verdict"})
            + "\n"
            + json.dumps(good)
            + "\n",
            encoding="utf-8",
        )
        entries = pii_attribution.read_attribution_memo(path)
        assert set(entries) == {"k-good"}

    def test_last_write_wins_on_a_duplicate_key(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        path = pii_attribution.default_attribution_memo_path(context.contacts_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"key": "k", "verdict": "own", "score": 0.1})
            + "\n"
            + json.dumps({"key": "k", "verdict": "other-unknown", "score": 0.9})
            + "\n",
            encoding="utf-8",
        )
        entries = pii_attribution.read_attribution_memo(path)
        assert entries["k"].verdict == pii_attribution.JUDGEMENT_OTHER_UNKNOWN
        assert entries["k"].score == 0.9

    def test_missing_memo_reads_as_empty(self, tmp_path: Path) -> None:
        assert pii_attribution.read_attribution_memo(tmp_path / "nope.jsonl") == {}

    def test_memo_row_stores_the_value_key_not_the_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        _escalate(context, verdict, client=_judge_client(verdict="own", score=0.95))
        rows = [
            json.loads(line)
            for line in pii_attribution.default_attribution_memo_path(context.contacts_root)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert rows
        for row in rows:
            assert row["value_key"] == verdict.value_key
            assert "value" not in row


# ---------------------------------------------------------------------------
# B3 -- the human queue
# ---------------------------------------------------------------------------


class TestHumanQueueTriggers:
    def _queued(self, outcome: pii_attribution.EscalationOutcome) -> str:
        assert outcome.escalated is True
        assert len(outcome.queue_items) == 1
        assert outcome.queue_items[0].conflict_type == "ambiguous"
        return outcome.queue_items[0].description

    def test_below_threshold(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.90))
        context = _context(tmp_path)
        # score 0.6 -> the middle bin -> calibrated 0.70 < 0.90.
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=0.6),
        )
        assert outcome.calibrated_p == 0.70
        assert outcome.threshold == 0.90
        assert outcome.above_threshold is False
        assert outcome.placement is None
        assert "below this class's threshold" in self._queued(outcome)

    def test_unclear_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="unclear", score=0.0),
        )
        assert outcome.judgement is not None
        assert outcome.judgement.verdict == pii_attribution.JUDGEMENT_UNCLEAR
        assert "unclear" in self._queued(outcome)

    def test_a_failed_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=FakeLLMClient(raises=RuntimeError("provider is down")),
        )
        assert outcome.judgement is not None
        assert outcome.judgement.verdict == pii_attribution.JUDGEMENT_UNCLEAR
        assert "the call failed" in self._queued(outcome)

    def test_no_client_at_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        outcome = _escalate(context, _suspect_verdict(context), client=None)
        assert outcome.judgement is not None
        assert outcome.judgement.verdict == pii_attribution.JUDGEMENT_UNCLEAR
        self._queued(outcome)

    def test_no_valid_calibration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch)  # an empty record: no entry for any class
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=1.0),
        )
        assert outcome.threshold is None
        assert "no valid calibration record" in self._queued(outcome)

    def test_alias_with_no_relation_never_reaches_the_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`place_contact_value` RAISES on this; it must not get the chance.

        `judge_contact_attribution` nulls an out-of-vocabulary relation, so
        a model answering `alias` with a relation it invented produces
        exactly this shape.
        """
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(
                verdict="alias", alias_relation="not-a-real-relation", score=1.0
            ),
        )
        assert outcome.judgement is not None
        assert outcome.judgement.alias_relation is None
        assert outcome.placement is None
        assert "no valid identifier relation" in self._queued(outcome)

    @pytest.mark.parametrize("verdict", ["other-known", "same-entity"])
    def test_owner_bearing_verdict_with_no_owner_uid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict=verdict, score=1.0),
        )
        assert outcome.placement is None
        assert "without naming an" in self._queued(outcome)

    def test_the_recheck_returning_suspect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Design §7.2: it goes to the queue and never back to the model."""
        _patch_record(monkeypatch, _entry(threshold=0.5))
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        _write_page(wiki, uid=_TARGET_UID, name=_TARGET_NAME)
        _write_page(wiki, uid=_HOLDER_UID, name=_HOLDER_NAME)
        _write_page(wiki, uid="uid-third", name="Third Person")
        contacts = knowledge / "excluded"
        # Two OTHER uids already hold the value, so the re-check against the
        # owner fires D2 and D4 and answers `suspect`.
        _write_contact_record(contacts, "holder.md", uid=_HOLDER_UID, emails=[_VALUE])
        _write_contact_record(contacts, "third.md", uid="uid-third", emails=[_VALUE])
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = _suspect_verdict(context)
        outcome = _escalate(
            context,
            verdict,
            client=_judge_client(verdict="other-known", owner_uid="uid-third", score=1.0),
            apply=True,
        )
        assert outcome.above_threshold is True
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_QUEUED
        assert "re-check returned 'suspect'" in self._queued(outcome)

    def test_an_internal_failure_degrades_to_the_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never raises: design §6.3's fail-closed rule, for ANY failure."""
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the memo disk is on fire")

        monkeypatch.setattr(pii_attribution, "memo_lookup", _boom)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=1.0),
        )
        assert outcome.escalated is True
        assert "the escalation itself failed" in self._queued(outcome)


class TestQueueItemContents:
    def _item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[pii_attribution.EscalationOutcome, str]:
        _patch_record(monkeypatch, _entry(threshold=0.99))
        context = _context(tmp_path)
        verdict = _suspect_verdict(
            context,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE, uids=(_HOLDER_UID,)
                ),
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER,
                    uids=(_HOLDER_UID,),
                ),
            ),
        )
        outcome = _escalate(
            context,
            verdict,
            client=_judge_client(verdict="other-known", owner_uid=_HOLDER_UID, score=0.6),
        )
        assert len(outcome.queue_items) == 1
        return outcome, outcome.queue_items[0].description

    def test_names_the_target_by_name_and_uid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outcome, description = self._item(tmp_path, monkeypatch)
        assert f"{_TARGET_NAME} (uid={_TARGET_UID})" in description

    def test_names_the_holders_by_name_and_uid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outcome, description = self._item(tmp_path, monkeypatch)
        assert f"{_HOLDER_NAME} (uid={_HOLDER_UID})" in description
        assert "Other uids holding or naming the value:" in description

    def test_names_the_candidates_by_name_and_uid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context(tmp_path)
        _patch_record(monkeypatch, _entry(threshold=0.99))
        verdict = _suspect_verdict(
            context,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER,
                    uids=(_HOLDER_UID,),
                ),
            ),
            candidates=(_HOLDER_UID,),
        )
        outcome = _escalate(
            context, verdict, client=_judge_client(verdict="own", score=0.6)
        )
        description = outcome.queue_items[0].description
        assert (
            f"Candidate owners the evidence points at: {_HOLDER_NAME} (uid={_HOLDER_UID})"
            in description
        )

    def test_names_the_fired_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outcome, description = self._item(tmp_path, monkeypatch)
        assert "Checks that fired: D2, C2" in description

    def test_names_the_verdict_and_the_calibrated_probability(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outcome, description = self._item(tmp_path, monkeypatch)
        assert "Model verdict: other-known" in description
        assert f"(owner {_HOLDER_UID})" in description
        assert outcome.calibrated_p == 0.70
        assert "Calibrated probability: 0.700" in description
        assert "class threshold: 0.990" in description

    def test_carries_the_suspect_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outcome, description = self._item(tmp_path, monkeypatch)
        expected = pii_attribution.suspect_id_for(
            pii_attribution.value_key_for(_context(tmp_path), _VALUE), _TARGET_UID
        )
        assert outcome.suspect_id == expected
        assert f"Suspect id: {expected}" in description
        assert f"athenaeum attribution show {expected}" in description

    def test_shows_the_value_masked_in_the_description_and_the_entity_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outcome, description = self._item(tmp_path, monkeypatch)
        masked = pii.mask_contact_address(_VALUE)
        assert masked in description
        assert _VALUE not in description
        assert masked in outcome.queue_items[0].entity_name
        assert _VALUE not in outcome.queue_items[0].entity_name

    def test_names_the_originating_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _outcome, description = self._item(tmp_path, monkeypatch)
        assert f"writer: {pii_attribution.WRITER_OBSERVED_CONTACT}" in description


class TestSourceExcerpt:
    """Design §6.2: "a window ... around each occurrence of the value"."""

    def _question(
        self, tmp_path: Path, source_ref: Any
    ) -> pii_attribution.AttributionQuestion:
        context = _context(tmp_path)
        return pii_attribution.build_attribution_question(
            context,
            _VALUE,
            _suspect_verdict(context),
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            target_type="person",
            source_ref=source_ref,
        )

    def test_a_window_around_the_occurrence(self, tmp_path: Path) -> None:
        source = tmp_path / "note.md"
        source.write_text(
            "A" * 500 + f"\nReferred by {_VALUE} at the conference.\n" + "B" * 500,
            encoding="utf-8",
        )
        question = self._question(tmp_path, str(source))
        assert question.source_excerpt is not None
        assert _VALUE in question.source_excerpt
        assert "Referred by" in question.source_excerpt
        # Bounded: the window is 200 chars each side by default, not the file.
        assert len(question.source_excerpt) < 700

    def test_overlapping_windows_are_merged_once(self, tmp_path: Path) -> None:
        source = tmp_path / "note.md"
        source.write_text(f"cc: {_VALUE} and again {_VALUE}\n", encoding="utf-8")
        question = self._question(tmp_path, str(source))
        assert question.source_excerpt is not None
        assert question.source_excerpt.count("\n...\n") == 0

    def test_distant_occurrences_are_separate_windows(self, tmp_path: Path) -> None:
        source = tmp_path / "note.md"
        source.write_text(
            f"first {_VALUE}\n" + "X" * 2000 + f"\nsecond {_VALUE}\n", encoding="utf-8"
        )
        question = self._question(tmp_path, str(source))
        assert question.source_excerpt is not None
        assert "\n...\n" in question.source_excerpt

    @pytest.mark.parametrize("source_ref", [None, "raw/does/not/exist.md", {}])
    def test_no_resolvable_source_yields_no_excerpt(
        self, tmp_path: Path, source_ref: Any
    ) -> None:
        assert self._question(tmp_path, source_ref).source_excerpt is None

    def test_a_source_the_value_is_absent_from_yields_no_excerpt(
        self, tmp_path: Path
    ) -> None:
        """C3's own "the value is absent from it" branch: there is no window
        around an occurrence that does not exist, and inventing one would
        hand the judge text with no established relation to the value."""
        source = tmp_path / "note.md"
        source.write_text("No addresses in this note at all.\n", encoding="utf-8")
        assert self._question(tmp_path, str(source)).source_excerpt is None

    def test_the_excerpt_is_fenced_in_the_prompt_not_here(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fencing happens ONCE, at the prompt-assembly site that already
        owns it -- double-fencing would nest delimiters inside a fence."""
        _patch_record(monkeypatch, _entry())
        source = tmp_path / "note.md"
        source.write_text(f"Referred by {_VALUE}.\n", encoding="utf-8")
        context = _context(tmp_path)
        client = _judge_client(verdict="own", score=0.5)
        _escalate(
            context,
            _suspect_verdict(context),
            client=client,
            source_ref=str(source),
        )
        sent = client.calls[0]["messages"][0]["content"]
        assert sent.count("<source_excerpt") == 1
        assert "Referred by" in sent

    def test_a_dict_source_ref_resolves_through_its_path_key(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "note.md"
        source.write_text(f"cc: {_VALUE}\n", encoding="utf-8")
        question = self._question(tmp_path, {"path": str(source), "ref": "note"})
        assert question.source_excerpt is not None
        assert _VALUE in question.source_excerpt


class TestRawValueNeverReachesPendingQuestions:
    """The explicit AC, through a REAL ``tier4_escalate`` write.

    Asserting on the ``EscalationItem`` alone would not settle it: the raw
    value must be absent from the file ``_pending_questions.md`` actually
    ends up holding, after whatever rendering ``tier4_escalate`` applies.
    """

    def _render(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        _patch_record(monkeypatch, _entry(threshold=0.99))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="other-known", owner_uid=_HOLDER_UID, score=0.6),
        )
        assert outcome.queue_items
        pending = tmp_path / "knowledge" / "wiki" / "_pending_questions.md"
        tier4_escalate(list(outcome.queue_items), pending)
        return pending.read_text(encoding="utf-8")

    def test_the_raw_value_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = self._render(tmp_path, monkeypatch)
        assert _VALUE not in text
        assert "introducer@" not in text

    def test_the_masked_form_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = self._render(tmp_path, monkeypatch)
        assert pii.mask_contact_address(_VALUE) in text

    def test_the_pii_scanner_finds_no_inline_address_in_the_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stronger than a substring check: the masked form must not itself
        be detectable as a fresh inline contact token by the very scanner
        the wiki is protected with."""
        text = self._render(tmp_path, monkeypatch)
        assert pii.find_inline_emails(text) == []

    def test_the_block_still_identifies_the_value_to_a_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = self._render(tmp_path, monkeypatch)
        assert "i***@example.org" in text


# ---------------------------------------------------------------------------
# B3 -- the placement handoff
# ---------------------------------------------------------------------------


class TestPlacementHandoff:
    def test_at_threshold_reaches_the_executor_in_observe_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.98))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=0.95),
        )
        # calibrated 0.98 == threshold 0.98 -> "at or above".
        assert outcome.calibrated_p == 0.98
        assert outcome.above_threshold is True
        assert outcome.queue_items == ()
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_PROPOSED
        assert outcome.placement.placed_uid == _TARGET_UID
        assert outcome.placement.field == "emails"

    def test_apply_mode_files_with_the_model_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=0.95),
            apply=True,
        )
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_FILED
        assert outcome.placement.path is not None
        meta, _ = parse_frontmatter(outcome.placement.path.read_text(encoding="utf-8"))
        source = meta["field_sources"]["emails"][0]["source"]
        assert source["type"] == "attribution"
        assert source["decided_by"] == "model"
        assert source["writer"] == pii_attribution.WRITER_RE_ATTRIBUTION
        assert source["original_target"] == _TARGET_UID
        assert source["calibrated_p"] == 0.98
        assert source["model"] == _MODEL
        assert source["prompt_sha256"]

    def test_same_entity_proposes_a_merge_and_writes_no_contact_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        before = _snapshot(tmp_path)
        calls: list[dict[str, Any]] = []

        def _proposer(**kwargs: Any) -> None:
            calls.append(kwargs)

        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(
                verdict="same-entity", owner_uid=_HOLDER_UID, score=0.95
            ),
            apply=True,
            propose_merge=_proposer,
        )
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_MERGE_PROPOSED
        assert len(calls) == 1
        assert outcome.merge_proposal is not None
        assert outcome.merge_proposal.owner_uid == _HOLDER_UID
        assert outcome.merge_proposal.target_uid == _TARGET_UID
        # The rationale an adapter may render carries only the masked value.
        assert pii.mask_contact_address(_VALUE) in outcome.merge_proposal.rationale
        assert _VALUE not in outcome.merge_proposal.rationale
        assert _snapshot(tmp_path) == before

    def test_same_entity_with_no_proposer_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        before = _snapshot(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(
                verdict="same-entity", owner_uid=_HOLDER_UID, score=0.95
            ),
            apply=True,
        )
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_SKIPPED
        assert outcome.merge_proposal is None
        assert _snapshot(tmp_path) == before

    def test_other_unknown_stays_parked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        pii_attribution.park_suspect(
            context,
            _VALUE,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )
        outcome = _escalate(
            context,
            verdict,
            client=_judge_client(verdict="other-unknown", score=0.95),
            apply=True,
        )
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_PARKED
        suspect = pii_attribution.read_suspect(context.contacts_root, outcome.suspect_id)
        assert suspect is not None
        assert suspect.judgement is not None
        assert suspect.judgement["verdict"] == pii_attribution.JUDGEMENT_OTHER_UNKNOWN

    def test_the_escalation_logs_the_proposal_with_its_calibrated_probability(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.98))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=0.95),
        )
        rows = [
            json.loads(line)
            for line in pii_attribution.default_attribution_log_path(context.contacts_root)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        escalations = [
            r
            for r in rows
            if r.get("kind") == pii_attribution.ATTRIBUTION_LOG_KIND_ESCALATION
        ]
        assert len(escalations) == 1
        row = escalations[0]
        assert row["placement"] == pii.PLACEMENT_PROPOSED
        assert row["calibrated_p"] == 0.98
        assert row["threshold"] == 0.98
        assert row["above_threshold"] is True
        assert row["question_class"] == pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE
        assert row["mode"] == "observe"
        assert row["value_key"] == _suspect_verdict(context).value_key
        assert outcome.judgement is not None
        assert row["model"] == outcome.judgement.model

    def test_the_escalation_log_row_carries_no_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.98))
        context = _context(tmp_path)
        _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=0.95),
        )
        text = pii_attribution.default_attribution_log_path(
            context.contacts_root
        ).read_text(encoding="utf-8")
        assert _VALUE not in text
        assert pii.find_inline_emails(text) == []


class TestNeverGrantsOutreachEligibility:
    """Design §3: a model never grants outreach eligibility.

    Lane A asserts this at the executor. This asserts it END TO END from the
    escalation, for every verdict that writes -- the path a real run takes.
    """

    @pytest.mark.parametrize(
        ("judge_payload", "expected_field"),
        [
            ({"verdict": "own", "score": 0.95}, "emails"),
            (
                {"verdict": "other-known", "owner_uid": _HOLDER_UID, "score": 0.95},
                "emails",
            ),
            (
                {"verdict": "alias", "alias_relation": "role-inbox", "score": 0.95},
                "alt_emails",
            ),
        ],
    )
    def test_no_placement_writes_observed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        judge_payload: dict[str, Any],
        expected_field: str,
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        outcome = _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(**judge_payload),
            apply=True,
        )
        assert outcome.placement is not None
        assert outcome.placement.outcome == pii.PLACEMENT_FILED
        assert outcome.placement.field == expected_field
        assert outcome.placement.usage_class == pii.USAGE_CLASS_UNCLASSIFIED
        assert outcome.placement.path is not None
        meta, _ = parse_frontmatter(outcome.placement.path.read_text(encoding="utf-8"))
        entries = meta.get(pii.CONTACT_CLASSIFICATION_FIELD) or []
        assert entries
        for entry in entries:
            assert entry.get("usage_class") != pii.USAGE_CLASS_OBSERVED
        assert (
            pii.classification_for_value(meta, _VALUE).usage_class
            != pii.USAGE_CLASS_OBSERVED
        )

    def test_no_file_anywhere_on_the_surface_records_observed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        _escalate(
            context,
            _suspect_verdict(context),
            client=_judge_client(verdict="own", score=0.95),
            apply=True,
        )
        for path in sorted(context.contacts_root.rglob("*.md")):
            assert "usage_class: observed" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B3 -- the suspect-file judgement record
# ---------------------------------------------------------------------------


class TestSuspectJudgementRecord:
    def _parked(
        self, tmp_path: Path
    ) -> tuple[
        pii_attribution.AttributionContext, pii_attribution.AttributionVerdict, Path
    ]:
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        path = pii_attribution.park_suspect(
            context,
            _VALUE,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )
        return context, verdict, path

    def test_records_the_judgement_additively(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.99))
        context, verdict, path = self._parked(tmp_path)
        before, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        _escalate(
            context,
            verdict,
            client=_judge_client(verdict="other-unknown", score=0.6),
        )
        after, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        # Nothing park_suspect wrote was dropped or reordered.
        assert list(after)[: len(before)] == list(before)
        for key in before:
            if key != "updated_at":
                assert after[key] == before[key]
        assert after["judgement"]["verdict"] == pii_attribution.JUDGEMENT_OTHER_UNKNOWN
        assert after["judgement"]["calibrated_p"] == 0.70
        assert after["judgement"]["threshold"] == 0.99
        assert (
            after["judgement"]["question_class"]
            == pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE
        )

    def test_a_re_park_carries_the_judgement_forward(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.99))
        context, verdict, path = self._parked(tmp_path)
        _escalate(context, verdict, client=_judge_client(verdict="own", score=0.6))
        pii_attribution.park_suspect(
            context,
            _VALUE,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["judgement"]["verdict"] == pii_attribution.JUDGEMENT_OWN

    def test_an_unchanged_re_park_still_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``park_suspect``'s idempotency property, preserved."""
        _patch_record(monkeypatch, _entry(threshold=0.99))
        context, verdict, path = self._parked(tmp_path)
        _escalate(context, verdict, client=_judge_client(verdict="own", score=0.6))
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        pii_attribution.park_suspect(
            context,
            _VALUE,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime

    def test_re_recording_the_same_judgement_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.99))
        context, verdict, path = self._parked(tmp_path)
        judgement = pii_attribution.AttributionJudgement(
            verdict=pii_attribution.JUDGEMENT_OWN, score=0.6, model="m", prompt_sha256="s"
        )
        pii_attribution.record_suspect_judgement(
            context,
            suspect_id=path.stem,
            question_class=pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE,
            judgement=judgement,
            calibrated_p=0.7,
            threshold=0.99,
        )
        before = path.read_bytes()
        pii_attribution.record_suspect_judgement(
            context,
            suspect_id=path.stem,
            question_class=pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE,
            judgement=judgement,
            calibrated_p=0.7,
            threshold=0.99,
        )
        assert path.read_bytes() == before

    def test_an_absent_suspect_file_is_not_an_error(
        self, tmp_path: Path
    ) -> None:
        context = _context(tmp_path)
        result = pii_attribution.record_suspect_judgement(
            context,
            suspect_id=pii_attribution.suspect_id_for("0" * 64, "uid-nobody"),
            question_class=pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE,
            judgement=pii_attribution.AttributionJudgement(verdict="own"),
            calibrated_p=None,
            threshold=None,
        )
        assert result is None


class TestReadSuspect:
    def test_reads_a_parked_suspect_with_its_raw_value(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = _suspect_verdict(context)
        pii_attribution.park_suspect(
            context,
            _VALUE,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )
        suspect_id = pii_attribution.suspect_id_for(verdict.value_key, _TARGET_UID)
        suspect = pii_attribution.read_suspect(context.contacts_root, suspect_id)
        assert suspect is not None
        assert suspect.value == _VALUE
        assert suspect.target_uid == _TARGET_UID
        assert suspect.target_name == _TARGET_NAME
        assert suspect.checks == (pii_attribution.CHECK_D2_HELD_ELSEWHERE,)
        assert suspect.candidates == (_HOLDER_UID,)
        assert suspect.shadow is True

    def test_missing_suspect_is_none_not_an_error(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        assert (
            pii_attribution.read_suspect(
                context.contacts_root,
                pii_attribution.suspect_id_for("a" * 64, "uid-nobody"),
            )
            is None
        )

    def test_an_unreadable_surface_raises_rather_than_reporting_absence(
        self, tmp_path: Path
    ) -> None:
        """The fail-closed gate: a false "no such suspect" is the failure
        mode ``pii``'s readable-surface check exists to prevent."""
        with pytest.raises(pii.ExcludedSurfaceUnavailable):
            pii_attribution.read_suspect(
                tmp_path / "not-a-surface",
                pii_attribution.suspect_id_for("a" * 64, "uid-nobody"),
            )

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "not-a-suspect-id",
            "../../etc/passwd",
            "a" * 64 + "-../escape",
            "a" * 63 + "-uid",  # too short a digest
            "z" * 64 + "-uid",  # not hex
            "a" * 64,  # no uid
        ],
    )
    def test_a_malformed_suspect_id_is_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(pii_attribution.SuspectIdError):
            pii_attribution.suspect_path_for_id(tmp_path, bad)

    def test_a_well_formed_id_resolves_inside_the_suspects_directory(
        self, tmp_path: Path
    ) -> None:
        path = pii_attribution.suspect_path_for_id(
            tmp_path, pii_attribution.suspect_id_for("a" * 64, "uid-x")
        )
        assert path.parent == tmp_path / pii_attribution.ATTRIBUTION_SUSPECTS_DIRNAME


# ---------------------------------------------------------------------------
# B4 -- observe mode
# ---------------------------------------------------------------------------


class TestShadowSweepObserveMode:
    def _park(
        self,
        context: pii_attribution.AttributionContext,
        *,
        value: str,
        target_uid: str,
        shadow: bool = True,
    ) -> pii_attribution.AttributionVerdict:
        verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_SUSPECT,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE, uids=(_HOLDER_UID,)
                ),
            ),
            candidates=(_HOLDER_UID,),
            value_key=pii_attribution.value_key_for(context, value),
        )
        pii_attribution.park_suspect(
            context,
            value,
            verdict,
            target_uid=target_uid,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=shadow,
        )
        return verdict

    def test_logs_a_proposed_placement_and_files_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.98))
        context = _context(tmp_path)
        _write_contact_record(
            context.contacts_root, "target.md", uid=_TARGET_UID, emails=["old@example.com"]
        )
        self._park(context, value=_VALUE, target_uid=_TARGET_UID)
        before = _snapshot(tmp_path)

        summary = pii_attribution.escalate_shadow_suspects(
            context,
            client=_judge_client(verdict="own", score=0.95),
            config=EXCLUDED_CONFIG,
            usage=TokenUsage(),
        )
        assert summary.considered == 1
        assert summary.escalated == 1
        assert summary.proposed == 1
        assert summary.queued == 0
        assert [p.outcome for p in summary.placements] == [pii.PLACEMENT_PROPOSED]

        # Nothing a writer produced moved: the wiki pages and the contact
        # record are byte-identical, and no new record appeared.
        assert _snapshot(tmp_path) == before

        rows = [
            json.loads(line)
            for line in pii_attribution.default_attribution_log_path(context.contacts_root)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        escalations = [
            r
            for r in rows
            if r.get("kind") == pii_attribution.ATTRIBUTION_LOG_KIND_ESCALATION
        ]
        assert len(escalations) == 1
        assert escalations[0]["placement"] == pii.PLACEMENT_PROPOSED
        assert escalations[0]["calibrated_p"] == 0.98

    def test_the_target_record_never_gains_the_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry(threshold=0.5))
        context = _context(tmp_path)
        record = _write_contact_record(
            context.contacts_root, "target.md", uid=_TARGET_UID, emails=["old@example.com"]
        )
        self._park(context, value=_VALUE, target_uid=_TARGET_UID)
        pii_attribution.escalate_shadow_suspects(
            context,
            client=_judge_client(verdict="own", score=0.95),
            config=EXCLUDED_CONFIG,
        )
        assert _VALUE not in record.read_text(encoding="utf-8")

    def test_non_shadow_suspects_are_not_escalated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        self._park(context, value=_VALUE, target_uid=_TARGET_UID, shadow=False)
        summary = pii_attribution.escalate_shadow_suspects(
            context, client=_NoCallClient(), config=EXCLUDED_CONFIG
        )
        assert summary.considered == 0
        assert summary.escalated == 0

    def test_no_client_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A clientless sweep must not flood the human queue with `unclear`."""
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        self._park(context, value=_VALUE, target_uid=_TARGET_UID)
        before = _snapshot(tmp_path, skip_attribution=False)
        summary = pii_attribution.escalate_shadow_suspects(
            context, client=None, config=EXCLUDED_CONFIG
        )
        assert summary == pii_attribution.ShadowEscalationSummary()
        assert summary.queue_items == ()
        assert _snapshot(tmp_path, skip_attribution=False) == before

    def test_the_cap_bounds_the_work_and_reports_the_remainder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        for n in range(5):
            self._park(context, value=f"person{n}@example.org", target_uid=f"uid-t{n}")
        client = _judge_client(verdict="own", score=0.95)
        summary = pii_attribution.escalate_shadow_suspects(
            context, client=client, config=EXCLUDED_CONFIG, limit=2
        )
        assert summary.considered == 5
        assert summary.escalated == 2
        assert summary.skipped_over_cap == 3
        assert len(client.calls) == 2

    def test_a_non_positive_cap_disables_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        self._park(context, value=_VALUE, target_uid=_TARGET_UID)
        summary = pii_attribution.escalate_shadow_suspects(
            context, client=_NoCallClient(), config=EXCLUDED_CONFIG, limit=0
        )
        assert summary == pii_attribution.ShadowEscalationSummary()

    def test_the_sweep_order_is_stable_across_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capped sweep must work through a backlog, not re-roll a subset."""
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        for n in range(4):
            self._park(context, value=f"person{n}@example.org", target_uid=f"uid-t{n}")
        first = [
            s.suspect_id
            for s in pii_attribution.iter_parked_suspects(
                context.contacts_root, shadow_only=True
            )
        ]
        second = [
            s.suspect_id
            for s in pii_attribution.iter_parked_suspects(
                context.contacts_root, shadow_only=True
            )
        ]
        assert first == second == sorted(first)

    def test_the_sweep_queues_what_it_cannot_settle_without_filing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch)  # no calibration at all
        context = _context(tmp_path)
        self._park(context, value=_VALUE, target_uid=_TARGET_UID)
        summary = pii_attribution.escalate_shadow_suspects(
            context,
            client=_judge_client(verdict="own", score=1.0),
            config=EXCLUDED_CONFIG,
        )
        assert summary.queued == 1
        assert len(summary.queue_items) == 1
        # Returned for an L4 caller to act on -- NOT written anywhere here.
        assert not (tmp_path / "knowledge" / "wiki" / "_pending_questions.md").exists()

    def test_an_unreadable_surface_raises_rather_than_reporting_an_empty_backlog(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(pii.ExcludedSurfaceUnavailable):
            pii_attribution.iter_parked_suspects(tmp_path / "not-a-surface")

    def test_a_malformed_suspect_file_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_record(monkeypatch, _entry())
        context = _context(tmp_path)
        self._park(context, value=_VALUE, target_uid=_TARGET_UID)
        bad = (
            context.contacts_root
            / pii_attribution.ATTRIBUTION_SUSPECTS_DIRNAME
            / ("f" * 64 + "-uid-broken.md")
        )
        bad.write_text("no frontmatter here at all\n", encoding="utf-8")
        found = pii_attribution.iter_parked_suspects(
            context.contacts_root, shadow_only=True
        )
        assert len(found) == 1


class TestLibrarianShadowPhase:
    def test_the_phase_is_off_by_default(self, tmp_path: Path) -> None:
        from athenaeum.librarian import _run_attribution_shadow_phase
        from tests.test_librarian_run_phases import _make_ctx

        ctx = _make_ctx(tmp_path)
        ctx.config = {}
        _run_attribution_shadow_phase(ctx)
        # A disabled phase never touches the field -- "didn't run" stays
        # distinguishable from "ran and saw nothing".
        assert ctx.attribution_shadow_summary is None

    def test_the_phase_runs_and_files_nothing_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian
        from tests.test_librarian_run_phases import _make_ctx

        _patch_record(monkeypatch, _entry(threshold=0.98))
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        _write_page(knowledge / "wiki", uid=_TARGET_UID, name=_TARGET_NAME)
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        record = _write_contact_record(
            context.contacts_root, "target.md", uid=_TARGET_UID, emails=["old@example.com"]
        )
        verdict = _suspect_verdict(context)
        pii_attribution.park_suspect(
            context,
            _VALUE,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )
        before = record.read_bytes()

        client = _judge_client(verdict="own", score=0.95)
        monkeypatch.setattr(librarian, "build_llm_client", lambda *a, **k: client)
        ctx = _make_ctx(tmp_path, knowledge_root=knowledge)
        ctx.config = {
            **EXCLUDED_CONFIG,
            "librarian": {"attribution": {"escalate": True}},
        }
        librarian._run_attribution_shadow_phase(ctx)

        assert ctx.attribution_shadow_summary == {
            "considered": 1,
            "escalated": 1,
            "proposed": 1,
            "would_queue": 0,
            "memo_hits": 0,
            "skipped_over_cap": 0,
        }
        assert record.read_bytes() == before
        assert ctx.knob_models["attribution"]

    def test_the_phase_is_skipped_once_the_deadline_tripped(self, tmp_path: Path) -> None:
        from athenaeum.librarian import _run_attribution_shadow_phase
        from tests.test_librarian_run_phases import _make_ctx

        ctx = _make_ctx(tmp_path)
        ctx.config = {"librarian": {"attribution": {"escalate": True}}}
        ctx.deadline_tripped = True
        _run_attribution_shadow_phase(ctx)
        assert ctx.attribution_shadow_summary == {"skipped_deadline_tripped": True}

    def test_a_failure_inside_the_phase_does_not_fail_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import librarian
        from tests.test_librarian_run_phases import _make_ctx

        monkeypatch.setattr(librarian, "build_llm_client", lambda *a, **k: _NoCallClient())

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the surface is gone")

        monkeypatch.setattr(
            librarian.pii_attribution, "build_attribution_context", _boom
        )
        ctx = _make_ctx(tmp_path)
        ctx.config = {"librarian": {"attribution": {"escalate": True}}}
        librarian._run_attribution_shadow_phase(ctx)
        assert ctx.attribution_shadow_summary == {"skipped_failed": True}
