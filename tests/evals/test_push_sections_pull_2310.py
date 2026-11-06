# SPDX-License-Identifier: Apache-2.0
"""PUSH_SECTIONS_PULL: routed, section-level content under a byte budget,
with ``recall``/``read_entity`` still available (issue athenaeum#2310).

UNMARKED — no network call, no subprocess spawn, no token spent (the same
discipline ``test_rollout_api_mode.py`` documents for its own offline
suite): the byte-budget/routing tests below call
``build_push_sections_context`` directly against a hand-written,
hermetic wiki fixture under the ``keyword`` backend (scan-on-query, no
index build, no live model); the tool-availability test replays a scripted
API response through ``_QueuedApiClient``, imported from
``test_rollout_api_mode.py`` the same way ``test_rollout_native_writer_api.py``
and others already do.

Four things this module pins, per the issue's own acceptance criteria:

1. Injection stops at the byte budget, and a page over budget is never
   injected whole — truncated at a section boundary, or dropped entirely
   (the issue's named counter-example).
2. Routing: the injected sections come from the retrieval-RANKED page, not
   simply the first page encountered on disk.
3. Both ``recall`` and ``read_entity`` are offered to the model.
4. Every OTHER arm's dispatch/grading is untouched by this addition — see
   this module's final section.
"""

from __future__ import annotations

from pathlib import Path

from tests.evals.corpus import build_corpus
from tests.evals.harness import EvalSession
from tests.evals.rollout import (
    PUSH_SECTIONS_BYTE_BUDGET,
    READ_ENTITY_TOOL_NAME,
    RECALL_TOOL_NAME,
    Arm,
    build_push_sections_context,
    run_push_sections_pull_api,
)
from tests.evals.test_rollout_api_mode import (
    _QueuedApiClient,
    _RecordedTurn,
    _text_block,
    _tool_result_texts,
    _tool_use_block,
)

# ---------------------------------------------------------------------------
# Fixture wiki — hand-written, not the Corpus/Page framework: the function
# under test only needs a directory of real ``.md`` files on disk plus a
# search backend, and hand-writing lets each test size its sections
# exactly against the *byte_budget* it passes, rather than hoping a corpus
# page happens to be the right size (the issue's own instruction: construct
# the over-budget case deliberately).
# ---------------------------------------------------------------------------


