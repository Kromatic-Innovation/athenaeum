# SPDX-License-Identifier: Apache-2.0
"""Tests for the recall-metrics scorer (athenaeum#2611).

Every ledger/transcript row is a hand-authored synthetic fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum import recall_metrics as rm

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "tests/evals/data/recall_metrics/gold-set-example.yaml"


def _gold(*tasks: tuple[str, list[tuple[str, str, str]]], session: bool = False) -> rm.GoldSet:
    return rm.parse_gold_set(
        {
            "version": 1,
            "tasks": [
                {
                    "id": tid,
                    "prompt": "p",
                    **({"session_id": f"s-{tid}"} if session else {}),
                    "relevant": [
                        {"uid": u, "source": "x.yaml", "relevance": rel, "reach": reach}
                        for u, rel, reach in pages
                    ],
                }
                for tid, pages in tasks
            ],
        }
    )


def _obs(tid: str, pushed=(), recalled=(), used=()) -> rm.TaskObservation:
    return rm.TaskObservation(tid, frozenset(pushed), frozenset(recalled), frozenset(used))


class TestExample:
    def test_example_loads_with_enough_tasks_and_non_pushed_labels(self) -> None:
        gold = rm.load_gold_set(EXAMPLE)
        assert len(gold.tasks) >= 10
        reaches = {p.reach for t in gold.tasks for p in t.relevant}
        assert reaches == {rm.REACH_LEXICAL, rm.REACH_LINKED}

    def test_every_label_names_a_real_corpus_page(self) -> None:
        gold = rm.load_gold_set(EXAMPLE)
        for task in gold.tasks:
            for page in task.relevant:
                text = (REPO / page.source).read_text(encoding="utf-8")
                assert f"- uid: {page.uid}\n" in text, (task.task_id, page.uid)

    def test_example_carries_no_session_ids(self) -> None:
        assert all(t.session_id is None for t in rm.load_gold_set(EXAMPLE).tasks)


class TestSchema:
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(version=2),
            lambda d: d.update(tasks=[]),
            lambda d: d["tasks"][0].update(relevant=[]),
            lambda d: d["tasks"][0]["relevant"][0].update(reach="nearby"),
            lambda d: d["tasks"][0]["relevant"][0].update(relevance="maybe"),
            lambda d: d["tasks"][0].update(prompt=""),
            lambda d: d["tasks"].append(dict(d["tasks"][0])),
        ],
    )
    def test_rejects_bad_shapes(self, mutate) -> None:
        data = {
            "version": 1,
            "tasks": [
                {"id": "a", "prompt": "p", "relevant": [{"uid": "u", "source": "s"}]}
            ],
        }
        mutate(data)
        with pytest.raises(rm.GoldSetError):
            rm.parse_gold_set(data)

    def test_defaults(self) -> None:
        g = rm.parse_gold_set(
            {
                "version": 1,
                "tasks": [
                    {"id": "a", "prompt": "p", "relevant": [{"uid": "u", "source": "s"}]}
                ],
            }
        )
        page = g.tasks[0].relevant[0]
        assert (page.relevance, page.reach) == (rm.RELEVANCE_REQUIRED, rm.REACH_LEXICAL)


class TestScoreTask:
    def test_all_four_metrics_by_hand(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical"), ("b", "required", "linked"),
                            ("c", "helpful", "lexical"), ("d", "required", "linked")]))
        # pushed a, c, z(irrelevant); recalled a and b (b never pushed) and z; used a only
        s = rm.score_task(gold.tasks[0], _obs("t", {"a", "c", "z"}, {"a", "b", "z"}, {"a"}))
        assert s.rate("push_coverage") == 2 / 4
        assert s.rate("recall_through") == 2 / 4
        assert s.rate("recall_through_of_pushed") == 1 / 2
        assert s.rate("use_precision") == 1 / 3
        assert s.rate("session_use_rate") == 1.0
        assert s.opened_without_push == 1  # b: found by the agent, not pushed
        assert (s.relevant_linked_only, s.relevant_linked_pushed) == (2, 0)

    def test_zero_denominators_are_none_not_zero(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical")]))
        s = rm.score_task(gold.tasks[0], _obs("t"))
        assert s.rate("push_coverage") == 0.0
        assert s.rate("use_precision") is None
        assert s.rate("recall_through_of_pushed") is None
        assert s.rate("session_use_rate") == 0.0

    def test_used_pushed_but_never_opened_counts_for_session_not_precision(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical")]))
        s = rm.score_task(gold.tasks[0], _obs("t", {"a"}, (), {"a"}))
        assert s.rate("session_use_rate") == 1.0
        assert s.rate("use_precision") is None

    def test_mismatched_observation_rejected(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical")]))
        with pytest.raises(ValueError):
            rm.score_task(gold.tasks[0], _obs("other"))


class TestAggregate:
    def test_pooled_rate_intervals_and_unobserved(self) -> None:
        gold = _gold(
            ("t1", [("a", "required", "lexical"), ("b", "required", "linked")]),
            ("t2", [("c", "required", "lexical")]),
            ("t3", [("d", "required", "lexical")]),
        )
        report = rm.score(gold, [_obs("t1", {"a"}, {"a"}, {"a"}), _obs("t2", {"c"}, (), ())])
        cov = report.aggregate["push_coverage"]
        assert (cov.numerator, cov.denominator, cov.n_tasks) == (2, 3, 2)
        assert cov.rate == pytest.approx(2 / 3)
        assert cov.wilson is not None and cov.wilson[0] < 2 / 3 < cov.wilson[1]
        assert cov.bootstrap is not None and 0 <= cov.bootstrap[0] <= cov.bootstrap[1] <= 1
        assert report.unobserved_tasks == ["t3"]  # never scored as zero
        assert report.aggregate["session_use_rate"].denominator == 2
        assert report.aggregate["use_precision"].denominator == 1  # t2 undefined, dropped
        assert report.linked_coverage.numerator == 0 and report.linked_coverage.denominator == 1

    def test_bootstrap_is_reproducible_and_seed_sensitive_only_in_bounds(self) -> None:
        gold = _gold(*[(f"t{i}", [("a", "required", "lexical")]) for i in range(8)])
        obs = [_obs(f"t{i}", {"a"} if i % 2 else (), (), ()) for i in range(8)]
        a = rm.score(gold, obs).aggregate["push_coverage"].bootstrap
        b = rm.score(gold, obs).aggregate["push_coverage"].bootstrap
        assert a == b and a is not None

    def test_single_task_has_wilson_but_no_bootstrap(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical")]))
        est = rm.score(gold, [_obs("t", {"a"})]).aggregate["push_coverage"]
        assert est.wilson is not None and est.bootstrap is None

    def test_empty_observations(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical")]))
        report = rm.score(gold, [])
        assert report.aggregate["push_coverage"].rate is None
        assert report.unobserved_tasks == ["t"]

    def test_unknown_task_rejected(self) -> None:
        gold = _gold(("t", [("a", "required", "lexical")]))
        with pytest.raises(ValueError):
            rm.score(gold, [_obs("nope")])

    def test_to_dict_is_json_and_has_no_ids_beyond_task_ids(self) -> None:
        gold = _gold(("t", [("secretpageid", "required", "lexical")]))
        ids = {"secretpageid"}
        d = rm.score(gold, [_obs("t", ids, ids, ids)]).to_dict()
        text = json.dumps(d)
        assert "secretpageid" not in text
        assert d["per_task"][0]["task_id"] == "t"


def _seed_push(cache: Path, session: str, pid: str, ts: str) -> None:
    rec = push_metrics.build_push_record(
        session_id=session,
        query="q",
        backend="fts5",
        hits=[("f.md", {"uid": pid}, push_metrics.estimate_tokens("body"))],
    )
    rec.ts = ts
    push_metrics.record_push(rec, cache_dir=cache)


def _write(projects: Path, session: str, records: list[dict]) -> None:
    d = projects / "-scope"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _asst(ts: str, *blocks: dict) -> dict:
    msg = {"role": "assistant", "content": list(blocks)}
    return {"type": "assistant", "timestamp": ts, "message": msg}


def _user_text(ts: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


class TestFromLedger:
    def test_end_to_end_synthetic_session(self, tmp_path: Path) -> None:
        cache, projects = tmp_path / "cache", tmp_path / "projects"
        sid = "s-t1"
        _seed_push(cache, sid, "aaaa1111", "2026-01-01T00:00:00Z")
        _seed_push(cache, sid, "bbbb2222", "2026-01-01T00:00:00Z")
        _write(
            projects,
            sid,
            [
                _asst(
                    "2026-01-01T00:05:00Z",
                    {"type": "tool_use", "id": "t1", "name": "mcp__athenaeum__read_entity",
                     "input": {"uid": "aaaa1111"}},
                    # cccc3333 is relevant but never pushed: the agent finds it itself
                    {"type": "tool_use", "id": "t2", "name": "mcp__athenaeum__read_entity",
                     "input": {"uid": "cccc3333"}},
                ),
                _user_text("2026-01-01T00:06:00Z", "please follow aaaa1111 here"),
            ],
        )
        gold = _gold(
            ("t1", [("aaaa1111", "required", "lexical"), ("cccc3333", "required", "linked"),
                    ("dddd4444", "required", "linked")]),
            session=True,
        )
        report = rm.score_from_ledger(gold, cache_dir=cache, projects_root=projects)
        s = report.per_task[0]
        assert s.relevant_pushed == 1  # aaaa1111 only
        assert s.relevant_recalled == 2  # aaaa1111 + cccc3333 (opened without push)
        assert s.opened_without_push == 1
        assert s.engaged == 2 and s.engaged_used == 1  # aaaa1111 cited, cccc3333 not
        assert s.session_used is True

    def test_open_before_push_does_not_count_for_a_pushed_page(self, tmp_path: Path) -> None:
        cache, projects = tmp_path / "cache", tmp_path / "projects"
        _seed_push(cache, "s-t1", "aaaa1111", "2026-01-01T01:00:00Z")
        _write(
            projects,
            "s-t1",
            [_asst("2026-01-01T00:05:00Z", {"type": "tool_use", "id": "t1",
                                            "name": "mcp__athenaeum__read_entity",
                                            "input": {"uid": "aaaa1111"}})],
        )
        gold = _gold(("t1", [("aaaa1111", "required", "lexical")]), session=True)
        s = rm.score_from_ledger(gold, cache_dir=cache, projects_root=projects).per_task[0]
        assert s.relevant_pushed == 1 and s.relevant_recalled == 0

    def test_own_push_echo_is_not_use(self, tmp_path: Path) -> None:
        cache, projects = tmp_path / "cache", tmp_path / "projects"
        _seed_push(cache, "s-t1", "aaaa1111", "2026-01-01T00:00:00Z")
        _write(
            projects,
            "s-t1",
            [{"type": "user", "timestamp": "2026-01-01T00:01:00Z",
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "x", "content": "**Uid:** aaaa1111"}]}}],
        )
        gold = _gold(("t1", [("aaaa1111", "required", "lexical")]), session=True)
        s = rm.score_from_ledger(gold, cache_dir=cache, projects_root=projects).per_task[0]
        assert s.session_used is False

    def test_listing_counts_as_recalled_documented_upper_bound(self, tmp_path: Path) -> None:
        """A recall result merely LISTING a relevant id scores it recalled."""
        cache, projects = tmp_path / "cache", tmp_path / "projects"
        _seed_push(cache, "s-t1", "aaaa1111", "2026-01-01T00:00:00Z")
        _write(
            projects,
            "s-t1",
            [
                _asst("2026-01-01T00:05:00Z", {"type": "tool_use", "id": "r1",
                                               "name": "mcp__athenaeum__recall",
                                               "input": {"query": "q"}}),
                {"type": "user", "timestamp": "2026-01-01T00:05:01Z",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "r1",
                      "content": "1. zzzz9999 other\n2. aaaa1111 listed only"}]}},
            ],
        )
        gold = _gold(("t1", [("aaaa1111", "required", "lexical")]), session=True)
        s = rm.score_from_ledger(gold, cache_dir=cache, projects_root=projects).per_task[0]
        assert s.relevant_recalled == 1  # upper bound: listing != deliberate read
        assert s.engaged_used == 0  # ...and it shows up as zero use precision

    def test_missing_transcript_is_unobserved_not_zero(self, tmp_path: Path) -> None:
        gold = _gold(("t1", [("aaaa1111", "required", "lexical")]), session=True)
        report = rm.score_from_ledger(
            gold, cache_dir=tmp_path / "c", projects_root=tmp_path / "p"
        )
        assert report.per_task == [] and report.unobserved_tasks == ["t1"]

    def test_task_without_session_id_is_unobserved(self, tmp_path: Path) -> None:
        gold = _gold(("t1", [("a", "required", "lexical")]))
        report = rm.score_from_ledger(gold, cache_dir=tmp_path / "c", projects_root=tmp_path / "p")
        assert report.unobserved_tasks == ["t1"]
