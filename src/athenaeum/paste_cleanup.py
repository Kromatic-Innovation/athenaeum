# SPDX-License-Identifier: Apache-2.0
"""Tier-0 attributed-paste cleanup pass for person pages (issue athenaeum#1717).

Two LLM passes over pre-athenaeum#1684 unbounded pastes in person pages' ``## Notes``
sections (the same bullet shape ``audit.py``'s sibling sweeps target — see
``review-2026-09-19``/``eval-set-prelabelled-2026-09-24`` on the issue):

1. **Extraction** (:func:`extract_paste_span`, no LLM) — splits one bullet's
   content into the attributed-paste span and anything after it, and flags
   a bullet as ``hold`` rather than guessing when a workshop/mural paste
   looks fused to unrelated legitimate content with no clean boundary
   (issue athenaeum#1717 operator finding, 2026-09-24 correction comment).
   A ``hold`` bullet is never split, never removed, never rewritten by
   :func:`apply_paste_cleanup_report` — only a human clears it.
2. **Proposer pass** (:func:`propose_page`, cheap model) — classifies one
   extracted paste as ``keep``/``rewrite``/``remove`` with a reason and a
   confidence.
3. **Verifier pass** (:func:`verify_page`, stronger model) — re-checks a
   subset chosen by :func:`select_verify_sample`.

Mirrors ``audit.py``'s dry-run/apply split and page-identity discipline:
:func:`build_paste_cleanup_report` never writes; :func:`apply_paste_cleanup_report`
re-reads each page at write time and re-locates the exact bullet chunk
before touching it, never trusting the scan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.models import TokenUsage, parse_frontmatter, render_frontmatter

log = logging.getLogger(__name__)

PASTE_CLEANUP_VERSION = "paste-cleanup-v1"

#: Bullet shape emitted by pre-athenaeum#1684 intake (see ``athenaeum.intake`` history
#: cited on athenaeum#1717): ``- {YYYY-MM-DD}: {raw_body.strip()}``, bullets
#: separated by a blank line before the next dated bullet.
_BULLET_START_RE = re.compile(r"(?:\A|\n\n)(?=- \d{4}-\d{2}-\d{2}: )")
_BULLET_HEAD_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}): ", re.S)

#: A confident-tier paste (>= this many chars) that also opens like a
#: workshop/mural board summary is a fusion CANDIDATE -- not automatically
#: fused, just eligible for the marker check below.
CONFIDENT_TIER_THRESHOLD_CHARS = 2000

#: Typical clean mural-paste length observed on the 2026-09-24 operator
#: review (~1,100-1,600 chars) -- content past this length inside a
#: workshop-summary-shaped bullet is the fusion tail this module guards.
_MURAL_PASTE_TYPICAL_MAX_CHARS = 1_700

_WORKSHOP_SUMMARY_OPENERS = ("This board", "This session", "This was a", "This is a")

#: Substrings the 2026-09-24 operator review found in the LEGITIMATE
#: (non-paste) tail of every confirmed fusion case -- CRM/pipeline fields, a
#: previous-name cross-reference, a PII-remediation note. Matching one of
#: these past the typical mural length is the fusion signal; it is
#: deliberately a narrow, evidence-derived list rather than a general
#: "looks like a topic change" heuristic, so a page with no such phrase is
#: never held on suspicion alone.
_FUSION_TAIL_MARKERS = (
    "crm",
    "pipeline",
    "previously known as",
    "previous name",
    "pii",
    "remediat",
    "stage:",
    "deal size",
)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Thin wrapper so this module has one frontmatter entry point; delegates
    to :func:`athenaeum.models.parse_frontmatter`."""
    meta, body = parse_frontmatter(text)
    return meta, body


