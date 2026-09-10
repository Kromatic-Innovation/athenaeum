# SPDX-License-Identifier: Apache-2.0
"""Tests for the push-precision + coverage baseline instrument (issue athenaeum#711).

Covers: push-record shape and redaction (no content, no personal data, opaque
uid never a name-derived slug), precision computation, the coverage worksheet,
the snapshot writer's idempotence, the enable/disable config accessor, and
that instrumentation ON does not change what ``recall`` returns.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from athenaeum import push_metrics
from athenaeum.config import resolve_cache_dir, resolve_push_metrics_enabled

# ---------------------------------------------------------------------------
# Cache-dir isolation (athenaeum#791 AC1) — the push-records ledger is one of
# the artifacts athenaeum#791 found the test suite writing into the
# operator's real ~/.cache/athenaeum/_push_records.jsonl (75 synthetic
# records, 62% of the live ledger at filing).
# ---------------------------------------------------------------------------


class TestPushRecordsPathIsolation:
    def test_push_records_path_no_arg_resolves_outside_real_cache_dir(self) -> None:
        """AC1: ``push_records_path()`` with NO argument — the exact no-arg
        resolution :func:`athenaeum.push_metrics.record_push`'s production
        call site uses — must resolve under a test-owned tmp dir, never the
        operator's real ``~/.cache/athenaeum``, because an autouse isolation
        mechanism in ``tests/conftest.py`` (the whole-suite ``pytest_configure``
        redirect, narrowed per-test by ``_isolate_cache_dir``) has already
        pointed ``ATHENAEUM_CACHE_DIR`` at one.
        """
        resolved = push_metrics.push_records_path()
        real_default = Path("~/.cache/athenaeum").expanduser()
        assert resolved != real_default / push_metrics.PUSH_RECORDS_FILENAME
        assert not str(resolved).startswith(str(real_default))
        assert resolved.name == push_metrics.PUSH_RECORDS_FILENAME
        # It must live under whatever ATHENAEUM_CACHE_DIR an autouse fixture
        # pointed at, and that dir must NOT be the real home cache dir.
        env_cache_dir = Path(os.environ["ATHENAEUM_CACHE_DIR"]).expanduser()
        assert env_cache_dir != real_default
        assert resolved == env_cache_dir / push_metrics.PUSH_RECORDS_FILENAME
        assert resolve_cache_dir(cache_dir=None) == env_cache_dir

    def test_reference_records_path_no_arg_resolves_outside_real_cache_dir(self) -> None:
        """Same property as above, for the sibling reference-determination
        ledger (``_push_references.jsonl``) — the other artifact
        :func:`athenaeum.push_metrics.record_reference_result` writes with no
        explicit ``cache_dir`` at its production call site.
        """
        resolved = push_metrics.reference_records_path()
        real_default = Path("~/.cache/athenaeum").expanduser()
        assert resolved != real_default / push_metrics.REFERENCE_RECORDS_FILENAME
        assert not str(resolved).startswith(str(real_default))


# ---------------------------------------------------------------------------
# Session-id resolution (issue athenaeum#734)
# ---------------------------------------------------------------------------


class TestResolveSessionId:
    """The single helper that resolves the consuming session's id. athenaeum#734
    fixed a silent no-op: every call site read ``CLAUDE_SESSION_ID``, a name
    Claude Code never sets (it exports ``CLAUDE_CODE_SESSION_ID``), so the
    ``if session_id:`` guard was always false and no push record was ever
    written.
    """

    def test_candidate_names_are_pinned_in_precedence_order(self) -> None:
        # The name list is asserted EXPLICITLY, so silently reading a name
        # nothing exports (the athenaeum#734 defect) is a failing diff, not a
        # silent no-op. CLAUDE_CODE_SESSION_ID must come first.
        assert push_metrics.SESSION_ID_ENV_VARS == (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_SESSION_ID",
        )

    def test_reads_the_real_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-code")
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert push_metrics.resolve_session_id() == "sess-code"

    def test_code_name_takes_precedence_over_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-code")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-legacy")
        assert push_metrics.resolve_session_id() == "sess-code"

    def test_falls_back_to_legacy_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-legacy")
        assert push_metrics.resolve_session_id() == "sess-legacy"

    def test_empty_string_env_is_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An exported-but-empty CLAUDE_CODE_SESSION_ID falls through to the
        # fallback rather than resolving to "".
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-legacy")
        assert push_metrics.resolve_session_id() == "sess-legacy"

    def test_neither_set_returns_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert push_metrics.resolve_session_id() == ""


# ---------------------------------------------------------------------------
# Config accessor
# ---------------------------------------------------------------------------


class TestResolvePushMetricsEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled(None) is True
        assert resolve_push_metrics_enabled({}) is True

    def test_yaml_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": False}}) is False

    def test_yaml_true_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": True}}) is True

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", "0")
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": True}}) is False

    def test_env_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "true", "yes", "on", "anything"):
            monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", val)
            assert resolve_push_metrics_enabled(None) is True

    def test_env_falsey_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("0", "false", "no", "off", "False", "OFF"):
            monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", val)
            assert resolve_push_metrics_enabled(None) is False

    def test_non_bool_yaml_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PUSH_METRICS_ENABLED", raising=False)
        assert resolve_push_metrics_enabled({"push_metrics": {"enabled": "yes"}}) is True


# ---------------------------------------------------------------------------
# opaque_push_id / redaction
# ---------------------------------------------------------------------------


class TestOpaquePushId:
    def test_uses_uid_when_present(self) -> None:
        fm = {"uid": "abc12345", "name": "Jane Doe"}
        assert push_metrics.opaque_push_id("abc12345-jane-doe.md", fm) == "abc12345"

    def test_falls_back_to_filename_when_no_uid(self) -> None:
        fm: dict[str, object] = {"name": "some note"}
        assert (
            push_metrics.opaque_push_id("20260802T023311Z-3f0ea402.md", fm)
            == "20260802T023311Z-3f0ea402.md"
        )

    def test_falls_back_when_fm_empty(self) -> None:
        assert push_metrics.opaque_push_id("raw/foo.md", {}) == "raw/foo.md"

    def test_never_returns_name_derived_slug_for_person(self) -> None:
        # The whole point: a person page's FILENAME embeds the slugified
        # name (uid-slug.md). The id recorded must be the uid, never the
        # filename, so a push record can never leak a name via its id.
        fm = {"uid": "deadbeef", "type": "person", "name": "Alice Wonderland"}
        pid = push_metrics.opaque_push_id("deadbeef-alice-wonderland.md", fm)
        assert pid == "deadbeef"
        assert "alice" not in pid.lower()
        assert "wonderland" not in pid.lower()


class TestOpaquePushIdFromFilename:
    """``opaque_push_id_from_filename`` (issue athenaeum#1362): the id-safety
    fallback for a caller with only a filename, no frontmatter — the sidecar
    push-telemetry path, which sees an FTS5 index row (no ``uid`` column),
    never the compiled page's frontmatter. Reproduces the pre-convergence
    shell hook's ``_pm_opaque_push_id`` case pattern (both branches)."""

    def test_entity_filename_truncates_to_the_8_hex_uid_prefix(self) -> None:
        # A compiled entity's on-disk filename is `<uid>-<slug>.md`
        # (`WikiEntity.filename`) — the name-derived slug must never reach
        # the ledger, even with no frontmatter to consult.
        pid = push_metrics.opaque_push_id_from_filename("deadbeef-alice-wonderland.md")
        assert pid == "deadbeef"
        assert "alice" not in pid.lower()
        assert "wonderland" not in pid.lower()

    def test_raw_intake_filename_is_recorded_whole(self) -> None:
        # Raw-intake filenames are `<timestamp>Z-<hash>.md` — the 9th
        # character is `T`, never `-`, so the uid-prefix pattern never
        # matches and the (name-free) filename is safe to record as-is.
        fname = "20260802T023311Z-3f0ea402.md"
        assert push_metrics.opaque_push_id_from_filename(fname) == fname

    def test_non_matching_filename_returned_whole(self) -> None:
        assert push_metrics.opaque_push_id_from_filename("raw/foo.md") == "raw/foo.md"

    def test_short_hex_like_prefix_without_dash_is_not_truncated(self) -> None:
        # Exactly 8 hex chars but no trailing dash: not the entity shape,
        # must not be mistaken for one.
        assert push_metrics.opaque_push_id_from_filename("deadbeef.md") == "deadbeef.md"


class TestBuildPushRecordRedaction:
    def test_no_content_or_pii_in_record(self) -> None:
        fm = {
            "uid": "abc12345",
            "type": "person",
            "access": "internal",
            "name": "Jane Doe",
            "aliases": ["J. Doe"],
        }
        record = push_metrics.build_push_record(
            session_id="sess-1",
            query="what is jane doe's phone number",
            backend="fts5",
            hits=[("abc12345-jane-doe.md", fm, "Jane Doe's phone is 555-1234")],
        )
        blob = json.dumps(record.to_dict()).lower()
        assert "jane" not in blob
        assert "doe" not in blob
        assert "555-1234" not in blob
        assert "phone" not in blob
        # But the opaque id and counts ARE present.
        assert "abc12345" in blob
        assert record.to_dict()["pushed_count"] == 1

    def test_only_query_hash_retained_never_raw_query(self) -> None:
        record = push_metrics.build_push_record(
            session_id="s",
            query="jane doe personal cell number",
            backend="keyword",
            hits=[],
        )
        blob = json.dumps(record.to_dict())
        assert "jane" not in blob.lower()
        assert "personal cell" not in blob.lower()
        assert record.to_dict()["query_hash"] != "jane doe personal cell number"

    def test_record_shape_has_required_fields(self) -> None:
        fm = {"uid": "x1", "access": "open"}
        record = push_metrics.build_push_record(
            session_id="sess-9",
            query="q",
            backend="fts5",
            hits=[("x1-thing.md", fm, "some body text here")],
        )
        d = record.to_dict()
        for key in (
            "session_id",
            "ts",
            "query_hash",
            "backend",
            "items",
            "pushed_count",
            "token_cost",
            "token_cost_estimated",
        ):
            assert key in d
        assert d["session_id"] == "sess-9"
        item = d["items"][0]
        for key in ("id", "tier", "scope", "token_cost"):
            assert key in item


class TestBuildPushRecordMemoryTier:
    """athenaeum#1345 AC7 added a per-item ``memory_tier`` (the
    retrieval-cost tier) beside the pre-existing ``tier`` field (the ACCESS
    tier), so the push mix could be watched shifting off 3.5% hot. It was
    watched (athenaeum#1560), and issue athenaeum#1514 retired the
    vocabulary — so this class now pins the field's REMOVAL from the write
    path, and the two things that removal must not break.

    ``SCHEMA_VERSION`` is deliberately NOT bumped, for the same reason the
    addition did not bump it: this is a per-item optional key, and every
    documented reader rule for the ledger is already "check for the
    specific value; an absent key means the writer did not have one."
    """

    def test_writer_emits_no_memory_tier_key(self) -> None:
        fm = {"uid": "p1", "access": "open"}
        record = push_metrics.build_push_record(
            session_id="sess-mt",
            query="q",
            backend="fts5",
            hits=[("p1-page.md", fm, "some body text")],
        )
        item = record.to_dict()["items"][0]
        assert "memory_tier" not in item
        # The unrelated ACCESS tier is untouched by the retirement.
        assert item["tier"] == "open"

    def test_build_push_record_no_longer_accepts_a_tier_map(self) -> None:
        """The ``memory_tier_by_filename`` parameter is gone, not merely
        ignored. A caller still passing it must fail loudly rather than
        silently having its map dropped — the shape of bug that would
        otherwise let a stale caller believe it was still recording tiers.
        """
        with pytest.raises(TypeError):
            push_metrics.build_push_record(
                session_id="sess-mt2",
                query="q",
                backend="fts5",
                hits=[("p2-page.md", {"uid": "p2"}, "body")],
                memory_tier_by_filename={"p2-page.md": "hot"},  # type: ignore[call-arg]
            )

    def test_pushed_item_has_no_memory_tier_field(self) -> None:
        """The dataclass field itself is gone, so a direct construction
        cannot reintroduce the key either."""
        import dataclasses

        names = {f.name for f in dataclasses.fields(push_metrics.PushedItem)}
        assert names == {"id", "tier", "scope", "token_cost"}

    def test_schema_version_is_unchanged_by_the_removal(self) -> None:
        record = push_metrics.PushRecord(
            session_id="old-shape",
            ts="2020-01-01T00:00:00Z",
            query_hash="deadbeef",
            backend="fts5",
            items=[push_metrics.PushedItem(id="x", tier="internal", scope="owner", token_cost=1)],
        )
        d = record.to_dict()
        assert d["v"] == push_metrics.SCHEMA_VERSION == 1
        assert "memory_tier" not in d["items"][0]


    def test_source_defaults_to_omitted_the_recall_path_contract(self) -> None:
        """Issue athenaeum#1362's ``source`` field: additive, no SCHEMA_VERSION
        bump, and — the load-bearing part — a record built with NO ``source``
        (every MCP ``recall``-path call site, unchanged by this issue) must
        OMIT the key entirely, never write an explicit ``"recall"`` value.
        The documented reader rule is "key absent, or any other value, means
        an explicit recall push" — a record from before this issue has no
        ``source`` key, so the default must reproduce that exactly."""
        record = push_metrics.PushRecord(
            session_id="s",
            ts="2020-01-01T00:00:00Z",
            query_hash="deadbeef",
            backend="fts5",
            items=[push_metrics.PushedItem(id="x", tier="internal", scope="owner", token_cost=1)],
        )
        d = record.to_dict()
        assert "source" not in d

    def test_source_sidecar_is_written_when_set(self) -> None:
        record = push_metrics.PushRecord(
            session_id="s",
            ts="2020-01-01T00:00:00Z",
            query_hash="deadbeef",
            backend="fts5",
            items=[push_metrics.PushedItem(id="x", tier="internal", scope="owner", token_cost=1)],
            source="sidecar",
        )
        d = record.to_dict()
        assert d["source"] == "sidecar"


# ---------------------------------------------------------------------------
# record_push / read_push_records
# ---------------------------------------------------------------------------


class TestRecordPush:
    def test_writes_and_reads_back(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="s1",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "u1"}, "body")],
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is True
        rows = push_metrics.read_push_records(cache_dir=tmp_path)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "s1"

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        ok = push_metrics.record_push(
            record, cache_dir=tmp_path, config={"push_metrics": {"enabled": False}}
        )
        assert ok is False
        assert push_metrics.read_push_records(cache_dir=tmp_path) == []

    def test_noop_when_no_session_id(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is False

    def test_no_raw_query_text_reaches_the_ledger_file_on_default_path(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#1036 AC4: the push-record ledger schema is unchanged —
        this asserts it directly against the FILE ``record_push`` writes on
        the default (instrumentation-enabled, no special config) path, not
        just the in-memory ``PushRecord.to_dict()`` shape.
        """
        record = push_metrics.build_push_record(
            session_id="s1",
            query="jane doe personal cell number, project moonshot budget",
            backend="fts5",
            hits=[("f.md", {"uid": "u1"}, "body text nobody should see")],
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is True

        ledger_path = push_metrics.push_records_path(tmp_path)
        raw_text = ledger_path.read_text(encoding="utf-8")

        assert "jane" not in raw_text.lower()
        assert "personal cell" not in raw_text.lower()
        assert "moonshot" not in raw_text.lower()
        assert "body text" not in raw_text.lower()
        # The hash IS present — that's the sanctioned correlation signal.
        assert record.query_hash in raw_text

    def test_noop_when_no_items(self, tmp_path: Path) -> None:
        record = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[]
        )
        assert push_metrics.record_push(record, cache_dir=tmp_path) is False

    def test_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        path = push_metrics.push_records_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        ).to_dict()
        path.write_text(json.dumps(good) + "\n" + '{"broken json')
        rows = push_metrics.read_push_records(cache_dir=tmp_path)
        assert len(rows) == 1


class TestRecordHookPush:
    """Tests for issue athenaeum#1478 — the hook-path push-recording entry
    point. Each acceptance criterion gets its own test, with the
    counter-example the issue names as a comment on the test it defeats.

    AC: "A recording entry point exists that an external caller can invoke
    with a session id and a list of injected ids, writing a push record
    indistinguishable in shape from one `record_push` writes today."
    """

    def test_writes_a_push_record(self, tmp_path: Path) -> None:
        """Counter-example that must fail: a silent no-op — precisely what
        athenaeum#734 caught once when `CLAUDE_SESSION_ID` was read but
        never set, and zero push records were ever written."""
        wrote = push_metrics.record_hook_push(
            "sess-1", ["abc12345", "def67890"], backend="fts5", cache_dir=tmp_path
        )
        assert wrote is True
        rows = push_metrics.read_push_records(cache_dir=tmp_path)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-1"
        assert rows[0]["pushed_count"] == 2
        assert [it["id"] for it in rows[0]["items"]] == ["abc12345", "def67890"]

    def test_record_shape_matches_an_ordinary_recall_row(self, tmp_path: Path) -> None:
        """Counter-example that must fail: two independent serializers
        drifting apart (the same hazard `test_sidecar_and_mcp_records_share_key_structure`
        in `tests/test_context_push_telemetry.py` guards for the sidecar
        path). Compares the hook-path row's key set — top level and per
        item — against an ordinary MCP `recall` row's."""
        push_metrics.record_hook_push("sess-1", ["abc12345"], cache_dir=tmp_path)
        hook_row = push_metrics.read_push_records(cache_dir=tmp_path)[0]

        recall_record = push_metrics.build_push_record(
            session_id="sess-1",
            query="q",
            backend="fts5",
            hits=[("abc12345-page.md", {"uid": "abc12345"}, "body")],
        )
        recall_dict = recall_record.to_dict()

        # The ONE intentional discriminator: a hook-path row carries
        # `source`, an ordinary recall row omits it entirely.
        assert hook_row.keys() - recall_dict.keys() == {"source"}
        assert recall_dict.keys() - hook_row.keys() == set()
        assert hook_row["items"][0].keys() == recall_dict["items"][0].keys()

    def test_opaque_push_id_from_filename_applied_to_each_id(self, tmp_path: Path) -> None:
        """A caller handing through a raw compiled-entity filename (rather
        than an already-opaque id) must never leak its name-derived slug."""
        push_metrics.record_hook_push(
            "sess-1", ["abc12345-jane-doe.md", "20260802T023311Z-3f0ea402.md"], cache_dir=tmp_path
        )
        row = push_metrics.read_push_records(cache_dir=tmp_path)[0]
        ids = [it["id"] for it in row["items"]]
        assert ids == ["abc12345", "20260802T023311Z-3f0ea402.md"]
        assert "jane-doe" not in json.dumps(row)

    def test_blank_ids_are_dropped(self, tmp_path: Path) -> None:
        push_metrics.record_hook_push("sess-1", ["abc12345", "  ", ""], cache_dir=tmp_path)
        row = push_metrics.read_push_records(cache_dir=tmp_path)[0]
        assert [it["id"] for it in row["items"]] == ["abc12345"]

    def test_only_query_hash_retained_never_raw_query(self, tmp_path: Path) -> None:
        push_metrics.record_hook_push(
            "sess-1", ["abc12345"], query="jane doe's personal cell number", cache_dir=tmp_path
        )
        raw_text = push_metrics.push_records_path(tmp_path).read_text(encoding="utf-8")
        assert "jane" not in raw_text.lower()
        assert "cell number" not in raw_text.lower()

    # -------------------------------------------------------------------
    # AC: "Records carry which path produced them, so hook-path and
    # explicit-`recall` pushes can be told apart in `usage-report` and in
    # athenaeum#1479's tail."
    # -------------------------------------------------------------------

    def test_source_is_the_named_hook_constant(self, tmp_path: Path) -> None:
        push_metrics.record_hook_push("sess-1", ["abc12345"], cache_dir=tmp_path)
        row = push_metrics.read_push_records(cache_dir=tmp_path)[0]
        assert row["source"] == push_metrics.SOURCE_HOOK
        assert push_metrics.SOURCE_HOOK == "hook"
        # Distinct from the sidecar's own source tag and from an ordinary
        # recall row's omitted key (see the reader-rule tests below).
        assert push_metrics.SOURCE_HOOK != "sidecar"

    def test_source_hook_is_distinguishable_from_sidecar_and_recall(self, tmp_path: Path) -> None:
        """Counter-example that must fail: a reader treating 'anything but
        sidecar' as an explicit recall push — a hook-path row is a THIRD
        case, not `recall` by elimination."""
        push_metrics.record_hook_push("hook-sess", ["a"], cache_dir=tmp_path)
        recall_record = push_metrics.build_push_record(
            session_id="recall-sess", query="q", backend="fts5", hits=[("b.md", {"uid": "b"}, "x")]
        )
        push_metrics.record_push(recall_record, cache_dir=tmp_path)
        sidecar_record = push_metrics.PushRecord(
            session_id="sidecar-sess",
            ts="2026-01-01T00:00:00Z",
            query_hash="deadbeef00000000",
            backend="fts5",
            items=[push_metrics.PushedItem(id="c", tier="internal", scope="owner", token_cost=1)],
            source="sidecar",
        )
        push_metrics.record_push(sidecar_record, cache_dir=tmp_path)

        rows = push_metrics.read_push_records(cache_dir=tmp_path)
        by_session = {r["session_id"]: r.get("source") for r in rows}
        assert by_session == {"hook-sess": "hook", "recall-sess": None, "sidecar-sess": "sidecar"}

    # -------------------------------------------------------------------
    # AC: "Invoking it is safe when the ledger is unwritable: it fails
    # quietly and never raises into the caller."
    # -------------------------------------------------------------------

    def test_survives_an_unwritable_ledger_path(self, tmp_path: Path) -> None:
        """Counter-example that must fail: an unwritable ledger path taking
        down the caller. The ledger path is replaced with a DIRECTORY
        (deterministic across users/containers) so the append write raises
        `IsADirectoryError` — this must still return `False`, never raise."""
        push_metrics.push_records_path(tmp_path).mkdir(parents=True)
        result = push_metrics.record_hook_push("sess-1", ["abc12345"], cache_dir=tmp_path)
        assert result is False
        assert push_metrics.push_records_path(tmp_path).is_dir()

    def test_survives_a_malformed_ids_argument(self, tmp_path: Path) -> None:
        """A caller passing something not actually iterable-of-strings must
        not raise into the hook it instruments — it degrades to a no-op."""
        result = push_metrics.record_hook_push("sess-1", None, cache_dir=tmp_path)  # type: ignore[arg-type]
        assert result is False
        assert push_metrics.read_push_records(cache_dir=tmp_path) == []

    # -------------------------------------------------------------------
    # No-op cases: never a masked failure, never a fabricated row.
    # -------------------------------------------------------------------

    def test_noop_when_no_session_id(self, tmp_path: Path) -> None:
        assert push_metrics.record_hook_push("", ["abc12345"], cache_dir=tmp_path) is False
        assert push_metrics.read_push_records(cache_dir=tmp_path) == []

    def test_noop_when_no_ids(self, tmp_path: Path) -> None:
        assert push_metrics.record_hook_push("sess-1", [], cache_dir=tmp_path) is False
        assert push_metrics.read_push_records(cache_dir=tmp_path) == []

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        result = push_metrics.record_hook_push(
            "sess-1", ["abc12345"], cache_dir=tmp_path, config={"push_metrics": {"enabled": False}}
        )
        assert result is False
        assert push_metrics.read_push_records(cache_dir=tmp_path) == []

    # -------------------------------------------------------------------
    # Known limitation: ids only, so tier/scope/token_cost are safe
    # defaults, never a fabricated real value.
    # -------------------------------------------------------------------

    def test_known_limitation_defaults_are_safe_not_fabricated(self, tmp_path: Path) -> None:
        push_metrics.record_hook_push("sess-1", ["abc12345"], cache_dir=tmp_path)
        item = push_metrics.read_push_records(cache_dir=tmp_path)[0]["items"][0]
        assert item["tier"] == "internal"
        assert item["scope"] == "owner"
        assert item["token_cost"] == 0
        # No `memory_tier` at all since issue athenaeum#1514 — the safest
        # "known limitation" default for a retired field is not to write it.
        assert "memory_tier" not in item


# ---------------------------------------------------------------------------
# Precision computation
# ---------------------------------------------------------------------------


class TestReferenceResultPrecision:
    def test_full_precision(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=["a", "b"], referenced_ids=["a", "b"]
        )
        assert result.precision == 1.0

    def test_partial_precision(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=["a", "b", "c", "d"], referenced_ids=["a"]
        )
        assert result.precision == 0.25

    def test_zero_precision(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=["a", "b"], referenced_ids=[]
        )
        assert result.precision == 0.0

    def test_none_when_nothing_pushed(self) -> None:
        result = push_metrics.ReferenceResult(
            session_id="s", ts="t", pushed_ids=[], referenced_ids=[]
        )
        assert result.precision is None


class TestDetermineReferences:
    def test_referenced_id_found_in_assistant_text(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-a",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "abc12345"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-a.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Used abc12345 here."}],
                        },
                    },
                ]
            )
            + "\n"
        )

        result = push_metrics.determine_references(
            "sess-a", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        assert result.referenced_ids == ["abc12345"]
        assert result.precision == 1.0

    def test_matches_tool_result_text_not_just_user_text(self, tmp_path: Path) -> None:
        # Reference-determination must catch ids referenced via tool output —
        # NOT just user-authored text (that is what distinguishes it from
        # transcript_verify.verify_user_stated, which only matches user text).
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-b",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "toolid99"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-b.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "grep found toolid99 in results",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )

        result = push_metrics.determine_references(
            "sess-b", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        assert result.referenced_ids == ["toolid99"]

    def test_unreferenced_id_not_marked(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-c",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "neverused"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope-hash"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-c.jsonl").write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "unrelated"}})
            + "\n"
        )

        result = push_metrics.determine_references(
            "sess-c", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        assert result.referenced_ids == []
        assert result.precision == 0.0

    def test_none_when_no_push_records_for_session(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        result = push_metrics.determine_references(
            "nonexistent", cache_dir=cache, projects_root=tmp_path / "projects"
        )
        assert result is None

    def test_none_when_transcript_missing(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-d",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "x"}, "body")],
        )
        push_metrics.record_push(record, cache_dir=cache)
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        result = push_metrics.determine_references(
            "sess-d", cache_dir=cache, projects_root=projects_root
        )
        assert result is None


class TestRunReferenceDetermination:
    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-e", query="q", backend="fts5", hits=[("f.md", {"uid": "x"}, "b")]
        )
        push_metrics.record_push(record, cache_dir=cache)
        result = push_metrics.run_reference_determination(
            "sess-e",
            cache_dir=cache,
            config={"push_metrics": {"enabled": False}},
        )
        assert result is None
        assert push_metrics.reference_records_path(cache).is_file() is False

    def test_writes_reference_record_when_enabled(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="sess-f", query="q", backend="fts5", hits=[("f.md", {"uid": "refid"}, "b")]
        )
        push_metrics.record_push(record, cache_dir=cache)

        projects_root = tmp_path / "projects"
        scope_dir = projects_root / "-scope"
        scope_dir.mkdir(parents=True)
        (scope_dir / "sess-f.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"content": "refid used"}}) + "\n"
        )

        result = push_metrics.run_reference_determination(
            "sess-f", cache_dir=cache, projects_root=projects_root
        )
        assert result is not None
        rows = push_metrics._read_jsonl(push_metrics.reference_records_path(cache))
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-f"


# ---------------------------------------------------------------------------
# compute_baseline
# ---------------------------------------------------------------------------


class TestComputeBaseline:
    def test_empty_ledger_is_honest_not_fabricated(self, tmp_path: Path) -> None:
        baseline = push_metrics.compute_baseline(cache_dir=tmp_path, repo_root=Path("."))
        assert baseline.session_count == 0
        assert baseline.push_record_count == 0
        assert baseline.reference_record_count == 0
        assert baseline.precision is None  # never fabricated as 0.0

    def test_aggregates_reference_records(self, tmp_path: Path) -> None:
        cache = tmp_path
        r1 = push_metrics.ReferenceResult(
            session_id="s1", ts="2026-01-01T00:00:00Z", pushed_ids=["a", "b"], referenced_ids=["a"]
        )
        r2 = push_metrics.ReferenceResult(
            session_id="s2", ts="2026-01-01T00:00:00Z", pushed_ids=["c"], referenced_ids=["c"]
        )
        push_metrics.record_reference_result(r1, cache_dir=cache)
        push_metrics.record_reference_result(r2, cache_dir=cache)
        push_rec = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "a"}, "b")]
        )
        push_metrics.record_push(push_rec, cache_dir=cache)

        baseline = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        # 3 total pushed (a,b,c), 2 referenced (a,c) -> 2/3
        assert baseline.precision == pytest.approx(2 / 3)
        assert baseline.reference_record_count == 2

    def test_since_window_excludes_old_records(self, tmp_path: Path) -> None:
        from datetime import datetime, timedelta, timezone

        cache = tmp_path
        old = push_metrics.ReferenceResult(
            session_id="old",
            ts="2020-01-01T00:00:00Z",
            pushed_ids=["x"],
            referenced_ids=[],
        )
        push_metrics.record_reference_result(old, cache_dir=cache)
        baseline = push_metrics.compute_baseline(
            since=datetime.now(tz=timezone.utc) - timedelta(days=1),
            cache_dir=cache,
            repo_root=Path("."),
        )
        assert baseline.reference_record_count == 0


