# SPDX-License-Identifier: Apache-2.0
"""Measure the accuracy of the viewer's ``used`` column (issue athenaeum#1575).

The viewer's third leg — pushed -> pulled -> **used** — is
``athenaeum.push_metrics.determine_references``. This module measures that
rule against ground truth; it never reimplements it, so the matrix moves on
its own when the rule changes.

Originally (issue athenaeum#1575) the rule was "the push id appears as a
substring anywhere in the transcript", and this measurement is what showed two
of its four reachable quadrants were wrong. Issue athenaeum#1585 changed the
rule — whole-token id matching in author-written text, plus a content signal
over the assistant's own text — and added a fifth quadrant for short-id
collision. Re-run the measurement rather than editing its numbers.

Two measurements live here:

**Synthetic confusion matrix (AC1).** :func:`build_fixtures` constructs five
transcripts whose ground truth is known by construction, one per quadrant:

====================   =============  ==================================
quadrant               truly used?    what the transcript contains
====================   =============  ==================================
``used_with_uid``      yes            the uid, cited in the answer
``used_without_uid``   yes            a breadcrumb (``name — description``,
                                      **no uid**) whose content the answer
                                      acts on
``echoed_not_used``    no             a tool result echoing recall output
                                      (uid present), never acted on
``not_used``           no             neither the uid nor the content
``short_id_collision`` no             an unrelated git SHA whose first
                                      eight characters are the push id
====================   =============  ==================================

:func:`run_synthetic_eval` materializes each fixture as a real push record
plus a real transcript file, runs the real ``determine_references`` over it,
and scores the verdict against the known truth. **The unit of the matrix is
one pushed page**, not one session — "false-negative rate on content-only
use" (AC3/AC4) is a per-page rate.

These rates are a property of the fixture set by construction: they
characterize the heuristic's *decision rule*, not the frequency of each
quadrant in real sessions. The quadrants exist because each is reachable,
not because they are equiprobable.

**Rollout agreement (AC2).** When a ``ResultStore`` of north-star rollout
rows is present, :func:`compute_rollout_agreement` compares, per
``probe_class``, the ledger-equivalent signal (uid citation) against the
free content signal (distinctive n-gram overlap) on PUSH_PAGES_UPPER_BOUND arm
rows (issue athenaeum#1574 renamed the bare PUSH arm). Both
signals are already computed by ``north_star_report`` — no judge, no
Anthropic API call, anywhere in this module. When no rows are present the
report says **no records**; it never reports absence as zero agreement.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from athenaeum import push_metrics
from tests.evals.north_star_report import (
    RolloutRow,
    delivered_text_for_utilization,
    delivered_uids_for_utilization,
    distinctive_ngram_overlap,
    uid_citation_rate,
)
from tests.evals.rollout import Arm

#: Where AC3's dated measurement is written. Deliberately NOT
#: ``north_star_report.DEFAULT_MEASUREMENTS_DIR`` (top-level ``measurements/``,
#: which holds rollout run output): the tracked corpus of measurement write-ups
#: the issue compares against — retrieval, tier, floor, memory-model, cost —
#: lives under ``docs/measurements/``, and AC3 names that directory.
DEFAULT_MEASUREMENTS_DIR = Path("docs/measurements")

#: A row's content signal fires when *any* 4-word shingle of the delivered
#: text reappears verbatim in the answer, i.e. overlap strictly above zero.
#: Expressed as a floor rather than a tuned threshold on purpose — a magic
#: cutoff would make the agreement number an artifact of the cutoff.
CONTENT_SIGNAL_MIN_OVERLAP = 0.0

#: The issue that CHANGED the rule this measurement scores. Named in the
#: rendered report so a reader lands on the change rather than assuming the
#: numbers below describe the original substring rule — and so a re-run keeps
#: the link.
HEURISTIC_ISSUE = "athenaeum#1585"

#: Back-compatible alias: this was the "where the finding is tracked" pointer
#: while the finding was still open.
FOLLOWUP_ISSUE = HEURISTIC_ISSUE


# ---------------------------------------------------------------------------
# AC1: synthetic fixtures with known ground truth
# ---------------------------------------------------------------------------


#: The two production push paths (issue athenaeum#1585 AC6). ``mcp`` records the
#: id from frontmatter (``push_metrics.opaque_push_id``); ``sidecar`` derives it
#: from the filename alone (``push_metrics.opaque_push_id_from_filename``),
#: because the FTS5 ``wiki`` table it queries has no ``uid`` column.
PUSH_PATH_MCP = "mcp"
PUSH_PATH_SIDECAR = "sidecar"


@dataclasses.dataclass(frozen=True)
class Fixture:
    """One synthetic session: a single pushed page and a transcript whose
    "did the agent actually use this page?" answer is known by construction."""

    quadrant: str
    session_id: str
    uid: str
    page_body: str
    truly_used: bool
    #: Why this quadrant's truth value is what it is — reproduced in the
    #: measurement doc so a reader can audit the ground truth, not just
    #: trust it.
    rationale: str
    transcript_records: tuple[dict[str, object], ...]
    #: Which production path recorded this page's push id (AC6).
    push_path: str = PUSH_PATH_MCP
    #: Slug half of the on-disk page name, ``<uid>-<slug>.md``.
    page_slug: str = "page"

    @property
    def page_filename(self) -> str:
        """The entity page's on-disk name (``athenaeum.models.WikiEntity.filename``)."""
        return f"{self.uid}-{self.page_slug}.md"


