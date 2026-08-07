# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#578 — per-stage explicit ``thinking`` + re-baselined ``max_tokens``.

Cross-cutting assertions that span every LLM call site (``tiers.py``,
``resolutions.py``, ``contradictions.py``, ``claim_kind.py``,
``query_topics.py``, ``reasoning_tiers.py``):

1. No call site ever builds a params dict containing ``budget_tokens`` — that
   parameter is fully removed on Opus 5 / Sonnet 5 / Opus 4.7+ and returns a
   400 if sent (see the model facts recorded in this issue).
2. Every call site's built params dict carries an explicit ``thinking`` key
   (never omitted, never ``None``) with the correct per-stage posture.
3. The re-baselined ``max_tokens`` defaults (resolver, merge_patch, and the
   other write/resolve-knob stages) never shrink relative to the pre-athenaeum#578
   values.
4. No stage combines ``output_config.effort`` of ``xhigh``/``max`` with
   ``thinking: {"type": "disabled"}`` — that 400s on Opus 5.
5. The ``claude-cli`` backend (``honors_max_tokens=False``) still receives an
   explicit ``thinking`` key in the params dict it is asked to serve — the
   capability gap is about ``max_tokens``, not ``thinking``.

No network: every "client" is a :class:`unittest.mock.MagicMock` mirroring
the Anthropic SDK shape, exactly like the sibling ``test_provider.py`` /
``test_tiers.py`` suites.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Issue athenaeum#750/#791: ``_all_params()`` below (and its
# ``_all_direct_builder_params``/``_all_inline_call_site_params`` halves) drives
# real call sites — ``classify_claim_kind``, ``detect_contradictions``, the
# ``tiers``/``reasoning_tiers`` builders — that end in
# ``athenaeum.llm_schemas.observe*``. It is invoked by
# ``@pytest.mark.parametrize("label", sorted(_all_params()))`` decorator
# ARGUMENTS below, which pytest evaluates at COLLECTION time (module import),
# before any fixture has run. This module used to carry its own module-level
# ``os.environ[...]`` workaround for that gap; it is no longer needed —
# ``tests/conftest.py``'s ``pytest_configure`` hook (athenaeum#791) redirects
# ``ATHENAEUM_CACHE_DIR`` before collection begins for the whole suite, which
# covers this module's collection-time call the same as every other module's.
from athenaeum.claim_kind import _CLAIM_KIND_MAX_TOKENS, classify_claim_kind
from athenaeum.contradictions import _DETECT_MAX_TOKENS, detect_contradictions
from athenaeum.models import AutoMemoryFile, EntityAction, RawFile
from athenaeum.provider import response_text
from athenaeum.query_topics import _TOPIC_MAX_TOKENS
from athenaeum.reasoning_tiers import (
    _T1_MAX_TOKENS,
    _T2_MAX_TOKENS,
    BoundedSourceView,
    ReasoningProposal,
    build_t1_request_params,
    build_t2_request_params,
)
from athenaeum.tiers import (
    _MERGE_MAX_TOKENS,
    _MERGE_PATCH_MAX_TOKENS,
    _TIER2_CLASSIFY_MAX_TOKENS,
    _TIER3_CREATE_MAX_TOKENS,
    tier2_request_params,
    tier3_create_params,
    tier3_merge_full_params,
    tier3_merge_params,
)

# Pre-athenaeum#578 values (see issue body's "Call sites and current max_tokens
# defaults" table + the classify/topic/reasoning stages not in that table but
# covered by the "every LLM call site" acceptance criterion). Used only to
# assert the re-baseline NEVER SHRINKS a budget.
_PRE_578_MAX_TOKENS = {
    "resolve": 1024,
    "freetext_edit": 4096,
    "merge_patch": 2048,
    "merge_create": 2048,
    "merge_full": 8192,
    "classify": 4096,  # tier2 classify — untouched by athenaeum#578, must not shrink
    "claim_kind": 64,  # untouched
    "contradiction_detect": 1024,  # untouched
    "topic": 256,  # untouched
    "reasoning_t1": 256,  # untouched
    "reasoning_t2": 4096,  # untouched
}


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _entity_action(kind: str = "create") -> EntityAction:
    return EntityAction(
        kind=kind,  # type: ignore[arg-type]
        name="Acme Corp",
        entity_type="company",
        tags=[],
        access="internal",
        existing_uid="a1b2c3d4" if kind == "update" else None,
        observations="Acme raised Series C in Q1 2024.",
    )


