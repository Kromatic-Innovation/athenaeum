# SPDX-License-Identifier: Apache-2.0
"""Immediate apply for the answer write path (issue athenaeum#2531, design
athenaeum#2489 section 6).

``apply: immediate | deferred`` (default ``immediate``): write the
``raw/answers`` record, then apply ONLY that one file through the per-file
applier. These tests pin each acceptance criterion; the file-scoping and the
lock fallback are the load-bearing ones.
"""

from __future__ import annotations

import dataclasses
import inspect
import io
import json
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from athenaeum import decision_answers as da
from athenaeum.cli import main as cli_main
from athenaeum.decision_answers import (
    apply_decision_answers,
    write_and_apply_decision_answer,
    write_decision_answer,
)
from athenaeum.runlock import RunLock
from tests.test_decision_answers import _write_merge, _write_question_block
from tests.test_decisions_answer_coordinate_cli import _seed_coordinate_item


@pytest.fixture
def kroot(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "raw").mkdir()
    return tmp_path


def _question(kroot: Path) -> str:
    return _write_question_block(kroot / "wiki" / "_pending_questions.md")


def _checked(kroot: Path) -> bool:
    return "- [x]" in (kroot / "wiki" / "_pending_questions.md").read_text(encoding="utf-8")


def _answer(kroot: Path, qid: str, **kw):
    return write_and_apply_decision_answer(
        kroot / "raw",
        kroot / "wiki",
        decision_id=qid,
        decision_type="question",
        verdict="Series B.",
        **kw,
    )


_COORD_VERDICT = json.dumps(
    {"answers": [{"pair": "page-a+page-b", "dimensions": {"subject": {"page-a": "x"}}}]}
)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def _cli_answer(kroot: Path, qid: str, dtype: str, verdict: str, *extra: str):
    return _run(
        [
            "decisions", "answer", "--path", str(kroot), "--id", qid, "--type", dtype,
            "--answer", json.dumps({"verdict": verdict}), "--json", *extra,
        ]
    )  # fmt: skip


class TestDefaultIsImmediate:
    def test_function_default(self) -> None:
        sig = inspect.signature(write_and_apply_decision_answer)
        assert sig.parameters["apply"].default == "immediate"

    def test_bare_call_applies_now(self, kroot: Path) -> None:
        result = _answer(kroot, _question(kroot))
        assert result.apply_requested == "immediate"
        assert result.status == "applied"
        assert _checked(kroot)

    def test_invalid_mode_rejected_before_writing(self, kroot: Path) -> None:
        with pytest.raises(ValueError):
            _answer(kroot, _question(kroot), apply="sometime")
        assert not (kroot / "raw" / "answers").exists()

    def test_cli_default_is_immediate(self, kroot: Path) -> None:
        qid = _question(kroot)
        rc, out, _err = _cli_answer(kroot, qid, "question", "Series B.")
        payload = json.loads(out)
        assert rc == 0 and payload["apply_status"] == "applied" and payload["applied"] is True
        assert _checked(kroot)

    def test_mcp_default_is_immediate(self, kroot: Path) -> None:
        sig = _mcp_fn(kroot, "resolve_question")
        assert inspect.signature(sig).parameters["apply"].default == "immediate"


def _mcp_fn(kroot: Path, name: str):
    pytest.importorskip("fastmcp")
    import asyncio

    from athenaeum.mcp_server import create_server

    server = create_server(raw_root=kroot / "raw", wiki_root=kroot / "wiki")

    async def _get():
        return (await server.get_tool(name)).fn

    return asyncio.run(_get())