#: The distinctive fact each page carries. Chosen so no uid is a substring of
#: any page body, name or answer text — see
#: ``test_used_heuristic.py::test_fixture_uid_presence_preconditions``, which
#: asserts that rather than trusting it.
_HARBOR_FACT = "the harbour clock is wound anticlockwise on the second Tuesday"
_FERRY_FACT = "the lantern ferry departs on the quarter hour from the east slip"
_SIREN_FACT = "the quarry siren is tested at noon on the first of the month"
_ALMANAC_FACT = "the tidal almanac lists a double low water in late autumn"
_SIGNAL_FACT = "the signal box is inspected before the first down train"


def _user(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict[str, object]:
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


def _tool_result(text: str) -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": text}],
        },
    }


def build_fixtures() -> tuple[Fixture, ...]:
    """The four ground-truth quadrants, in matrix-reading order."""
    return (
        Fixture(
            quadrant="used_with_uid",
            session_id="used-heuristic-with-uid",
            uid="ath-1575-page-alpha",
            page_slug="harbour-clock-winding",
            page_body=f"Harbour clock winding — {_HARBOR_FACT}.",
            truly_used=True,
            rationale=(
                "The agent recalled the page and cited its uid while answering "
                "from its content. Genuine use, and the uid is present."
            ),
            transcript_records=(
                _user("When is the harbour clock wound?"),
                _tool_result(
                    f"**Uid:** ath-1575-page-alpha\nHarbour clock winding — {_HARBOR_FACT}."
                ),
                _assistant(
                    "Per ath-1575-page-alpha, "
                    f"{_HARBOR_FACT}, so schedule the inspection for that morning."
                ),
            ),
        ),
        Fixture(
            quadrant="used_without_uid",
            session_id="used-heuristic-content-only",
            uid="ath-1575-page-beta",
            page_slug="lantern-ferry-schedule",
            page_body=f"Lantern ferry schedule — {_FERRY_FACT}.",
            truly_used=True,
            rationale=(
                "The sidecar injected a breadcrumb line (`name — description`, "
                "which carries no uid) and the agent answered from that "
                "content without ever calling recall. Genuine use; no uid "
                "anywhere in the transcript."
            ),
            transcript_records=(
                _user(
                    "<system-reminder>Relevant memory: Lantern ferry schedule — "
                    f"{_FERRY_FACT}.</system-reminder>\n"
                    "What time should I get to the east slip?"
                ),
                _assistant(f"Since {_FERRY_FACT}, arrive by 14:10 to make the 14:15 departure."),
            ),
        ),
        Fixture(
            quadrant="echoed_not_used",
            session_id="used-heuristic-echo",
            uid="ath-1575-page-gamma",
            page_slug="quarry-siren-testing",
            page_body=f"Quarry siren testing — {_SIREN_FACT}.",
            truly_used=False,
            rationale=(
                "A tool result echoed the recall output verbatim — uid included "
                "— and the agent then explicitly set the page aside. The uid is "
                "in the transcript; nothing downstream drew on it."
            ),
            transcript_records=(
                _user("Why did last night's build fail?"),
                _tool_result(
                    f"**Uid:** ath-1575-page-gamma\nQuarry siren testing — {_SIREN_FACT}."
                ),
                _assistant(
                    "That memory is not relevant here. The build failed because "
                    "a transitive dependency was yanked from the index."
                ),
            ),
        ),
        Fixture(
            quadrant="not_used",
            session_id="used-heuristic-unused",
            uid="ath-1575-page-delta",
            page_slug="tidal-almanac",
            page_body=f"Tidal almanac — {_ALMANAC_FACT}.",
            truly_used=False,
            rationale=(
                "The page was pushed and neither its uid nor its content ever "
                "surfaced. A cheap offer that went unused — the system working "
                "as designed."
            ),
            transcript_records=(
                _user("Rename the staging bucket."),
                _assistant("Renamed the staging bucket and updated the two references."),
            ),
        ),
        Fixture(
            quadrant="short_id_collision",
            session_id="used-heuristic-short-id",
            # A real uid is `uuid4().hex[:8]` (``athenaeum.models.generate_uid``),
            # so EVERY compiled entity's push id is eight hex characters — on
            # both production paths, not just the sidecar one.
            uid="4b17ac02",
            page_slug="signal-box-inspection",
            page_body=f"Signal box inspection — {_SIGNAL_FACT}.",
            truly_used=False,
            push_path=PUSH_PATH_SIDECAR,
            rationale=(
                "The page was pushed by the sidecar and never used. The "
                "session happens to contain a git SHA whose first eight "
                "characters are the push id, so an unanchored substring test "
                "scores it used on hex the page had nothing to do with."
            ),
            transcript_records=(
                _user("Which commit introduced the regression?"),
                _tool_result(
                    "4b17ac02f8e31d5c07ba9e4d6c2185fa30bd7e91 "
                    "fix(runner): retry the upload once\n"
                    "9c3d51e0aa47b28f16d05c7e4fb39a82d1e6470c chore: bump the linter"
                ),
                _assistant(
                    "The regression came in with "
                    "4b17ac02f8e31d5c07ba9e4d6c2185fa30bd7e91; revert that commit."
                ),
            ),
        ),
    )


