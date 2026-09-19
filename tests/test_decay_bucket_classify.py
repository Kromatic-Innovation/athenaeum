# SPDX-License-Identifier: Apache-2.0
"""Tests for the decay-bucket classifier and its intake wiring (issue athenaeum#1837).

Covers :mod:`athenaeum.decay_bucket` (a structural mirror of
:mod:`athenaeum.claim_kind`) and the production call site added in
:func:`athenaeum.librarian._stamp_unbucketed_auto_memory`, invoked from
:func:`athenaeum.librarian._run_auto_memory_phase` immediately AFTER
:func:`athenaeum.librarian._stamp_unclassified_claim_kinds`:

- AC1: :func:`classify_decay_bucket` returns a member of
  :data:`athenaeum.models.MEMORY_BUCKETS` or ``""``, and never raises — across
  every failure mode the module claims to absorb.
- AC2: :func:`stamp_decay_bucket` is idempotent — a file already carrying a
  valid ``bucket:`` is returned with ZERO classifier calls (asserted on the
  stub's call count, not merely on the file's content).
- AC3 (counter-example): the classifier RAISING leaves the file
  byte-identical, with no ``bucket`` key, a WARNING logged, and ``""``
  returned. It must NOT fall back to ``durable``: the stamp is idempotent, so
  a blip-written ``durable`` would be permanent, and ``durable`` is already
  indistinguishable from unset for the sweep.
- AC4 (counter-example): an out-of-vocabulary label (``"eventually"``) writes
  nothing, and a SECOND pass with a working stub then stamps ``daily``.
- AC5: the librarian wrapper runs after the claim_kind stamp, no-ops on
  ``client is None``, tolerates ``SimpleNamespace`` doubles via ``getattr``,
  and sets ``am.bucket`` on the in-memory record.
- AC6: lazy-import guard, mirroring
  ``tests/test_claim_kind_intake_wiring.py::TestAC6NoHotPathLazyImport`` —
  importing ``athenaeum.librarian`` does not import ``athenaeum.decay_bucket``.
- AC7: ``DECAY_BUCKET_SYSTEM`` has a ``prompt_registry`` row and an in-place
  comment justifying the inline string.
- AC8: ``observe_decay_bucket`` fires on a parsed payload, and
  ``observe_parse_failure(contract="decay_bucket", ...)`` fires at BOTH
  parse-failure early returns.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from athenaeum.decay_bucket import (
    DECAY_BUCKET_SYSTEM,
    classify_decay_bucket,
    stamp_decay_bucket,
)
from athenaeum.librarian import (
    _stamp_unbucketed_auto_memory,
    discover_auto_memory_files,
)
from athenaeum.models import MEMORY_BUCKETS, TokenUsage, parse_bucket, parse_frontmatter

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "athenaeum"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(payload_text: str) -> MagicMock:
    """A stub whose single response carries ``payload_text`` as its text block."""
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    response.usage = MagicMock(
        input_tokens=1,
        output_tokens=1,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    client.messages.create.return_value = response
    return client


def _raising_client(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


def _response_client(response: object) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _dual_client(*, claim_kind: str, bucket: str) -> MagicMock:
    """One stub serving BOTH nightly classify prompts, routed by system prompt.

    The auto-memory phase hands the same ``classify``-knob client to the
    claim_kind stamp and the decay_bucket stamp, so a phase-level test needs a
    stub that answers each with its own contract's shape.
    """
    client = MagicMock()

    def _create(**kwargs):
        system = kwargs.get("system", "")
        if "HOW IT DECAYS" in system:
            text = '{"bucket": "%s"}' % bucket
        elif "EPISTEMIC KIND" in system:
            text = '{"claim_kind": "%s"}' % claim_kind
        else:  # pragma: no cover - defensive: an unrecognized prompt is a bug
            raise AssertionError(f"unexpected system prompt: {system[:80]!r}")
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.usage = MagicMock(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return response

    client.messages.create.side_effect = _create
    return client


def _seed_auto_memory_root(knowledge_root: Path) -> Path:
    auto = knowledge_root / "raw" / "auto-memory"
    auto.mkdir(parents=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return auto


def _scope_dir(auto_root: Path, tmp_path: Path) -> Path:
    """A scope directory whose name is DERIVED from ``tmp_path``.

    Production scope names are path-hash identifiers (``-Users-alice-Code-x``).
    Deriving one from ``tmp_path`` reproduces that shape without embedding a
    synthetic absolute-path literal anywhere in the repo — ``public-safe-lint.sh``
    rejects those, test fixtures included.
    """
    return auto_root / tmp_path.as_posix().replace("/", "-")


def _write_auto_memory_file(
    scope_dir: Path,
    filename: str,
    *,
    name: str,
    body: str,
    claim_kind: str | None = None,
    bucket: str | None = None,
) -> Path:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    lines = ["---", f"name: {name}", "type: feedback"]
    if claim_kind is not None:
        lines.append(f"claim_kind: {claim_kind}")
    if bucket is not None:
        lines.append(f"bucket: {bucket}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC1 — classify_decay_bucket returns MEMORY_BUCKETS | "", and never raises
# ---------------------------------------------------------------------------


class TestAC1ClassifyVocabularyAndNeverRaises:
    @pytest.mark.parametrize("bucket", sorted(MEMORY_BUCKETS))
    def test_each_valid_label_round_trips(self, bucket: str) -> None:
        client = _client('{"bucket": "%s"}' % bucket)
        assert classify_decay_bucket("The staging deploy is waiting on CI.", client) == bucket

    def test_no_client_short_circuits_with_no_call(self) -> None:
        assert classify_decay_bucket("Anything at all.", None) == ""

    def test_empty_text_short_circuits_with_no_call(self) -> None:
        client = _client('{"bucket": "daily"}')
        assert classify_decay_bucket("   \n\n  ", client) == ""
        client.messages.create.assert_not_called()

    @pytest.mark.parametrize(
        "make_client",
        [
            pytest.param(lambda: _raising_client(RuntimeError("boom")), id="api-error"),
            pytest.param(
                lambda: _response_client(SimpleNamespace(content=[])),
                id="malformed-response",
            ),
            pytest.param(lambda: _client("sorry, I cannot do that"), id="no-json"),
            pytest.param(lambda: _client('{"bucket": "eventually"}'), id="out-of-vocab"),
            pytest.param(lambda: _client('{"bucket": 7}'), id="non-string"),
            pytest.param(lambda: _client('{"other": "daily"}'), id="missing-key"),
        ],
    )
    def test_every_failure_mode_returns_empty_and_never_raises(
        self, make_client
    ) -> None:
        result = classify_decay_bucket("The develop tip is abc123.", make_client())
        assert result == ""
        # The contract is "a member of MEMORY_BUCKETS, or ''" — never a
        # best-effort guess, and in particular never "durable".
        assert result in MEMORY_BUCKETS or result == ""

    def test_usage_is_accumulated_on_a_successful_call(self) -> None:
        usage = TokenUsage()
        client = _client('{"bucket": "weekly"}')
        assert classify_decay_bucket("Alice is out until Friday.", client, None, usage)
        assert usage.api_calls == 1


# ---------------------------------------------------------------------------
# AC2 — stamp_decay_bucket is idempotent (asserted by stub CALL COUNT)
# ---------------------------------------------------------------------------


class TestAC2StampIdempotence:
    def test_existing_valid_bucket_returns_with_zero_classifier_calls(
        self, tmp_path: Path
    ) -> None:
        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_declared.md",
            name="Declared durable",
            body="We pivoted from Heroku to Fly.io.",
            bucket="durable",
        )
        before = path.read_bytes()

        # A stub that would DISAGREE, to prove the short-circuit is what
        # produced the answer rather than a coincidence of content.
        client = _client('{"bucket": "daily"}')
        assert stamp_decay_bucket(path, client) == "durable"

        assert client.messages.create.call_count == 0
        assert path.read_bytes() == before

    def test_second_stamp_of_the_same_file_costs_no_second_call(
        self, tmp_path: Path
    ) -> None:
        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_status.md",
            name="CI status",
            body="The staging deploy is waiting on CI.",
        )
        client = _client('{"bucket": "daily"}')

        assert stamp_decay_bucket(path, client) == "daily"
        assert client.messages.create.call_count == 1
        after_first = path.read_bytes()

        assert stamp_decay_bucket(path, client) == "daily"
        # STILL one — the second pass read the stamp it wrote, not the model.
        assert client.messages.create.call_count == 1
        assert path.read_bytes() == after_first

    def test_stamp_round_trips_through_parse_bucket(self, tmp_path: Path) -> None:
        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_policy.md",
            name="Merge policy",
            body="Never commit directly to main.",
        )
        assert stamp_decay_bucket(path, _client('{"bucket": "durable"}')) == "durable"
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("bucket") == "durable"
        assert parse_bucket(meta) == "durable"
        # The pre-existing frontmatter survived the rewrite.
        assert meta.get("name") == "Merge policy"

    def test_unwritable_path_leaves_the_file_unbucketed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write half of fail-open: a classification that cannot be
        persisted returns ``""``, not the (unwritten) label — otherwise the
        caller would set ``am.bucket`` in memory for a file that will come
        back unbucketed on the next run."""
        import athenaeum.decay_bucket as decay_bucket

        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_status.md",
            name="CI status",
            body="The staging deploy is waiting on CI.",
        )
        before = path.read_bytes()

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(decay_bucket, "atomic_write_text", _boom)
        assert stamp_decay_bucket(path, _client('{"bucket": "daily"}')) == ""
        assert path.read_bytes() == before

    def test_unreadable_path_returns_empty_without_calling(
        self, tmp_path: Path
    ) -> None:
        client = _client('{"bucket": "daily"}')
        assert stamp_decay_bucket(tmp_path / "does_not_exist.md", client) == ""
        assert client.messages.create.call_count == 0