class TestRecordBeforeApplier:
    def test_record_exists_when_applier_runs_and_change_is_visible(
        self, kroot: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _question(kroot)
        seen: list[bool] = []
        real = da._apply_loaded_answer

        def spy(wiki_root, path, answer, **kw):
            seen.append(path.exists() and path.parent == kroot / "raw" / "answers")
            return real(wiki_root, path, answer, **kw)

        monkeypatch.setattr(da, "_apply_loaded_answer", spy)
        result = _answer(kroot, qid)
        assert seen == [True]
        assert result.path.exists()
        # Visible with no ingest-answers tick in between.
        assert _checked(kroot)

    def test_mcp_resolve_question_applies_synchronously(self, kroot: Path) -> None:
        qid = _question(kroot)
        res = _mcp_fn(kroot, "resolve_question")(qid, "Series B.")
        assert res["ok"] is True and res["applied"] is True
        assert res["apply_status"] == "applied" and res["deferred"] is False
        assert Path(res["answer_file"]).exists()
        assert _checked(kroot)

    def test_mcp_resolve_merge_applies_synchronously(self, kroot: Path) -> None:
        wiki = kroot / "wiki"
        mid = _write_merge(
            wiki / "_pending_merges.md",
            target="imm-topic",
            src_a=wiki / "feedback_a.md",
            src_b=wiki / "feedback_b.md",
        )
        res = _mcp_fn(kroot, "resolve_merge")(mid, "approve")
        assert res["applied"] is True
        assert (wiki / "imm-topic.md").exists()

    def test_mcp_invalid_apply_refused_without_writing(self, kroot: Path) -> None:
        qid = _question(kroot)
        res = _mcp_fn(kroot, "resolve_question")(qid, "x", apply="whenever")
        assert res["ok"] is False and res["error_code"] == "invalid_apply"
        assert not (kroot / "raw" / "answers").exists()


class TestApplierRaises:
    def test_record_stays_and_next_tick_retries(
        self, kroot: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qid = _question(kroot)

        def boom(*a, **kw):
            raise RuntimeError("applier exploded")

        monkeypatch.setattr(da, "_apply_loaded_answer", boom)
        result = _answer(kroot, qid)
        assert result.status == "error" and result.error_code == "applier_error"
        assert result.deferred is True
        assert "applier exploded" in result.message
        assert result.path.exists()
        assert not _checked(kroot)

        monkeypatch.undo()
        report = apply_decision_answers(kroot / "wiki", kroot / "raw")
        assert report.applied == 1
        assert _checked(kroot)


class TestDeferredMatchesToday:
    def test_deferred_writes_record_only(self, kroot: Path) -> None:
        qid = _question(kroot)
        result = _answer(kroot, qid, apply="deferred")
        assert result.status == "deferred" and result.deferred is True
        assert result.outcome is None
        assert result.path.exists()
        assert not _checked(kroot)
        # Byte-identical to what the bare writer produces for the same input.
        other = write_decision_answer(
            kroot / "raw", decision_id=qid, decision_type="question", verdict="Series B."
        )
        strip = lambda p: [  # noqa: E731
            ln for ln in p.read_text(encoding="utf-8").splitlines() if "resolved_at" not in ln
        ]
        assert strip(result.path) == strip(other)
        # And the tick still applies it.
        assert apply_decision_answers(kroot / "wiki", kroot / "raw").applied >= 1

    def test_cli_deferred(self, kroot: Path) -> None:
        qid = _question(kroot)
        rc, out, _ = _cli_answer(kroot, qid, "question", "Series B.", "--apply", "deferred")
        payload = json.loads(out)
        assert rc == 0 and payload["apply_status"] == "deferred" and payload["deferred"] is True
        assert payload["applied"] is False
        assert not _checked(kroot)

    def test_mcp_deferred(self, kroot: Path) -> None:
        qid = _question(kroot)
        res = _mcp_fn(kroot, "resolve_question")(qid, "x", apply="deferred")
        assert res["ok"] is True and res["deferred"] is True and res["applied"] is False
        assert not _checked(kroot)


class TestOnlyTheJustWrittenFileApplies:
    def test_second_unapplied_file_stays_unapplied(self, kroot: Path) -> None:
        wiki = kroot / "wiki"
        qid = _question(kroot)
        # A concurrent session's unapplied answer, already in raw/answers/.
        other = write_decision_answer(
            kroot / "raw", decision_id=qid, decision_type="question", verdict="theirs"
        )
        mid = _write_merge(
            wiki / "_pending_merges.md",
            target="scoped-topic",
            src_a=wiki / "feedback_c.md",
            src_b=wiki / "feedback_d.md",
        )
        result = write_and_apply_decision_answer(
            kroot / "raw", wiki, decision_id=mid, decision_type="merge", verdict="approve"
        )
        assert result.status == "applied"
        assert (wiki / "scoped-topic.md").exists()
        # The other file's decision is untouched ...
        assert not _checked(kroot)
        assert other.exists() and result.path != other
        # ... and still applies on the next tick.
        assert apply_decision_answers(wiki, kroot / "raw").applied == 1
        assert _checked(kroot)

    def test_per_file_entry_point_ignores_siblings(self, kroot: Path) -> None:
        qid = _question(kroot)
        mine = write_decision_answer(
            kroot / "raw", decision_id=qid, decision_type="question", verdict="mine"
        )
        outcome = da.apply_decision_answer_file(kroot / "wiki", mine)
        assert outcome.applied is True and outcome.decision_id == qid

    def test_per_file_entry_point_refuses_unusable_files(self, kroot: Path) -> None:
        answers = kroot / "raw" / "answers"
        answers.mkdir(parents=True)
        legacy = answers / "legacy.md"
        legacy.write_text("just prose\n", encoding="utf-8")
        out = da.apply_decision_answer_file(kroot / "wiki", legacy)
        assert out.applied is False and out.error_code == "no_decision_id"
        assert legacy.exists()


class TestRunLock:
    def _coord(self, kroot: Path) -> str:
        return _seed_coordinate_item(kroot / "wiki")

    def test_needs_lock_for_coordinate_only_by_default(self, kroot: Path) -> None:
        q = da.DecisionAnswer(
            decision_id="a", decision_type="question", verdict="x", note="",
            resolved_at="", path=kroot,
        )  # fmt: skip
        c = dataclasses.replace(q, decision_type="coordinate")
        assert da.answer_needs_run_lock(c, None) is True
        assert da.answer_needs_run_lock(q, None) is False

    @pytest.mark.parametrize(
        ("verdict", "needs"),
        [("approve", True), ("approve-structure-only", True), ("reject", False)],
    )
    def test_page_recompile_lock_follows_the_verdict(
        self, kroot: Path, verdict: str, needs: bool
    ) -> None:
        a = da.DecisionAnswer(
            decision_id="p", decision_type="page-recompile", verdict=verdict,
            note="", resolved_at="", path=kroot,
        )  # fmt: skip
        assert da.answer_needs_run_lock(a, None) is needs

    def test_lock_free_coordinate_applies_and_releases(self, kroot: Path) -> None:
        cid = self._coord(kroot)
        result = write_and_apply_decision_answer(
            kroot / "raw", kroot / "wiki", decision_id=cid,
            decision_type="coordinate", verdict=_COORD_VERDICT,
        )  # fmt: skip
        assert result.status == "applied", result.message
        # The lock was released: we can take it now.
        RunLock(kroot).acquire()

    def test_lock_held_past_wait_falls_back_to_deferred(self, kroot: Path) -> None:
        cid = self._coord(kroot)
        holder = RunLock(kroot)
        holder.acquire()
        try:
            started = time.monotonic()
            result = write_and_apply_decision_answer(
                kroot / "raw", kroot / "wiki", wait=0.3, decision_id=cid,
                decision_type="coordinate", verdict=_COORD_VERDICT,
            )  # fmt: skip
            assert time.monotonic() - started >= 0.25  # it did wait
        finally:
            holder.release()
        assert result.status == "held" and result.error_code == "lock_held"
        assert result.deferred is True and "deferred" in result.message
        assert result.path.exists()  # recorded, not failed
        # The tick applies it once the lock is free (it holds the lock itself).
        tick_lock = RunLock(kroot)
        tick_lock.acquire()
        try:
            report = apply_decision_answers(kroot / "wiki", kroot / "raw", lock=tick_lock)
        finally:
            tick_lock.release()
        assert report.applied == 1

    def test_wait_is_honoured_when_holder_releases_in_time(self, kroot: Path) -> None:
        cid = self._coord(kroot)
        holder = RunLock(kroot)
        holder.acquire()
        threading.Timer(0.3, holder.release).start()
        result = write_and_apply_decision_answer(
            kroot / "raw", kroot / "wiki", wait=10, decision_id=cid,
            decision_type="coordinate", verdict=_COORD_VERDICT,
        )  # fmt: skip
        assert result.status == "applied", result.message

    def test_lockless_types_ignore_a_held_lock(self, kroot: Path) -> None:
        holder = RunLock(kroot)
        holder.acquire()
        try:
            result = _answer(kroot, _question(kroot))
        finally:
            holder.release()
        assert result.status == "applied"

    def test_cli_held_reports_and_exits_zero(self, kroot: Path) -> None:
        cid = self._coord(kroot)
        holder = RunLock(kroot)
        holder.acquire()
        try:
            rc, out, _ = _cli_answer(
                kroot, cid, "coordinate", _COORD_VERDICT, "--wait", "0.2"
            )
        finally:
            holder.release()
        payload = json.loads(out)
        assert rc == 0
        assert payload["apply_status"] == "held" and payload["deferred"] is True
        assert payload["error_code"] == "lock_held"
        assert Path(payload["answer_file"]).exists()

    def test_cli_coordinate_applies_when_lock_free(self, kroot: Path) -> None:
        cid = self._coord(kroot)
        rc, out, _ = _cli_answer(kroot, cid, "coordinate", _COORD_VERDICT)
        assert rc == 0 and json.loads(out)["apply_status"] == "applied"


class TestOutcomeReturnedSynchronously:
    def test_applied(self, kroot: Path) -> None:
        r = _answer(kroot, _question(kroot))
        assert (r.status, r.error_code) == ("applied", None)
        assert r.outcome is not None and r.outcome.applied is True

    def test_refused_carries_error_code(self, kroot: Path) -> None:
        qid = _question(kroot)
        from athenaeum.answers import resolve_by_id

        resolve_by_id(kroot / "wiki" / "_pending_questions.md", qid, "already")
        r = _answer(kroot, qid)
        assert r.status == "refused"
        assert r.error_code == "already_answered"
        assert r.outcome is not None and r.outcome.applied is False
        assert r.path.exists()  # audit trail stays

    def test_cli_refused_exits_nonzero_with_code(self, kroot: Path) -> None:
        qid = _question(kroot)
        from athenaeum.answers import resolve_by_id

        resolve_by_id(kroot / "wiki" / "_pending_questions.md", qid, "already")
        rc, out, _ = _cli_answer(kroot, qid, "question", "again")
        payload = json.loads(out)
        assert rc == 1 and payload["ok"] is False
        assert payload["apply_status"] == "refused"
        assert payload["error_code"] == "already_answered"

    def test_mcp_refused_sets_ok_false(self, kroot: Path, monkeypatch) -> None:
        qid = _question(kroot)
        fn = _mcp_fn(kroot, "resolve_question")
        from athenaeum.answers import resolve_by_id

        # Preflight passes, then the question is answered underneath us.
        real = da.apply_decision_answer_file

        def race(wiki_root, path, **kw):
            resolve_by_id(kroot / "wiki" / "_pending_questions.md", qid, "raced")
            return real(wiki_root, path, **kw)

        monkeypatch.setattr(da, "apply_decision_answer_file", race)
        res = fn(qid, "late")
        assert res["ok"] is False and res["error_code"] == "already_answered"
        assert res["apply_status"] == "refused"

    def test_held_status_covered_by_lock_tests(self) -> None:
        assert "held" in da.AnswerWriteResult.__doc__  # type: ignore[operator]


class TestNoS3Fields:
    """Counter-example: S3's answer-record fields are neither written nor
    required (held by operator ruling; this slice must not smuggle them in)."""

    _S3 = ("answer_version", "answerer", "channel", "shown_fingerprint")

    def test_record_carries_none_of_them(self, kroot: Path) -> None:
        r = _answer(kroot, _question(kroot))
        text = r.path.read_text(encoding="utf-8")
        for key in self._S3:
            assert key not in text

    def test_write_signature_and_dataclasses_have_none_of_them(self) -> None:
        names = set(inspect.signature(write_and_apply_decision_answer).parameters)
        names |= set(inspect.signature(write_decision_answer).parameters)
        names |= {f.name for f in dataclasses.fields(da.AnswerWriteResult)}
        names |= {f.name for f in dataclasses.fields(da.DecisionAnswer)}
        for key in self._S3:
            assert key not in names

    def test_cli_and_mcp_expose_none_of_them(self, kroot: Path) -> None:
        qid = _question(kroot)
        rc, out, _ = _cli_answer(kroot, qid, "question", "Series B.")
        assert rc == 0
        for key in self._S3:
            assert key not in json.loads(out)
        params = set(inspect.signature(_mcp_fn(kroot, "resolve_question")).parameters)
        for key in self._S3:
            assert key not in params

    def test_a_record_without_them_is_accepted(self, kroot: Path) -> None:
        # Legacy shape (no version/channel/...) applies fine: nothing required.
        r = _answer(kroot, _question(kroot))
        assert r.status == "applied"
