# SPDX-License-Identifier: Apache-2.0
"""Shared eval harness (issue #331).

Two responsibilities:

1. **Live-API mode** (``pytest -m eval``): call the real Anthropic client,
   score the response against the golden expectation, and — when
   ``--record`` is passed — serialize the raw response body to a fixture
   under ``tests/fixtures/recorded/<layer>/<case_id>.json`` so the replay
   suite can re-exercise the parser without touching the network.

2. **Threshold scoring + JSON summary**: eval assertions are aggregate
   ("detector ≥ 8/10"), not per-case, so single-case model
   nondeterminism does not flake main. Per-case results + total
   ``TokenUsage`` land in ``eval-summary.json`` at the end of the session
   (workflow artifact).

3. **Prompt-hash staleness contract**: every recorded fixture stores a
   sha256 of the canonicalised prompt (system + user messages + model).
   :func:`replay_client` re-computes it at replay time; a mismatch fails
   loudly with "fixture stale — re-run evals with --record", so a prompt
   edit cannot silently keep testing against responses to the old prompt.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from athenaeum.provider import response_text as provider_response_text

# Hard budget guard — this is the run-level TokenUsage ceiling asserted
# at session end. A future golden set that balloons past this will fail
# the run loudly rather than quietly burning budget. Sized generously
# above expected steady-state so the guard does not fight noise.
EVAL_TOKEN_CEILING = 250_000

# Layer directory names — used to look up fixtures and to slice the
# eval-summary JSON.
LAYER_DETECTOR = "detector"
LAYER_RESOLVER = "resolver"
LAYER_RECALL = "recall"
LAYER_BACKFILL = "backfill"
# Issue #552: the two additional layers covering tiers.py's CLASSIFY and
# WRITE/MERGE stages (see docs/evals-inventory.md for the inventory this
# fills in).
LAYER_CLASSIFY = "classify"
LAYER_MERGE = "merge"

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DATA_ROOT = REPO_ROOT / "tests" / "evals" / "data"
RECORDED_ROOT = REPO_ROOT / "tests" / "fixtures" / "recorded"


# ---------------------------------------------------------------------------
# Prompt-hash canonicalisation
# ---------------------------------------------------------------------------


def _text_of_system(system: Any) -> str:
    """Flatten ``system`` to a stable text form for hashing.

    Handles the two shapes the SDK accepts: a plain string, or a list of
    ``{"type": "text", "text": ..., ...}`` blocks (resolver uses this to
    attach a ``cache_control`` marker). ``cache_control`` metadata is
    IGNORED — it is a delivery-time optimisation, not a semantic input.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(system)


