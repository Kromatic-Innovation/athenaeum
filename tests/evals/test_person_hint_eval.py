# SPDX-License-Identifier: Apache-2.0
"""Person-hint live-API eval — issue athenaeum#1867.

Runs :func:`athenaeum.librarian.process_one` — the whole tier chain, against a
real materialized wiki, **with the person registry enabled** — once per case in
``tests/evals/data/person_hint/cases.yaml``, and grades what each named
person's page gained.

**Why ``person_registry=`` is the whole point.** ``test_attachment_eval.py``
(issue athenaeum#1580) calls the same entry point and does NOT pass one, so
tier 0's person-registry consult never engages there. That consult is the step
this layer grades: it claims any raw file naming a known person, prepends a
dated Notes bullet to that person's page, and early-returns, so tiers 1-3
never see the file. Passing a registry is what puts it on the path.

**The anti-vacuity trap.** On the shipped librarian the person page *does*
change. A grader asking "did the person page change" would score the shipped
path as a pass on all five cases. ``tests/evals/person_hint.py`` separates
four outcomes instead — ``unchanged``, ``notes_bullet``, ``citation_only``,
``footnoted_claim`` — and ``notes_bullet`` is a failure in every case. AC4's
``decided_by`` label reports ``write_merge`` for both ``citation_only`` and
``footnoted_claim``, so the tier attribution alone cannot separate "cited the
source" from "made a claim"; the page delta is the second, required
observable, and both are recorded per case.

**Expected RED on the shipped librarian.** :data:`PERSON_HINT_FLOOR` is what a
CORRECT librarian scores, not what today's scores — the same aspirational
floor ``ATTACHMENT_FLOOR`` carries, and for the same reason: tuning it down to
observed behaviour would make the layer a rubber stamp for the behaviour it
exists to measure. The red is carried by
``pytest.mark.xfail(strict=False, raises=AssertionError)`` on
:func:`test_person_hint_aggregate_floor` so the Live-API eval job stays green
while the floor is unmet, and ``raises=AssertionError`` stops the marker
swallowing an infrastructure error as an "expected" failure. Non-strict for
athenaeum#1686's reason: a strict marker reds a main-push job on a lucky run
rather than on a fix.

**The shipped-path baseline was measured OFFLINE, and that is sound here.**
Every case in this layer is claimed by tier 0, which makes zero model calls —
so unlike every other eval layer, this one's shipped-path score can be taken
with no API key at all. :data:`OFFLINE_BASELINE_ENV` substitutes a client that
RAISES on ``messages.create``: a run that completes is positive evidence that
no tier past 0 ran, and a run that reaches one errors loudly rather than
passing quietly. That is a stronger reading than a live run would give, not a
weaker one, because "zero calls" is asserted by construction rather than
counted. It is never set in CI or in ``evals.yml``; the command is recorded in
``docs/measurements/person-hint-baseline-2026-09-19.md``. The day athenaeum#1866
lands, cases that stop being claimed at tier 0 WILL call a model, the offline
run will error, and the baseline must be re-taken live — which is the correct
signal, not a breakage.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, RawFile, TokenUsage
from athenaeum.person_registry import PersonRegistry
from athenaeum.tiers import DEFAULT_WRITE_MODEL
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_PERSON_HINT,
    RecordingClient,
    build_live_client,
    live_ready,
)
from tests.evals.person_hint import (
    PersonHintObservation,
    attribute_tier,
    classify_person_outcome,
    deterministic_matched,
    diff_wiki,
    duplicated_sentences,
    non_person_pointer_uids,
    read_page_bodies,
    read_page_types,
    score_case,
    snapshot_wiki,
)

pytestmark = pytest.mark.eval

#: Aspirational, NOT descriptive — see the module docstring. Five cases; a
#: correct librarian leaves the page alone on A/D, claims on B/C, and cites
#: without restating on E. One case of slack for model noise, matching
#: ATTACHMENT_FLOOR / MERGE_FLOOR's ratio, and above the score athenaeum#1867
#: recorded for the shipped librarian (0/5) — so the layer is RED until the
#: tier-0 routing change (athenaeum#1866) lands.
PERSON_HINT_FLOOR = 4  # >= 4/5

#: Offline shipped-path measurement (AC6). See the module docstring for why a
#: raising client is the RIGHT instrument for this layer and not a shortcut.
OFFLINE_BASELINE_ENV = "ATHENAEUM_PERSON_HINT_OFFLINE_BASELINE"

VALID_ACCESS = ["open", "internal", "confidential", "personal"]
VALID_TYPES = ["person", "company", "project", "concept", "source"]
VALID_TAGS = ["staff", "supplier", "active"]

_DATA_ROOT = EVAL_DATA_ROOT / "person_hint"
_CASES_PATH = _DATA_ROOT / "cases.yaml"
_WIKI_SRC = _DATA_ROOT / "wiki"
_RAW_SRC = _DATA_ROOT / "raw"


def load_cases() -> list[dict[str, Any]]:
    spec = dict(yaml.safe_load(_CASES_PATH.read_text(encoding="utf-8")))
    return list(spec.get("cases") or [])


def build_case_wiki(root: Path) -> Path:
    """Copy the hand-authored case wiki into *root*.

    A fresh tree per case, so one case's writes can never be another case's
    starting state — the delta grading in ``person_hint.py`` is only
    meaningful against a known BEFORE.
    """
    wiki = root / "wiki"
    shutil.copytree(_WIKI_SRC, wiki)
    return wiki


def build_raw(root: Path, case: dict[str, Any]) -> RawFile:
    raw_dir = root / "raw" / "sessions"
    raw_dir.mkdir(parents=True, exist_ok=True)
    content = (_RAW_SRC / str(case["raw"])).read_text(encoding="utf-8")
    path = raw_dir / f"20260701T120000Z-{str(case['id'])[:8]}.md"
    path.write_text(content, encoding="utf-8")
    return RawFile(
        path=path,
        source="sessions",
        timestamp="20260701T120000Z",
        uuid8="a77ac401",
        _content=content,
    )


class _NoCallClient:
    """A client that turns any model call into a loud error (AC6).

    Used ONLY under :data:`OFFLINE_BASELINE_ENV`. The point is not to avoid
    spend — it is that "the shipped path decided this deterministically" is
    then established by construction rather than by counting calls after the
    fact.
    """

    def __init__(self) -> None:
        self.messages = self

    def create(self, **params: Any) -> Any:  # pragma: no cover - failure path
        raise AssertionError(
            "person_hint offline baseline: a model call was made "
            f"(model={params.get('model')!r}). The shipped tier-0 person "
            "consult claims every case in this layer without one, so this "
            "means the routing changed — re-take the baseline LIVE."
        )


class _SequencedRecorder:
    """A :class:`RecordingClient` giving every call its own fixture id.

    Same shape, and same reason, as ``test_attachment_eval.py``'s: an intake
    run makes several calls per case (Tier 2 once, Tier 3 once per action) and
    the plain recorder's one-fixture-per-case path would record only the last.
    Stamping ``<case_id>-<NN>`` keeps the recording faithful and gives the tier
    attribution the ordered model sequence it needs.
    """

    def __init__(
        self,
        inner: Any,
        *,
        record: bool,
        case_id: str,
        observed: list[str],
        session: Any,
    ) -> None:
        self._recorder = RecordingClient(inner, record=record, layer=LAYER_PERSON_HINT)
        self._case_id = case_id
        self._observed = observed
        self._session = session
        self._n = 0
        self.messages = self

    def create(self, **params: Any) -> Any:
        self._recorder.start_case(f"{self._case_id}-{self._n:02d}")
        self._n += 1
        try:
            response = self._recorder.messages.create(**params)
        finally:
            self._recorder.end_case()
        model = str(params.get("model", ""))
        self._session.observe_response(model, response)
        self._observed.append(model)
        return response


def _offline() -> bool:
    return os.environ.get(OFFLINE_BASELINE_ENV) == "1"


@pytest.fixture(scope="module")
def _live_ready() -> None:
    if _offline():
        return
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_person_hint_case(
    case: dict[str, Any],
    tmp_path: Path,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one person-hint case; record its outcome for the aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (:func:`test_person_hint_aggregate_floor`) does.
    """
    wiki = build_case_wiki(tmp_path)
    raw = build_raw(tmp_path, case)

    before = snapshot_wiki(wiki)
    before_bodies = read_page_bodies(wiki)
    page_types = read_page_types(wiki)

    observed_models: list[str] = []
    if _offline():
        classify_client: Any = _NoCallClient()
        write_client: Any = _NoCallClient()
    else:
        inner = build_live_client()
        classify_client = _SequencedRecorder(
            inner,
            record=eval_record,
            case_id=f"{case['id']}-classify",
            observed=observed_models,
            session=eval_session,
        )
        write_client = _SequencedRecorder(
            inner,
            record=eval_record,
            case_id=f"{case['id']}-write",
            observed=observed_models,
            session=eval_session,
        )

    usage = TokenUsage()
    result = process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        classify_client,
        valid_types=VALID_TYPES,
        valid_tags=VALID_TAGS,
        valid_access=VALID_ACCESS,
        usage=usage,
        write_client=write_client,
        # The one argument that distinguishes this layer from `attachment`.
        person_registry=PersonRegistry(wiki),
    )

    after = snapshot_wiki(wiki)
    after_bodies = read_page_bodies(wiki)
    # Types are read from the AFTER tree too, so a page minted this run is
    # classified by the type it was minted WITH rather than dropping out of
    # the non-person check for having no BEFORE entry.
    page_types = {**read_page_types(wiki), **page_types}
    delta = diff_wiki(before, after)

    person_uids = [uid for uid, ptype in page_types.items() if ptype == "person"]
    outcomes = {
        uid: classify_person_outcome(before_bodies.get(uid, ""), after_bodies.get(uid, ""), raw.ref)
        for uid in person_uids
        if uid in before_bodies or uid in after_bodies
    }
    pointers = non_person_pointer_uids(
        delta=delta,
        before_bodies=before_bodies,
        after_bodies=after_bodies,
        page_types=page_types,
        raw_ref=raw.ref,
    )
    duplicated = {uid: duplicated_sentences(after_bodies.get(uid, "")) for uid in person_uids}

    attribution = attribute_tier(
        observed_models,
        matched=deterministic_matched(result, observed_models),
        escalated=len(result.escalated),
        classify_model=DEFAULT_CLASSIFY_MODEL,
        write_model=DEFAULT_WRITE_MODEL,
    )
    observation = PersonHintObservation(
        case_id=str(case["id"]),
        attribution=attribution,
        person_outcomes=outcomes,
        non_person_pointer_uids=pointers,
    )

    passed, detail = score_case(case, delta, outcomes, pointer_uids=pointers, duplicated=duplicated)

    eval_session.record_case(
        LAYER_PERSON_HINT,
        str(case["id"]),
        expected=str(case["expected"]),
        # AC4: the file-level `decided_by` label AND every graded person
        # page's outcome, in one observation.
        observed=(
            f"{observation.describe()} minted={sorted(delta.minted_names.values())} "
            f"touched={sorted(delta.touched_uids)}"
        ),
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {attribution.describe()} {detail}",
    )

    # An invariant, not a score (docs/north-star.md 2.8): an irreversible
    # outcome reaches the pending queue as a proposal, never as an applied
    # change. Asserted outright so no aggregate floor can absorb it.
    assert not delta.removed_uids, (
        f"{case['id']}: pages vanished from the wiki ({sorted(delta.removed_uids)}). "
        "An irreversible outcome must reach the pending queue as a proposal, "
        "never be applied by a compile (docs/north-star.md 2.8)."
    )