def _reasoning_proposal() -> tuple[ReasoningProposal, tuple[BoundedSourceView, ...]]:
    proposal = ReasoningProposal(
        proposal_id="p1", merge_target_name="merged-entity", sources=("a.md",)
    )
    views = (
        BoundedSourceView(
            path="a.md", title="A", frontmatter={}, body_excerpt="hello world"
        ),
    )
    return proposal, views


def _mock_client(payload_text: str = '{"ok": true}') -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(
        input_tokens=1,
        output_tokens=1,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# The full set of params-builders / call sites under test, each yielding a
# (label, params_dict) pair so the cross-cutting assertions below can iterate
# every call site uniformly.
# ---------------------------------------------------------------------------


def _all_direct_builder_params() -> dict[str, dict]:
    """Call sites with a standalone ``*_params`` / ``build_*_request_params``
    builder function — these can be invoked directly with no mocked client."""
    raw = _make_raw("Some rich source text with entities.")
    create_action = _entity_action("create")
    update_action = _entity_action("update")
    proposal, views = _reasoning_proposal()

    return {
        "tiers.tier2_request_params": tier2_request_params(
            raw, [], ["person", "reference"], [], ["internal"]
        ),
        "tiers.tier3_create_params": tier3_create_params(create_action, "sessions/raw.md"),
        "tiers.tier3_merge_params": tier3_merge_params(
            update_action, "Existing body.", "sessions/raw.md"
        ),
        "tiers.tier3_merge_full_params": tier3_merge_full_params(
            update_action, "Existing body.", "sessions/raw.md"
        ),
        "reasoning_tiers.build_t1_request_params": build_t1_request_params(
            proposal, views
        ),
        "reasoning_tiers.build_t2_request_params": build_t2_request_params(
            proposal, views
        ),
    }


def _write_am(scope_dir: Path, filename: str, body: str) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: probe\ntype: feedback\n---\n" + body + "\n", encoding="utf-8"
    )
    return AutoMemoryFile(
        path=path, origin_scope="scope-x", memory_type="feedback", name="probe"
    )


#: A process-lifetime temp dir for the AutoMemoryFile fixtures below. These
#: are read-only probe files (never written to by the code under test — the
#: contradiction detector only reads member bodies), so a single shared dir
#: created once at import time is safe and avoids needing a pytest tmp_path
#: fixture inside a module-level helper that parametrize() calls at collection
#: time.
_FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="athenaeum-thinking-seam-"))


def _all_inline_call_site_params() -> dict[str, dict]:
    """Call sites with NO standalone builder — the params dict is built
    inline inside ``client.messages.create(...)``. Captured via a mocked
    client's ``call_args.kwargs``."""
    params: dict[str, dict] = {}

    client = _mock_client('{"claim_kind": "fact"}')
    classify_claim_kind("The develop tip is abc123.", client)
    params["claim_kind.classify_claim_kind"] = dict(client.messages.create.call_args.kwargs)

    client = _mock_client('{"detected": false}')
    scope = _FIXTURE_DIR / "scope"
    members = [
        _write_am(scope, "feedback_a.md", "Always commit directly to develop."),
        _write_am(scope, "feedback_b.md", "Never commit directly to develop."),
    ]
    detect_contradictions(members, client)
    params["contradictions.detect_contradictions"] = dict(
        client.messages.create.call_args.kwargs
    )

    return params


def _all_params() -> dict[str, dict]:
    merged = {}
    merged.update(_all_direct_builder_params())
    merged.update(_all_inline_call_site_params())
    return merged


# ---------------------------------------------------------------------------
# 1. No call site ever sends budget_tokens.
# ---------------------------------------------------------------------------


class TestNoBudgetTokensAnywhere:
    @pytest.mark.parametrize("label", sorted(_all_params()))
    def test_no_budget_tokens_key(self, label: str) -> None:
        params = _all_params()[label]
        assert "budget_tokens" not in params, label
        # Also assert it never sneaks in nested under "thinking".
        assert "budget_tokens" not in params.get("thinking", {}), label

    def test_no_budget_tokens_substring_in_serialized_params(self) -> None:
        # Belt-and-suspenders: the literal string must not appear anywhere in
        # any call site's built params, not just as a top-level dict key.
        for label, params in _all_params().items():
            rendered = json.dumps(params, default=str)
            assert "budget_tokens" not in rendered, label