def extract_notes_bullets(body: str) -> list[tuple[str, str, str]]:
    """Every ``- YYYY-MM-DD: ...`` bullet under ``## Notes`` in *body*.

    Returns ``(date, content, raw_chunk)`` tuples where ``raw_chunk`` is the
    exact substring (including the ``- date: `` head) that
    :func:`apply_paste_cleanup_report` re-locates verbatim before writing.
    """
    idx = body.find("## Notes")
    if idx == -1:
        return []
    section = body[idx + len("## Notes") :]
    parts = _BULLET_START_RE.split(section)
    out = []
    for part in parts:
        part = part.strip("\n")
        if not part:
            continue
        m = _BULLET_HEAD_RE.match(part)
        if not m:
            continue
        date = m.group(1)
        content = part[m.end() :]
        out.append((date, content, part))
    return out


def _find_legitimate_content_boundary(content: str) -> int | None:
    """Index into *content* where a paragraph break separates the paste from
    unrelated legitimate content that follows it, or ``None`` when no such
    boundary is found.

    Only fires past :data:`_MURAL_PASTE_TYPICAL_MAX_CHARS` and only at a
    ``\\n\\n`` paragraph break whose following text contains one of
    :data:`_FUSION_TAIL_MARKERS` -- both conditions evidence-derived from the
    2026-09-24 operator review's 7 confirmed fusion cases, never a generic
    "looks different" guess.
    """
    for m in re.finditer(r"\n\n", content):
        boundary = m.end()
        if boundary < _MURAL_PASTE_TYPICAL_MAX_CHARS:
            continue
        tail = content[boundary : boundary + 400].lower()
        if any(marker in tail for marker in _FUSION_TAIL_MARKERS):
            return boundary
    return None


def _looks_like_workshop_summary(content: str) -> bool:
    head = content[:60]
    return head.startswith(_WORKSHOP_SUMMARY_OPENERS)


def extract_paste_span(
    content: str, *, confident_tier_threshold: int = CONFIDENT_TIER_THRESHOLD_CHARS
) -> tuple[str, str, str]:
    """Split one bullet's *content* into ``(paste_text, remainder_text, status)``.

    - ``status == "split"``: a legitimate-content boundary was found;
      *paste_text* is the attributed paste, *remainder_text* is the
      legitimate tail (verbatim, never touched by a cleanup verdict).
    - ``status == "hold"``: *content* is confident-tier, opens like a
      workshop/mural summary, but no clean boundary was found -- the whole
      bullet is fusion-suspect. *paste_text* is the ENTIRE *content*
      (nothing trimmed) so a hold can never be mistaken for a safe partial
      extraction; *remainder_text* is ``""``.
    - ``status == "clean"``: no fusion suspected; *paste_text* is the whole
      *content*, *remainder_text* is ``""``.
    """
    boundary = _find_legitimate_content_boundary(content)
    if boundary is not None:
        return content[:boundary].rstrip(), content[boundary:].lstrip(), "split"
    if len(content) >= confident_tier_threshold and _looks_like_workshop_summary(content):
        return content, "", "hold"
    return content, "", "clean"


# --- proposer / verifier verdicts --------------------------------------

_VALID_VERDICTS = ("keep", "rewrite", "remove")
_VALID_CONFIDENCE = ("high", "medium", "low")


def render_propose_prompt(meta: dict[str, Any], paste_text: str) -> str:
    """Build the per-paste proposer user prompt. Pure text assembly, no I/O.

    The instructions themselves live in ``prompts/paste_cleanup_propose.md``
    (issue athenaeum#1717 / policy ``prompt-text-is-content.md``) -- this
    function only interpolates the page identity and the paste text.
    """
    instructions = (Path(__file__).parent / "prompts" / "paste_cleanup_propose.md").read_text(
        encoding="utf-8"
    )
    name = meta.get("name") or meta.get("title") or meta.get("uid") or "(unknown)"
    return f"{instructions}\n\nPage subject: {name}\n\nPaste text:\n{paste_text}\n"