def _text_of_messages(messages: Any) -> str:
    """Flatten ``messages`` to a stable text form for hashing."""
    if not isinstance(messages, list):
        return str(messages or "")
    parts: list[str] = []
    for msg in messages:
        role = ""
        content: Any = ""
        if isinstance(msg, dict):
            role = str(msg.get("role", ""))
            content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(f"{role}: {block.get('text', '')}")
                elif isinstance(block, str):
                    parts.append(f"{role}: {block}")
        else:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def prompt_hash(model: str, system: Any, messages: Any) -> str:
    """Return a stable sha256 hash for a (model, system, messages) triple.

    The hash is the staleness contract: if any of the three change, the
    corresponding recorded fixture is stale and must be re-recorded.
    """
    canonical = "\n---\n".join(
        (
            f"model:{model}",
            _text_of_system(system),
            _text_of_messages(messages),
        )
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fixture I/O
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RecordedResponse:
    """Serialised Anthropic response body + provenance.

    ``content_blocks`` records the ORDERED ``response.content`` block sequence
    (issue #684): each entry is ``{"type": "thinking"|"text"|…}`` plus a
    ``"text"`` key on blocks that carry one. It exists so the replay stub can
    reconstruct the exact block shape production received — a thinking-prefixed
    response has one or more leading ``type == "thinking"`` blocks with no
    ``.text`` before the text block (issue #578). Recording only the extracted
    ``response_text`` (the pre-#684 format) made the replay suite structurally
    incapable of producing that shape, so a regression in
    :func:`athenaeum.provider.response_text`'s block-walking was invisible to
    replay. ``response_text`` is retained as the extracted answer for
    human-readability and backward compatibility.
    """

    case_id: str
    layer: str
    model: str
    prompt_hash: str
    response_text: str
    usage: dict[str, int]
    recorded_at: str
    content_blocks: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RecordedResponse":
        response_text = str(payload["response_text"])
        raw_blocks = payload.get("content_blocks")
        if raw_blocks:
            content_blocks = [dict(block) for block in raw_blocks]
        else:
            # Backward compatibility: a pre-#684 fixture carried only the
            # extracted text. Synthesise a single text block so replay still
            # reconstructs a faithful (single-block) response.
            content_blocks = [{"type": "text", "text": response_text}]
        return cls(
            case_id=str(payload["case_id"]),
            layer=str(payload["layer"]),
            model=str(payload["model"]),
            prompt_hash=str(payload["prompt_hash"]),
            response_text=response_text,
            usage={k: int(v) for k, v in dict(payload.get("usage", {})).items()},
            recorded_at=str(payload.get("recorded_at", "")),
            content_blocks=content_blocks,
        )


def recorded_path(layer: str, case_id: str) -> Path:
    """Return the on-disk fixture path for a (layer, case_id) pair."""
    return RECORDED_ROOT / layer / f"{case_id}.json"


def load_recorded(layer: str, case_id: str) -> RecordedResponse:
    """Load one recorded fixture; raises if the file is missing."""
    path = recorded_path(layer, case_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"recorded fixture missing: {path.relative_to(REPO_ROOT)} — "
            "run evals with --record to seed it"
        )
    return RecordedResponse.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_recorded(rec: RecordedResponse) -> None:
    """Serialise ``rec`` to disk, creating the layer directory as needed."""
    path = recorded_path(rec.layer, rec.case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec.to_json(), indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Live client wrapping + recording
# ---------------------------------------------------------------------------


def _cache_usage_from_response(response: Any) -> dict[str, int]:
    """Extract the four token counters used elsewhere in athenaeum."""
    usage = getattr(response, "usage", None)

    def _get(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {
        "input_tokens": _get("input_tokens"),
        "output_tokens": _get("output_tokens"),
        "cache_creation_input_tokens": _get("cache_creation_input_tokens"),
        "cache_read_input_tokens": _get("cache_read_input_tokens"),
    }


def _blocks_of_response(response: Any) -> list[dict[str, Any]]:
    """Serialise ``response.content`` into an ordered ``[{type, text?}]`` list.

    Captures the block SEQUENCE so replay can reconstruct a thinking-prefixed
    response faithfully (issue #684). A block's ``.type`` is recorded when it is
    a string; its ``.text`` is recorded only when present (a ``ThinkingBlock``
    has none), mirroring the anthropic SDK shape the call sites parse.
    """
    blocks: list[dict[str, Any]] = []
    for block in getattr(response, "content", None) or []:
        entry: dict[str, Any] = {}
        btype = getattr(block, "type", None)
        if isinstance(btype, str):
            entry["type"] = btype
        text = getattr(block, "text", None)
        if isinstance(text, str):
            entry["text"] = text
        blocks.append(entry)
    return blocks


class EmptyRecordingError(BaseException):
    """Raised when a ``record=true`` run captures a response with no usable text.

    The pre-#684 recorder swallowed ``AttributeError``/``IndexError`` from a
    bare ``response.content[0].text`` and wrote ``response_text=""`` to disk — a
    silently-empty fixture that passed every check between the API response and
    replay (6 of 8 resolver fixtures, issue #684). A recording that captured no
    text is a FAILED recording: it must go red at record time, not surface weeks
    later at replay.

    Inherits from :class:`BaseException` — not :class:`Exception` — for the same
    reason as :class:`FixtureStaleError`: the ``except Exception`` fallbacks in
    :func:`athenaeum.resolutions.propose_resolution` /
    :func:`athenaeum.contradictions.detect_contradictions` (which wrap the very
    ``messages.create`` call the recorder hooks) must NOT swallow it and let the
    run pass down a fallback path.
    """


class RecordingClient:
    """Wraps a real Anthropic client to persist responses as fixtures.

    Only the ``messages.create`` slice used by the athenaeum call sites
    (:mod:`athenaeum.contradictions`, :mod:`athenaeum.resolutions`,
    :mod:`athenaeum.query_topics`) is exposed. All keyword args pass
    through byte-for-byte so prompt caching / cache_control / retries
    behave identically to a bare client.

    Recording activates ONLY when the surrounding harness has stamped the
    thread-local ``_pending_case_id`` — call sites go through
    :func:`recording_call_context`.
    """

    def __init__(self, inner: Any, record: bool, layer: str) -> None:
        self._inner = inner
        self._record = record
        self._layer = layer
        self._pending_case: str | None = None
        self.messages = _RecordingMessages(self)

    def start_case(self, case_id: str) -> None:
        self._pending_case = case_id

    def end_case(self) -> None:
        self._pending_case = None


class _RecordingMessages:
    def __init__(self, parent: RecordingClient) -> None:
        self._parent = parent

    def create(self, **params: Any) -> Any:
        response = self._parent._inner.messages.create(**params)
        if self._parent._record and self._parent._pending_case is not None:
            case_id = self._parent._pending_case
            layer = self._parent._layer
            # Extract via the ONE shared walker production uses — never a second
            # copy of "where is the text in a response" (issue #684). It skips
            # any leading thinking block (#578); a bare content[0].text would
            # read the wrong block on a thinking-enabled stage (resolver).
            try:
                text = provider_response_text(response)
            except (AttributeError, IndexError) as exc:
                raise EmptyRecordingError(
                    f"record run captured no text block for "
                    f"{layer}/{case_id} (model {params.get('model', '')!r}); "
                    "the response has no type=='text' block — re-run the eval, "
                    "do not commit an empty fixture"
                ) from exc
            # A recording that captured nothing is a FAILED recording — raise so
            # the eval run goes red now, rather than writing "" and surfacing at
            # replay weeks later (the actual defect behind issue #684).
            if not text.strip():
                raise EmptyRecordingError(
                    f"record run captured empty text for {layer}/{case_id} "
                    f"(model {params.get('model', '')!r}); output_tokens="
                    f"{_cache_usage_from_response(response).get('output_tokens', 0)}"
                    " — re-run the eval, do not commit an empty fixture"
                )
            rec = RecordedResponse(
                case_id=case_id,
                layer=layer,
                model=str(params.get("model", "")),
                prompt_hash=prompt_hash(
                    str(params.get("model", "")),
                    params.get("system"),
                    params.get("messages"),
                ),
                response_text=text,
                usage=_cache_usage_from_response(response),
                recorded_at=datetime.now(timezone.utc).isoformat(),
                content_blocks=_blocks_of_response(response),
            )
            save_recorded(rec)
        return response


# ---------------------------------------------------------------------------
# Replay client (used by regular CI, no network)
# ---------------------------------------------------------------------------


class FixtureStaleError(BaseException):
    """Raised when a replay test's live prompt hash disagrees with the
    fixture's stored hash. The stored response was produced against a
    different prompt, so its shape / phrasing may not exercise the current
    parser. Re-record to fix.

    Deliberately inherits from :class:`BaseException` — NOT :class:`Exception`
    — so the ``except Exception`` guards in
    :func:`athenaeum.contradictions.detect_contradictions` and
    :func:`athenaeum.resolutions.propose_resolution` (which exist to fall back
    to ``detected=False`` / the deterministic fallback on real API errors) do
    NOT swallow it. A stale fixture is a test-only condition; letting it be
    caught by production error paths would silently degrade every replay
    test to a fallback-path pass instead of surfacing the drift.
    """


class _ReplayBlock:
    """A single reconstructed ``response.content`` block for replay.

    Carries ``.type`` and, only when the recorded block had one, ``.text`` —
    so a recorded ``{"type": "thinking"}`` block reconstructs WITHOUT a
    ``.text`` attribute (accessing it raises ``AttributeError``), exactly like
    the anthropic ``ThinkingBlock`` production receives. This is what lets the
    replay suite exercise :func:`athenaeum.provider.response_text`'s
    skip-thinking block-walk instead of a synthetic single text block that
    could never reproduce the shape (issue #684).
    """

    def __init__(self, block: dict[str, Any]) -> None:
        self.type = block.get("type")
        if "text" in block:
            self.text = block["text"]


def replay_client(layer: str, case_id: str) -> Any:
    """Build a stub client that returns a recorded response and enforces
    the staleness contract on the (model, system, messages) it is called
    with. The stub mirrors the ``anthropic.Anthropic`` slice the call
    sites use — ``client.messages.create(**params)`` returns an object whose
    ``.content`` is the RECORDED block sequence (thinking blocks included, in
    order) and whose ``.usage`` mirrors the fixture, so the same
    :func:`athenaeum.provider.response_text` walk production uses is exercised
    (issue #684).
    """
    rec = load_recorded(layer, case_id)

    def _create(**params: Any) -> Any:
        seen = prompt_hash(
            str(params.get("model", "")),
            params.get("system"),
            params.get("messages"),
        )
        if seen != rec.prompt_hash:
            raise FixtureStaleError(
                f"fixture stale — re-run evals with --record: "
                f"{recorded_path(layer, case_id).relative_to(REPO_ROOT)} "
                f"(fixture prompt-hash {rec.prompt_hash} != current {seen})"
            )
        response = MagicMock()
        response.content = [_ReplayBlock(block) for block in rec.content_blocks]
        response.usage = MagicMock(**rec.usage)
        return response

    client = MagicMock()
    client.messages.create.side_effect = _create
    return client


# ---------------------------------------------------------------------------
# Per-session eval accumulator + JSON summary
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CaseResult:
    layer: str
    case_id: str
    expected: str
    observed: str
    passed: bool
    detail: str = ""


class EvalSession:
    """Per-session accumulator for case results + token usage.

    Case results are appended by the eval tests; the session emits an
    aggregate ``eval-summary.json`` at teardown (see
    :func:`emit_summary`). Threshold assertions (``detector ≥ 8/10``,
    ``resolver ≥ 4/5``, ``recall ≥ 5/6``) fire per-layer via
    :func:`assert_layer_floor`.
    """

    def __init__(self) -> None:
        self.results: list[CaseResult] = []
        # Token counters are updated by the harness's response wrappers
        # (see :meth:`observe_response`) so every live call — from any
        # layer — folds into the same run-level total for the budget guard.
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.per_model: dict[str, dict[str, int]] = {}

    def observe_response(self, model: str, response: Any) -> None:
        counts = _cache_usage_from_response(response)
        self.input_tokens += counts["input_tokens"]
        self.output_tokens += counts["output_tokens"]
        self.cache_creation_input_tokens += counts["cache_creation_input_tokens"]
        self.cache_read_input_tokens += counts["cache_read_input_tokens"]
        bucket = self.per_model.setdefault(
            model,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        for key, value in counts.items():
            bucket[key] += value

    def record_case(
        self,
        layer: str,
        case_id: str,
        *,
        expected: str,
        observed: str,
        passed: bool,
        detail: str = "",
    ) -> None:
        self.results.append(
            CaseResult(
                layer=layer,
                case_id=case_id,
                expected=expected,
                observed=observed,
                passed=passed,
                detail=detail,
            )
        )

    def layer_score(self, layer: str) -> tuple[int, int]:
        cases = [r for r in self.results if r.layer == layer]
        return sum(1 for r in cases if r.passed), len(cases)

    def emit_summary(self, path: Path) -> None:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "layer_scores": {
                layer: {"passed": passed, "total": total}
                for layer, (passed, total) in (
                    (layer, self.layer_score(layer))
                    for layer in (
                        LAYER_DETECTOR,
                        LAYER_RESOLVER,
                        LAYER_RECALL,
                        LAYER_BACKFILL,
                        LAYER_CLASSIFY,
                        LAYER_MERGE,
                    )
                )
                if total
            },
            "token_usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "per_model": self.per_model,
            },
            "cases": [dataclasses.asdict(r) for r in self.results],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Live-mode gating
# ---------------------------------------------------------------------------


def build_live_client() -> Any:
    """Construct the LLM client for eval runs (issue #331 provider seam).

    Routes through :func:`athenaeum.provider.build_llm_client` so the
    ``ATHENAEUM_LLM_PROVIDER=claude-cli`` env var makes a local eval run
    subscription-covered ($0 metered) — the same seam #330 wired for the
    production call sites. CI (``evals.yml``) runs on the ``api`` backend
    with ``ANTHROPIC_API_KEY`` sourced from 1Password.
    """
    from athenaeum.provider import build_llm_client

    client = build_llm_client(None)
    if client is None:
        raise RuntimeError(
            "no LLM backend available — set ANTHROPIC_API_KEY (api backend) "
            "or ATHENAEUM_LLM_PROVIDER=claude-cli (subscription backend)"
        )
    return client


def live_ready() -> tuple[bool, str]:
    """Return ``(ok, reason)`` for whether live-API calls can proceed.

    Probes :func:`build_live_client` — succeeds when either
    ``ANTHROPIC_API_KEY`` is set (api backend) or the ``claude-cli``
    provider is selected. Eval tests use this to skip cleanly (rather
    than error) when a contributor runs ``pytest -m eval`` locally
    without either configured.
    """
    try:
        build_live_client()
    except (RuntimeError, Exception) as exc:  # noqa: BLE001 — skip on any construction failure
        return False, str(exc)
    return True, ""