# ---------------------------------------------------------------------------
# 2. Every call site's params dict carries an explicit "thinking" key with
#    the correct per-stage posture.
# ---------------------------------------------------------------------------

# label -> expected thinking type ("adaptive" | "disabled")
_EXPECTED_THINKING_TYPE = {
    # classify-tier (Haiku): disabled — cheap/fast classification does not
    # benefit from thinking.
    "tiers.tier2_request_params": "disabled",
    "claim_kind.classify_claim_kind": "disabled",
    "contradictions.detect_contradictions": "disabled",
    "reasoning_tiers.build_t1_request_params": "disabled",
    # write/resolve-tier (bumped to Sonnet 5 / Opus 5 under athenaeum#580): adaptive —
    # these stages do genuine drafting/reasoning work.
    "tiers.tier3_create_params": "adaptive",
    "tiers.tier3_merge_params": "adaptive",
    "tiers.tier3_merge_full_params": "adaptive",
    # T2 already runs Opus-tier deep reasoning today.
    "reasoning_tiers.build_t2_request_params": "adaptive",
}


class TestThinkingKeyPresentPerStage:
    @pytest.mark.parametrize("label", sorted(_EXPECTED_THINKING_TYPE))
    def test_thinking_key_present_with_expected_posture(self, label: str) -> None:
        params = _all_params()[label]
        assert "thinking" in params, f"{label} sent no explicit thinking value"
        assert params["thinking"] is not None, label
        assert params["thinking"] == {"type": _EXPECTED_THINKING_TYPE[label]}, label

    def test_query_topics_hot_path_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # query_topics builds its params inline behind a lazy import of
        # anthropic; exercise it directly rather than through the shared
        # _all_params() helper (it needs its own client construction path).
        import anthropic

        from athenaeum import query_topics
        from tests.conftest import FakeLLMClient

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        fake = FakeLLMClient(text='["topic one", "topic two"]')
        monkeypatch.setattr(anthropic, "Anthropic", fake)

        query_topics.extract_topics("Tell me about the Q1 roadmap review.")
        assert fake.calls[0]["thinking"] == {"type": "disabled"}

    def test_every_registered_stage_has_a_declared_posture(self) -> None:
        # No stage in _all_params() is missing from the expectation table —
        # catches a future call site added without a thinking value.
        for label in _all_params():
            assert label in _EXPECTED_THINKING_TYPE, (
                f"{label} has no declared expected thinking posture in this test"
            )


# ---------------------------------------------------------------------------
# 3. max_tokens re-baseline never shrinks a pre-athenaeum#578 budget.
# ---------------------------------------------------------------------------


class TestMaxTokensNeverShrinks:
    def test_resolver_and_merge_patch_are_raised(self) -> None:
        # The two stages issue athenaeum#578 explicitly flagged as tightest / highest
        # risk must have gone UP, not just "not down".
        from athenaeum.resolutions import _RESOLVER_MAX_TOKENS

        assert _RESOLVER_MAX_TOKENS > _PRE_578_MAX_TOKENS["resolve"]
        assert _MERGE_PATCH_MAX_TOKENS > _PRE_578_MAX_TOKENS["merge_patch"]

    @pytest.mark.parametrize(
        "label,current,pre_578_key",
        [
            ("tiers.tier2_request_params", "max_tokens", "classify"),
            ("tiers.tier3_create_params", "max_tokens", "merge_create"),
            ("tiers.tier3_merge_params", "max_tokens", "merge_patch"),
            ("tiers.tier3_merge_full_params", "max_tokens", "merge_full"),
            ("claim_kind.classify_claim_kind", "max_tokens", "claim_kind"),
            (
                "contradictions.detect_contradictions",
                "max_tokens",
                "contradiction_detect",
            ),
            ("reasoning_tiers.build_t1_request_params", "max_tokens", "reasoning_t1"),
            ("reasoning_tiers.build_t2_request_params", "max_tokens", "reasoning_t2"),
        ],
    )
    def test_stage_max_tokens_at_least_pre_578_value(
        self, label: str, current: str, pre_578_key: str
    ) -> None:
        params = _all_params()[label]
        assert params[current] >= _PRE_578_MAX_TOKENS[pre_578_key], label

    def test_resolver_max_tokens_ge_pre_578(self) -> None:
        from athenaeum.resolutions import _RESOLVER_MAX_TOKENS

        assert _RESOLVER_MAX_TOKENS >= _PRE_578_MAX_TOKENS["resolve"]

    def test_freetext_edit_max_tokens_ge_pre_578(self) -> None:
        from athenaeum.resolutions import _FREETEXT_EDIT_MAX_TOKENS

        assert _FREETEXT_EDIT_MAX_TOKENS >= _PRE_578_MAX_TOKENS["freetext_edit"]

    def test_named_constants_ge_pre_578(self) -> None:
        assert _MERGE_MAX_TOKENS >= _PRE_578_MAX_TOKENS["merge_full"]
        assert _MERGE_PATCH_MAX_TOKENS >= _PRE_578_MAX_TOKENS["merge_patch"]
        assert _TIER3_CREATE_MAX_TOKENS >= _PRE_578_MAX_TOKENS["merge_create"]
        assert _TIER2_CLASSIFY_MAX_TOKENS >= _PRE_578_MAX_TOKENS["classify"]
        assert _CLAIM_KIND_MAX_TOKENS >= _PRE_578_MAX_TOKENS["claim_kind"]
        assert _DETECT_MAX_TOKENS >= _PRE_578_MAX_TOKENS["contradiction_detect"]
        assert _TOPIC_MAX_TOKENS >= _PRE_578_MAX_TOKENS["topic"]
        assert _T1_MAX_TOKENS >= _PRE_578_MAX_TOKENS["reasoning_t1"]
        assert _T2_MAX_TOKENS >= _PRE_578_MAX_TOKENS["reasoning_t2"]