def push_id_for(fixture: Fixture) -> str:
    """The id *fixture*'s push path actually records — never a hand-written one.

    Issue athenaeum#1585 AC6: the two paths derive the id differently
    (frontmatter vs filename), so the measurement asks ``push_metrics`` which
    one each path produces rather than asserting an id shape it assumed.
    """
    if fixture.push_path == PUSH_PATH_SIDECAR:
        return push_metrics.opaque_push_id_from_filename(fixture.page_filename)
    return push_metrics.opaque_push_id(fixture.page_filename, {"uid": fixture.uid})


def materialize(
    fixture: Fixture, *, cache_dir: Path, projects_root: Path, wiki_root: Path
) -> None:
    """Write *fixture* as a real push record, a real transcript file and a real
    wiki page, in the exact shapes ``determine_references`` reads.

    Uses only ``push_metrics``' public record-building entry points and its
    ``cache_dir`` / ``projects_root`` / ``wiki_root`` injection seams — the
    heuristic under measurement is never monkeypatched or reimplemented here.
    The wiki page is what the content signal reads (issue athenaeum#1585); a
    fixture whose page was absent could only ever exercise the id half of the
    rule.
    """
    if fixture.push_path == PUSH_PATH_SIDECAR:
        # The sidecar has no frontmatter to consult — it builds the record
        # straight from index rows (``athenaeum.context``), so the measurement
        # does too rather than routing a second path through the MCP builder.
        record = push_metrics.PushRecord(
            session_id=fixture.session_id,
            ts=push_metrics.now_iso(),
            query_hash=push_metrics._query_hash("synthetic"),
            backend="fts5",
            items=[
                push_metrics.PushedItem(
                    id=push_id_for(fixture),
                    tier="internal",
                    scope="owner",
                    token_cost=push_metrics.estimate_tokens(fixture.page_body),
                )
            ],
            source="sidecar",
        )
    else:
        record = push_metrics.build_push_record(
            session_id=fixture.session_id,
            query="synthetic",
            backend="fts5",
            hits=[(fixture.page_filename, {"uid": fixture.uid}, fixture.page_body)],
        )
    push_metrics.record_push(record, cache_dir=cache_dir)

    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / fixture.page_filename).write_text(
        f"---\nuid: {fixture.uid}\ntype: concept\n---\n\n{fixture.page_body}\n",
        encoding="utf-8",
    )

    scope_dir = projects_root / "-synthetic-scope"
    scope_dir.mkdir(parents=True, exist_ok=True)
    (scope_dir / f"{fixture.session_id}.jsonl").write_text(
        "".join(json.dumps(rec) + "\n" for rec in fixture.transcript_records),
        encoding="utf-8",
    )