# ---------------------------------------------------------------------------
# AC3 (counter-example) — the classifier RAISING leaves the file untouched
# ---------------------------------------------------------------------------


class TestAC3ClassifierRaisesLeavesFileUntouched:
    def test_raise_is_byte_identical_no_key_warning_logged_empty_returned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_blip.md",
            name="Transient blip",
            body="The staging deploy is waiting on CI.",
        )
        before = path.read_bytes()

        client = _raising_client(RuntimeError("upstream exploded"))
        with caplog.at_level(logging.WARNING, logger="athenaeum.decay_bucket"):
            result = stamp_decay_bucket(path, client)

        assert result == ""
        assert path.read_bytes() == before
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "bucket" not in meta
        assert any(
            record.levelno == logging.WARNING and "classify call failed" in record.message
            for record in caplog.records
        ), caplog.text

    def test_failure_does_not_fall_back_to_durable(self, tmp_path: Path) -> None:
        """The load-bearing half of the counter-example.

        ``durable`` is indistinguishable from unset for the deterministic
        sweep, and the stamp is idempotent — so a blip-written ``durable``
        would be PERMANENT and silently wrong. Unstamped-and-retried is the
        only correct failure state.
        """
        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_blip.md",
            name="Transient blip",
            body="The staging deploy is waiting on CI.",
        )
        assert stamp_decay_bucket(path, _raising_client(RuntimeError("boom"))) == ""
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("bucket") != "durable"

        # …and the NEXT run, with a working client, still gets its chance.
        working = _client('{"bucket": "daily"}')
        assert stamp_decay_bucket(path, working) == "daily"
        assert working.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# AC4 (counter-example) — an out-of-vocabulary label writes nothing
