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
import re
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
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

#: Mode vocabulary. ``none`` is inert, ``shadow`` scores and records without
#: ever changing a verdict (issue athenaeum#2063), and ``prefilter`` lets the
#: model decide PHONE-shaped candidates outside the abstention band while the
#: regex keeps deciding everything else (issue athenaeum#2064).
MODE_NONE = "none"
MODE_SHADOW = "shadow"
MODE_PREFILTER = "prefilter"
VALID_MODES: tuple[str, ...] = (MODE_NONE, MODE_SHADOW, MODE_PREFILTER)

#: The two modes that actually call :mod:`athenaeum.yesno`. ``none`` is not
#: here, which is what makes it inert.
SCORING_MODES: tuple[str, ...] = (MODE_SHADOW, MODE_PREFILTER)

#: Ledger directory name, a sibling of ``spend.jsonl`` under the cache dir.
LEDGER_DIRNAME = "pii_hazard_shadow"

#: Recognizer vocabulary this module ever scores.
RECOGNIZER_EMAIL = "email"
RECOGNIZER_PHONE = "phone"

#: Decision vocabulary :func:`decide_prefilter` returns, one value per
#: candidate. ``clear`` drops the regex finding, ``keep`` keeps it with the
#: model's agreement recorded, and ``regex`` means the model does not decide
#: this candidate at all -- the regex verdict stands, untouched, which is the
#: outcome for every email-shaped candidate in every mode.
DECISION_CLEAR = "clear"
DECISION_KEEP = "keep"
DECISION_REGEX = "regex"

#: Abstention band for ``prefilter`` mode as ``(low, high)``, CLOSED at both
#: ends: a score inside it is the model declining to decide, so the regex
#: verdict stands.
#:
#: 0.4/0.6 is the operating point MEASURED on this feature's own shadow data
#: (issue athenaeum#2064), not a guess. Re-scoring the 822 host-side eval
#: fixtures at the weights' own temperature (1.65) abstains on 77.1% of rows
#: with a [0.3, 0.7] band -- unusable -- and on 7.7% with [0.4, 0.6], which
#: left one false negative in 759 scored rows. Changing the band is out of
#: scope for that issue, so this is a module constant with NO config knob;
#: a caller (the eval script) may still pass its own ``band`` without an
#: operator-facing surface existing.
DEFAULT_ABSTENTION_BAND: tuple[float, float] = (0.4, 0.6)

#: The ONLY phone-shaped token Maya may clear: a bare digit run of 10 to 12
#: digits, no separators and no leading ``+``.
#:
#: Issue athenaeum#2064's operator adjudication of 2026-10-25 is the whole
#: basis: every phone-axis shadow "no" was an identifier of exactly this
#: shape (Actions run ids, a merge decision id, a retire approval id, a
#: receipt number), and Maya was right on every one. A candidate carrying a
#: ``+`` or separators is formatted the way humans write a number they dial,
#: so it stays regex-decided regardless of score -- the model was never
#: evidenced on that population.
_CLEARABLE_PHONE_SHAPE_RE = re.compile(r"[0-9]{10,12}")


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


def is_clearable_phone_shape(text: str) -> bool:
    """True when *text* is the bare identifier shape Maya is allowed to clear.

    See :data:`_CLEARABLE_PHONE_SHAPE_RE` for why the shape is this narrow.
    A separator-formatted or ``+``-prefixed candidate returns ``False`` and
    so stays regex-decided whatever it scores (issue athenaeum#2064 AC3).
    """
    return _CLEARABLE_PHONE_SHAPE_RE.fullmatch(text) is not None


