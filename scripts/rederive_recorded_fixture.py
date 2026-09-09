#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Mechanically re-derive recorded eval fixtures after a pure vocabulary rename
(issue athenaeum#1496).

**The problem.** ``tests/fixtures/recorded/<layer>/<case_id>.json`` stores a
sha256 ``prompt_hash`` over the exact (model, system, messages) triple a live
recording sent (:func:`tests.evals.harness.prompt_hash`). Replay
(:func:`tests.evals.harness.replay_client`) recomputes that hash from the
CURRENT prompt and raises ``FixtureStaleError`` on any mismatch — the
staleness contract that stops a prompt edit from silently testing against a
response to a different prompt.

Renaming case text (e.g. a fictional company name that turned out to collide
with a real one) is exactly such a prompt edit: the fixture goes stale, and
this repo has no live API key to re-record for real (see
``tests/evals/README.md``).

**Why mechanical re-derivation is legitimate here, and only here.** A pure
find-and-replace rename does not change what the case is ABOUT — it changes
only which literal string spells a name that was always meant to be
invented. The response text a live model produced against the renamed
prompt would, with overwhelming likelihood, be the SAME response with the
same substitution applied (the model is not reasoning about the name itself,
just echoing/discussing content that happens to contain it). So instead of
guessing at the "new" response, this script:

1. Drives the exact call path the corresponding test drives (see
   ``_DRIVERS`` below) with a capturing stub client, to get the prompt the
   CURRENT (renamed) source case actually produces.
2. Applies the INVERSE of the declared rename map to that prompt.
3. Asserts the result hashes to EXACTLY the fixture's stored ``prompt_hash``.
   This is the whole safety property: it proves the only difference between
   the old prompt and the new one is the declared substitution, because any
   other change (added/removed text, reworded prose, a different case
   entirely) would make the reverted prompt hash to something else and this
   assertion would fail.
4. Only on that proof does it update ``prompt_hash`` (to the new prompt's
   hash) and apply the FORWARD rename to ``response_text``/``content_blocks``.

If step 3's equality does not hold, the script REFUSES that case — prints
why and leaves the fixture untouched (still stale, exactly as the staleness
contract intends) — rather than guess. A refusal is not a bug in this tool;
it is the tool correctly declining to vouch for a diff it cannot prove.

**The rename map is an explicit input** (``--rename OLD=NEW``, repeatable),
never a constant baked into this module — this script re-derives against
WHATEVER rename the caller declares, and carries no opinion about what
athenaeum#1496's specific rename should be (that lives in the issue and in
the commit that renamed the source ``cases.yaml``/wiki files).

**Non-injective maps.** If two or more ``OLD`` values share the same ``NEW``
value (athenaeum#1496's map does: "Google Sheets", "Google-Sheets", "Google
Docs" and "gsheets" all become "Tallyfold" — the two real products never
co-occur in one fixture, so collapsing them is safe, but it means a single
global reverse substitution is ambiguous), reverting tries every candidate
origin for the ambiguous token and accepts the case only if EXACTLY ONE
candidate reconstructs the stored hash. Zero matches is a refusal, like any
other; more than one match is also a refusal (genuinely ambiguous — this has
not happened for athenaeum#1496's fixtures, and if it ever does the case needs a
real re-record, not a guess).

**Usage**::

    .venv/bin/python scripts/rederive_recorded_fixture.py \\
        --rename "Meridian's=Thornhollow's" --rename "Meridian=Thornhollow" \\
        --rename "meridian=thornhollow" --rename "Notion=Pagemoor" \\
        --rename "notion=pagemoor" --rename "Google Sheets=Tallyfold" \\
        --rename "Google-Sheets=Tallyfold" --rename "Google Docs=Tallyfold" \\
        --rename "gsheets=tallyfold" --rename "gdocs=tallyfold" \\
        --rename "Heroku=Hostmoor" --rename "heroku=hostmoor" \\
        --rename "Airtable=Latticebase" --rename "airtable=latticebase" \\
        --rename "Zendesk=Ticketwell" --rename "zendesk=ticketwell" \\
        --rename "WeWork=Kingsway Works" --rename "wework=kingsway-works" \\
        --apply