# ---------------------------------------------------------------------------


class TestAC4OutOfVocabularyLabel:
    def test_eventually_writes_nothing_then_a_working_stub_stamps_daily(
        self, tmp_path: Path
    ) -> None:
        path = _write_auto_memory_file(
            tmp_path / "scope",
            "feedback_status.md",
            name="CI status",
            body="The staging deploy is waiting on CI.",
        )
        before = path.read_bytes()

        bad = _client('{"bucket": "eventually"}')
        assert stamp_decay_bucket(path, bad) == ""
        assert bad.messages.create.call_count == 1
        assert path.read_bytes() == before
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "bucket" not in meta

        good = _client('{"bucket": "daily"}')
        assert stamp_decay_bucket(path, good) == "daily"
        assert good.messages.create.call_count == 1
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("bucket") == "daily"


# ---------------------------------------------------------------------------
# AC5 — the librarian call site
# ---------------------------------------------------------------------------


class TestAC5LibrarianCallSite:
    def test_stamps_unbucketed_auto_memory_file_and_sets_am_bucket(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = _scope_dir(auto, tmp_path)
        path = _write_auto_memory_file(
            scope,
            "feedback_status.md",
            name="CI status",
            body="The staging deploy is waiting on CI.",
        )

        files = discover_auto_memory_files(knowledge_root)
        assert files[0].bucket == ""  # nothing written it yet — that is the bug

        client = _client('{"bucket": "daily"}')
        usage = TokenUsage()
        _stamp_unbucketed_auto_memory(files, client, None, usage)

        client.messages.create.assert_called_once()
        assert files[0].bucket == "daily"  # in-memory record updated
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("bucket") == "daily"
        assert usage.api_calls == 1

        # A fresh discovery pass (the NEXT run) reads the stamp back.
        assert discover_auto_memory_files(knowledge_root)[0].bucket == "daily"

    def test_noop_when_client_is_none(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = _scope_dir(auto, tmp_path)
        path = _write_auto_memory_file(
            scope, "feedback_x.md", name="X", body="Some claim."
        )
        before = path.read_bytes()

        files = discover_auto_memory_files(knowledge_root)
        _stamp_unbucketed_auto_memory(files, None, None, None)

        assert files[0].bucket == ""
        assert path.read_bytes() == before

    def test_empty_file_list_is_noop(self) -> None:
        client = _client('{"bucket": "daily"}')
        _stamp_unbucketed_auto_memory([], client, None, None)
        client.messages.create.assert_not_called()

    def test_author_supplied_bucket_is_never_overwritten(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = _scope_dir(auto, tmp_path)
        path = _write_auto_memory_file(
            scope,
            "feedback_declared.md",
            name="Declared durable",
            body="We pivoted from Heroku to Fly.io.",
            bucket="durable",
        )

        files = discover_auto_memory_files(knowledge_root)
        assert files[0].bucket == "durable"

        client = _client('{"bucket": "daily"}')
        _stamp_unbucketed_auto_memory(files, client, None, None)

        client.messages.create.assert_not_called()
        assert files[0].bucket == "durable"
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("bucket") == "durable"

    def test_simple_namespace_doubles_are_tolerated(self) -> None:
        """Several pre-existing budget/deadline tests substitute
        ``SimpleNamespace(origin_scope=...)`` doubles for
        ``discover_auto_memory_files``. A bare double has neither ``bucket``
        nor ``path`` — it must be skipped via ``getattr``, not crash.
        """
        client = _client('{"bucket": "daily"}')
        doubles = [
            SimpleNamespace(origin_scope="-scope-a"),
            SimpleNamespace(origin_scope="-scope-b", bucket="durable"),
            SimpleNamespace(origin_scope="-scope-c", bucket="", path=None),
        ]
        _stamp_unbucketed_auto_memory(doubles, client, None, None)  # type: ignore[arg-type]
        client.messages.create.assert_not_called()


class TestAC5PhaseOrdering:
    def test_phase_calls_bucket_stamp_after_claim_kind_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the ORDER inside the real ``_run_auto_memory_phase``.

        The issue is explicit that the bucket stamp runs AFTER the claim_kind
        stamp (``librarian.py``'s existing call site), so recording both calls
        and comparing their positions is the assertion — not merely that both
        happened.
        """
        from athenaeum import librarian

        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = _scope_dir(auto, tmp_path)
        _write_auto_memory_file(
            scope, "feedback_x.md", name="X status", body="The staging deploy is waiting on CI."
        )
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir()

        order: list[str] = []
        monkeypatch.setattr(
            librarian,
            "_stamp_unclassified_claim_kinds",
            lambda *a, **kw: order.append("claim_kind"),
        )
        monkeypatch.setattr(
            librarian,
            "_stamp_unbucketed_auto_memory",
            lambda *a, **kw: order.append("decay_bucket"),
        )
        monkeypatch.setattr(librarian, "_compile_auto_memory", lambda *a, **kw: [])
        monkeypatch.setattr(librarian, "_run_reresolve_pass", lambda *a, **kw: 0)

        ctx = _phase_ctx(librarian, knowledge_root, wiki_root, _client('{"bucket": "daily"}'))
        librarian._run_auto_memory_phase(ctx)

        assert order == ["claim_kind", "decay_bucket"]

    def test_phase_actually_stamps_the_bucket_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same phase function, real helpers this time: the nightly run writes
        ``bucket:`` into the raw file's frontmatter."""
        from athenaeum import librarian

        knowledge_root = tmp_path / "knowledge"
        auto = _seed_auto_memory_root(knowledge_root)
        scope = _scope_dir(auto, tmp_path)
        path = _write_auto_memory_file(
            scope, "feedback_x.md", name="X status", body="The staging deploy is waiting on CI."
        )
        wiki_root = knowledge_root / "wiki"
        wiki_root.mkdir()

        monkeypatch.setattr(librarian, "_compile_auto_memory", lambda *a, **kw: [])
        monkeypatch.setattr(librarian, "_run_reresolve_pass", lambda *a, **kw: 0)

        client = _dual_client(claim_kind="observation", bucket="daily")
        ctx = _phase_ctx(librarian, knowledge_root, wiki_root, client)
        librarian._run_auto_memory_phase(ctx)

        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta.get("bucket") == "daily"
        assert meta.get("claim_kind") == "observation"


def _phase_ctx(librarian, knowledge_root: Path, wiki_root: Path, client):
    from athenaeum.config import load_config

    ctx = librarian.RunContext(
        raw_root=knowledge_root / "raw",
        wiki_root=wiki_root,
        knowledge_root=knowledge_root,
        dry_run=False,
        max_files=None,
        max_api_calls=None,
        max_runtime=None,
        cluster_only=False,
        merge_only=False,
        strict_budget=False,
        batch_mode=None,
        retire=False,
        push_after_run=None,
        pull_before_run=None,
        projects_root=None,
        install_signal_handlers=False,
        changed_paths=None,
        full_compile=False,
        now=None,
        heartbeat=None,
        out_run_stats=None,
    )
    ctx.config = load_config(knowledge_root)
    ctx.classify_client = client
    return ctx


# ---------------------------------------------------------------------------
# AC6 — lazy import at the call site (mirrors TestAC6NoHotPathLazyImport)
# ---------------------------------------------------------------------------


class TestAC6NoHotPathLazyImport:
    def test_call_site_imports_decay_bucket_lazily_not_at_module_scope(self) -> None:
        src = _SRC / "librarian.py"
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        module_level_imports: set[str] = set()
        for node in tree.body:  # top-level statements ONLY
            if isinstance(node, ast.ImportFrom) and node.module:
                module_level_imports.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_level_imports.add(alias.name)
        assert "athenaeum.decay_bucket" not in module_level_imports

        # It IS imported somewhere (inside the stamping function) — so this
        # test cannot pass by the wiring silently being removed.
        assert (
            "from athenaeum.decay_bucket import stamp_decay_bucket"
            in src.read_text(encoding="utf-8")
        )

    def test_importing_librarian_does_not_import_decay_bucket(self) -> None:
        """Run in a fresh subprocess so no other test's imports leak in."""
        import subprocess
        import sys

        probe = (
            "import sys\n"
            "import athenaeum.librarian\n"
            "assert 'athenaeum.decay_bucket' not in sys.modules, "
            "'decay_bucket classifier reached at athenaeum.librarian import time'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# AC7 — prompt registry row + the in-place inline-string justification
# ---------------------------------------------------------------------------


class TestAC7PromptRegistryRow:
    def test_registry_row_matches_the_specified_tuple(self) -> None:
        from athenaeum.prompt_registry import PROMPT_META, PROMPTS

        meta = PROMPT_META["decay_bucket.decay_bucket_system"]
        assert (meta.module, meta.constant, meta.knob, meta.max_tokens, meta.cacheable) == (
            "athenaeum.decay_bucket",
            "DECAY_BUCKET_SYSTEM",
            "classify",
            64,
            False,
        )
        # The registry indexes the LIVE constant, never a copy.
        assert PROMPTS["decay_bucket.decay_bucket_system"] is DECAY_BUCKET_SYSTEM

    def test_inline_prompt_string_carries_an_in_place_justification(self) -> None:
        """``policies/prompt-text-is-content.md`` requires the inline string to
        justify itself where it lives, not only in the registry."""
        source = (_SRC / "decay_bucket.py").read_text(encoding="utf-8")
        head, _, _ = source.partition("DECAY_BUCKET_SYSTEM = ")
        assert "prompt-text-is-content.md" in head
        assert "prompt_registry" in head

    def test_prompt_names_the_whole_vocabulary(self) -> None:
        for bucket in MEMORY_BUCKETS:
            assert bucket in DECAY_BUCKET_SYSTEM


# ---------------------------------------------------------------------------
# AC8 — llm_schemas instrumentation at BOTH parse-failure early returns
# ---------------------------------------------------------------------------


class TestAC8SchemaObservation:
    def test_observe_decay_bucket_fires_on_a_parsed_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import llm_schemas

        seen: list[tuple] = []
        monkeypatch.setattr(
            llm_schemas,
            "observe_decay_bucket",
            lambda payload, *, call_site, wiki_root=None: seen.append(
                (payload, call_site)
            ),
        )
        client = _client('{"bucket": "daily"}')
        assert classify_decay_bucket("The staging deploy waits on CI.", client) == "daily"
        assert seen == [({"bucket": "daily"}, "decay_bucket.classify_decay_bucket")]

    @pytest.mark.parametrize(
        "make_client,detail",
        [
            pytest.param(
                lambda: _response_client(SimpleNamespace(content=[])),
                "malformed-classify-response",
                id="malformed-response",
            ),
            pytest.param(
                lambda: _client("sorry, no JSON here"),
                "no-json-object",
                id="no-json-object",
            ),
        ],
    )
    def test_both_parse_failure_early_returns_observe(
        self, monkeypatch: pytest.MonkeyPatch, make_client, detail: str
    ) -> None:
        from athenaeum import llm_schemas

        seen: list[dict] = []
        monkeypatch.setattr(
            llm_schemas,
            "observe_parse_failure",
            lambda **kwargs: seen.append(kwargs),
        )
        assert classify_decay_bucket("Anything.", make_client()) == ""
        assert len(seen) == 1
        assert seen[0]["contract"] == "decay_bucket"
        assert seen[0]["call_site"] == "decay_bucket.classify_decay_bucket"
        assert seen[0]["detail"] == detail

    def test_response_model_accepts_the_vocabulary_and_flags_drift(self) -> None:
        import pydantic

        from athenaeum.llm_schemas import DecayBucketResponse

        for bucket in MEMORY_BUCKETS:
            assert DecayBucketResponse.model_validate({"bucket": bucket})
        with pytest.raises(pydantic.ValidationError):
            DecayBucketResponse.model_validate({"bucket": "eventually"})