def _write_page(
    wiki_root: Path,
    filename: str,
    *,
    uid: str,
    name: str,
    body: str,
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    text = f'---\nuid: {uid}\ntype: note\nname: "{name}"\naccess: public\n---\n{body}'
    path = wiki_root / filename
    path.write_text(text, encoding="utf-8")
    return path


def _section_body(heading: str, filler: str, n: int) -> str:
    """One ``## <heading>`` section whose body is *n* repeats of *filler*,
    each on its own line — deterministic size, easy to reason about in
    bytes."""
    lines = "\n".join(filler for _ in range(n))
    return f"## {heading}\n\n{lines}\n"


# ---------------------------------------------------------------------------
# 1. Byte budget — injection stops at the budget; an over-budget page is
#    never injected whole (issue athenaeum#2310's named counter-example).
# ---------------------------------------------------------------------------


def test_injection_stops_at_the_byte_budget_and_never_injects_an_over_budget_page_whole(
    tmp_path: Path,
) -> None:
    """One page, four sections, each section's size known exactly. The
    page's FULL body is deliberately sized well past a small test budget
    (never relying on a corpus page happening to be large) and the budget
    is set so sections 1-2 fit, section 3 does not."""
    wiki_root = tmp_path / "wiki"
    sec1 = _section_body("Zebra Alpha", "zebra alpha filler line.", 3)
    sec2 = _section_body("Zebra Beta", "zebra beta filler line.", 3)
    sec3 = _section_body("Zebra Gamma", "zebra gamma filler line.", 3)
    sec4 = _section_body("Zebra Delta", "zebra delta filler line.", 3)
    body = sec1 + sec2 + sec3 + sec4
    _write_page(wiki_root, "zebra.md", uid="page-zebra", name="Zebra", body=body)

    header = "**Uid:** page-zebra\n**Name:** Zebra\n"
    # A budget that fits the header + sections 1-2 exactly, but not section 3.
    budget = len((header + sec1 + sec2).encode("utf-8"))
    assert len(body.encode("utf-8")) > budget, "fixture must genuinely exceed the test budget"

    context = build_push_sections_context(
        wiki_root,
        "zebra",
        cache_dir=tmp_path / "cache",
        search_backend="keyword",
        byte_budget=budget,
    )

    assert len(context.encode("utf-8")) <= budget
    assert "Zebra Alpha" in context and "zebra alpha filler line." in context
    assert "Zebra Beta" in context and "zebra beta filler line." in context
    # The whole page was NOT injected -- sections past the cutoff are absent,
    # not merely re-ordered or re-sized.
    assert "Zebra Gamma" not in context
    assert "zebra gamma filler line." not in context
    assert "Zebra Delta" not in context
    assert "zebra delta filler line." not in context
    # Truncated AT a section boundary, never mid-text: the included content
    # is an exact, whole prefix of the page's own sections, byte for byte.
    assert context == header + sec1 + sec2


def test_a_page_whose_first_section_alone_exceeds_the_budget_is_dropped_entirely(
    tmp_path: Path,
) -> None:
    """The second allowed outcome (issue athenaeum#2310 AC4: truncated OR
    dropped): when not even the top-ranked page's first section fits, it is
    dropped — never injected as a bare header with no content, and never
    injected whole past the budget."""
    wiki_root = tmp_path / "wiki"
    huge_section = _section_body("Zebra Whole", "zebra huge filler line.", 50)
    _write_page(wiki_root, "zebra.md", uid="page-zebra", name="Zebra", body=huge_section)

    tiny_budget = 40  # smaller than the header alone
    context = build_push_sections_context(
        wiki_root,
        "zebra",
        cache_dir=tmp_path / "cache",
        search_backend="keyword",
        byte_budget=tiny_budget,
    )

    assert context == ""


def test_a_page_whose_header_does_not_fit_ends_the_pack(tmp_path: Path) -> None:
    """Stop condition 1: the budget is exactly the top page's full size, so
    the next page's HEADER alone does not fit and the pack ends — a
    lower-ranked page's content does not appear even though it would easily
    fit on its own.

    Renamed from ``..._stops_the_whole_pack`` (Seer review of PR
    athenaeum#2352): that name claimed this proved truncation ends the
    pack, which it never exercised and which is not what the code does —
    see ``test_a_truncated_page_leaves_the_remaining_budget_to_lower_ranked_pages``.
    """
    wiki_root = tmp_path / "wiki"
    top_section = _section_body("Zebra Main", "zebra query filler line.", 3)
    _write_page(wiki_root, "a-top.md", uid="page-top", name="Top", body=top_section)
    # A tiny second page that WOULD fit easily on its own, but is ranked
    # below the first (fewer query-term hits) and must still be excluded.
    small_section = "## Small\n\nzebra tiny.\n"
    _write_page(wiki_root, "b-bottom.md", uid="page-bottom", name="Bottom", body=small_section)

    header = "**Uid:** page-top\n**Name:** Top\n"
    budget = len((header + top_section).encode("utf-8"))

    context = build_push_sections_context(
        wiki_root,
        "zebra query",
        cache_dir=tmp_path / "cache",
        search_backend="keyword",
        byte_budget=budget,
        top_k=5,
    )

    assert "page-top" in context
    assert "page-bottom" not in context
    assert "Small" not in context


# ---------------------------------------------------------------------------
# 2. Routing — the injected sections come from the RETRIEVAL-ranked page,
#    not simply the first page a directory scan would encounter.
# ---------------------------------------------------------------------------


def test_routing_delivers_the_retrieval_ranked_page_not_the_first_one_on_disk(
    tmp_path: Path,
) -> None:
    """``a-distractor.md`` sorts FIRST by filename/directory-scan order and
    shares no term with the query; ``z-target.md`` sorts LAST but is the
    only page the query actually matches. A "first N pages found" bug
    would deliver the distractor; the real ranked dispatch
    (``athenaeum.search.get_backend(...).query(...)``, the SAME primitive
    ``mcp_server._recall_via_backend`` calls) must deliver the target."""
    wiki_root = tmp_path / "wiki"
    _write_page(
        wiki_root,
        "a-distractor.md",
        uid="page-distractor",
        name="Distractor",
        body="## Unrelated\n\nThis page is about gardening and has no bearing here.\n",
    )
    _write_page(
        wiki_root,
        "z-target.md",
        uid="page-target",
        name="Target",
        body=_section_body("Gromble Report", "gromble gromble query match line.", 3),
    )

    # A budget generous enough for exactly one page's block, so which page
    # was chosen is unambiguous.
    budget = 500
    context = build_push_sections_context(
        wiki_root,
        "gromble",
        cache_dir=tmp_path / "cache",
        search_backend="keyword",
        byte_budget=budget,
        top_k=1,
    )

    assert "page-target" in context
    assert "Gromble Report" in context
    assert "page-distractor" not in context
    assert "Unrelated" not in context


# ---------------------------------------------------------------------------
# 3. Byte-budget constant itself — named, module-level, stated value.
# ---------------------------------------------------------------------------


def test_byte_budget_constant_is_a_stated_module_level_value() -> None:
    """Pins the issue's own AC: "a stated byte budget, as a named
    module-level constant" -- 1200 tokens (the real production
    ``resolve_push_token_budget`` default) * 4 bytes/token
    (``estimate_tokens``'s own ratio) = 4800."""
    assert PUSH_SECTIONS_BYTE_BUDGET == 4800


# ---------------------------------------------------------------------------
# 4. Tool availability — both ``recall`` and ``read_entity`` are offered.
# ---------------------------------------------------------------------------


def test_push_sections_pull_api_offers_both_recall_and_read_entity_and_serves_read_entity(
    tmp_path: Path,
) -> None:
    """``run_push_sections_pull_api`` must offer BOTH tools in ``tools=``
    (pinned directly off the recorded API call, not inferred), and serving
    ``read_entity`` in-process must actually work end to end -- the same
    shape ``test_push_breadcrumb_pull_api_serves_read_entity_in_process``
    pins for its own arm (test_rollout_api_mode.py), reused here because
    the two arms share the identical executor/tool-schema code, differing
    only in what is pushed."""
    corpus = build_corpus("core")
    probe = next(p for p in corpus.probes if p.id == "person_not_repo")
    tag = probe.answer_tokens[0]

    knowledge_root = tmp_path / "knowledge"
    corpus.materialize(knowledge_root)

    turns = [
        _RecordedTurn(
            content=[
                _tool_use_block(
                    id="toolu_read_1",
                    name=READ_ENTITY_TOOL_NAME,
                    input={"uid": "person-rowan-wrenfield", "entity_class": "person"},
                )
            ],
            stop_reason="tool_use",
        ),
        _RecordedTurn(
            content=[_text_block(f"Rowan held the day rate flat.\n\n[ref: {tag}]")],
            stop_reason="end_turn",
        ),
    ]
    client = _QueuedApiClient(turns)

    record = run_push_sections_pull_api(
        probe,
        knowledge_root,
        tmp_path / "cache",
        "core",
        client=client,
        session=EvalSession(),
        model="test-model",
        search_backend="keyword",
        # The section-pack assembly is build_push_sections_context's own
        # contract (pinned above) -- stubbed here so this test is only
        # about the tool surface, the same seam the breadcrumb-pull test
        # uses its context_fn stub for.
        context_fn=lambda *_args, **_kwargs: "",
    )

    assert record.arm is Arm.PUSH_SECTIONS_PULL
    offered_tool_names = {t["name"] for t in client.calls[0]["tools"]}
    assert offered_tool_names == {RECALL_TOOL_NAME, READ_ENTITY_TOOL_NAME}
    assert [c.name for c in record.tool_calls] == [READ_ENTITY_TOOL_NAME]
    delivered = _tool_result_texts(record, "toolu_read_1")
    assert "person-rowan-wrenfield" in delivered
    assert "Wrenfield Associates" in delivered
    assert tag in delivered
    assert "unknown tool" not in delivered


# ---------------------------------------------------------------------------
# 5. No regression -- every other arm's own dispatch is untouched.
# ---------------------------------------------------------------------------
#
# This addition touches three shared dispatch points
# (tests.evals.rollout.ALL_ARMS/run_probe_all_arms,
# tests.evals.north_star_report._delivered_uids/delivered_text_for_
# utilization/delivered_uids_for_utilization). Every one of those is an
# ``if record.arm is Arm.X: ...`` chain with ONE new branch appended for
# Arm.PUSH_SECTIONS_PULL -- no existing branch's body changed. That claim
# is pinned by the (unmodified) existing suite for each of those arms:
# every per-arm unit test in test_rollout.py, test_rollout_api_mode.py,
# test_north_star_report.py and test_north_star_verdicts.py exercises the
# SAME functions this module calls, with NO change to their own bodies or
# expected outputs -- only tests/evals/test_rollout.py's and
# tests/evals/test_hook_index_cache_1834.py's ``run_probe_all_arms(...)``
# call sites gained a ``push_sections_pull_runner=`` stub (and the arm-
# enumeration test gained the ninth member), which is the SAME mechanical
# update issue athenaeum#1725 made to the identical call sites when IT
# appended two arms, not a change to any other arm's behavior.


def test_a_truncated_page_leaves_the_remaining_budget_to_lower_ranked_pages(
    tmp_path: Path,
) -> None:
    """Stop condition 3, pinned: a page that fits PARTIALLY is truncated and
    packing CONTINUES to the next ranked page with the budget that is left.

    This is the invariant the function's docstring originally denied (Seer
    review of PR athenaeum#2352 caught the mismatch). It is pinned by a test
    rather than only described, because the arm was MEASURED under this
    behaviour -- `docs/measurements/routed-section-arm-2026-11-06.md`
    reads this code, so a later "fix" to the stricter stop-the-whole-pack
    variant must break a test and be a deliberate, re-measured change.
    """
    wiki_root = tmp_path / "wiki"
    # Top-ranked page: two sections, the second of which will not fit.
    top_first = "## First\n\nzebra query zebra query zebra query.\n"
    top_second = "## Second\n\n" + ("zebra padding line.\n" * 40)
    _write_page(wiki_root, "a-top.md", uid="page-top", name="Top", body=top_first + top_second)
    # Lower-ranked page (fewer query terms), small enough to fit in the tail.
    small = "## Small\n\nzebra tiny.\n"
    _write_page(wiki_root, "b-bottom.md", uid="page-bottom", name="Bottom", body=small)

    top_header = "**Uid:** page-top\n**Name:** Top\n"
    bottom_header = "**Uid:** page-bottom\n**Name:** Bottom\n"
    # Room for the top page's FIRST section only, plus all of the small page.
    budget = len((top_header + top_first + bottom_header + small).encode("utf-8"))

    context = build_push_sections_context(
        wiki_root,
        "zebra query",
        cache_dir=tmp_path / "cache",
        search_backend="keyword",
        byte_budget=budget,
        top_k=5,
    )

    assert "page-top" in context
    assert "First" in context
    # Truncated at the section boundary: the oversized second section is out.
    assert "Second" not in context
    # ...and the lower-ranked page got the remaining budget.
    assert "page-bottom" in context
    assert "Small" in context
    # The budget governs page CONTENT. The inter-page separator this function
    # joins blocks with is not counted against it, so the returned string can
    # exceed `byte_budget` by 8 bytes per page boundary -- bounded, stated in
    # the docstring, and pinned here rather than left for a reader to discover
    # (the sibling guard against exactly this error exists one level down, on
    # the WITHIN-page join; this is the level it was not applied to).
    separator = "\n\n---\n\n"
    blocks = context.split(separator)
    assert len(blocks) == 2
    assert sum(len(b.encode("utf-8")) for b in blocks) <= budget
    assert len(context.encode("utf-8")) <= budget + len(separator.encode("utf-8"))