# ---------------------------------------------------------------------------
# 4. No stage combines output_config.effort xhigh/max with thinking:disabled.
# ---------------------------------------------------------------------------


class TestNoXhighOrMaxWithDisabledThinking:
    @pytest.mark.parametrize("label", sorted(_all_params()))
    def test_effort_and_thinking_are_compatible(self, label: str) -> None:
        params = _all_params()[label]
        output_config = params.get("output_config")
        effort = None
        if isinstance(output_config, dict):
            effort = output_config.get("effort")
        thinking = params.get("thinking") or {}
        if effort in ("xhigh", "max"):
            assert thinking.get("type") != "disabled", (
                f"{label} sets effort={effort!r} with thinking disabled — "
                "400s on Opus 5"
            )

    def test_no_call_site_sets_output_config_at_all_today(self) -> None:
        # Today no stage sets output_config.effort (out of scope per issue
        # athenaeum#578 — that's a separate future tuning pass). This test documents
        # the current state so the guard above stays meaningful: if this
        # ever flips true, the xhigh/max guard above is what protects us.
        for label, params in _all_params().items():
            assert "output_config" not in params, label


# ---------------------------------------------------------------------------
# 5. claude-cli (honors_max_tokens=False) still gets an explicit thinking key.
# ---------------------------------------------------------------------------


class TestClaudeCliHonorsMaxTokensFalseStillGetsThinking:
    """The claude-cli backend drops max_tokens (ProviderCapabilities.
    honors_max_tokens is False) and strips cache_control — but ``thinking``
    is a model-facing param, not a transport-level one, so the params dict
    built for that backend must still carry the same explicit thinking key
    as the api backend. The CLI adapter itself doesn't forward ``thinking``
    to the ``claude -p`` invocation (no CLI equivalent), but the CONTRACT
    under test here is at the params-dict-construction layer shared by both
    backends — call sites must not special-case thinking away when the CLI
    backend is selected."""

    _CLI_CONFIG = {"llm": {"provider": "claude-cli"}}

    def test_tier2_request_params_on_cli_config_still_has_thinking(self) -> None:
        raw = _make_raw("Some rich source text with entities.")
        params = tier2_request_params(
            raw, [], ["person", "reference"], [], ["internal"], config=self._CLI_CONFIG
        )
        assert params["thinking"] == {"type": "disabled"}
        # max_tokens is still RESOLVED into the dict (the CLI adapter is what
        # drops it later, at _create() time) — the builder itself is
        # provider-agnostic.
        assert "max_tokens" in params

    def test_tier3_merge_params_on_cli_config_still_has_thinking(self) -> None:
        action = _entity_action("update")
        params = tier3_merge_params(
            action, "Existing body.", "sessions/raw.md", config=self._CLI_CONFIG
        )
        assert params["thinking"] == {"type": "adaptive"}

    def test_capabilities_for_cli_is_still_max_tokens_false(self) -> None:
        # Sanity check the premise: this is genuinely the honors_max_tokens
        # =False branch under test, not an accidental api-backend run.
        from athenaeum.provider import capabilities_for

        assert capabilities_for("claude-cli").honors_max_tokens is False

    def test_cli_adapter_drops_max_tokens_but_thinking_key_reaches__create(
        self,
    ) -> None:
        # End-to-end through the real ClaudeCliClient._create: thinking is
        # present in the params handed to _create (even though the adapter
        # has no CLI flag for it and does not forward it into argv/stdin —
        # documented in provider.py as "no CLI equivalent" for max_tokens;
        # thinking is a param the CLI's own `claude -p` invocation does not
        # expose either way). This test pins that _create() does not choke
        # on receiving it and the call still completes.
        import json as _json
        from unittest.mock import patch

        from athenaeum.provider import ClaudeCliClient

        cli = ClaudeCliClient(binary="claude")
        envelope = _json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        )
        with (
            patch("athenaeum.provider.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "athenaeum.provider.subprocess.run",
                return_value=MagicMock(returncode=0, stdout=envelope, stderr=""),
            ),
        ):
            response = cli.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4096,
                thinking={"type": "disabled"},
                system="sys",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert response.content[0].text == "ok"