Omit ``--apply`` for a dry run (reports what WOULD change, touches nothing).
Order the ``--rename`` pairs longest-phrase-first when one old value is a
substring of another (not needed for the map above — none overlap).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.evals.harness import (  # noqa: E402
    RECORDED_ROOT,
    RecordedResponse,
    load_recorded,
    prompt_hash,
    save_recorded,
)
from tests.evals.harness import _text_of_messages, _text_of_system  # noqa: E402


# ---------------------------------------------------------------------------
# Capturing stub client — gets the prompt without needing a plausible fake
# response. Raises the instant the outgoing request is visible, so the call
# site never has to complete.
# ---------------------------------------------------------------------------


class _PromptCaptured(BaseException):
    """Carries the captured (model, system, messages). Inherits from
    BaseException, NOT Exception, for the identical reason
    FixtureStaleError/EmptyRecordingError do (see harness.py): the athenaeum
    call sites this script drives (detect_contradictions, propose_resolution,
    query_topics.extract_topics, tier2_classify, tier3_merge) each wrap their
    ``messages.create`` call in ``except Exception`` for real-world API-error
    fallback handling, and that must not swallow this control-flow signal.
    """

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__("prompt captured")
        self.params = params


class _CapturingMessages:
    def create(self, **params: Any) -> Any:
        raise _PromptCaptured(params)


class _CapturingClient:
    def __init__(self) -> None:
        self.messages = _CapturingMessages()