@pytest.mark.xfail(
    strict=False,
    raises=AssertionError,
    reason=(
        "athenaeum#1867: the person_hint layer is aspirationally RED until "
        "the tier-0 routing change (athenaeum#1866) stops the person-registry "
        "consult claiming every file that merely NAMES a known person. "
        "NON-strict on purpose (athenaeum#1686): once the routing change "
        "lands the score becomes a live-API measurement that varies run to "
        "run around the floor, and a strict marker reds the job on a lucky "
        "run rather than on a fix. Removing this marker needs several "
        "consecutive at-or-above-floor runs alongside a routing change that "
        "explains them, not one run."
    ),
)
def test_person_hint_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the person-hint layer meets the aggregate floor.

    EXPECTED RED on the shipped librarian. The failure message is the layer's
    product: it names which person pages are gaining claims they were never
    given, and which files stopped being compiled as a result.
    """
    passed, total = eval_session.layer_score(LAYER_PERSON_HINT)
    assert total > 0, "person_hint eval collected no cases"
    assert passed >= PERSON_HINT_FLOOR, (
        f"person_hint below aggregate floor: {passed}/{total} "
        f"(need >= {PERSON_HINT_FLOOR}). Classify model: {DEFAULT_CLASSIFY_MODEL}; "
        f"write model: {DEFAULT_WRITE_MODEL}. "
        "Check eval-summary.json for per-case outcomes and tier attribution."
    )