class TestComputeBaselineExcludeSessions:
    """athenaeum#791 AC3: a ledger containing a synthetic session must either be
    excludable, or its contamination must be reported as a distinct field —
    never silently folded into a clean-looking total.

    ``synth`` below mirrors the athenaeum#791 evidence shape: one session
    whose push items are fixture-style filenames, mixed into a ledger that
    also has genuinely clean sessions.
    """

    @staticmethod
    def _seed_ledger(cache: Path) -> None:
        clean_push = push_metrics.build_push_record(
            session_id="clean-session",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "a1b2c3d4"}, "clean body")],
        )
        push_metrics.record_push(clean_push, cache_dir=cache)
        clean_ref = push_metrics.ReferenceResult(
            session_id="clean-session",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["a1b2c3d4"],
            referenced_ids=["a1b2c3d4"],
        )
        push_metrics.record_reference_result(clean_ref, cache_dir=cache)

        synth_push = push_metrics.build_push_record(
            session_id="synth-session",
            query="q",
            backend="fts5",
            hits=[("test-page.md", None, "fixture body")],
        )
        push_metrics.record_push(synth_push, cache_dir=cache)
        synth_ref = push_metrics.ReferenceResult(
            session_id="synth-session",
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["test-page.md"],
            referenced_ids=[],
        )
        push_metrics.record_reference_result(synth_ref, cache_dir=cache)

    def test_default_reports_the_contaminated_total_not_silently(self, tmp_path: Path) -> None:
        """No exclusion requested: the contamination is still visible — the
        session/record counts include the synthetic session (nothing is
        silently dropped by default), and the exclusion fields are the
        honest zero/empty, not fabricated.
        """
        cache = tmp_path
        self._seed_ledger(cache)
        baseline = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        assert baseline.session_count == 2
        assert baseline.push_record_count == 2
        assert baseline.reference_record_count == 2
        assert baseline.excluded_sessions == ()
        assert baseline.excluded_push_record_count == 0
        assert baseline.excluded_reference_record_count == 0

    def test_exclude_sessions_drops_the_synthetic_session(self, tmp_path: Path) -> None:
        cache = tmp_path
        self._seed_ledger(cache)
        baseline = push_metrics.compute_baseline(
            cache_dir=cache, repo_root=Path("."), exclude_sessions=["synth-session"]
        )
        assert baseline.session_count == 1
        assert baseline.push_record_count == 1
        assert baseline.reference_record_count == 1
        # 1 pushed (a1b2c3d4), 1 referenced -> precision 1.0, no longer diluted
        # by the synthetic session's 0/1.
        assert baseline.precision == pytest.approx(1.0)
        assert baseline.excluded_sessions == ("synth-session",)
        assert baseline.excluded_push_record_count == 1
        assert baseline.excluded_reference_record_count == 1

    def test_excluding_an_unknown_session_id_raises(self, tmp_path: Path) -> None:
        """athenaeum#987: a value that matches no known session id anywhere
        in the ledger must be a hard error, never a silent zero-effect
        success — the exact defect that let a session-id PREFIX exclude
        nothing while the run still reported success.
        """
        cache = tmp_path
        self._seed_ledger(cache)
        with pytest.raises(ValueError, match="matches no known session id"):
            push_metrics.compute_baseline(
                cache_dir=cache, repo_root=Path("."), exclude_sessions=["never-ran"]
            )

    def test_excluding_an_unambiguous_prefix_drops_the_session(self, tmp_path: Path) -> None:
        """athenaeum#987: an unambiguous prefix of a known session id is
        accepted and resolved to the full id, same effect as passing the
        full id.
        """
        cache = tmp_path
        self._seed_ledger(cache)
        baseline = push_metrics.compute_baseline(
            cache_dir=cache, repo_root=Path("."), exclude_sessions=["synth-sess"]
        )
        assert baseline.session_count == 1
        assert baseline.excluded_sessions == ("synth-session",)
        assert baseline.excluded_push_record_count == 1

    def test_excluding_an_ambiguous_prefix_raises(self, tmp_path: Path) -> None:
        """athenaeum#987: a prefix shared by more than one known session id
        must not silently resolve to either — it's a hard error.
        """
        cache = tmp_path
        self._seed_ledger(cache)
        for extra_sid, extra_uid in (("synth-session-2", "z8"), ("synth-session-3", "z9")):
            extra_push = push_metrics.build_push_record(
                session_id=extra_sid,
                query="q",
                backend="fts5",
                hits=[("f2.md", {"uid": extra_uid}, "b")],
            )
            push_metrics.record_push(extra_push, cache_dir=cache)
        with pytest.raises(ValueError, match="ambiguous"):
            push_metrics.compute_baseline(
                cache_dir=cache, repo_root=Path("."), exclude_sessions=["synth-session-"]
            )