def decide_prefilter(
    recognizer: str,
    text: str,
    p_yes: float | None,
    band: tuple[float, float] = DEFAULT_ABSTENTION_BAND,
) -> str:
    """Decide ONE candidate in ``prefilter`` mode. Pure, no I/O, no state.

    The whole policy of issue athenaeum#2064 lives here, in one function, so
    the ledger row and the gate's own behaviour cannot disagree about what
    was decided -- both read this.

    Returns :data:`DECISION_CLEAR` (drop the regex finding),
    :data:`DECISION_KEEP` (regex finding stands, model agreed) or
    :data:`DECISION_REGEX` (model did not decide; regex verdict stands).

    Four reasons a candidate is never cleared, in the order checked:

    1. **It is email-shaped.** Maya may not clear an email in ANY mode. The
       operator's 2026-10-25 adjudication found every email-shaped shadow
       "no" to be a real contact value under Kromatic policy (role and
       no-reply addresses count as PII), so the model's disagreement there
       is a policy disagreement, not noise, and no band would fix it.
    2. **It is phone-shaped but not the identifier shape** --
       :func:`is_clearable_phone_shape`.
    3. **It was not scored** (``p_yes is None``): a ``scorer: unavailable``
       run clears nothing, it falls back to regex-only.
    4. **The score is inside the band**, which is closed at both ends.
    """
    if recognizer != RECOGNIZER_PHONE:
        return DECISION_REGEX
    if not is_clearable_phone_shape(text):
        return DECISION_REGEX
    if p_yes is None:
        return DECISION_REGEX
    low, high = band
    if p_yes < low:
        return DECISION_CLEAR
    if p_yes > high:
        return DECISION_KEEP
    return DECISION_REGEX


def prefilter_decisions(
    p_yes_by_token: Mapping[tuple[str, str], float],
    band: tuple[float, float] = DEFAULT_ABSTENTION_BAND,
) -> dict[tuple[str, str], str]:
    """:func:`decide_prefilter` over a whole scored batch, keyed as scored."""
    return {
        (recognizer, text): decide_prefilter(recognizer, text, p_yes, band)
        for (recognizer, text), p_yes in p_yes_by_token.items()
    }


def prefilter_cleared_tokens(
    p_yes_by_token: Mapping[tuple[str, str], float],
    band: tuple[float, float] = DEFAULT_ABSTENTION_BAND,
) -> frozenset[tuple[str, str]]:
    """The ``(recognizer, text)`` keys ``prefilter`` mode clears.

    Empty for an empty batch, so a ``scorer: unavailable`` run (which
    returns no scores at all) clears nothing by construction rather than by
    a second check at each call site.
    """
    return frozenset(
        key
        for key, decision in prefilter_decisions(p_yes_by_token, band).items()
        if decision == DECISION_CLEAR
    )


@dataclass(frozen=True)
class PrefilterSummary:
    """Counts for ``prefilter`` mode's one operator-facing summary line.

    The five buckets PARTITION the scored batch -- every scored candidate
    lands in exactly one -- so the line can be read as an account of what
    the model was allowed to do, not just of what it did.
    """

    cleared: int = 0
    kept: int = 0
    in_band: int = 0
    email_untouched: int = 0
    phone_shape_untouched: int = 0

    @property
    def total(self) -> int:
        return (
            self.cleared
            + self.kept
            + self.in_band
            + self.email_untouched
            + self.phone_shape_untouched
        )


def summarize_prefilter(
    p_yes_by_token: Mapping[tuple[str, str], float],
    band: tuple[float, float] = DEFAULT_ABSTENTION_BAND,
) -> PrefilterSummary:
    """Bucket a scored batch into :class:`PrefilterSummary`'s five counts."""
    cleared = kept = in_band = email = phone_shape = 0
    for (recognizer, text), p_yes in p_yes_by_token.items():
        if recognizer != RECOGNIZER_PHONE:
            email += 1
            continue
        if not is_clearable_phone_shape(text):
            phone_shape += 1
            continue
        decision = decide_prefilter(recognizer, text, p_yes, band)
        if decision == DECISION_CLEAR:
            cleared += 1
        elif decision == DECISION_KEEP:
            kept += 1
        else:
            in_band += 1
    return PrefilterSummary(
        cleared=cleared,
        kept=kept,
        in_band=in_band,
        email_untouched=email,
        phone_shape_untouched=phone_shape,
    )