@dataclasses.dataclass(frozen=True)
class ConfusionMatrix:
    """Per-pushed-page confusion matrix for the ``used`` heuristic.

    Positive class = "the heuristic says this page was used".
    """

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.false_negative + self.true_negative

    @property
    def actually_used(self) -> int:
        return self.true_positive + self.false_negative

    @property
    def actually_unused(self) -> int:
        return self.false_positive + self.true_negative

    @property
    def false_negative_rate(self) -> float | None:
        """Fraction of genuinely-used pages the heuristic misses. ``None``
        (never ``0.0``) when no fixture is genuinely used."""
        return None if not self.actually_used else self.false_negative / self.actually_used

    @property
    def false_positive_rate(self) -> float | None:
        """Fraction of genuinely-unused pages the heuristic flags as used."""
        return None if not self.actually_unused else self.false_positive / self.actually_unused


@dataclasses.dataclass(frozen=True)
class QuadrantOutcome:
    """What the heuristic said about one fixture, next to what is true."""

    quadrant: str
    uid: str
    truly_used: bool
    heuristic_used: bool
    rationale: str
    #: Which production push path recorded ``uid`` (issue athenaeum#1585 AC6).
    push_path: str = PUSH_PATH_MCP

    @property
    def cell(self) -> str:
        if self.truly_used:
            return "true_positive" if self.heuristic_used else "false_negative"
        return "false_positive" if self.heuristic_used else "true_negative"


@dataclasses.dataclass(frozen=True)
class SyntheticResult:
    outcomes: tuple[QuadrantOutcome, ...]
    matrix: ConfusionMatrix


def run_synthetic_eval(workdir: Path) -> SyntheticResult:
    """Materialize every fixture under *workdir* and score the real
    ``determine_references`` against their known ground truth.

    Offline and free: no network, no client, no Anthropic API call.
    """
    cache_dir = workdir / "cache"
    projects_root = workdir / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    wiki_root = workdir / "wiki"

    outcomes: list[QuadrantOutcome] = []
    counts = {"true_positive": 0, "false_positive": 0, "false_negative": 0, "true_negative": 0}
    for fixture in build_fixtures():
        materialize(
            fixture, cache_dir=cache_dir, projects_root=projects_root, wiki_root=wiki_root
        )
        result = push_metrics.determine_references(
            fixture.session_id,
            cache_dir=cache_dir,
            projects_root=projects_root,
            wiki_root=wiki_root,
        )
        pushed_id = push_id_for(fixture)
        heuristic_used = result is not None and pushed_id in result.referenced_ids
        outcome = QuadrantOutcome(
            quadrant=fixture.quadrant,
            uid=pushed_id,
            truly_used=fixture.truly_used,
            heuristic_used=heuristic_used,
            rationale=fixture.rationale,
            push_path=fixture.push_path,
        )
        outcomes.append(outcome)
        counts[outcome.cell] += 1

    return SyntheticResult(outcomes=tuple(outcomes), matrix=ConfusionMatrix(**counts))


# ---------------------------------------------------------------------------
# AC2: agreement with the free utilization signals over rollout records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AgreementStat:
    """Ledger-signal vs content-signal agreement for one ``probe_class``."""

    probe_class: str
    n: int
    ledger_used: int
    content_used: int
    agree: int
    ledger_no_content_yes: int
    ledger_yes_content_no: int

    @property
    def agreement_rate(self) -> float | None:
        return None if not self.n else self.agree / self.n