# ---------------------------------------------------------------------------
# 6. REGRESSION: adaptive thinking is production-safe (issue athenaeum#578).
#
# On the CURRENT Opus 4.7 / Sonnet 4.6 defaults (not just a future Opus 5 /
# Sonnet 5 bump), a stage that sets ``thinking: {"type": "adaptive"}`` receives
# a response whose FIRST content block is a ``type == "thinking"`` block —
# which precedes the text block and (with ``display`` omitted) carries empty
# text and, on the anthropic SDK, has no ``.text`` attribute at all. A bare
# ``response.content[0].text`` would then raise AttributeError / read the wrong
# block in production. These tests would FAIL against the old
# ``content[0].text`` code and PASS with ``response_text``.
# ---------------------------------------------------------------------------


def _sdk_thinking_block(thinking_text: str = "") -> SimpleNamespace:
    """A SimpleNamespace mimicking the anthropic SDK ThinkingBlock: ``type`` is
    the literal ``"thinking"`` and there is NO ``.text`` attribute (accessing it
    raises AttributeError, exactly like the real block with display omitted)."""
    return SimpleNamespace(type="thinking", thinking=thinking_text)


def _sdk_redacted_thinking_block() -> SimpleNamespace:
    return SimpleNamespace(type="redacted_thinking", data="opaque")


