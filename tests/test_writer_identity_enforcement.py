# SPDX-License-Identifier: Apache-2.0
"""Writer-identity enforcement flag (issue athenaeum#2041).

``librarian.writer_identity_enforced`` is DEFAULT OFF. Off, behaviour is
byte-identical to before the flag existed. On, self-declared identity (the
frontmatter ``asserter`` block, ``asserter.grants``, the channel-implied
``resolved_by="human"``) grants no authority. Nothing here VERIFIES a writer.

Imports of anything introduced by athenaeum#2041 are deferred into the tests
that need them, so :func:`collect_flag_off_outputs` can be executed against a
pre-change checkout to regenerate :data:`GOLDEN_FLAG_OFF`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from athenaeum.answers import ingest_answers
from athenaeum.asserter_authority import compare_authority, strictly_greater_authority
from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    ComparatorPage,
    CompareOutcome,
    page_from_text,
)
from athenaeum.fingerprint import claim_pair_fingerprint, load_resolved_records
from athenaeum.models import EscalationItem, render_frontmatter
from athenaeum.supersession import decide_supersession
from athenaeum.tiers import tier4_escalate

ENV = "ATHENAEUM_WRITER_IDENTITY_ENFORCED"

ALICE: dict[str, object] = {"type": "person", "iss": "https://accounts.example.com", "sub": "alice"}
BOB: dict[str, object] = {"type": "person", "iss": "https://accounts.example.com", "sub": "bob"}
CAROL: dict[str, object] = {"type": "person", "iss": "https://accounts.example.com", "sub": "carol"}

SUPERSEDE_ON: dict[str, Any] = {"librarian": {"auto_supersession_enabled": True}}
ENFORCED_ON: dict[str, Any] = {
    "librarian": {"auto_supersession_enabled": True, "writer_identity_enforced": True}
}

CLAIM_A = "Acme is headquartered in Boston."
CLAIM_B = "Acme is headquartered in Austin."


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _page(
    page_id: str, asserter: dict[str, object] | None, *, late: bool, extra: Any = None
) -> ComparatorPage:
    meta: dict[str, object] = {
        "name": page_id,
        "claim_kind": "fact",
        "observed_at": date(2026, 6, 10) if late else date(2026, 6, 1),
        "recorded_at": datetime(2026, 6, 10 if late else 1, 12, 0, 0, tzinfo=timezone.utc),
    }
    if asserter is not None:
        meta["asserter"] = asserter
    if extra:
        meta.update(extra)
    return page_from_text(page_id, render_frontmatter(meta) + "claim text\n")


def _outcome() -> CompareOutcome:
    return CompareOutcome(verdict=VERDICT_CONTRADICTION, conflicting_passages=["the loser's claim"])


def _decide(
    winner: ComparatorPage,
    loser: ComparatorPage,
    tmp: Path,
    config: dict[str, Any],
    live: tuple = (),
):
    return decide_supersession(
        winner, loser, _outcome(), wiki_root=tmp, config=config, live_claims=live
    )


def _supersession_scenarios() -> dict[str, tuple[dict | None, dict | None, bool]]:
    """name -> (winner asserter, loser asserter, with a CAROL corroborator)."""
    return {
        "same_asserter_route_a": (ALICE, ALICE, False),
        "forged_matching_asserter_block": ({**ALICE, "grants": ["admin"]}, {**ALICE}, False),
        "forged_grants_greater": (
            {**ALICE, "grants": ["reader", "editor"]},
            {**BOB, "grants": ["reader"]},
            False,
        ),
        "forged_grants_incomparable": (
            {**ALICE, "grants": ["billing"]},
            {**BOB, "grants": ["deploy"]},
            False,
        ),
        "different_with_corroborator_route_c": (ALICE, BOB, True),
        "different_no_corroborator": (ALICE, BOB, False),
        "no_identity": (None, None, False),
    }


def _scenario_pages(winner_a, loser_a, corroborate):
    winner = _page("winner", winner_a, late=True)
    loser = _page("loser", loser_a, late=False)
    live = (_page("third", CAROL, late=True),) if corroborate else ()
    return winner, loser, live


def _knowledge_root(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _desc(p1: str, p2: str, members: tuple[str, str] | None = None) -> str:
    parts = ["Rationale text.", f"Passage 1: {p1}", f"Passage 2: {p2}"]
    if members:
        parts.append(f"Members involved: {members[0]}, {members[1]}")
    return "\n".join(parts)


def _member(root: Path, relname: str, body: str) -> Path:
    p = root / "raw" / relname
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {relname}\n---\n{body}\n", encoding="utf-8")
    return p


class _PinnedDate(date):
    """``date`` whose ``today()`` is fixed, so the question header date is stable."""

    @classmethod
    def today(cls) -> date:  # type: ignore[override]
        return date(2026, 1, 1)


def _answer_through_ingest(root: Path, config: dict[str, Any] | None) -> None:
    """Escalate CLAIM_A/CLAIM_B on a first page, answer it, run ingest-answers."""
    pending = root / "wiki" / "_pending_questions.md"
    (root / "raw" / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "wiki" / "x.md").write_text("Acme notes.\n", encoding="utf-8")
    # athenaeum#2120: pin the header date; the block id hashes it, so today's date made the golden rot daily.
    with mock.patch("athenaeum.tiers.date", _PinnedDate):
        tier4_escalate(
            [
                EscalationItem(
                    raw_ref="wiki/x.md",
                    entity_name="Acme",
                    conflict_type="factual",
                    description=_desc(CLAIM_A, CLAIM_B),
                )
            ],
            pending,
        )
    text = pending.read_text().replace("- [ ]", "- [x]", 1) + "\nnot_a_conflict Both are fine.\n"
    pending.write_text(text, encoding="utf-8")
    ingest_answers(pending, root / "raw", config=config)


def _resurfaced(root: Path) -> int:
    """Re-surface the same claim pair on another page; return the suppressed count."""
    item = EscalationItem(
        raw_ref="wiki/y.md",
        entity_name="AcmeOnPageY",
        conflict_type="factual",
        description=_desc(CLAIM_A, CLAIM_B, ("pageY/c.md", "pageY/d.md")),
    )
    return tier4_escalate([item], root / "wiki" / "_pending_questions.md")


def _ingest_records(root: Path) -> list[dict]:
    cache = root / "raw" / "_resolved_contradictions.jsonl"
    rows = [json.loads(ln) for ln in cache.read_text().splitlines() if ln.strip()]
    for row in rows:
        row.pop("resolved_at", None)  # wall-clock; not part of the decision
    return rows


# ---------------------------------------------------------------------------
# flag-off pin
# ---------------------------------------------------------------------------


def collect_flag_off_outputs(tmp_path: Path) -> dict[str, Any]:
    """Every decision this flag can touch, with the flag unset. Old API only."""
    out: dict[str, Any] = {}
    for name, (wa, la, corroborate) in _supersession_scenarios().items():
        winner, loser, live = _scenario_pages(wa, la, corroborate)
        d = _decide(winner, loser, tmp_path, SUPERSEDE_ON, live)
        out[f"supersession:{name}"] = [
            d.action,
            d.reason,
            d.blocked_by,
            sorted(d.conditions.items()),
        ]
        out[f"authority:{name}"] = [
            compare_authority(winner.meta, loser.meta),
            strictly_greater_authority(winner.meta, loser.meta),
        ]
    root = _knowledge_root(tmp_path / "answers")
    _answer_through_ingest(root, None)
    out["ingest:records"] = _ingest_records(root)
    out["tier4:resurfaced_suppressed"] = _resurfaced(root)
    return out


#: Recorded from the code on ``develop`` BEFORE athenaeum#2041 landed.
GOLDEN_FLAG_OFF: dict[str, Any] = json.loads(
    r"""
{
 "authority:different_no_corroborator": [
  "equal",
  false
 ],
 "authority:different_with_corroborator_route_c": [
  "equal",
  false
 ],
 "authority:forged_grants_greater": [
  "greater",
  true
 ],
 "authority:forged_grants_incomparable": [
  "incomparable",
  false
 ],
 "authority:forged_matching_asserter_block": [
  "incomparable",
  false
 ],
 "authority:no_identity": [
  "equal",
  false
 ],
 "authority:same_asserter_route_a": [
  "equal",
  false
 ],
 "ingest:records": [
  {
   "action": "not_a_conflict",
   "fingerprint": "e911decd52a5d379",
   "member_key": null,
   "pair_text": "acme is headquartered in austin.\n##\nacme is headquartered in boston.",
   "resolved_by": "human",
   "side_a_norm": "acme is headquartered in boston.",
   "side_b_norm": "acme is headquartered in austin.",
   "source_verdict_id": "7af7d1776734"
  }
 ],
 "supersession:different_no_corroborator": [
  "queue",
  "queued: blocked by asserter_route",
  [
   "asserter_route"
  ],
  [
   [
    "asserter_route",
    false
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    false
   ],
   [
    "route_b_greater_authority",
    false
   ],
   [
    "route_c_corroborated",
    false
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "supersession:different_with_corroborator_route_c": [
  "applied",
  "applied: route (c) -- equal/incomparable authority with an independent corroborating live claim",
  [],
  [
   [
    "asserter_route",
    true
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    false
   ],
   [
    "route_b_greater_authority",
    false
   ],
   [
    "route_c_corroborated",
    true
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "supersession:forged_grants_greater": [
  "applied",
  "applied: route (b) -- winner has strictly greater authority",
  [],
  [
   [
    "asserter_route",
    true
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    false
   ],
   [
    "route_b_greater_authority",
    true
   ],
   [
    "route_c_corroborated",
    false
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "supersession:forged_grants_incomparable": [
  "queue",
  "queued: blocked by asserter_route",
  [
   "asserter_route"
  ],
  [
   [
    "asserter_route",
    false
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    false
   ],
   [
    "route_b_greater_authority",
    false
   ],
   [
    "route_c_corroborated",
    false
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "supersession:forged_matching_asserter_block": [
  "applied",
  "applied: route (a) -- same asserter revising its own claim",
  [],
  [
   [
    "asserter_route",
    true
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    true
   ],
   [
    "route_b_greater_authority",
    false
   ],
   [
    "route_c_corroborated",
    false
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "supersession:no_identity": [
  "queue",
  "queued: blocked by asserter_route",
  [
   "asserter_route"
  ],
  [
   [
    "asserter_route",
    false
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    false
   ],
   [
    "route_b_greater_authority",
    false
   ],
   [
    "route_c_corroborated",
    false
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "supersession:same_asserter_route_a": [
  "applied",
  "applied: route (a) -- same asserter revising its own claim",
  [],
  [
   [
    "asserter_route",
    true
   ],
   [
    "auto_supersession_enabled",
    true
   ],
   [
    "located",
    true
   ],
   [
    "no_overlaps",
    true
   ],
   [
    "no_third_conflicting_live_claim",
    true
   ],
   [
    "observed_time_strictly_later",
    true
   ],
   [
    "rate_limit_per_asserter",
    true
   ],
   [
    "rate_limit_per_claim",
    true
   ],
   [
    "recorded_time_not_earlier",
    true
   ],
   [
    "route_a_same_asserter",
    true
   ],
   [
    "route_b_greater_authority",
    false
   ],
   [
    "route_c_corroborated",
    false
   ],
   [
    "standing_state",
    true
   ],
   [
    "verdict_is_contradiction",
    true
   ]
  ]
 ],
 "tier4:resurfaced_suppressed": 1
}
"""
)


class TestFlagOffByteIdentical:
    """Pins today's self-declared behaviour as UNCHANGED, not as correct.

    Forged ``asserter.grants`` and a forged matching ``asserter`` block still
    confer authority with the flag off; that is the pre-existing exposure the
    flag exists to close, and this test deliberately does not bless it.
    """

    def test_flag_off_matches_pre_change_golden(self, tmp_path: Path) -> None:
        got = json.loads(json.dumps(collect_flag_off_outputs(tmp_path), default=list))
        assert got == GOLDEN_FLAG_OFF

    def test_explicit_false_is_same_as_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unset = json.loads(json.dumps(collect_flag_off_outputs(tmp_path / "u"), default=list))
        monkeypatch.setenv(ENV, "0")
        explicit = json.loads(json.dumps(collect_flag_off_outputs(tmp_path / "e"), default=list))
        assert unset == explicit


# ---------------------------------------------------------------------------
# resolver (AC5)
# ---------------------------------------------------------------------------


class TestResolveWriterIdentityEnforced:
    def test_default_off(self) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        assert resolve_writer_identity_enforced(None) is False
        assert resolve_writer_identity_enforced({}) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " ON "])
    def test_env_truthy(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        monkeypatch.setenv(ENV, value)
        assert resolve_writer_identity_enforced(None) is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "", "enabled", "2"])
    def test_env_other_strings_are_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        monkeypatch.setenv(ENV, value)
        assert (
            resolve_writer_identity_enforced({"librarian": {"writer_identity_enforced": True}})
            is False
        )

    def test_yaml_true(self) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        assert (
            resolve_writer_identity_enforced({"librarian": {"writer_identity_enforced": True}})
            is True
        )

    @pytest.mark.parametrize("raw", ["true", 1, "yes", None, [True]])
    def test_non_bool_yaml_falls_through_to_off(self, raw: object) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        assert (
            resolve_writer_identity_enforced({"librarian": {"writer_identity_enforced": raw}})
            is False
        )

    def test_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        monkeypatch.setenv(ENV, "0")
        assert (
            resolve_writer_identity_enforced({"librarian": {"writer_identity_enforced": True}})
            is False
        )

    def test_no_defaults_seed(self) -> None:
        from athenaeum import config

        assert "writer_identity_enforced" not in json.dumps(config._DEFAULTS, default=str)

    def test_docstring_states_both_states(self) -> None:
        from athenaeum.config import resolve_writer_identity_enforced

        doc = resolve_writer_identity_enforced.__doc__ or ""
        assert "**Off (the default)**" in doc and "**On**" in doc


# ---------------------------------------------------------------------------
# flag on: resolver authority
# ---------------------------------------------------------------------------


class TestFlagOnResolverAuthority:
    def test_forged_grants_confer_nothing(self) -> None:
        winner = _page("w", {**ALICE, "grants": ["reader", "editor"]}, late=True)
        loser = _page("l", {**BOB, "grants": ["reader"]}, late=False)
        assert compare_authority(winner.meta, loser.meta) == "greater"  # flag-off arg default
        on = compare_authority(winner.meta, loser.meta, writer_identity_enforced=True)
        none_declared = compare_authority(
            _page("a", ALICE, late=True).meta, _page("b", BOB, late=True).meta
        )
        assert on == none_declared == "equal"
        assert (
            strictly_greater_authority(winner.meta, loser.meta, writer_identity_enforced=True)
            is False
        )

    def test_declared_grants_empty_when_enforced(self) -> None:
        from athenaeum.asserter_authority import declared_grants

        meta = {"asserter": {**ALICE, "grants": ["admin"]}}
        assert declared_grants(meta) == frozenset({"admin"})
        assert declared_grants(meta, writer_identity_enforced=True) == frozenset()

    def test_forged_grants_memory_queues_when_on_applies_when_off(self, tmp_path: Path) -> None:
        winner, loser, live = _scenario_pages(
            {**ALICE, "grants": ["reader", "editor"]}, {**BOB, "grants": ["reader"]}, False
        )
        off = _decide(winner, loser, tmp_path, SUPERSEDE_ON, live)
        on = _decide(winner, loser, tmp_path, ENFORCED_ON, live)
        assert off.action == "applied" and off.conditions["route_b_greater_authority"] is True
        assert on.action == "queue"
        assert on.conditions["route_b_greater_authority"] is False
        assert on.blocked_by == ["asserter_route"]

    def test_forged_matching_asserter_block_queues_when_on(self, tmp_path: Path) -> None:
        winner, loser, live = _scenario_pages(ALICE, ALICE, False)
        off = _decide(winner, loser, tmp_path, SUPERSEDE_ON, live)
        on = _decide(winner, loser, tmp_path, ENFORCED_ON, live)
        assert off.action == "applied" and off.conditions["route_a_same_asserter"] is True
        assert on.action == "queue"
        assert on.conditions["route_a_same_asserter"] is False

    def test_route_c_finds_no_corroboration_when_on(self, tmp_path: Path) -> None:
        winner, loser, live = _scenario_pages(ALICE, BOB, True)
        off = _decide(winner, loser, tmp_path, SUPERSEDE_ON, live)
        on = _decide(winner, loser, tmp_path, ENFORCED_ON, live)
        assert off.action == "applied" and off.conditions["route_c_corroborated"] is True
        assert on.action == "queue"
        assert on.conditions["route_c_corroborated"] is False

    def test_enforced_comparator_answers_unknown(self) -> None:
        from athenaeum.supersession import _compare_asserters

        assert _compare_asserters(ALICE, ALICE, False) == "same"
        assert _compare_asserters(ALICE, BOB, False) == "different"
        assert _compare_asserters(ALICE, ALICE, True) == "unknown"
        assert _compare_asserters(ALICE, BOB, True) == "unknown"

    def test_env_turns_enforcement_on_without_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        winner, loser, live = _scenario_pages(ALICE, ALICE, False)
        monkeypatch.setenv(ENV, "1")
        assert _decide(winner, loser, tmp_path, SUPERSEDE_ON, live).action == "queue"


# ---------------------------------------------------------------------------
# flag on: escalation answers
# ---------------------------------------------------------------------------


class TestFlagOnEscalationAnswers:
    def test_ingest_writes_human_unverified_and_answer_still_applies(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        _answer_through_ingest(root, ENFORCED_ON)
        rows = _ingest_records(root)
        assert [r["resolved_by"] for r in rows] == ["human-unverified"]
        assert rows[0]["action"] == "not_a_conflict"  # the answer itself is recorded
        archive = root / "wiki" / "_pending_questions_archive.md"
        assert archive.exists() and "Both are fine." in archive.read_text()

    def test_resurfaced_fingerprint_re_escalates_when_on(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        _answer_through_ingest(root, ENFORCED_ON)
        assert _resurfaced(root) == 0
        assert "AcmeOnPageY" in (root / "wiki" / "_pending_questions.md").read_text()

    def test_resurfaced_fingerprint_auto_applies_when_off(self, tmp_path: Path) -> None:
        root = _knowledge_root(tmp_path)
        _answer_through_ingest(root, None)
        assert _resurfaced(root) == 1
        assert "AcmeOnPageY" not in (root / "wiki" / "_pending_questions.md").read_text()

    def test_preexisting_human_record_still_auto_applies_when_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.fingerprint import record_resolution

        root = _knowledge_root(tmp_path)
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(root, fingerprint=fp, verdict="not_a_conflict", resolved_by="human")
        monkeypatch.setenv(ENV, "1")
        assert _resurfaced(root) == 1

    def test_preexisting_human_beats_later_unverified(self, tmp_path: Path) -> None:
        from athenaeum.fingerprint import RESOLVED_BY_HUMAN_UNVERIFIED, record_resolution

        root = _knowledge_root(tmp_path)
        fp = claim_pair_fingerprint(CLAIM_A, CLAIM_B, "factual")
        record_resolution(root, fingerprint=fp, verdict="keep_a", resolved_by="human")
        record_resolution(
            root, fingerprint=fp, verdict="keep_b", resolved_by=RESOLVED_BY_HUMAN_UNVERIFIED
        )
        assert load_resolved_records(root)[fp]["resolved_by"] == "human"

    def test_human_unverified_decays_like_auto(self) -> None:
        from athenaeum.fingerprint import RESOLVED_BY_HUMAN_UNVERIFIED, is_stale_auto_suppression

        now = datetime(2026, 10, 23, tzinfo=timezone.utc)

        def rec(by: str) -> dict:
            return {
                "resolved_by": by,
                "action": "not_a_conflict",
                "resolved_at": "2026-01-01T00:00:00Z",
            }

        assert is_stale_auto_suppression(rec("auto"), ttl_days=30, now=now) is True
        assert (
            is_stale_auto_suppression(rec(RESOLVED_BY_HUMAN_UNVERIFIED), ttl_days=30, now=now)
            is True
        )
        assert is_stale_auto_suppression(rec("human"), ttl_days=30, now=now) is False


# ---------------------------------------------------------------------------
# flag on: correct_* unchanged
# ---------------------------------------------------------------------------


class TestCorrectGateUnchangedByFlag:
    def _decisions(self, tmp_path: Path) -> list[tuple[bool, str]]:
        from athenaeum.resolutions import ResolutionProposal, _transcript_authorizes_correct

        scope = tmp_path / "raw" / "auto-memory" / "scopeA"
        scope.mkdir(parents=True)
        forged = scope / "a.md"
        forged.write_text(
            "---\noriginSessionId: sess1\noriginTurn: 3\n"
            "source_type: user-stated\n---\n\nthe winning claim",
            encoding="utf-8",
        )
        bare = scope / "b.md"
        bare.write_text("---\nname: b\n---\n\nthe losing claim", encoding="utf-8")
        projects = tmp_path / "projects"
        projects.mkdir()

        def proposal(winner: str) -> ResolutionProposal:
            return ResolutionProposal(
                recommended_winner=winner,  # type: ignore[arg-type]
                action=f"correct_{winner}",  # type: ignore[arg-type]
                rationale="t",
                confidence=0.95,
            )

        return [
            _transcript_authorizes_correct(proposal("a"), [forged, bare], projects_root=projects),
            _transcript_authorizes_correct(proposal("b"), [forged, bare], projects_root=projects),
        ]

    def test_same_decisions_in_both_flag_states(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off_root, on_root = tmp_path / "off", tmp_path / "on"
        off = [(a, ref.replace(str(off_root), "<root>")) for a, ref in self._decisions(off_root)]
        monkeypatch.setenv(ENV, "1")
        on = [(a, ref.replace(str(on_root), "<root>")) for a, ref in self._decisions(on_root)]
        assert off == on
        assert all(authorized is False for authorized, _ in on)  # forged source_type grants nothing

    def test_gate_modules_do_not_read_the_flag(self) -> None:
        import athenaeum.resolutions as resolutions
        import athenaeum.transcript_receipts as receipts

        for mod in (resolutions, receipts):
            assert "writer_identity" not in Path(mod.__file__).read_text(encoding="utf-8")