def _capture(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        fn(*args, **kwargs)
    except _PromptCaptured as exc:
        return exc.params
    raise RuntimeError(
        f"{fn!r} never called client.messages.create — the call path "
        "short-circuited before reaching the LLM call (bad case data, or "
        "the call site's own gating skipped it)"
    )


# ---------------------------------------------------------------------------
# Per-layer drivers. Each reuses the SAME helpers the corresponding pytest
# module uses to build its call, so the captured prompt is provably the one
# that module's own test asserts on.
# ---------------------------------------------------------------------------


def _driver_detector(case_id: str, tmp_dir: Path) -> dict[str, Any]:
    from athenaeum.contradictions import detect_contradictions
    from tests.test_recorded_fixtures import LAYER_DETECTOR, _load_golden, _materialise_members

    case = _load_golden(LAYER_DETECTOR)[case_id]
    members = _materialise_members(tmp_dir / f"scope-{case_id}", case)
    return _capture(detect_contradictions, members, _CapturingClient())


def _driver_resolver(case_id: str, tmp_dir: Path) -> dict[str, Any]:
    from athenaeum.resolutions import propose_resolution
    from tests.test_recorded_fixtures import (
        LAYER_RESOLVER,
        _detector_result,
        _load_golden,
        _materialise_members,
    )

    case = _load_golden(LAYER_RESOLVER)[case_id]
    members = _materialise_members(tmp_dir / f"scope-{case_id}", case)
    detector = _detector_result(case, members)
    return _capture(propose_resolution, detector, members, _CapturingClient())


def _driver_recall(case_id: str, tmp_dir: Path) -> dict[str, Any]:
    import anthropic

    from athenaeum.query_topics import extract_topics
    from tests.test_recorded_fixtures import LAYER_RECALL, _load_golden

    case = _load_golden(LAYER_RECALL)[case_id]
    capture = _CapturingClient()
    old_env = os.environ.get("ANTHROPIC_API_KEY")
    old_cls = anthropic.Anthropic
    os.environ["ANTHROPIC_API_KEY"] = "rederive-capture-no-network"
    anthropic.Anthropic = lambda **kw: capture  # type: ignore[assignment]
    try:
        return _capture(extract_topics, case["prompt"], timeout=15.0)
    finally:
        anthropic.Anthropic = old_cls  # type: ignore[assignment]
        if old_env is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = old_env


def _driver_classify(case_id: str, tmp_dir: Path) -> dict[str, Any]:
    from athenaeum.models import TokenUsage
    from athenaeum.tiers import tier2_classify
    from tests.evals.test_classify_eval import _load_cases, _make_raw

    case = {c["id"]: c for c in _load_cases()}[case_id]
    raw = _make_raw(case)
    return _capture(
        tier2_classify,
        raw,
        list(case.get("matched_names") or []),
        list(case["valid_types"]),
        list(case["valid_tags"]),
        list(case["valid_access"]),
        _CapturingClient(),
        usage=TokenUsage(),
    )


def _driver_merge(case_id: str, tmp_dir: Path) -> dict[str, Any]:
    from athenaeum.models import TokenUsage
    from athenaeum.tiers import tier3_merge
    from tests.evals.test_merge_eval import _load_cases, _make_action

    case = {c["id"]: c for c in _load_cases()}[case_id]
    action = _make_action(case)
    return _capture(
        tier3_merge,
        action,
        str(case["existing_body"]),
        str(case["source_ref"]),
        _CapturingClient(),
        usage=TokenUsage(),
    )


_DRIVERS: dict[str, Callable[[str, Path], dict[str, Any]]] = {
    "detector": _driver_detector,
    "resolver": _driver_resolver,
    "recall": _driver_recall,
    "classify": _driver_classify,
    "merge": _driver_merge,
}


# ---------------------------------------------------------------------------
# Canonicalisation + reverse substitution
# ---------------------------------------------------------------------------


def _canonical_text(model: str, system: Any, messages: Any) -> str:
    """Reproduce harness.prompt_hash's canonical join, textually.

    Must stay byte-identical to that function's own construction — verified
    at runtime by :func:`_self_check_canonicalisation` before this script
    trusts either one.
    """
    return "\n---\n".join((f"model:{model}", _text_of_system(system), _text_of_messages(messages)))


def _hash_of(canonical: str) -> str:
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _self_check_canonicalisation() -> None:
    model, system, messages = "m", "s", [{"role": "user", "content": "hi"}]
    if _hash_of(_canonical_text(model, system, messages)) != prompt_hash(model, system, messages):
        raise AssertionError(
            "this script's _canonical_text() has drifted from "
            "tests.evals.harness.prompt_hash()'s canonicalisation -- fix "
            "before trusting any equality check below"
        )


def _group_reverse(rename_pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for old, new in rename_pairs:
        groups.setdefault(new, []).append(old)
    return groups


def _revert_candidates(text: str, rename_pairs: list[tuple[str, str]]) -> list[str]:
    """All candidate reversions of *text* under the inverse of *rename_pairs*.

    Exactly one candidate for an injective map. More than one only when two+
    OLD values share a NEW value (see module docstring) AND that NEW value
    actually occurs in *text* -- each candidate substitutes one of the tied
    origins for every occurrence.
    """
    groups = _group_reverse(rename_pairs)
    unambiguous = {new: olds[0] for new, olds in groups.items() if len(olds) == 1}
    ambiguous = {new: olds for new, olds in groups.items() if len(olds) > 1}

    base = text
    for new, old in unambiguous.items():
        base = base.replace(new, old)

    candidates = [base]
    for new, olds in ambiguous.items():
        expanded = []
        for cand in candidates:
            if new not in cand:
                expanded.append(cand)
                continue
            expanded.extend(cand.replace(new, old) for old in olds)
        candidates = expanded
    return candidates


def _apply_forward(text: str, rename_pairs: list[tuple[str, str]]) -> str:
    for old, new in rename_pairs:
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------------------
# Per-case re-derivation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Outcome:
    layer: str
    case_id: str
    status: str  # "unchanged" | "rederived" | "refused"
    detail: str = ""


def rederive_case(
    layer: str,
    case_id: str,
    rename_pairs: list[tuple[str, str]],
    tmp_dir: Path,
    *,
    apply: bool,
    driver: Callable[[str, Path], dict[str, Any]] | None = None,
) -> Outcome:
    """Check (and, if ``apply``, rewrite) one recorded fixture.

    ``driver`` defaults to the real per-layer driver in :data:`_DRIVERS`
    (which exercises the actual athenaeum call path against the real
    ``tests/evals/data/`` case source). Tests inject a synthetic driver here
    so they can prove the equality-check/revert/apply mechanism in isolation,
    on fixtures under ``tmp_path``, without touching real fixtures or the
    real athenaeum call paths (see
    ``tests/test_rederive_recorded_fixture.py``).
    """
    driver = driver or _DRIVERS[layer]
    fixture = load_recorded(layer, case_id)
    params = driver(case_id, tmp_dir)
    model = str(params.get("model", ""))
    system = params.get("system")
    messages = params.get("messages")

    new_hash = prompt_hash(model, system, messages)
    if new_hash == fixture.prompt_hash:
        return Outcome(layer, case_id, "unchanged")

    new_canonical = _canonical_text(model, system, messages)
    candidates = _revert_candidates(new_canonical, rename_pairs)
    matches = [c for c in candidates if _hash_of(c) == fixture.prompt_hash]

    if len(matches) != 1:
        return Outcome(
            layer,
            case_id,
            "refused",
            f"{len(matches)} of {len(candidates)} reverted candidate(s) "
            f"reconstructed the stored hash (need exactly 1) -- the prompt "
            "diff is not provably just the declared rename; a real "
            "re-record is needed",
        )

    if apply:
        new_response_text = _apply_forward(fixture.response_text, rename_pairs)
        new_blocks = [
            {**block, "text": _apply_forward(block["text"], rename_pairs)}
            if "text" in block
            else dict(block)
            for block in fixture.content_blocks
        ]
        # ``case_id`` is metadata, not response content, but keeping it in
        # sync with the (already-renamed) on-disk filename stem avoids
        # leaving stale metadata behind -- nothing in the replay suite reads
        # this field (it keys fixtures by filename stem), so this is purely
        # for a human reading the file.
        new_case_id = _apply_forward(fixture.case_id, rename_pairs)
        updated = dataclasses.replace(
            fixture,
            case_id=new_case_id,
            prompt_hash=new_hash,
            response_text=new_response_text,
            content_blocks=new_blocks,
            rederived={
                "at": datetime.now(timezone.utc).isoformat(),
                "tool": "scripts/rederive_recorded_fixture.py",
                "rename": [f"{old}={new}" for old, new in rename_pairs],
                "from_prompt_hash": fixture.prompt_hash,
            },
        )
        save_recorded(updated)

    return Outcome(layer, case_id, "rederived")


def _case_ids(layer: str) -> list[str]:
    layer_dir = RECORDED_ROOT / layer
    if not layer_dir.is_dir():
        return []
    return sorted(p.stem for p in layer_dir.glob("*.json"))


def _parse_rename(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"--rename must be OLD=NEW, got: {spec!r}")
    old, new = spec.split("=", 1)
    if not old or not new:
        raise argparse.ArgumentTypeError(f"--rename OLD and NEW must both be non-empty: {spec!r}")
    return old, new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rename",
        action="append",
        type=_parse_rename,
        default=[],
        metavar="OLD=NEW",
        help="one substitution pair; repeat for every pair in the rename map",
    )
    parser.add_argument(
        "--layer",
        action="append",
        choices=sorted(_DRIVERS),
        help="restrict to this layer (repeatable); default: all known layers",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        help="restrict to this case id (repeatable); default: every fixture on disk",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes; omit for a dry run",
    )
    args = parser.parse_args(argv)

    if not args.rename:
        parser.error("at least one --rename OLD=NEW is required")

    _self_check_canonicalisation()

    layers = args.layer or sorted(_DRIVERS)
    outcomes: list[Outcome] = []
    with tempfile.TemporaryDirectory(prefix="rederive-") as tmp:
        tmp_dir = Path(tmp)
        for layer in layers:
            case_ids = args.case_id or _case_ids(layer)
            for case_id in case_ids:
                if not (RECORDED_ROOT / layer / f"{case_id}.json").is_file():
                    continue
                try:
                    outcome = rederive_case(layer, case_id, args.rename, tmp_dir, apply=args.apply)
                except Exception as exc:  # noqa: BLE001 -- report, keep going
                    outcome = Outcome(layer, case_id, "refused", f"{exc.__class__.__name__}: {exc}")
                outcomes.append(outcome)
                print(f"{outcome.status:>10}  {layer}/{case_id}" + (f" -- {outcome.detail}" if outcome.detail else ""))

    refused = [o for o in outcomes if o.status == "refused"]
    rederived = [o for o in outcomes if o.status == "rederived"]
    print(
        f"\n{len(outcomes)} case(s) checked: {len(rederived)} rederived, "
        f"{len(outcomes) - len(rederived) - len(refused)} unchanged, "
        f"{len(refused)} refused."
        + ("" if args.apply else " (dry run -- pass --apply to write)")
    )
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
