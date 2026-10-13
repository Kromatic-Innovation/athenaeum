# SPDX-License-Identifier: Apache-2.0
"""Maya PII-hazard classifier glue (issue athenaeum#2063, Vitruvius design).

L4 service wiring the :mod:`athenaeum.yesno` ``YesNoScorer`` seam into the
TWO operator commands the design approved -- ``storage lint-pii`` and
``merges recompare`` -- and nowhere else (never the nightly librarian, never
the sidecar, never :func:`athenaeum.sensitivity.classify`, never a
redaction site). :mod:`athenaeum.pii` (L1) is not touched.

**What this module scores.** Only email/phone tokens the regex gate has
ALREADY flagged, after BOTH operator-authority exclusions are applied:

1. the ``_pii-allowlist.yml`` / sticky not-PII verdict allowlist (both
   callers already merge these before calling this module);
2. a sticky off-corpus "is PII" verdict
   (:func:`athenaeum.pii_verdicts.load_is_pii_values`) -- checked HERE,
   shadow-mode only, since neither existing caller consulted it before.

Operator authority is always read before the model (mode ``none`` never
calls :func:`load_is_pii_values` either, so a config lookup -- the off-corpus
root resolution -- never runs unless shadow mode is actually selected).

**Mode ``none`` is inert.** :func:`collect_and_score` returns immediately,
calling nothing in :mod:`athenaeum.yesno`, creating no ledger file, and
never importing ``torch``/``transformers`` (issue athenaeum#2063 AC1).

**Shadow ledger.** One JSONL partition per month,
``<cache_dir>/pii_hazard_shadow/<YYYY-MM>.jsonl``, beside ``spend.jsonl`` --
NOT under ``wiki/_verdicts/`` (that's in git; the module docstring of
:mod:`athenaeum.pii_verdicts` already states the erasure-class/low-entropy
rule against committing this kind of record there). Two row kinds,
discriminated by ``kind``:

- ``kind: "token"`` -- one per scored string: ``p_yes``, the regex
  recognizer, agreement-relevant fields, ``question_sha``, ``weights_id``,
  ``transform``. **Never** a value, a hash of a value, or a page path.
- ``kind: "summary"`` -- one per command invocation: counts and timings,
  including ``scorer: "unavailable"`` plus a reason class on any fail-closed
  path.

Writes use :func:`athenaeum.store.append_line_durable` (the same
``O_APPEND`` + ``fsync`` primitive :mod:`athenaeum.spend` uses for its own
ledger) with a tolerant reader expectation -- a crash can at worst leave a
torn trailing line, never corrupt an already-written record.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir
from athenaeum.store import append_line_durable, now_iso
from athenaeum.yesno import (
    SubprocessYesNoScorer,
    YesNoScorer,
    YesNoScorerUnavailable,
    preflight_yesno_scorer,
)

#: Mode vocabulary for this slice. ``prefilter`` is the follow-on child
#: (issue athenaeum#2063's re-slice) and is deliberately NOT in this tuple --
#: naming it in config raises, exactly like any other unknown mode.
MODE_NONE = "none"
MODE_SHADOW = "shadow"
VALID_MODES: tuple[str, ...] = (MODE_NONE, MODE_SHADOW)

#: Ledger directory name, a sibling of ``spend.jsonl`` under the cache dir.
LEDGER_DIRNAME = "pii_hazard_shadow"

#: Recognizer vocabulary this module ever scores.
RECOGNIZER_EMAIL = "email"
RECOGNIZER_PHONE = "phone"


@dataclass(frozen=True)
class HazardToken:
    """One post-allowlist candidate token, named by which recognizer found it."""

    recognizer: str
    text: str


@dataclass
class ShadowCollector:
    """Accumulates hazard tokens for ONE command invocation, deduped.

    Both callers (``lint-pii``, ``recompare``) build one of these across
    their whole run and flush it exactly once, so a real scorer is launched
    at most once per command invocation rather than once per file/proposal
    (issue athenaeum#2063's "one batch per command invocation").
    """

    surface: str
    n_skipped_allowlisted: int = 0
    n_skipped_is_pii: int = 0
    _seen: set[tuple[str, str]] = field(default_factory=set)
    tokens: list[HazardToken] = field(default_factory=list)

    def add(self, recognizer: str, text: str) -> None:
        """Add *text* if not already present (first-seen order is kept)."""
        key = (recognizer, text)
        if key in self._seen:
            return
        self._seen.add(key)
        self.tokens.append(HazardToken(recognizer=recognizer, text=text))

    def add_many(self, recognizer: str, texts: Iterable[str]) -> None:
        for text in texts:
            self.add(recognizer, text)


def load_pii_hazard_question() -> str:
    """Read the shared hazard question from its packaged prompt file.

    Prompt text is content, not code (``policies/prompt-text-is-content.md``)
    -- loaded via ``importlib.resources``, the same convention
    :func:`athenaeum.tiers._load_name_resolution_confirm_prompt` already
    established. Both production (this function) and
    ``scripts/eval_maya_pii_hazard.py`` read the SAME file, so
    ``question_sha`` in a ledger row is always comparable across the two
    (issue athenaeum#2063 AC10).
    """
    resource = importlib.resources.files("athenaeum.prompts").joinpath("pii_hazard_question.md")
    return resource.read_text(encoding="utf-8")


def question_sha256(question: str) -> str:
    """sha256 hex digest of the question text, exactly as stored."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def compute_weights_id(weights_path: Path) -> str | None:
    """sha256 over ``config.json`` + ``maya_config.json`` bytes (never the weights).

    ``None`` when neither file is readable -- a caller treats that as
    "unknown weights identity", never as an error.
    """
    digest = hashlib.sha256()
    found = False
    for name in ("config.json", "maya_config.json"):
        try:
            digest.update((weights_path / name).read_bytes())
            found = True
        except OSError:
            continue
    return digest.hexdigest() if found else None


def _read_temperature(weights_path: Path) -> float:
    # Function-level import: this module must stay importable (and this
    # function callable with mode=none, which never calls it) with no
    # torch/transformers in sys.modules -- `_yesno_worker`'s own module
    # scope is stdlib-only, so this import is always cheap and safe.
    from athenaeum._yesno_worker import read_temperature

    return read_temperature(weights_path)


def _ledger_partition_path(cache_dir: Path, *, now: datetime | None = None) -> Path:
    when = now or datetime.now(timezone.utc)
    return cache_dir / LEDGER_DIRNAME / f"{when:%Y-%m}.jsonl"


def _write_ledger_line(path: Path, row: dict[str, Any]) -> None:
    append_line_durable(path, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))


def _write_summary_row(
    path: Path,
    *,
    surface: str,
    mode: str,
    run_id: str,
    n_candidates: int,
    n_scored: int,
    n_skipped_allowlisted: int,
    n_skipped_is_pii: int,
    load_secs: float,
    score_secs: float,
    scorer: str,
    reason: str | None,
) -> None:
    _write_ledger_line(
        path,
        {
            "kind": "summary",
            "v": 1,
            "ts": now_iso(),
            "run_id": run_id,
            "surface": surface,
            "mode": mode,
            "n_candidates": n_candidates,
            "n_scored": n_scored,
            "n_skipped_allowlisted": n_skipped_allowlisted,
            "n_skipped_is_pii": n_skipped_is_pii,
            "load_secs": round(load_secs, 4),
            "score_secs": round(score_secs, 4),
            "scorer": scorer,
            "reason": reason,
        },
    )


def run_shadow_scoring(
    collector: ShadowCollector,
    *,
    mode: str,
    weights_path: str | None,
    interpreter: str | None,
    timeout_secs: float,
    cache_dir: Path | None = None,
    scorer: YesNoScorer | None = None,
) -> dict[tuple[str, str], float]:
    """Score *collector*'s tokens and append shadow ledger rows.

    A no-op for ``mode != "shadow"`` -- the call sites still build and pass
    a :class:`ShadowCollector` in every mode, but this function is the ONLY
    place that branches on mode, so ``mode="none"`` truly calls nothing in
    :mod:`athenaeum.yesno` (issue athenaeum#2063 AC1). Never raises: every
    failure is caught, reported on stderr naming the key, and recorded as
    one ``scorer: "unavailable"`` summary row -- the gate's own exit code
    and output are this function's caller's concern, never this function's.

    Returns ``{(recognizer, text): p_yes}`` for every token actually
    scored -- empty for ``mode="none"`` and for any fail-closed path -- so a
    caller (``lint-pii``'s human-text output) can annotate what shadow mode
    saw without re-reading the ledger it just wrote.
    """
    if mode != MODE_SHADOW:
        return {}

    run_id = uuid.uuid4().hex
    resolved_cache_dir = resolve_cache_dir(cache_dir)
    ledger_path = _ledger_partition_path(resolved_cache_dir)
    question = load_pii_hazard_question()
    q_sha = question_sha256(question)

    if not collector.tokens:
        _write_summary_row(
            ledger_path,
            surface=collector.surface,
            mode=mode,
            run_id=run_id,
            n_candidates=0,
            n_scored=0,
            n_skipped_allowlisted=collector.n_skipped_allowlisted,
            n_skipped_is_pii=collector.n_skipped_is_pii,
            load_secs=0.0,
            score_secs=0.0,
            scorer="ok",
            reason=None,
        )
        return {}

    if scorer is None:
        unavailable_reason = preflight_yesno_scorer(
            interpreter=interpreter, weights_path=weights_path
        )
        if unavailable_reason is not None:
            print(f"pii hazard classifier: {unavailable_reason}", file=sys.stderr)
            _write_summary_row(
                ledger_path,
                surface=collector.surface,
                mode=mode,
                run_id=run_id,
                n_candidates=len(collector.tokens),
                n_scored=0,
                n_skipped_allowlisted=collector.n_skipped_allowlisted,
                n_skipped_is_pii=collector.n_skipped_is_pii,
                load_secs=0.0,
                score_secs=0.0,
                scorer="unavailable",
                reason="config",
            )
            return {}
        assert weights_path is not None and interpreter is not None
        scorer = SubprocessYesNoScorer(
            interpreter=interpreter,
            weights_path=Path(weights_path).expanduser(),
            timeout_secs=timeout_secs,
        )

    start = time.perf_counter()
    try:
        p_yes_values = scorer.score(question, [t.text for t in collector.tokens])
    except YesNoScorerUnavailable as exc:
        elapsed = time.perf_counter() - start
        print(f"pii hazard classifier: scorer unavailable: {exc}", file=sys.stderr)
        _write_summary_row(
            ledger_path,
            surface=collector.surface,
            mode=mode,
            run_id=run_id,
            n_candidates=len(collector.tokens),
            n_scored=0,
            n_skipped_allowlisted=collector.n_skipped_allowlisted,
            n_skipped_is_pii=collector.n_skipped_is_pii,
            load_secs=0.0,
            score_secs=elapsed,
            scorer="unavailable",
            reason=exc.__class__.__name__,
        )
        return {}
    elapsed = time.perf_counter() - start
    load_secs = float(getattr(scorer, "last_load_secs", 0.0) or 0.0)

    weights_id = None
    transform = 1.0
    if weights_path:
        resolved_weights = Path(weights_path).expanduser()
        weights_id = compute_weights_id(resolved_weights)
        transform = _read_temperature(resolved_weights)

    ts = now_iso()
    results: dict[tuple[str, str], float] = {}
    for token, p_yes in zip(collector.tokens, p_yes_values, strict=True):
        results[(token.recognizer, token.text)] = p_yes
        _write_ledger_line(
            ledger_path,
            {
                "kind": "token",
                "v": 1,
                "ts": ts,
                "run_id": run_id,
                "surface": collector.surface,
                "mode": mode,
                "recognizer": token.recognizer,
                "p_yes": round(p_yes, 4),
                "regex_hazard": True,
                "would_clear_at": round(p_yes, 4),
                "question_sha": q_sha,
                "weights_id": weights_id,
                "transform": transform,
            },
        )

    _write_summary_row(
        ledger_path,
        surface=collector.surface,
        mode=mode,
        run_id=run_id,
        n_candidates=len(collector.tokens),
        n_scored=len(collector.tokens),
        n_skipped_allowlisted=collector.n_skipped_allowlisted,
        n_skipped_is_pii=collector.n_skipped_is_pii,
        load_secs=load_secs,
        score_secs=max(elapsed - load_secs, 0.0),
        scorer="ok",
        reason=None,
    )
    return results


__all__ = [
    "MODE_NONE",
    "MODE_SHADOW",
    "VALID_MODES",
    "LEDGER_DIRNAME",
    "RECOGNIZER_EMAIL",
    "RECOGNIZER_PHONE",
    "HazardToken",
    "ShadowCollector",
    "load_pii_hazard_question",
    "question_sha256",
    "compute_weights_id",
    "run_shadow_scoring",
]