def format_prefilter_summary(
    summary: PrefilterSummary, band: tuple[float, float] = DEFAULT_ABSTENTION_BAND
) -> str:
    """The single summary line both call sites print (issue athenaeum#2064)."""
    low, high = band
    return (
        f"pii hazard prefilter (band [{low:g}, {high:g}]): "
        f"{summary.cleared} cleared, {summary.kept} kept, "
        f"{summary.in_band} in band (regex decided), "
        f"{summary.email_untouched} email untouched, "
        f"{summary.phone_shape_untouched} phone-shape untouched"
    )


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
    # Fail closed, never into the caller (run_shadow_scoring's own "Never
    # raises" contract): a disk-full, permission, or read-only-filesystem
    # OSError here must not abort the gate command the classifier is wired
    # into -- it is strictly better to lose one shadow-ledger row than to
    # crash `storage lint-pii` / `merges recompare` over telemetry.
    try:
        append_line_durable(path, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    except OSError as exc:
        print(
            f"pii hazard classifier: shadow ledger write failed ({exc.__class__.__name__}): {path}",
            file=sys.stderr,
        )


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
    band: tuple[float, float] = DEFAULT_ABSTENTION_BAND,
) -> dict[tuple[str, str], float]:
    """Score *collector*'s tokens and append hazard ledger rows.

    A no-op for any mode outside :data:`SCORING_MODES` -- the call sites
    still build and pass a :class:`ShadowCollector` in every mode, but this
    function is the ONLY place that branches on mode, so ``mode="none"``
    truly calls nothing in :mod:`athenaeum.yesno` (issue athenaeum#2063
    AC1). Never raises: every failure is caught, reported on stderr naming
    the key, and recorded as one ``scorer: "unavailable"`` summary row --
    the gate's own exit code and output are this function's caller's
    concern, never this function's.

    Returns ``{(recognizer, text): p_yes}`` for every token actually
    scored -- empty for ``mode="none"`` and for any fail-closed path -- so a
    caller can annotate what the model saw (``lint-pii``'s human-text
    output) and, in ``prefilter`` mode, derive the decisions via
    :func:`prefilter_cleared_tokens` off the SAME scores this function
    ledgered.

    ``mode="prefilter"`` (issue athenaeum#2064) differs from ``"shadow"``
    in the ledger only: each token row additionally carries ``decision`` and
    ``cleared``, computed by :func:`decide_prefilter`. A shadow row is
    unchanged, field for field, from issue athenaeum#2063 -- the mode's
    golden tests pin that. *band* is only consulted in ``prefilter`` mode.
    """
    if mode not in SCORING_MODES:
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
            # Expand ~ the same way weights_path is expanded two lines
            # below. preflight_yesno_scorer tolerates a tilde path (it
            # expands its own local `candidate` to run the is_file/X_OK
            # check) but returns only a pass/fail message, never the
            # resolved string -- so the raw `interpreter` reaching here
            # unexpanded passes preflight and then fails at
            # subprocess.run, which (unlike a shell) never expands `~`.
            interpreter=str(Path(interpreter).expanduser()),
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
    # One pass over the whole batch, from the SAME pure function the call
    # sites use, so a `cleared: true` row can never describe a token the
    # gate went on to report (issue athenaeum#2064 AC6).
    decisions = prefilter_decisions(results, band) if mode == MODE_PREFILTER else {}
    for token, p_yes in zip(collector.tokens, p_yes_values, strict=True):
        row = {
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
        }
        if mode == MODE_PREFILTER:
            decision = decisions[(token.recognizer, token.text)]
            row["decision"] = decision
            row["cleared"] = decision == DECISION_CLEAR
        _write_ledger_line(ledger_path, row)

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
    "MODE_PREFILTER",
    "VALID_MODES",
    "SCORING_MODES",
    "LEDGER_DIRNAME",
    "RECOGNIZER_EMAIL",
    "RECOGNIZER_PHONE",
    "DECISION_CLEAR",
    "DECISION_KEEP",
    "DECISION_REGEX",
    "DEFAULT_ABSTENTION_BAND",
    "HazardToken",
    "ShadowCollector",
    "PrefilterSummary",
    "load_pii_hazard_question",
    "question_sha256",
    "compute_weights_id",
    "is_clearable_phone_shape",
    "decide_prefilter",
    "prefilter_decisions",
    "prefilter_cleared_tokens",
    "summarize_prefilter",
    "format_prefilter_summary",
    "run_shadow_scoring",
]