def render_verify_prompt(meta: dict[str, Any], paste_text: str, proposed: dict[str, Any]) -> str:
    instructions = (Path(__file__).parent / "prompts" / "paste_cleanup_verify.md").read_text(
        encoding="utf-8"
    )
    name = meta.get("name") or meta.get("title") or meta.get("uid") or "(unknown)"
    proposed_json = json.dumps(
        {
            "verdict": proposed.get("verdict"),
            "claim": proposed.get("claim", ""),
            "reason": proposed.get("reason", ""),
            "confidence": proposed.get("confidence"),
        }
    )
    return (
        f"{instructions}\n\nPage subject: {name}\n\nProposed verdict:\n{proposed_json}"
        f"\n\nPaste text:\n{paste_text}\n"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    """Best-effort single-JSON-object extraction, tolerant of a fenced code
    block or leading/trailing prose (same tolerance ``audit.py``'s
    ``parse_audit_response`` applies to model output)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]
    return json.loads(text)


def parse_propose_response(text: str) -> dict[str, Any]:
    """Parse the proposer's JSON verdict. Raises ``ValueError`` on anything
    that doesn't fit the contract -- callers turn that into a per-page error
    verdict rather than a crash, matching ``audit_page``'s try/except shape."""
    obj = _extract_json_object(text)
    verdict = obj.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    confidence = obj.get("confidence")
    if confidence not in _VALID_CONFIDENCE:
        raise ValueError(f"invalid confidence: {confidence!r}")
    claim = obj.get("claim") or ""
    if verdict == "rewrite" and not claim.strip():
        raise ValueError("rewrite verdict with empty claim")
    return {
        "verdict": verdict,
        "claim": claim,
        "reason": obj.get("reason") or "",
        "confidence": confidence,
    }


def parse_verify_response(text: str) -> dict[str, Any]:
    obj = _extract_json_object(text)
    verdict = obj.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    claim = obj.get("claim") or ""
    if verdict == "rewrite" and not claim.strip():
        raise ValueError("rewrite verdict with empty claim")
    return {
        "verdict": verdict,
        "claim": claim,
        "reason": obj.get("reason") or "",
        "agree": bool(obj.get("agree", False)),
    }


@dataclass
class ProposalVerdict:
    """One proposer-pass result for one extracted paste."""

    uid: str
    path: Path
    date: str
    raw_chunk: str
    paste_text: str
    extraction_status: str  # "clean" | "split" | "hold"
    verdict: str  # "keep" | "rewrite" | "remove" | "hold" | "error"
    claim: str = ""
    reason: str = ""
    confidence: str | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    verified: bool = False
    verifier_verdict: str | None = None
    verifier_agree: bool | None = None
    verifier_reason: str = ""
    verifier_claim: str = ""
    error: str | None = None
    #: Set by :func:`verify_page` the moment it is called for this verdict
    #: (before its own try/except), regardless of whether the verifier call
    #: succeeds -- issue athenaeum#1903 step 1. Lets :meth:`final_verdict`
    #: (and any report/replay consumer) distinguish "never selected for
    #: verification under ``sampled``" (``verify_attempted is False``, a
    #: ``keep`` proposal is unaffected) from "verifier was attempted and
    #: errored" (``verify_attempted is True`` and :attr:`error` is set).
    verify_attempted: bool = False

    def final_verdict(self) -> str:
        if self.verified and self.verifier_verdict is not None:
            return self.verifier_verdict
        if self.error is not None:
            # Issue athenaeum#1903: a proposer-side error (never reached the
            # verifier) or a verifier-side error (attempted, but the call
            # raised or returned unparseable output) must never fall
            # through to a classification nothing actually confirmed --
            # hold for a human, don't write it.
            return "hold"
        return self.verdict

    def final_claim(self) -> str:
        """The claim text to write on ``rewrite``: the verifier's own claim
        when it overrode the proposer (``verified and not verifier_agree``),
        else the proposer's claim."""
        if self.verified and self.verifier_agree is False and self.verifier_claim:
            return self.verifier_claim
        return self.claim

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "path": str(self.path),
            "date": self.date,
            "raw_chunk": self.raw_chunk,
            "extraction_status": self.extraction_status,
            "verdict": self.verdict,
            "claim": self.claim,
            "confidence": self.confidence,
            "reason": self.reason,
            "model": self.model,
            "verified": self.verified,
            "verifier_verdict": self.verifier_verdict,
            "verifier_agree": self.verifier_agree,
            "verifier_claim": self.verifier_claim,
            "verifier_reason": self.verifier_reason,
            "verify_attempted": self.verify_attempted,
            "final_verdict": self.final_verdict(),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProposalVerdict":
        """Reconstruct from :meth:`to_dict`'s shape -- the ``--from-report``
        replay path (issue athenaeum#1903 step 3). ``paste_text`` is not
        round-tripped (never written by ``to_dict``): replay never calls the
        LLM, so nothing reads it; :func:`apply_paste_cleanup_report` only
        needs ``raw_chunk``."""
        return cls(
            uid=d["uid"],
            path=Path(d["path"]),
            date=d.get("date", ""),
            raw_chunk=d.get("raw_chunk", ""),
            paste_text="",
            extraction_status=d.get("extraction_status", ""),
            verdict=d["verdict"],
            claim=d.get("claim", ""),
            reason=d.get("reason", ""),
            confidence=d.get("confidence"),
            model=d.get("model", ""),
            verified=d.get("verified", False),
            verifier_verdict=d.get("verifier_verdict"),
            verifier_agree=d.get("verifier_agree"),
            verifier_reason=d.get("verifier_reason", ""),
            verifier_claim=d.get("verifier_claim", ""),
            error=d.get("error"),
            verify_attempted=d.get("verify_attempted", False),
        )


def propose_page(
    client: Any,
    *,
    uid: str,
    path: Path,
    date: str,
    raw_chunk: str,
    meta: dict[str, Any],
    content: str,
    model: str,
    max_tokens: int = 1024,
    usage: TokenUsage | None = None,
) -> ProposalVerdict:
    """Extract + classify ONE bullet. Never raises -- a per-page failure
    becomes an ``"error"`` verdict, matching ``audit_page``'s isolation
    guarantee (one bad page must not kill the run).

    *usage* (issue athenaeum#1717 AC4), when given, is credited with this
    call's own token delta -- via :meth:`TokenUsage.add`, which also counts
    the call -- immediately after a response is obtained, before the parse
    step below. A real API call bills tokens whether or not the response
    parses, so the credit must not be conditioned on parse success; the
    caller uses the same accumulator across every proposer/verifier call in
    the run for both ``spend.ceiling_tripped`` and the single end-of-run
    ``spend.record_spend``.
    """
    paste_text, _remainder, status = extract_paste_span(content)
    verdict = ProposalVerdict(
        uid=uid,
        path=path,
        date=date,
        raw_chunk=raw_chunk,
        paste_text=paste_text,
        extraction_status=status,
        verdict="hold" if status == "hold" else "error",
        model=model,
    )
    if status == "hold":
        verdict.reason = "extraction boundary ambiguous: fusion suspected, not attempted"
        return verdict

    from athenaeum.provider import response_text

    prompt = render_propose_prompt(meta, paste_text)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system="Return only the JSON object the instructions describe.",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - isolate one page's failure
        verdict.error = f"{type(exc).__name__}: {exc}"
        return verdict

    from athenaeum.models import cache_usage_counts

    input_tokens, output_tokens, _cache_w, _cache_r = cache_usage_counts(response)
    verdict.input_tokens = input_tokens
    verdict.output_tokens = output_tokens
    if usage is not None:
        usage.add(input_tokens, output_tokens, model=model, knob="classify")

    try:
        parsed = parse_propose_response(response_text(response))
    except (ValueError, json.JSONDecodeError, AttributeError, IndexError) as exc:
        # AttributeError/IndexError (issue athenaeum#1717): response_text()
        # deliberately falls back to response.content[0].text when no
        # type == "text" block is found (see its docstring), which can
        # raise either on a thinking-only response. That is a live crash
        # (2026-09-25 slice-1 dry run) this call site must isolate as one
        # bullet's error, not let escape and kill the whole pass -- same
        # isolation guarantee as the JSON-parse-error branch above.
        verdict.error = f"parse error: {exc}"
        return verdict

    verdict.verdict = parsed["verdict"]
    verdict.claim = parsed["claim"]
    verdict.reason = parsed["reason"]
    verdict.confidence = parsed["confidence"]
    return verdict


def verify_page(
    client: Any,
    verdict: ProposalVerdict,
    *,
    meta: dict[str, Any],
    model: str,
    max_tokens: int = 1024,
    usage: TokenUsage | None = None,
) -> ProposalVerdict:
    """Re-check *verdict* with a stronger model. Mutates and returns *verdict*.

    *usage*: see :func:`propose_page` -- same contract, credited with this
    call's own token delta (not *verdict*'s cumulative totals, which already
    include the proposer call's tokens) on the ``verify`` knob.
    """
    # Issue athenaeum#1903 step 1: mark the attempt BEFORE the try/except
    # below -- unconditionally, whether the call succeeds, raises, or
    # returns unparseable output -- so ``final_verdict`` (and any
    # report/replay consumer) can tell "attempted and errored" apart from
    # "never selected for verification".
    verdict.verify_attempted = True
    from athenaeum.provider import response_text

    proposed = {
        "verdict": verdict.verdict,
        "claim": verdict.claim,
        "reason": verdict.reason,
        "confidence": verdict.confidence,
    }
    prompt = render_verify_prompt(meta, verdict.paste_text, proposed)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system="Return only the JSON object the instructions describe.",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        verdict.error = f"verify {type(exc).__name__}: {exc}"
        return verdict

    from athenaeum.models import cache_usage_counts

    input_tokens, output_tokens, _cache_w, _cache_r = cache_usage_counts(response)
    verdict.input_tokens += input_tokens
    verdict.output_tokens += output_tokens
    if usage is not None:
        usage.add(input_tokens, output_tokens, model=model, knob="verify")

    try:
        parsed = parse_verify_response(response_text(response))
    except (ValueError, json.JSONDecodeError, AttributeError, IndexError) as exc:
        # See the matching branch in propose_page (issue athenaeum#1717):
        # response_text()'s intentional thinking-block fallback can raise
        # AttributeError/IndexError, and it must not escape this call site.
        verdict.error = f"verify parse error: {exc}"
        return verdict

    verdict.verified = True
    verdict.verifier_verdict = parsed["verdict"]
    verdict.verifier_agree = parsed["agree"]
    verdict.verifier_reason = parsed["reason"]
    verdict.verifier_claim = parsed["claim"]
    return verdict


def _stable_sample_fraction(key: str, fraction: float) -> bool:
    """Deterministic membership test: ``True`` for a stable ~*fraction* slice
    of any key set, keyed by a SHA-256 hash of *key* (never Python's salted
    ``hash()``, and never the corpus's iteration order) so the SAME uids
    fall in or out of the sample across repeated runs and across processes."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < fraction


def select_verify_sample(
    verdicts: list[ProposalVerdict], *, rule: str, sample_fraction: float = 0.10
) -> set[int]:
    """Indices into *verdicts* the verifier pass should re-check.

    *rule* is ``"sampled"`` (every low-confidence proposal plus a fixed
    stable *sample_fraction* of the rest) or ``"all"`` (every proposal).
    ``hold``/``error`` verdicts are never sent to the verifier -- there is
    nothing for it to confirm.
    """
    eligible = [i for i, v in enumerate(verdicts) if v.verdict in _VALID_VERDICTS]
    if rule == "all":
        return set(eligible)
    if rule != "sampled":
        raise ValueError(f"unknown verify rule: {rule!r}")
    selected = set()
    for i in eligible:
        v = verdicts[i]
        if v.confidence == "low":
            selected.add(i)
        elif _stable_sample_fraction(f"{v.uid}:{v.date}", sample_fraction):
            selected.add(i)
    return selected


def choose_verify_rule(agreement_rate: float, *, threshold: float = 0.90) -> str:
    """The issue's own AC: verify low-confidence + a fixed 10% sample when
    measured agreement is at least *threshold*; verify everything otherwise."""
    return "sampled" if agreement_rate >= threshold else "all"


# --- agreement measurement (step 3) ------------------------------------


@dataclass
class AgreementReport:
    total: int = 0
    agree: int = 0
    hold: int = 0
    confusion: dict[tuple[str, str], int] = field(default_factory=dict)
    by_confidence: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def agreement_rate(self) -> float:
        scored = self.total - self.hold
        return (self.agree / scored) if scored else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "agree": self.agree,
            "hold": self.hold,
            "agreement_rate": round(self.agreement_rate, 4),
            "confusion": {f"{k[0]}->{k[1]}": v for k, v in self.confusion.items()},
            "by_confidence": self.by_confidence,
        }


def measure_agreement(rows: list[dict[str, str]]) -> AgreementReport:
    """Agreement between a proposer label and an operator (ground-truth)
    label over a generic row list: each row is
    ``{"proposed": ..., "operator": ..., "confidence": ...}``.

    ``hold`` rows are counted separately, never as disagreements (they have
    no proposer label to compare) -- issue athenaeum#1717 step 3 instruction.
    """
    report = AgreementReport()
    for row in rows:
        report.total += 1
        proposed = row["proposed"]
        operator = row["operator"]
        confidence = row.get("confidence") or "unknown"
        if proposed == "hold":
            report.hold += 1
            continue
        agree = proposed == operator
        if agree:
            report.agree += 1
        report.confusion[(proposed, operator)] = report.confusion.get((proposed, operator), 0) + 1
        bucket = report.by_confidence.setdefault(confidence, {"total": 0, "agree": 0})
        bucket["total"] += 1
        if agree:
            bucket["agree"] += 1
    return report


# --- corpus scan (dry-run report) ---------------------------------------


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    return sorted(p for p in wiki_root.rglob("*.md") if p.is_file() and not p.name.startswith("_"))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
        return None


def _stratified_sample(candidates: list[Path], n: int, seed: int) -> list[Path]:
    rng = random.Random(seed)
    if n >= len(candidates):
        return list(candidates)
    return sorted(rng.sample(candidates, n), key=lambda p: str(p))


@dataclass
class PasteCleanupReport:
    scanned: int = 0
    bullets_found: int = 0
    proposed: list[ProposalVerdict] = field(default_factory=list)
    verify_rule: str = "sampled"
    model: str = ""
    verify_model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    #: Set (issue athenaeum#1717 AC4) when ``spend.ceiling_tripped`` stopped
    #: the pass before every candidate bullet was processed -- the human
    #: reason string it returned, e.g. "per-run API dollar ceiling reached
    #: (...)". ``None`` when the pass ran to completion (including every
    #: ``--mechanical-dry-run`` run, which never checks the ceiling).
    ceiling_reason: str | None = None

    def by_final_verdict(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.proposed:
            counts[v.final_verdict()] = counts.get(v.final_verdict(), 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": PASTE_CLEANUP_VERSION,
            "scanned": self.scanned,
            "bullets_found": self.bullets_found,
            "verify_rule": self.verify_rule,
            "model": self.model,
            "verify_model": self.verify_model,
            "by_final_verdict": self.by_final_verdict(),
            "proposed": [v.to_dict() for v in self.proposed],
            "ceiling_reason": self.ceiling_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PasteCleanupReport":
        """Reconstruct from :meth:`to_dict`'s shape -- the ``--from-report``
        replay path (issue athenaeum#1903 step 3). Raises :class:`ValueError`
        on a version mismatch; a report from a different
        :data:`PASTE_CLEANUP_VERSION` may not carry the fields the current
        :func:`apply_paste_cleanup_report` relies on, so replaying it is
        refused rather than guessed at.
        """
        version = d.get("version")
        if version != PASTE_CLEANUP_VERSION:
            raise ValueError(
                f"--from-report version mismatch: report is {version!r}, "
                f"this athenaeum build is {PASTE_CLEANUP_VERSION!r}"
            )
        return cls(
            scanned=d.get("scanned", 0),
            bullets_found=d.get("bullets_found", 0),
            proposed=[ProposalVerdict.from_dict(v) for v in d.get("proposed", [])],
            verify_rule=d.get("verify_rule", "sampled"),
            model=d.get("model", ""),
            verify_model=d.get("verify_model", ""),
            ceiling_reason=d.get("ceiling_reason"),
        )

    def render_text(self) -> str:
        lines = [
            f"scanned {self.scanned} pages, {self.bullets_found} bullets > 500 chars",
            f"verify rule: {self.verify_rule}",
        ]
        for verdict, count in sorted(self.by_final_verdict().items()):
            lines.append(f"  {verdict}: {count}")
        if self.ceiling_reason is not None:
            lines.append(f"stopped early: spend ceiling reached ({self.ceiling_reason})")
        return "\n".join(lines)


def build_paste_cleanup_report(
    wiki_root: Path,
    *,
    client: Any,
    verify_client: Any,
    model: str,
    verify_model: str,
    verify_rule: str,
    length_threshold: int = 500,
    limit: int | None = None,
    sample: int | None = None,
    seed: int = 0,
    uids: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> PasteCleanupReport:
    """Dry-run scan + proposer + verifier pass. Never writes to *wiki_root*.

    Issue athenaeum#1717 (AC4): when *client* is given (i.e. not a
    ``--mechanical-dry-run``), ``spend.ceiling_tripped`` is checked before
    each further proposer/verifier call against one run-scoped
    :class:`TokenUsage` accumulator, mirroring ``audit.py``'s per-item
    ceiling check; a trip stops the pass early -- recorded on
    ``report.ceiling_reason``, never raised -- and one
    ``spend.record_spend`` row under ``spend.RUN_TYPE_PASTE_CLEANUP`` is
    written for the run's accrued usage at the end, mirroring ``audit.py``'s
    single end-of-run call (never a per-batch row). *client* being ``None``
    skips both entirely: a mechanical dry run makes no LLM call, so there is
    nothing to record and no ceiling to check.
    """
    report = PasteCleanupReport(model=model, verify_model=verify_model, verify_rule=verify_rule)
    pages = discover_wiki_pages(wiki_root)
    if uids:
        wanted = set(uids)
        pages = [p for p in pages if p.stem in wanted or any(p.stem.startswith(u) for u in wanted)]
    if sample is not None:
        pages = _stratified_sample(pages, sample, seed)
    if limit is not None:
        pages = pages[:limit]

    candidates: list[tuple[Path, dict[str, Any], str, str, str]] = []
    for path in pages:
        text = _read(path)
        if text is None:
            continue
        report.scanned += 1
        meta, body = split_frontmatter(text)
        uid = meta.get("uid") or path.stem
        for date, content, raw_chunk in extract_notes_bullets(body):
            if len(content) <= length_threshold:
                continue
            report.bullets_found += 1
            candidates.append((path, meta, date, content, raw_chunk, uid))  # type: ignore[arg-type]

    # Issue athenaeum#1717 (AC4): resolved once, only when there is a client to
    # spend against -- `resolved_provider` stays a plain `str` (never
    # `None`) either way, but is only ever passed to `spend.*` inside a
    # `client is not None` guard, so the placeholder value is never used.
    resolved_provider = ""
    run_usage = TokenUsage()
    if client is not None:
        from athenaeum import spend
        from athenaeum.provider import resolve_provider

        resolved_provider = resolve_provider(config, knob="classify")

    proposals: list[ProposalVerdict] = []
    for path, meta, date, content, raw_chunk, uid in candidates:  # type: ignore[misc]
        if client is not None:
            _ceiling = spend.ceiling_tripped(run_usage, provider=resolved_provider, config=config)
            if _ceiling is not None:
                report.ceiling_reason = _ceiling
                log.error(
                    "paste-cleanup: spend ceiling reached (%s) -- stopping "
                    "proposer pass early, %d bullet(s) left unprocessed",
                    _ceiling,
                    len(candidates) - len(proposals),
                )
                break
        verdict = propose_page(
            client,
            uid=uid,
            path=path,
            date=date,
            raw_chunk=raw_chunk,
            meta=meta,
            content=content,
            model=model,
            usage=run_usage if client is not None else None,
        )
        proposals.append(verdict)

    if report.ceiling_reason is None:
        verify_indices = sorted(select_verify_sample(proposals, rule=verify_rule))
        for i in verify_indices:
            if client is not None:
                _ceiling = spend.ceiling_tripped(
                    run_usage, provider=resolved_provider, config=config
                )
                if _ceiling is not None:
                    report.ceiling_reason = _ceiling
                    log.error(
                        "paste-cleanup: spend ceiling reached (%s) -- "
                        "stopping verifier pass early",
                        _ceiling,
                    )
                    break
            v = proposals[i]
            meta, _ = split_frontmatter(_read(v.path) or "")
            verify_page(
                verify_client,
                v,
                meta=meta,
                model=verify_model,
                usage=run_usage if client is not None else None,
            )

    for v in proposals:
        report.usage.add_tokens(v.input_tokens, v.output_tokens, model=v.model)

    report.proposed = proposals

    if client is not None:
        spend.record_spend(
            run_usage,
            run_type=spend.RUN_TYPE_PASTE_CLEANUP,
            provider=resolved_provider,
            files_processed=len(proposals),
            config=config,
            wiki_root=wiki_root,
        )

    return report


def apply_paste_cleanup_report(report: PasteCleanupReport, wiki_root: Path) -> int:
    """Write every ``remove``/``rewrite`` final verdict in *report*. Returns
    files-changed count.

    Re-reads each page and re-locates the exact ``raw_chunk`` before writing
    -- never trusts the scan (same discipline as ``apply_audit_report``).
    ``keep``/``hold``/``error`` verdicts are never written.
    """
    from athenaeum.atomic_io import atomic_write_text

    changed_paths: set[Path] = set()
    by_path: dict[Path, list[ProposalVerdict]] = {}
    for v in report.proposed:
        by_path.setdefault(v.path, []).append(v)

    for path, verdicts in by_path.items():
        text = _read(path)
        if text is None:
            continue
        meta, body = split_frontmatter(text)
        original_body = body
        for v in verdicts:
            final = v.final_verdict()
            if final not in ("remove", "rewrite"):
                continue
            if v.raw_chunk not in body:
                # Page changed since the scan; skip rather than guess.
                continue
            if final == "remove":
                body = body.replace(f"\n\n{v.raw_chunk}", "", 1)
                body = body.replace(v.raw_chunk, "", 1)
            elif final == "rewrite":
                new_bullet = f"- {v.date}: {v.final_claim()}"
                body = body.replace(v.raw_chunk, new_bullet, 1)
        if body != original_body:
            atomic_write_text(path, render_frontmatter(meta) + "\n" + body)
            changed_paths.add(path)

    return len(changed_paths)


__all__ = [
    "PASTE_CLEANUP_VERSION",
    "PasteCleanupReport",
    "ProposalVerdict",
    "AgreementReport",
    "apply_paste_cleanup_report",
    "build_paste_cleanup_report",
    "choose_verify_rule",
    "discover_wiki_pages",
    "extract_notes_bullets",
    "extract_paste_span",
    "measure_agreement",
    "parse_propose_response",
    "parse_verify_response",
    "propose_page",
    "render_propose_prompt",
    "render_verify_prompt",
    "select_verify_sample",
    "split_frontmatter",
    "verify_page",
]