def compute_rollout_agreement(rows: Sequence[RolloutRow]) -> list[AgreementStat]:
    """Per-``probe_class`` agreement between the ledger heuristic (uid
    citation) and the free content signal (distinctive n-gram overlap), over
    PUSH_PAGES_UPPER_BOUND arm rows only (issue athenaeum#1574 renamed the
    bare ``PUSH`` arm to ``push_pages_upper_bound`` — the arm identity this
    function needs is unchanged, only its name).

    PUSH_PAGES_UPPER_BOUND is the arm the viewer's ``used`` column
    describes: it delivers full pages via the SAME ``recall_search``
    rendering production's real push telemetry uid-tags, so its delivered
    text and uids are read straight off the rollout record — no corpus
    rebuild, no judge. (The newer breadcrumb arms deliver no uid marker at
    all — see ``tests.evals.north_star_report.delivered_uids_for_utilization``
    — so they could not serve this cross-check even if substituted.) Rows
    that delivered nothing are skipped: "nothing to cite" is not evidence
    about the heuristic.

    An empty result means **no records**, not zero agreement; the renderer
    is what must say so.
    """
    buckets: dict[str, list[tuple[bool, bool]]] = {}
    for row in rows:
        if row.record.arm is not Arm.PUSH_PAGES_UPPER_BOUND:
            continue
        delivered_uids = delivered_uids_for_utilization(row)
        citation = uid_citation_rate(row.record, delivered_uids)
        if citation is None:
            continue
        overlap = distinctive_ngram_overlap(delivered_text_for_utilization(row), row.record.answer)
        buckets.setdefault(row.record.probe_class, []).append(
            (citation > 0.0, overlap > CONTENT_SIGNAL_MIN_OVERLAP)
        )

    stats: list[AgreementStat] = []
    for probe_class, pairs in sorted(buckets.items()):
        stats.append(
            AgreementStat(
                probe_class=probe_class,
                n=len(pairs),
                ledger_used=sum(1 for ledger, _ in pairs if ledger),
                content_used=sum(1 for _, content in pairs if content),
                agree=sum(1 for ledger, content in pairs if ledger == content),
                ledger_no_content_yes=sum(1 for ledger, content in pairs if not ledger and content),
                ledger_yes_content_no=sum(1 for ledger, content in pairs if ledger and not content),
            )
        )
    return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class UsedHeuristicReport:
    generated: str
    synthetic: SyntheticResult
    agreement: tuple[AgreementStat, ...]
    #: True when a store was consulted at all. ``False`` and an empty
    #: ``agreement`` are different facts: no store vs a store with no PUSH
    #: rows carrying delivered pages.
    store_consulted: bool
    store_path: str | None