# ---------------------------------------------------------------------------
# Snapshot writer idempotence
# ---------------------------------------------------------------------------


class TestWriteSnapshot:
    @staticmethod
    def _seed_valid_baseline(cache: Path, *, session_id: str = "s1") -> None:
        """Seed one push + one fully-referenced record so ``compute_baseline()``
        over *cache* is valid (``reference_record_count > 0``).

        ``write_snapshot`` refuses on an empty/invalid baseline (issue
        athenaeum#795), so the idempotence/append tests below — which are
        about the WRITE MECHANICS, not about the empty-ledger case — need a
        baseline that is actually writable.
        """
        push = push_metrics.build_push_record(
            session_id=session_id,
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "u1"}, "b")],
        )
        push_metrics.record_push(push, cache_dir=cache)
        ref = push_metrics.ReferenceResult(
            session_id=session_id,
            ts="2026-01-01T00:00:00Z",
            pushed_ids=["u1"],
            referenced_ids=["u1"],
        )
        push_metrics.record_reference_result(ref, cache_dir=cache)

    def test_creates_new_file(self, tmp_path: Path) -> None:
        """athenaeum#795: a baseline with zero reference records is REFUSED,
        not written as a placeholder — the athenaeum#711 incident this issue
        fixes. Nothing is written; the docs path never comes into existence.
        """
        baseline = push_metrics.compute_baseline(
            cache_dir=tmp_path / "cache", repo_root=Path(".")
        )
        assert baseline.reference_record_count == 0
        docs_path = tmp_path / "docs" / "measurements" / "memory-model-measurements.md"
        with pytest.raises(ValueError, match="reference_records"):
            push_metrics.write_snapshot(baseline, docs_path=docs_path)
        assert not docs_path.exists()

    def test_rerun_appends_dated_snapshot_never_corrupts(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        docs_path = tmp_path / "docs.md"
        self._seed_valid_baseline(cache)
        b1 = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        push_metrics.write_snapshot(b1, docs_path=docs_path)
        b2 = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        push_metrics.write_snapshot(b2, docs_path=docs_path)

        content = docs_path.read_text()
        assert content.count("## Push-precision and coverage baseline") == 1
        assert content.count("### Snapshot") == 2
        # File stays parseable markdown (no truncation mid-section).
        assert f"git_sha: {b2.git_sha}" in content
        assert f"git_sha: {b1.git_sha}" in content

    def test_idempotent_many_runs(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        docs_path = tmp_path / "docs.md"
        self._seed_valid_baseline(cache)
        for _ in range(5):
            b = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
            push_metrics.write_snapshot(b, docs_path=docs_path)
        content = docs_path.read_text()
        assert content.count("## Push-precision and coverage baseline") == 1
        assert content.count("### Snapshot") == 5

    def test_appends_section_to_existing_unrelated_file(self, tmp_path: Path) -> None:
        docs_path = tmp_path / "docs.md"
        docs_path.write_text("# Some other doc\n\nUnrelated content.\n")
        cache = tmp_path / "cache"
        self._seed_valid_baseline(cache)
        baseline = push_metrics.compute_baseline(cache_dir=cache, repo_root=Path("."))
        push_metrics.write_snapshot(baseline, docs_path=docs_path)
        content = docs_path.read_text()
        assert "Unrelated content." in content
        assert "## Push-precision and coverage baseline" in content


# ---------------------------------------------------------------------------
# Coverage worksheet
# ---------------------------------------------------------------------------


class TestCoverageWorksheet:
    def test_samples_up_to_n_sessions(self, tmp_path: Path) -> None:
        cache = tmp_path
        for i in range(5):
            rec = push_metrics.build_push_record(
                session_id=f"s{i}",
                query="q",
                backend="fts5",
                hits=[("f.md", {"uid": f"u{i}"}, "b")],
            )
            push_metrics.record_push(rec, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=3, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        assert ws["sampled_session_count"] == 3
        assert len(ws["sessions"]) == 3

    def test_candidates_exclude_own_pushed_set(self, tmp_path: Path) -> None:
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "pushed1"}, "b")]
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2", query="q", backend="fts5", hits=[("f.md", {"uid": "other2"}, "b")]
        )
        push_metrics.record_push(rec1, cache_dir=cache)
        push_metrics.record_push(rec2, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        s1 = next(s for s in ws["sessions"] if s["session_id"] == "s1")
        assert "pushed1" in s1["pushed"]
        assert "pushed1" not in s1["candidates_not_pushed"]
        assert "other2" in s1["candidates_not_pushed"]

    def test_no_marking_column_and_structural_fields_populated(self, tmp_path: Path) -> None:
        """athenaeum#1036 AC1/AC2/AC5: no per-candidate relevance-marking
        column and no ``coverage_miss_rate`` presented as a measurement;
        instead the structural facts (candidate-pool size, tier/scope
        concentration, window-mate filter removal, policy-set bounds) are
        populated, on a synthetic ledger.
        """
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "a"}, "b")]
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2", query="q", backend="fts5", hits=[("f.md", {"uid": "b"}, "b")]
        )
        push_metrics.record_push(rec1, cache_dir=cache)
        push_metrics.record_push(rec2, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )

        # No marking column, no coverage_miss_rate figure anywhere.
        assert "reviewer_verdict" not in json.dumps(ws)
        assert "coverage_miss_rate" not in json.dumps(ws)
        assert "miss_rate_formula" not in ws

        # Opens with a plain-language limitation statement (AC3).
        assert "limitation" in ws
        assert "query" in ws["limitation"].lower()
        assert "athenaeum#711" in ws["limitation"]

        # Structural facts populated (AC2).
        summary = ws["structural_summary"]
        concentration = summary["tier_scope_concentration"]
        assert concentration["total_pushed_items"] == 2
        assert concentration["share_of_pushed_items"] == pytest.approx(1.0)

        window_filter = summary["window_mate_filter"]
        assert window_filter["before_filter_id_count"] == 2
        assert window_filter["after_filter_id_count"] == 2
        assert window_filter["removed_fraction"] == pytest.approx(0.0)

        bounds = summary["policy_set_miss_rate_bounds"]
        assert bounds["lower_bound"] == 0.0
        assert bounds["lower_bound_label"]
        assert bounds["upper_bound"] is not None
        assert bounds["upper_bound_label"]
        assert "policy-set" in bounds["note"]

        for session in ws["sessions"]:
            assert "reviewer_verdict" not in session
            assert "candidate_pool_size" in session
            assert "window_mate_pool_before_filter" in session
            assert "filter_removed_fraction" in session

    def test_degenerate_tier_scope_distribution_is_flagged(self, tmp_path: Path) -> None:
        """athenaeum#1036 AC5: a ledger where every sampled session's pushed
        items share exactly one tier/scope pairing is reported as
        degenerate, not silently averaged into a filter-removal figure that
        would look like ordinary filter behaviour.
        """
        cache = tmp_path
        for i, sid in enumerate(["s1", "s2", "s3"]):
            rec = push_metrics.build_push_record(
                session_id=sid,
                query="q",
                backend="fts5",
                hits=[(f"f{i}.md", {"uid": f"u{i}", "access": "internal"}, "b")],
            )
            push_metrics.record_push(rec, cache_dir=cache)

        ws = push_metrics.build_coverage_worksheet(
            n=3, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        concentration = ws["structural_summary"]["tier_scope_concentration"]
        assert concentration["degenerate"] is True
        assert concentration["distinct_pairing_count"] == 1
        assert concentration["degenerate_note"] is not None
        assert "degenerate" in concentration["degenerate_note"]

    def test_non_degenerate_distribution_is_not_flagged(self, tmp_path: Path) -> None:
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "secret1", "access": "secret"}, "b")],
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "internal2", "access": "internal"}, "b")],
        )
        push_metrics.record_push(rec1, cache_dir=cache)
        push_metrics.record_push(rec2, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        concentration = ws["structural_summary"]["tier_scope_concentration"]
        assert concentration["degenerate"] is False
        assert concentration["degenerate_note"] is None
        assert concentration["distinct_pairing_count"] == 2

    def test_write_coverage_worksheet_is_a_file_not_console_only(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        rec = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "a"}, "b")]
        )
        push_metrics.record_push(rec, cache_dir=cache)
        ws = push_metrics.build_coverage_worksheet(
            n=1, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        out = tmp_path / "worksheet.json"
        push_metrics.write_coverage_worksheet(ws, output_path=out)
        assert out.is_file()
        loaded = json.loads(out.read_text())
        assert loaded["sampled_session_count"] == 1

    def test_candidates_restricted_to_sample_window(self, tmp_path: Path) -> None:
        """athenaeum#986 AC1: a session NOT drawn into the sample must never
        contribute a candidate — even though it shares the same default
        tier/scope pairing as every other session here. Pre-fix, candidates
        were drawn from ``all_pushed_ids`` (the WHOLE ledger), so this test
        fails against the old implementation.
        """
        cache = tmp_path
        ids: dict[str, str] = {}
        for i, sid in enumerate(["s1", "s2", "s3"]):
            pid = f"id{i}"
            rec = push_metrics.build_push_record(
                session_id=sid, query="q", backend="fts5", hits=[("f.md", {"uid": pid}, "b")]
            )
            push_metrics.record_push(rec, cache_dir=cache)
            ids[sid] = pid

        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        sampled_sids = {s["session_id"] for s in ws["sessions"]}
        assert len(sampled_sids) == 2
        excluded_sid = next(iter(set(ids) - sampled_sids))
        excluded_pid = ids[excluded_sid]

        for session in ws["sessions"]:
            assert excluded_pid not in session["candidates_not_pushed"]

    def test_candidates_require_matching_tier_scope_pairing(self, tmp_path: Path) -> None:
        """athenaeum#986 AC1: a same-window id with a DIFFERENT tier/scope
        pairing than the reviewing session's own pushed set is excluded; a
        same-window id with a MATCHING pairing is included.
        """
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "secret1", "access": "secret"}, "b")],
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "internal2", "access": "internal"}, "b")],
        )
        rec3 = push_metrics.build_push_record(
            session_id="s3",
            query="q",
            backend="fts5",
            hits=[("f.md", {"uid": "secret3", "access": "secret"}, "b")],
        )
        for rec in (rec1, rec2, rec3):
            push_metrics.record_push(rec, cache_dir=cache)

        ws = push_metrics.build_coverage_worksheet(
            n=3, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        s1 = next(s for s in ws["sessions"] if s["session_id"] == "s1")
        assert "secret3" in s1["candidates_not_pushed"]
        assert "internal2" not in s1["candidates_not_pushed"]

    def test_bounded_candidate_count_vs_whole_corpus(self, tmp_path: Path) -> None:
        """athenaeum#986 AC4 (live-shaped fixture): many sessions, overlapping
        pushes. Per-session candidate count must stay bounded by the sample
        window (the other ``n - 1`` sampled sessions), well below the full
        corpus size — the exact defect the 2026-08-20 live audit found
        (candidates == whole pushed-id corpus, 503 ids / 4,926 verdict slots).
        """
        cache = tmp_path
        n_sessions = 60
        items_per_session = 5
        for i in range(n_sessions):
            hits = [(f"f{i}_{j}.md", {"uid": f"u{i}_{j}"}, "b") for j in range(items_per_session)]
            rec = push_metrics.build_push_record(
                session_id=f"sess{i}", query="q", backend="fts5", hits=hits
            )
            push_metrics.record_push(rec, cache_dir=cache)
        corpus_size = n_sessions * items_per_session

        ws = push_metrics.build_coverage_worksheet(
            n=10, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        assert ws["sampled_session_count"] == 10
        for session in ws["sessions"]:
            # Bounded by the OTHER 9 sampled sessions' items, never the
            # whole 300-item corpus.
            assert session["candidate_pool_size"] <= 9 * items_per_session
            assert session["candidate_pool_size"] < corpus_size

    def test_policy_set_bounds_reproducible_from_worksheet_fields(self, tmp_path: Path) -> None:
        """athenaeum#1036 AC2: the policy-set miss-rate bounds are
        reproducible from the worksheet's own ``pushed_count`` /
        ``candidate_pool_size`` fields, and both endpoints are named labels,
        not just bare numbers.
        """
        cache = tmp_path
        rec1 = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "p1"}, "b")]
        )
        rec2 = push_metrics.build_push_record(
            session_id="s2", query="q", backend="fts5", hits=[("f.md", {"uid": "p2"}, "b")]
        )
        push_metrics.record_push(rec1, cache_dir=cache)
        push_metrics.record_push(rec2, cache_dir=cache)

        ws = push_metrics.build_coverage_worksheet(
            n=2, wiki_root=tmp_path / "wiki", cache_dir=cache, seed=1
        )
        s1 = next(s for s in ws["sessions"] if s["session_id"] == "s1")
        assert s1["pushed_count"] == len(s1["pushed"]) == 1
        assert s1["candidate_pool_size"] == len(s1["candidates_not_pushed"]) == 1

        bounds = ws["structural_summary"]["policy_set_miss_rate_bounds"]
        # Aggregate upper bound = total candidates / (total pushed + total candidates).
        pushed_total = sum(s["pushed_count"] for s in ws["sessions"])
        candidate_total = sum(s["candidate_pool_size"] for s in ws["sessions"])
        expected_upper = candidate_total / (pushed_total + candidate_total)
        assert bounds["upper_bound"] == pytest.approx(expected_upper)
        assert bounds["lower_bound"] == 0.0
        assert "relevant-missed" in bounds["upper_bound_label"]
        assert "relevant-missed" in bounds["lower_bound_label"]

    def test_exclude_session_drops_from_sample_and_candidates(self, tmp_path: Path) -> None:
        """athenaeum#986 AC2: ``--exclude-session`` semantics at the function
        level — a known-synthetic session is dropped from the sampling pool
        entirely AND cannot contribute candidates to other sessions, with the
        exclusion always reported (never silently dropped).
        """
        cache = tmp_path
        clean = push_metrics.build_push_record(
            session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        synth = push_metrics.build_push_record(
            session_id="synth",
            query="q",
            backend="fts5",
            hits=[("test-page.md", None, "b")],
        )
        push_metrics.record_push(clean, cache_dir=cache)
        push_metrics.record_push(synth, cache_dir=cache)

        ws = push_metrics.build_coverage_worksheet(
            n=5,
            wiki_root=tmp_path / "wiki",
            cache_dir=cache,
            seed=1,
            exclude_sessions=["synth"],
        )
        assert ws["sampled_session_count"] == 1
        assert ws["sessions"][0]["session_id"] == "clean"
        assert ws["excluded_sessions"] == ["synth"]
        assert ws["excluded_push_records"] == 1

    def test_excluding_an_unknown_session_id_raises(self, tmp_path: Path) -> None:
        """athenaeum#987: a value that matches no known session id in the
        push-records ledger must be a hard error, never a silent no-op.
        """
        cache = tmp_path
        rec = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        push_metrics.record_push(rec, cache_dir=cache)

        with pytest.raises(ValueError, match="matches no known session id"):
            push_metrics.build_coverage_worksheet(
                n=5,
                wiki_root=tmp_path / "wiki",
                cache_dir=cache,
                seed=1,
                exclude_sessions=["never-ran"],
            )

    def test_excluding_an_unambiguous_prefix_drops_from_sample(self, tmp_path: Path) -> None:
        """athenaeum#987: an unambiguous session-id prefix is accepted and
        resolved to the matching full id, same effect as the full id.
        """
        cache = tmp_path
        clean = push_metrics.build_push_record(
            session_id="clean", query="q", backend="fts5", hits=[("f.md", {"uid": "u1"}, "b")]
        )
        synth = push_metrics.build_push_record(
            session_id="d5774338-7d8b-4152-a252-248d156f95ef",
            query="q",
            backend="fts5",
            hits=[("test-page.md", None, "b")],
        )
        push_metrics.record_push(clean, cache_dir=cache)
        push_metrics.record_push(synth, cache_dir=cache)

        ws = push_metrics.build_coverage_worksheet(
            n=5,
            wiki_root=tmp_path / "wiki",
            cache_dir=cache,
            seed=1,
            exclude_sessions=["d5774338-7d8b"],
        )
        assert ws["sampled_session_count"] == 1
        assert ws["sessions"][0]["session_id"] == "clean"
        assert ws["excluded_sessions"] == ["d5774338-7d8b-4152-a252-248d156f95ef"]
        assert ws["excluded_push_records"] == 1

    def test_excluding_an_ambiguous_prefix_raises(self, tmp_path: Path) -> None:
        """athenaeum#987: a prefix shared by more than one known session id
        must not silently resolve to either — it's a hard error.
        """
        cache = tmp_path
        for sid, uid in (("synth-a", "u1"), ("synth-b", "u2")):
            rec = push_metrics.build_push_record(
                session_id=sid, query="q", backend="fts5", hits=[("f.md", {"uid": uid}, "b")]
            )
            push_metrics.record_push(rec, cache_dir=cache)

        with pytest.raises(ValueError, match="ambiguous"):
            push_metrics.build_coverage_worksheet(
                n=5,
                wiki_root=tmp_path / "wiki",
                cache_dir=cache,
                seed=1,
                exclude_sessions=["synth-"],
            )


# ---------------------------------------------------------------------------
# Token estimate
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_char_over_four_heuristic(self) -> None:
        assert push_metrics.estimate_tokens("a" * 40) == 10

    def test_empty_string(self) -> None:
        assert push_metrics.estimate_tokens("") == 0

    def test_never_negative(self) -> None:
        assert push_metrics.estimate_tokens("abc") == 0


# ---------------------------------------------------------------------------
# durable_push_records_path — issue athenaeum#1591: the ledger resolves to the
# cache dir, ALWAYS, and never into wiki_root. This withdraws issue
# athenaeum#980 AC4's relocation of this one artifact; see the function's own
# docstring for the archaeology, and athenaeum.store.ARTIFACT_REGISTRY's
# "push-records-ledger" entry for the (unchanged) R3 class/scope declaration.
#
# The four-way resolution matrix below is exhaustive over the two files'
# populated/absent states — the point being that the answer is now the same in
# every cell, so no state of the disk can migrate a deployment.
# ---------------------------------------------------------------------------


class TestDurablePushRecordsPath:
    @staticmethod
    def _roots(tmp_path: Path) -> tuple[Path, Path]:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        return wiki_root, cache_dir

    def test_matrix_neither_populated(self, tmp_path: Path) -> None:
        wiki_root, cache_dir = self._roots(tmp_path)
        assert push_metrics.durable_push_records_path(
            wiki_root, cache_dir=cache_dir
        ) == cache_dir / push_metrics.PUSH_RECORDS_FILENAME

    def test_matrix_legacy_populated(self, tmp_path: Path) -> None:
        wiki_root, cache_dir = self._roots(tmp_path)
        legacy = cache_dir / push_metrics.PUSH_RECORDS_FILENAME
        legacy.write_text('{"session_id":"legacy"}\n', encoding="utf-8")
        assert push_metrics.durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy

    def test_matrix_wiki_path_populated(self, tmp_path: Path) -> None:
        """The cell that used to migrate a deployment. A populated
        ``<wiki_root>/_push_records.jsonl`` — exactly what the live host grew on
        2026-09-10 — no longer captures resolution."""
        wiki_root, cache_dir = self._roots(tmp_path)
        (wiki_root / push_metrics.PUSH_RECORDS_FILENAME).write_text(
            '{"session_id":"misrouted"}\n', encoding="utf-8"
        )
        assert push_metrics.durable_push_records_path(
            wiki_root, cache_dir=cache_dir
        ) == cache_dir / push_metrics.PUSH_RECORDS_FILENAME

    def test_matrix_both_populated(self, tmp_path: Path) -> None:
        wiki_root, cache_dir = self._roots(tmp_path)
        legacy = cache_dir / push_metrics.PUSH_RECORDS_FILENAME
        legacy.write_text('{"session_id":"legacy"}\n', encoding="utf-8")
        (wiki_root / push_metrics.PUSH_RECORDS_FILENAME).write_text(
            '{"session_id":"misrouted"}\n', encoding="utf-8"
        )
        assert push_metrics.durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy

    def test_populated_wiki_path_warns_once_naming_both_paths(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC3: a deployment already carrying the misrouted file is left in a
        DELIBERATE state — nothing is auto-migrated or auto-deleted, and the
        operator is told the rows are stranded rather than silently losing
        them from precision. Warned once per path per process, because
        ``record_push`` calls this on every recall push."""
        wiki_root, cache_dir = self._roots(tmp_path)
        misrouted = wiki_root / push_metrics.PUSH_RECORDS_FILENAME
        misrouted.write_text('{"session_id":"misrouted"}\n', encoding="utf-8")
        push_metrics._MISROUTED_WARNED.discard(str(misrouted))

        with caplog.at_level(logging.WARNING, logger=push_metrics.log.name):
            push_metrics.durable_push_records_path(wiki_root, cache_dir=cache_dir)
            push_metrics.durable_push_records_path(wiki_root, cache_dir=cache_dir)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "must warn once per path, not once per push"
        message = warnings[0].getMessage()
        assert str(misrouted) in message
        assert str(cache_dir / push_metrics.PUSH_RECORDS_FILENAME) in message

    def test_artifact_registry_declares_the_cache_dir(self) -> None:
        """Issue athenaeum#1591 AC1: the file must say ONE thing. The R3
        declaration and the resolver have to agree, or the contradiction this
        issue was filed about simply moves to a different pair of files."""
        from athenaeum.store import ARTIFACT_REGISTRY

        decl = next(a for a in ARTIFACT_REGISTRY if a.name == "push-records-ledger")
        assert decl.persistence_class == "operational"
        assert decl.operational_scope == "store-durable"
        assert decl.location == "cache dir"
        # The sibling ledger precision is computed against has always been
        # store-durable in the cache dir — which is why "store-durable" never
        # implied "wiki root", and why these two must not diverge again.
        sibling = next(a for a in ARTIFACT_REGISTRY if a.name == "push-references-ledger")
        assert sibling.location == decl.location

    def test_absent_wiki_path_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Counter-example for the test above: the healthy case must not warn,
        or the warning is noise every deployment learns to ignore."""
        wiki_root, cache_dir = self._roots(tmp_path)
        with caplog.at_level(logging.WARNING, logger=push_metrics.log.name):
            push_metrics.durable_push_records_path(wiki_root, cache_dir=cache_dir)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_record_push_without_wiki_root_is_unchanged(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("p.md", {}, "snip")]
        )
        assert push_metrics.record_push(record, cache_dir=cache_dir) is True
        assert (cache_dir / push_metrics.PUSH_RECORDS_FILENAME).exists()

    def test_record_push_with_wiki_root_never_writes_into_the_corpus(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#1591 AC1, at the production WRITE path: passing
        ``wiki_root=`` — which every production caller does — must put nothing
        under it. This is the test the observed incident would have failed:
        112 telemetry rows accrued at ``<wiki_root>/_push_records.jsonl`` and a
        librarian run committed them into the knowledge corpus."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="s1", query="q", backend="fts5", hits=[("p.md", {}, "snip")]
        )
        assert push_metrics.record_push(record, cache_dir=cache_dir, wiki_root=wiki_root) is True
        assert (cache_dir / push_metrics.PUSH_RECORDS_FILENAME).exists()
        assert list(wiki_root.iterdir()) == [], "nothing may be written under wiki_root"

    def test_empty_wiki_path_never_captures_resolution(self, tmp_path: Path) -> None:
        """Issue athenaeum#1512 AC3 held that an EMPTY
        ``<wiki_root>/_push_records.jsonl`` — a stray ``touch``, a partially
        written file from a crashed run, a scratch-dir test invocation — must
        not look "already migrated". Kept as a regression guard: under
        athenaeum#1591 NO wiki-path file, empty or populated, can capture
        resolution, so athenaeum#1512's hazard is now closed by construction rather
        than by a content check. This test fails if a future edit reinstates
        any wiki-root branch.
        """
        wiki_root, cache_dir = self._roots(tmp_path)
        legacy = cache_dir / push_metrics.PUSH_RECORDS_FILENAME
        legacy.write_text('{"session_id":"populated-legacy-row"}\n', encoding="utf-8")
        (wiki_root / push_metrics.PUSH_RECORDS_FILENAME).touch()

        assert push_metrics.durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy

    def test_explicit_cache_dir_now_does_isolate_this_function(self, tmp_path: Path) -> None:
        """Issue athenaeum#1512 defect 1 at the function level. Before
        athenaeum#1591 this function could not isolate ``cache_dir``: an empty
        scratch cache dir sent the write to the LIVE ``wiki_root``, which is
        how one scratch invocation orphaned 399 rows. It is now isolated by
        construction — ``cache_dir`` is the only input that can affect the
        answer.

        The CLI-layer fix in ``_resolve_record_wiki_root`` is deliberately
        retained (see ``test_push_metrics_cli.py::
        test_cache_dir_alone_does_not_leak_into_the_live_wiki_root``): it
        scopes ``--cache-dir`` for every push-metrics surface, and this
        function should not be the only thing standing between a scratch run
        and the live store.
        """
        would_be_live_wiki_root = tmp_path / "wiki"
        would_be_live_wiki_root.mkdir()
        scratch_cache_dir = tmp_path / "scratch-cache"  # deliberately not created

        resolved = push_metrics.durable_push_records_path(
            would_be_live_wiki_root, cache_dir=scratch_cache_dir
        )

        assert resolved == scratch_cache_dir / push_metrics.PUSH_RECORDS_FILENAME

    def test_no_split_brain_on_a_fresh_store(self, tmp_path: Path) -> None:
        """The production WRITE path (record_push, as mcp_server.py calls it)
        and the production READ path (read_push_records, as
        build_coverage_worksheet/compute_baseline/determine_references call
        it) must agree on where a fresh store's ledger lives — issue
        athenaeum#980 AC4, now trivially true in both directions because
        athenaeum#1591 left exactly one location."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        cache_dir = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="split-brain-probe-session",
            query="q",
            backend="fts5",
            hits=[("p.md", {}, "snip")],
        )
        assert push_metrics.record_push(record, cache_dir=cache_dir, wiki_root=wiki_root) is True

        records = push_metrics.read_push_records(cache_dir, wiki_root=wiki_root)
        assert any(r.get("session_id") == "split-brain-probe-session" for r in records)

        # And a reader that forgets wiki_root= now sees the SAME rows rather
        # than an empty ledger — the split-brain shape is gone, not merely
        # avoided by every caller remembering to pass the same argument.
        assert push_metrics.read_push_records(cache_dir) == records


# ---------------------------------------------------------------------------
# Liveness assertion (issue athenaeum#1422)
# ---------------------------------------------------------------------------


def _write_raw_row(cache_dir: Path, row: dict) -> None:
    """Append one raw JSONL row directly to the push-records ledger, bypassing
    :func:`push_metrics.record_push` — needed here so a test can construct a
    row with NO ``source`` key at all (an explicit ``recall`` push), which
    ``build_push_record`` also produces but only via the full record-building
    path. Writing the raw dict is the more direct, less coupled fixture."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = push_metrics.push_records_path(cache_dir)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _recall_row(n: int) -> dict:
    """A row with no ``source`` key — the MCP `recall` path's shape."""
    return {"v": 1, "session_id": f"s{n}", "ts": "2026-01-01T00:00:00Z", "items": []}


def _sidecar_row(n: int) -> dict:
    """A row carrying ``"source": "sidecar"`` — the athenaeum#1362 shape."""
    row = _recall_row(n)
    row["source"] = "sidecar"
    return row


class TestSidecarLiveness:
    """issue athenaeum#1422: read-and-assert liveness check over the
    push-telemetry ledger's `source` field — never a write, never a raise."""

    def test_absent_ledger_is_inconclusive_never_pass(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"  # never created
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir)
        assert result.outcome == push_metrics.LIVENESS_INCONCLUSIVE
        assert result.rows_checked == 0
        assert result.sidecar_rows == 0

    def test_fewer_than_window_rows_is_inconclusive(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        for i in range(5):
            _write_raw_row(cache_dir, _recall_row(i))
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir, window=20)
        assert result.outcome == push_metrics.LIVENESS_INCONCLUSIVE
        assert result.rows_checked == 5

    def test_window_rows_with_zero_sidecar_tags_is_fail(self, tmp_path: Path) -> None:
        """The observed incident's exact signature: 186 rows, zero
        sidecar-tagged. A smaller window reproduces the same shape."""
        cache_dir = tmp_path / "cache"
        for i in range(20):
            _write_raw_row(cache_dir, _recall_row(i))
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir, window=20)
        assert result.outcome == push_metrics.LIVENESS_FAIL
        assert result.rows_checked == 20
        assert result.sidecar_rows == 0
        assert "next step" in result.message
        assert "which copy" in result.message

    def test_at_least_one_sidecar_row_in_window_is_pass(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        for i in range(19):
            _write_raw_row(cache_dir, _recall_row(i))
        _write_raw_row(cache_dir, _sidecar_row(19))
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir, window=20)
        assert result.outcome == push_metrics.LIVENESS_PASS
        assert result.sidecar_rows == 1

    def test_only_the_most_recent_window_rows_are_considered(self, tmp_path: Path) -> None:
        """A sidecar row OUTSIDE the trailing window must not turn a live
        FAIL into a false PASS — liveness is about the most RECENT window."""
        cache_dir = tmp_path / "cache"
        _write_raw_row(cache_dir, _sidecar_row(0))
        for i in range(1, 21):
            _write_raw_row(cache_dir, _recall_row(i))
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir, window=20)
        assert result.outcome == push_metrics.LIVENESS_FAIL
        assert result.rows_checked == 20

    def test_exactly_window_rows_is_conclusive_not_inconclusive(self, tmp_path: Path) -> None:
        """Boundary: total == window must be evaluated, not treated as
        'fewer than window' (AC2's INCONCLUSIVE condition is strictly
        `< window`, per the module docstring)."""
        cache_dir = tmp_path / "cache"
        for i in range(20):
            _write_raw_row(cache_dir, _recall_row(i))
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir, window=20)
        assert result.outcome != push_metrics.LIVENESS_INCONCLUSIVE

    def test_default_window_is_the_named_constant(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        for i in range(push_metrics.LIVENESS_WINDOW):
            _write_raw_row(cache_dir, _recall_row(i))
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir)
        assert result.window == push_metrics.LIVENESS_WINDOW
        assert result.outcome == push_metrics.LIVENESS_FAIL

    def test_never_raises_on_a_torn_trailing_line(self, tmp_path: Path) -> None:
        """`read_push_records` already tolerates a torn trailing line
        (`_read_jsonl`); the liveness assertion must inherit that, never a
        new raise path over the same file."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        path = push_metrics.push_records_path(cache_dir)
        with path.open("w", encoding="utf-8") as fh:
            for i in range(20):
                fh.write(json.dumps(_recall_row(i)) + "\n")
            fh.write('{"v": 1, "session_id": "torn"')  # no closing brace/newline
        result = push_metrics.check_sidecar_liveness(cache_dir=cache_dir, window=20)
        assert result.outcome == push_metrics.LIVENESS_FAIL


# ---------------------------------------------------------------------------
# tail_records — the documented NDJSON contract (issue athenaeum#1479)
# ---------------------------------------------------------------------------


class TestTailRecords:
    """``push_metrics.tail_records`` is the logic behind ``athenaeum
    push-metrics tail --json`` — see also ``tests/test_push_metrics_cli.py``
    for the CLI-surface tests (argv wiring, --json rendering)."""

    def _seed_push(
        self, cache_dir: Path, *, session_id: str, source: str = "", uid: str = "u1"
    ) -> None:
        if source == push_metrics.SOURCE_HOOK:
            push_metrics.record_hook_push(session_id, [uid], cache_dir=cache_dir)
            return
        record = push_metrics.build_push_record(
            session_id=session_id,
            query="q",
            backend="fts5",
            hits=[(f"{uid}.md", {"uid": uid, "access": "internal", "audience": ["owner"]}, "body")],
        )
        if source:
            record.source = source
        push_metrics.record_push(record, cache_dir=cache_dir)

    def _seed_reference(
        self, cache_dir: Path, *, session_id: str, ts: str, pushed=("u1",), referenced=("u1",)
    ) -> None:
        push_metrics.record_reference_result(
            push_metrics.ReferenceResult(
                session_id=session_id,
                ts=ts,
                pushed_ids=list(pushed),
                referenced_ids=list(referenced),
            ),
            cache_dir=cache_dir,
        )

    def test_field_set_is_pinned_for_a_push_record(self, tmp_path: Path) -> None:
        """AC: a test pins the emitted field set, so adding a field is a
        visible diff and removing one is a caught break."""
        cache_dir = tmp_path / "cache"
        self._seed_push(cache_dir, session_id="s1", source=push_metrics.SOURCE_HOOK)
        [rec] = list(push_metrics.tail_records(cache_dir=cache_dir))
        assert rec["record_type"] == "push"
        assert set(rec) == {
            "record_type",
            "v",
            "session_id",
            "ts",
            "query_hash",
            "backend",
            "items",
            "pushed_count",
            "token_cost",
            "token_cost_estimated",
            "source",
        }
        # A row written after issue athenaeum#1514 carries no
        # `memory_tier`, and the shaper must OMIT the key rather than
        # project it as `null` — see `_shape_tail_push_item`. The
        # historical-row direction is pinned by
        # `test_a_pre_1514_row_still_projects_its_memory_tier` below.
        assert set(rec["items"][0]) == {"id", "tier", "scope", "token_cost"}
        assert rec["source"] == "hook"

    def test_a_pre_1514_row_still_projects_its_memory_tier(self, tmp_path: Path) -> None:
        """Issue athenaeum#1514 stopped WRITING `memory_tier`, but the
        ledger's own history still carries it — including the exact rows
        athenaeum#1560's bucketed audit read to discharge this issue's
        evidence gate. The documented CLI surface must stay able to read
        them, so the field remains projectable, include-when-present.
        """
        import json

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True)
        legacy = {
            "v": 1,
            "session_id": "s-legacy",
            "ts": "2026-09-08T00:00:00Z",
            "query_hash": "deadbeef00000000",
            "backend": "vector",
            "items": [
                {
                    "id": "u1",
                    "tier": "internal",
                    "scope": "owner",
                    "token_cost": 7,
                    "memory_tier": "hot",
                }
            ],
            "pushed_count": 1,
            "token_cost": 7,
            "token_cost_estimated": True,
            "source": "sidecar",
        }
        (cache_dir / push_metrics.PUSH_RECORDS_FILENAME).write_text(
            json.dumps(legacy) + "\n", encoding="utf-8"
        )
        [rec] = list(push_metrics.tail_records(cache_dir=cache_dir))
        assert rec["items"][0]["memory_tier"] == "hot"

    def test_field_set_is_pinned_for_an_explicit_recall_push(self, tmp_path: Path) -> None:
        """An explicit MCP ``recall`` push omits ``source`` entirely — the
        documented reader rule (absent key means recall) must carry through
        into the shaped tail contract unchanged."""
        cache_dir = tmp_path / "cache"
        self._seed_push(cache_dir, session_id="s1")
        [rec] = list(push_metrics.tail_records(cache_dir=cache_dir))
        assert "source" not in rec
        assert set(rec) == {
            "record_type",
            "v",
            "session_id",
            "ts",
            "query_hash",
            "backend",
            "items",
            "pushed_count",
            "token_cost",
            "token_cost_estimated",
        }

    def test_field_set_is_pinned_for_a_reference_record(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        self._seed_reference(cache_dir, session_id="s1", ts="2026-01-01T00:00:00Z")
        [rec] = list(push_metrics.tail_records(cache_dir=cache_dir))
        assert rec["record_type"] == "reference"
        assert set(rec) == {
            "record_type",
            "v",
            "session_id",
            "ts",
            "pushed_count",
            "referenced_count",
            "referenced_ids",
            "precision",
        }

    def test_never_reintroduces_raw_query_text(self, tmp_path: Path) -> None:
        """Constraint: query_hash stays a hash — the tail contract must not
        widen the ledger to make output nicer."""
        cache_dir = tmp_path / "cache"
        record = push_metrics.build_push_record(
            session_id="s1", query="a raw query with secrets", backend="fts5", hits=[]
        )
        # build_push_record with no hits writes no items; force a session +
        # item so record_push doesn't no-op.
        record.items = [
            push_metrics.PushedItem(id="u1", tier="internal", scope="owner", token_cost=1)
        ]
        push_metrics.record_push(record, cache_dir=cache_dir)
        [rec] = list(push_metrics.tail_records(cache_dir=cache_dir))
        assert "query" not in rec
        assert "a raw query with secrets" not in json.dumps(rec)
        assert rec["query_hash"] == push_metrics._query_hash("a raw query with secrets")

    def test_source_distinguishes_all_three_writers(self, tmp_path: Path) -> None:
        """Constraint: a consumer must be able to tell hook / sidecar /
        recall apart — never by elimination."""
        cache_dir = tmp_path / "cache"
        self._seed_push(
            cache_dir, session_id="s1", uid="hook-item", source=push_metrics.SOURCE_HOOK
        )
        self._seed_push(cache_dir, session_id="s2", uid="sidecar-item", source="sidecar")
        self._seed_push(cache_dir, session_id="s3", uid="recall-item")

        by_uid = {r["items"][0]["id"]: r for r in push_metrics.tail_records(cache_dir=cache_dir)}
        assert by_uid["hook-item"]["source"] == "hook"
        assert by_uid["sidecar-item"]["source"] == "sidecar"
        assert "source" not in by_uid["recall-item"]

    def test_session_filter_isolates_one_session_from_a_multi_session_ledger(
        self, tmp_path: Path
    ) -> None:
        """AC: --session filtering verified against a ledger containing
        multiple sessions."""
        cache_dir = tmp_path / "cache"
        self._seed_push(cache_dir, session_id="viewer-session", uid="v1")
        self._seed_push(cache_dir, session_id="target-session", uid="t1")
        self._seed_push(cache_dir, session_id="third-session", uid="x1")
        self._seed_reference(cache_dir, session_id="target-session", ts="2026-01-01T00:00:05Z")
        self._seed_reference(cache_dir, session_id="viewer-session", ts="2026-01-01T00:00:06Z")

        recs = list(push_metrics.tail_records(cache_dir=cache_dir, session_id="target-session"))
        assert recs, "expected at least one record for target-session"
        assert all(r["session_id"] == "target-session" for r in recs)
        # both a push AND a reference record for the target session are seen
        assert {r["record_type"] for r in recs} == {"push", "reference"}

    def test_since_filter_excludes_older_records(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone

        cache_dir = tmp_path / "cache"
        self._seed_reference(cache_dir, session_id="s1", ts="2020-01-01T00:00:00Z")
        self._seed_reference(cache_dir, session_id="s1", ts="2030-01-01T00:00:00Z")
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        recs = list(push_metrics.tail_records(cache_dir=cache_dir, since=since))
        assert [r["ts"] for r in recs] == ["2030-01-01T00:00:00Z"]

    def test_newest_last_ordering(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        self._seed_reference(cache_dir, session_id="s1", ts="2026-06-01T00:00:00Z")
        self._seed_reference(cache_dir, session_id="s1", ts="2026-01-01T00:00:00Z")
        self._seed_reference(cache_dir, session_id="s1", ts="2026-12-01T00:00:00Z")
        recs = list(push_metrics.tail_records(cache_dir=cache_dir))
        assert [r["ts"] for r in recs] == [
            "2026-01-01T00:00:00Z",
            "2026-06-01T00:00:00Z",
            "2026-12-01T00:00:00Z",
        ]

    def test_never_mutates_the_ledgers(self, tmp_path: Path) -> None:
        """Constraint: read-only, never mutates the ledgers."""
        cache_dir = tmp_path / "cache"
        self._seed_push(cache_dir, session_id="s1")
        self._seed_reference(cache_dir, session_id="s1", ts="2026-01-01T00:00:00Z")
        push_path = push_metrics.push_records_path(cache_dir)
        ref_path = push_metrics.reference_records_path(cache_dir)
        before_push = push_path.read_bytes()
        before_ref = ref_path.read_bytes()
        list(push_metrics.tail_records(cache_dir=cache_dir, session_id="s1", since=None))
        list(push_metrics.tail_records(cache_dir=cache_dir, follow=False))
        assert push_path.read_bytes() == before_push
        assert ref_path.read_bytes() == before_ref

    def test_follow_picks_up_a_record_appended_after_draining(self, tmp_path: Path) -> None:
        """AC: --follow picks up records appended after the command starts.

        Bounded and deterministic (no wall-clock reliance, no sleeps that
        could hang CI): ``poll_interval=0`` and an explicit ``max_polls``
        make this an injectable stop condition, not a race against real
        time. The new record is appended to the ledger IN BETWEEN two
        ``next()`` calls on the generator, so the test controls exactly
        when the follow loop is given something new to find.
        """
        cache_dir = tmp_path / "cache"
        self._seed_push(cache_dir, session_id="s1", uid="first")

        gen = push_metrics.tail_records(
            cache_dir=cache_dir, follow=True, poll_interval=0, max_polls=5
        )
        first = next(gen)
        assert first["items"][0]["id"] == "first"

        # Nothing new yet: append after the generator already drained the
        # initial snapshot, simulating a ledger that grows while `tail` runs.
        self._seed_push(cache_dir, session_id="s1", uid="second")
        second = next(gen)
        assert second["items"][0]["id"] == "second"

        # The generator terminates once max_polls is exhausted with nothing
        # further appended — proving the stop condition is real, not an
        # infinite loop a test would hang on.
        with pytest.raises(StopIteration):
            next(gen)

    def test_follow_terminates_at_max_polls_with_nothing_appended(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        gen = push_metrics.tail_records(
            cache_dir=cache_dir, follow=True, poll_interval=0, max_polls=3
        )
        with pytest.raises(StopIteration):
            next(gen)

    def test_without_follow_drains_and_stops(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        self._seed_push(cache_dir, session_id="s1")
        recs = list(push_metrics.tail_records(cache_dir=cache_dir, follow=False))
        assert len(recs) == 1