def _sdk_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _adaptive_response(answer: str, *, thinking_text: str = "") -> SimpleNamespace:
    """A response whose content is [thinking_block, text_block] — the shape an
    adaptive-thinking call returns on Opus 4.7 / Sonnet 4.6."""
    return SimpleNamespace(
        content=[_sdk_thinking_block(thinking_text), _sdk_text_block(answer)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class TestResponseTextSkipsThinkingBlocks:
    def test_thinking_block_first_then_text(self) -> None:
        resp = _adaptive_response('{"answer": "real"}', thinking_text="reasoning...")
        assert response_text(resp) == '{"answer": "real"}'

    def test_empty_thinking_text_display_omitted(self) -> None:
        # display omitted: the thinking block carries empty text but is still a
        # type=="thinking" block ahead of the real answer.
        resp = _adaptive_response("THE ANSWER", thinking_text="")
        assert response_text(resp) == "THE ANSWER"

    def test_the_old_content0_text_would_have_broken(self) -> None:
        # Pin the exact bug the helper fixes: a bare content[0].text on this
        # SDK-shaped response raises AttributeError (the thinking block has no
        # .text) — so the old code path is genuinely broken, not merely
        # wrong-block.
        resp = _adaptive_response('{"answer": "real"}')
        with pytest.raises(AttributeError):
            _ = resp.content[0].text
        # ...but response_text returns the real answer.
        assert response_text(resp) == '{"answer": "real"}'

    def test_multiple_thinking_blocks_then_text(self) -> None:
        resp = SimpleNamespace(
            content=[
                _sdk_thinking_block("step 1"),
                _sdk_redacted_thinking_block(),
                _sdk_text_block("final"),
            ]
        )
        assert response_text(resp) == "final"

    def test_text_only_response_unchanged(self) -> None:
        # No thinking block (disabled posture / CLI): behaves exactly like the
        # old content[0].text.
        resp = SimpleNamespace(content=[_sdk_text_block("plain")])
        assert response_text(resp) == "plain"

    def test_single_magicmock_block_falls_back(self) -> None:
        # A MagicMock block has no ``type == "text"`` (its .type is a mock), so
        # response_text falls through to content[0].text — preserving the
        # behavior every existing mock-based test relies on.
        block = MagicMock(text="mocked")
        resp = MagicMock(content=[block])
        assert response_text(resp) == "mocked"

    def test_cli_response_single_text_block(self) -> None:
        # The claude-cli backend's constructed response: its single block has
        # type == "text", so response_text returns it directly.
        from athenaeum.provider import _CliResponse, _CliTextBlock, _CliUsage

        resp = _CliResponse(content=[_CliTextBlock(text="cli answer")], usage=_CliUsage())
        assert response_text(resp) == "cli answer"

    def test_no_text_block_falls_back_and_raises_on_thinking_only(self) -> None:
        # Degenerate: a response with ONLY a thinking block (no text answer at
        # all) falls back to content[0].text, surfacing the same AttributeError
        # the call sites already catch — the fallback preserves the existing
        # malformed-response error contract rather than masking it.
        resp = SimpleNamespace(content=[_sdk_thinking_block("only thinking")])
        with pytest.raises(AttributeError):
            response_text(resp)


class TestAdaptiveCallSiteEndToEndSkipsThinking:
    """End-to-end proof through a real adaptive call site: the resolver. Its
    fake client returns a [thinking_block, text_block] response, and the parsed
    proposal must reflect the TEXT block's JSON, never the thinking block."""

    def _members(self, tmp_path: Path):
        from athenaeum.contradictions import ContradictionResult

        scope = tmp_path / "scope"
        scope.mkdir(parents=True, exist_ok=True)

        def _write(name: str, body: str) -> AutoMemoryFile:
            path = scope / name
            path.write_text(
                "---\nname: probe\ntype: feedback\n---\n" + body + "\n",
                encoding="utf-8",
            )
            return AutoMemoryFile(
                path=path, origin_scope="scope-x", memory_type="feedback", name="probe"
            )

        a = _write("a.md", "Alice is German.")
        b = _write("b.md", "Alice is not German.")
        detector = ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=["scope-x/a.md", "scope-x/b.md"],
            conflicting_passages=["German.", "Not German."],
            rationale="conflict",
        )
        return detector, [a, b]

    def test_resolver_reads_text_block_not_thinking_block(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.resolutions import propose_resolution

        detector, members = self._members(tmp_path)

        answer = (
            '{"recommended_winner": "b", "action": "keep_b", '
            '"confidence": 0.92, "rationale": "newer + higher precedence", '
            '"source_precedence_used": []}'
        )
        # The fake client returns a thinking block FIRST (as Opus 4.7 / Sonnet
        # 4.6 do under adaptive thinking), then the real JSON text block.
        client = MagicMock()
        client.messages.create.return_value = _adaptive_response(
            answer, thinking_text="Weighing precedence and recency..."
        )

        proposal = propose_resolution(detector, members, client)

        # The resolver parsed the TEXT block's JSON, not the (empty/reasoning)
        # thinking block — a content[0].text read would have raised
        # AttributeError and degraded to the resolver-malformed fallback.
        assert proposal.action == "keep_b"
        assert proposal.recommended_winner == "b"
        assert proposal.confidence == pytest.approx(0.92)

    def test_resolver_would_degrade_without_helper(self, tmp_path: Path) -> None:
        # Control that pins WHY this matters: feeding the resolver a response
        # whose content[0] is a thinking block, a bare content[0].text read
        # would raise AttributeError -> the resolver's malformed-response
        # fallback. With response_text the real proposal is recovered (asserted
        # above). Here we just confirm the resolver does NOT crash outright and
        # returns a usable proposal — i.e. the AttributeError is handled by
        # extracting the text block, not by the degrade path.
        from athenaeum.resolutions import propose_resolution

        detector, members = self._members(tmp_path)
        client = MagicMock()
        client.messages.create.return_value = _adaptive_response(
            '{"recommended_winner": "a", "action": "keep_a", '
            '"confidence": 0.8, "rationale": "r", "source_precedence_used": []}'
        )
        proposal = propose_resolution(detector, members, client)
        assert proposal.action == "keep_a"