def build_report(
    synthetic: SyntheticResult,
    agreement: Sequence[AgreementStat],
    *,
    store_path: Path | None,
    store_consulted: bool,
    generated: str | None = None,
) -> UsedHeuristicReport:
    return UsedHeuristicReport(
        generated=generated or datetime.now(UTC).isoformat(timespec="seconds"),
        synthetic=synthetic,
        agreement=tuple(agreement),
        store_consulted=store_consulted,
        store_path=str(store_path) if store_path is not None else None,
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_report(report: UsedHeuristicReport) -> str:
    """Render *report* as the markdown written under ``docs/measurements/``."""
    m = report.synthetic.matrix
    lines: list[str] = [
        "# `used` column heuristic accuracy",
        "",
        f"Generated: {report.generated}",
        "",
        "Measures `athenaeum.push_metrics.determine_references` — the rule behind the "
        "viewer's `used` column — against known ground truth. The measurement drives "
        "the real function; it never reimplements it.",
        "",
        "## What `used` means today",
        "",
        "A pushed page is marked **used** when either signal fires:",
        "",
        "1. **Citation** — the page's recorded push id appears as a *whole token* "
        "(not embedded in a longer alphanumeric run) in user- or assistant-written "
        "text. A tool result is excluded: recall's own output quoted back is an echo, "
        "not a citation.",
        "2. **Content** — the assistant's own text reproduces a distinctive 4-word "
        "shingle of the pushed page. This is the only signal that can reach "
        "content-only use, where a breadcrumb delivered `name — description` and no "
        "id was ever written down.",
        "",
        "An id present only in a tool result, with no content signal, is an echo and "
        "is not counted — unless the page's own text cannot be resolved, in which case "
        "the two readings are indistinguishable and the prior verdict stands. Both "
        "signals are local and free: no judge, no model call.",
        "",
        "## Synthetic confusion matrix (AC1)",
        "",
        "Unit: **one pushed page**. Positive class: the heuristic says the page was "
        "used. Ground truth is known by construction — one fixture per reachable "
        "quadrant.",
        "",
        "**Push-path coverage.** Both production paths are exercised. A compiled "
        "entity's uid is `uuid4().hex[:8]` (`athenaeum.models.generate_uid`), so the "
        "MCP `recall` path (id read from frontmatter) and the sidecar path (id derived "
        "from the filename) record the *same* eight-hex id for the same page — the "
        "`short_id_collision` fixture goes through the sidecar builder and the other "
        "four through the MCP builder, and a test asserts the two derivations agree. "
        "What the paths do not share is raw-intake hits, where both record the whole "
        "filename; that shape is not fixtured here.",
        "",
        "| | heuristic: used | heuristic: not used |",
        "|---|---|---|",
        f"| **truly used** | {m.true_positive} (TP) | {m.false_negative} (FN) |",
        f"| **truly unused** | {m.false_positive} (FP) | {m.true_negative} (TN) |",
        "",
        f"- False-negative rate (genuinely-used pages missed): **{_pct(m.false_negative_rate)}** "
        f"({m.false_negative}/{m.actually_used})",
        f"- False-positive rate (unused pages flagged used): **{_pct(m.false_positive_rate)}** "
        f"({m.false_positive}/{m.actually_unused})",
        "",
        "### Per-quadrant detail",
        "",
        "| quadrant | push path | truly used | heuristic | cell | "
        "why the truth value is what it is |",
        "|---|---|---|---|---|---|",
    ]
    for outcome in report.synthetic.outcomes:
        lines.append(
            f"| `{outcome.quadrant}` | {outcome.push_path} "
            f"| {'yes' if outcome.truly_used else 'no'} "
            f"| {'used' if outcome.heuristic_used else 'not used'} "
            f"| {outcome.cell.replace('_', ' ')} | {outcome.rationale} |"
        )
    lines += [
        "",
        "**How to read these rates.** The fixture set has one page per quadrant, so "
        "each rate is a property of the fixture design, not an estimate of how often "
        "each quadrant occurs in real sessions. What the matrix establishes is which "
        "quadrants the decision rule can and cannot reach. Under the original "
        "substring rule, content-only use was *structurally* invisible, an echoed id "
        "was *structurally* indistinguishable from a cited one, and a push id was "
        "*structurally* confusable with any hex that happened to start the same way. "
        "Each of those is now reachable by a signal the rule consults; none of it "
        "depends on the sample.",
        "",
        "## Agreement with free utilization signals over rollout records (AC2)",
        "",
        "Compares the ledger-equivalent signal (uid citation) against the free content "
        "signal (distinctive 4-gram overlap between delivered text and the answer) on "
        "PUSH_PAGES_UPPER_BOUND arm rows, per `probe_class`. Both signals are already computed by "
        "`tests/evals/north_star_report.py`; no judge and no Anthropic API call is "
        "involved.",
        "",
    ]
    if not report.store_consulted:
        lines.append(
            "**No rollout result store was supplied — this section was not measured.** "
            "That is an absence of records, not an agreement of zero."
        )
    elif not report.agreement:
        lines.append(
            f"**No records.** The store (`{report.store_path}`) holds no PUSH_PAGES_UPPER_BOUND "
            "arm rows "
            "with delivered pages, so agreement was not measured. This is an absence "
            "of records, not an agreement of zero."
        )
    else:
        lines += [
            f"Store: `{report.store_path}`",
            "",
            "| probe_class | n | ledger says used | content says used | agreement | "
            "ledger no / content yes | ledger yes / content no |",
            "|---|---|---|---|---|---|---|",
        ]
        for stat in report.agreement:
            lines.append(
                f"| {stat.probe_class} | {stat.n} | {stat.ledger_used} | "
                f"{stat.content_used} | {_pct(stat.agreement_rate)} | "
                f"{stat.ledger_no_content_yes} | {stat.ledger_yes_content_no} |"
            )
        lines += [
            "",
            "`ledger no / content yes` counts the suspected false negatives — the "
            "content signal fired where the uid never appeared. `ledger yes / content "
            "no` counts the suspected echoes.",
        ]
    lines += [
        "",
        "## Scope",
        "",
        "Synthetic fixtures only. The rates above characterize the decision rule, not "
        "the frequency of each quadrant in real sessions, and no live session is read "
        f"here. The rule these numbers score was changed by {HEURISTIC_ISSUE}; earlier "
        "dated files in this directory score the original substring rule and are kept "
        "as the before-picture rather than edited.",
        "",
    ]
    return "\n".join(lines)


def write_report(
    report: UsedHeuristicReport,
    *,
    out_dir: Path = DEFAULT_MEASUREMENTS_DIR,
    filename: str | None = None,
) -> Path:
    """Write the rendered report under *out_dir* as a dated measurement (AC3)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    name = filename or f"used-column-heuristic-accuracy-{report.generated[:10]}.md"
    path = out_dir / name
    path.write_text(render_report(report), encoding="utf-8")
    return path
