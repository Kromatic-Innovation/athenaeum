# SPDX-License-Identifier: Apache-2.0
"""Intake-attachment (routing) live-API eval — issue athenaeum#1580.

Runs :func:`athenaeum.librarian.process_one` — the whole tier chain, against a
REAL materialized wiki — once per case in ``tests/evals/data/attachment/
cases.yaml``, and grades where the source landed.

Why the entry point is ``process_one`` and not a hand-composed
tier1→tier2→tier3 sequence: the attach-vs-mint decision is not made in any one
tier. Tier 1 (``tiers.tier1_programmatic_match`` over ``models.EntityIndex``)
resolves exact name/alias hits deterministically; Tier 2 decides which
entities a source is even about; the create-name gate
(``tiers.gate_create_name_classifications``) can flip a ``create`` to an
``update`` at Tier 3's doorstep by setting ``existing_uid``. An eval that
composed those calls itself would be grading its own composition, not the
librarian's. ``process_one`` is the seam every production caller uses
(``librarian.py:7125``), so it is the seam this grades.

**What is graded.** Structure only, as a BEFORE/AFTER delta of the wiki tree —
which pages exist, which changed, which ``related:``/``sources:`` rows
appeared, and whether a pending-decision surface grew. See
``tests/evals/attachment.py`` for the grader, including why an edge stamped by
the athenaeum#1576 relatedness writer (``role: term-overlap``) is excluded from
the attachment signal: that writer fires on every newly-created page in a
96-page corpus, so counting its edges would score the eval's own confound as a
success exactly where the librarian failed.

**Expected RED on the shipped librarian (AC3).** :data:`ATTACHMENT_FLOOR` is
set to what a CORRECT librarian scores, not to what today's scores. Every
sibling layer's floor is descriptive of expected behaviour; here that would be
backwards, because the issue's own anti-vacuity criterion is that this layer
must fail today on at least one of B/C/E while passing D. Tuning the floor to
observed behaviour would turn the layer into a rubber stamp. This is safe:
``-m eval`` is deselected from ordinary CI (``pyproject.toml``) and
``evals.yml`` is dispatch/main-push only, so a red layer here never blocks
develop.

**AC4 is an invariant, not a score.** ``docs/north-star.md`` §2.8: anything
irreversible — a merge, a demotion to source document — is a PROPOSAL reaching
the pending queue, never an applied change. A case that scores badly is a
model result; a page that VANISHED is a contract violation, so it is asserted
outright in :func:`test_attachment_case` rather than folded into the per-case
score.

**Observed baseline, 2026-09-10** (classify ``claude-haiku-4-5-20251001``,
write ``claude-sonnet-5``, corpus scale ``core``) — **3/5**:

* **A** ``same_name_source_attaches`` — PASS. Attached to the existing page;
  nothing minted.
* **B** ``name_variant_source_attaches`` — PASS. Tier 1 matched the qualified
  variant outright.
* **C** ``board_source_attaches_to_entity`` — **FAIL**. Minted a SECOND page
  named "Steepgate", and touched an unrelated corpus page instead of the
  client the board documents.
* **D** ``new_entity_mints_page`` — PASS. Minted, correctly. The negative
  control holds, so the layer does not license attach-everything.
* **E** ``two_existing_entities_both_touched`` — **FAIL**. Reached both
  entities, but ALSO minted a third page for an artifact the session merely
  discussed.

Every routing decision above was made at the WRITE tier. AC3's bar was "fails
on at least one of B/C/E while passing D"; C and E fail and D passes, so the
fixture is not too easy.

C is the operator-reported failure reproduced exactly: a board that is
evidence FOR an entity became a page competing with it. E is the same shape
one step milder. Both failures are mints, which is why
:attr:`~tests.evals.attachment.WikiDelta.minted` and not edge-counting is the
load-bearing signal.

On AC2: not one of the five was settled deterministically, so today's
attach-vs-mint routing costs Sonnet on every source. That is the second thing
this layer measures, and the first time it has been visible.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.config import DEFAULT_CLASSIFY_MODEL
from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, RawFile, TokenUsage
from athenaeum.tiers import DEFAULT_WRITE_MODEL
from tests.evals.attachment import (
    attribute_tier,
    diff_wiki,
    score_case,
    snapshot_wiki,
)
from tests.evals.corpus import build_corpus
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_ATTACHMENT,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


#: Aspirational, NOT descriptive — see the module docstring. Five cases, one
#: per outcome class; a correct librarian attaches on A/B/C/E and mints on D.
#: The floor leaves one case of slack for model noise on the hardest case,
#: matching MERGE_FLOOR / UNDERDETERMINED_FLOOR's ratio, but sits above the
#: baseline athenaeum#1580 recorded, so the layer is RED until the routing
#: behaviour it grades actually improves.
ATTACHMENT_FLOOR = 4  # >= 4/5

VALID_ACCESS = ["open", "internal", "confidential", "personal"]

_CASES_PATH = EVAL_DATA_ROOT / "attachment" / "cases.yaml"


def _load_spec() -> dict[str, Any]:
    return dict(yaml.safe_load(_CASES_PATH.read_text(encoding="utf-8")))


def _load_cases() -> list[dict[str, Any]]:
    return list(_load_spec().get("cases") or [])


def _render_overlay_page(page: dict[str, Any]) -> str:
    """Render one ``wiki_pages`` entry as a wiki page.

    Deliberately mirrors ``tests.evals.corpus.Page.to_markdown`` — quoted
    scalars, same key order — rather than reusing it, because these pages are
    NOT corpus pages: they carry no probe ground truth, are not in the
    fingerprint, and must not become a fourth thing ``validate_core`` has to
    know about. Quoting is load-bearing for the same YAML 1.1 reason that
    module documents.
    """

    def q(value: Any) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["---", f"uid: {page['uid']}", f"type: {page['type']}", f"name: {q(page['name'])}"]
    if page.get("aliases"):
        lines.append("aliases:")
        lines.extend(f"  - {q(a)}" for a in page["aliases"])
    lines.append(f"access: {page.get('access', 'internal')}")
    if page.get("tags"):
        lines.append("tags:")
        lines.extend(f"  - {q(t)}" for t in page["tags"])
    lines.append(f"source_type: {page.get('source_type', 'user-stated')}")
    lines.append(f"source_ref: {q(page.get('source_ref', ''))}")
    lines.append(f"created: {page.get('created', '2026-01-01')}")
    lines.append(f"updated: {page.get('updated', page.get('created', '2026-01-01'))}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + str(page["body"]).rstrip() + "\n"


def build_case_wiki(root: Path, spec: dict[str, Any]) -> Path:
    """Materialize the ``core`` corpus into *root* and overlay this layer's pages.

    A fresh tree per case, so one case's writes can never be another case's
    starting state — the delta grading in ``attachment.py`` is only meaningful
    against a known BEFORE.
    """
    wiki = build_corpus(scale="core").materialize(root)
    for page in spec.get("wiki_pages") or []:
        (wiki / f"{page['uid']}.md").write_text(_render_overlay_page(page), encoding="utf-8")
    return wiki


def declared_vocabulary(wiki: Path) -> tuple[list[str], list[str]]:
    """Return ``(valid_types, valid_tags)`` as DECLARED BY THE WIKI ITSELF.

    Read off the materialized tree rather than hardcoded, so the vocabulary a
    case is graded under is the vocabulary its own corpus actually uses. A
    hardcoded list would let a corpus edit silently narrow (or widen) what the
    classifier is allowed to say, and the resulting misroutes would read as
    model regressions.
    """
    from athenaeum.models import parse_frontmatter

    types: set[str] = set()
    tags: set[str] = set()
    for path in wiki.glob("*.md"):
        if path.name.startswith("_"):
            continue
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("type"):
            types.add(str(meta["type"]))
        for tag in meta.get("tags") or ():
            tags.add(str(tag))
    return sorted(types), sorted(tags)


class _SequencedRecorder:
    """A :class:`RecordingClient` that gives every call its own fixture id.

    Every other eval layer makes exactly ONE metered call per case, so
    ``RecordingClient``'s one-fixture-per-case path
    (``tests/fixtures/recorded/<layer>/<case_id>.json``) is a faithful record.
    An intake run makes several — Tier 2 once, then Tier 3 once per action —
    and under the plain recorder each would overwrite the last, leaving a
    fixture set that silently records only the FINAL call of every case.

    Stamping ``<case_id>-<NN>`` per call keeps the recording faithful and, as
    a side effect, gives the tier attribution below the ordered model sequence
    it needs. The counter is per-case and monotonic, so a re-record produces
    the same ids for the same call sequence.
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
        self._recorder = RecordingClient(inner, record=record, layer=LAYER_ATTACHMENT)
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
        # Fold this call into the RUN-level accumulator, same as every other
        # layer. An intake run makes several calls per case, so a layer that
        # skipped this would spend real budget invisibly to
        # ``EVAL_TOKEN_CEILING`` -- the one guard that exists to stop a golden
        # set ballooning cost unnoticed.
        self._session.observe_response(model, response)
        self._observed.append(model)
        return response


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_attachment_case(
    case: dict[str, Any],
    tmp_path: Path,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one intake-routing case; record its outcome for the aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (:func:`test_attachment_aggregate_floor`) does. The one exception is the
    AC4 invariant asserted at the end, which is a contract violation rather
    than a model result.
    """
    spec = _load_spec()
    wiki = build_case_wiki(tmp_path, spec)
    valid_types, valid_tags = declared_vocabulary(wiki)

    raw_dir = tmp_path / "raw" / "sessions"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"20260701T120000Z-{case['id'][:8]}.md"
    raw_path.write_text(str(case["source"]), encoding="utf-8")
    raw = RawFile(
        path=raw_path,
        source="sessions",
        timestamp="20260701T120000Z",
        uuid8="a77ac401",
        _content=str(case["source"]),
    )

    before = snapshot_wiki(wiki)

    observed_models: list[str] = []
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
        valid_types=valid_types,
        valid_tags=valid_tags,
        valid_access=VALID_ACCESS,
        usage=usage,
        write_client=write_client,
    )

    after = snapshot_wiki(wiki)
    delta = diff_wiki(before, after)
    attribution = attribute_tier(
        observed_models,
        matched=int(getattr(result, "matched", 0) or 0),
        escalated=len(result.escalated),
        classify_model=DEFAULT_CLASSIFY_MODEL,
        write_model=DEFAULT_WRITE_MODEL,
    )

    passed, detail = score_case(case, delta)

    observed = (
        f"minted={sorted(delta.minted_names.values())} "
        f"touched={sorted(delta.touched_uids)} "
        f"attachment_edges={sum(len(v) for v in delta.attachment_edges.values())} "
        f"incidental_term_overlap_edges="
        f"{sum(len(v) for v in delta.incidental_edges.values())} "
        f"queues_grown={sorted(delta.grown_queues)}"
    )
    eval_session.record_case(
        LAYER_ATTACHMENT,
        case["id"],
        expected=str(case["expected"]),
        observed=observed,
        # AC2: the tier that made the routing decision, OBSERVED from the call
        # sequence, so a pass bought with the expensive tier is visible here.
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {attribution.describe()} {detail}",
    )

    # AC4 / docs/north-star.md 2.8 — an invariant, not a score. A page that
    # disappeared means an irreversible act was APPLIED where a proposal was
    # required, and no aggregate floor should be able to absorb that.
    assert not delta.removed_uids, (
        f"{case['id']}: pages vanished from the wiki ({sorted(delta.removed_uids)}). "
        "An irreversible outcome must reach the pending queue as a proposal, "
        "never be applied by a compile (docs/north-star.md 2.8)."
    )


def test_attachment_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the attachment layer meets the aggregate floor.

    EXPECTED RED on the shipped librarian — see the module docstring and
    issue athenaeum#1580 AC3. The failure message is the layer's product: it
    names which routing decisions the librarian is getting wrong today.
    """
    passed, total = eval_session.layer_score(LAYER_ATTACHMENT)
    assert total > 0, "attachment eval collected no cases"
    assert passed >= ATTACHMENT_FLOOR, (
        f"attachment below aggregate floor: {passed}/{total} "
        f"(need >= {ATTACHMENT_FLOOR}). Classify model: {DEFAULT_CLASSIFY_MODEL}; "
        f"write model: {DEFAULT_WRITE_MODEL}. "
        "Check eval-summary.json for per-case failures and the per-case tier "
        "attribution."
    )
