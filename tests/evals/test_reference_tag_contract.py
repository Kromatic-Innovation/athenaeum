# SPDX-License-Identifier: Apache-2.0
"""The reference-tag INSTRUCTION contract (issue athenaeum#1753), and what
survives of it after issue athenaeum#1831.

Every arm's system prompt still carries one IDENTICAL
``REFERENCE_TAG_INSTRUCTION``, unchanged, so
``tests.evals.corpus.test_corpus*``/``test_north_star_report.py``'s own
fixtures keep planting a citable tag on every answer-bearing page. What
CHANGED (operator ruling on athenaeum#1791 comment 5732689494): the tag no
longer feeds :func:`tests.evals.north_star_report.grade_correctness` --
correctness is graded on content markers plus delivered-page evidence now
(see ``tests/evals/test_north_star_report.py``'s counter-example tests). The
tag's own satisfaction survives as the report-only
:func:`tests.evals.north_star_report.tag_followed` diagnostic, and this file
pins ITS contract: every arm's prompt carries the instruction, and
``tag_followed`` actually reads the tag that instruction asks for.

Token-free: no live rollout, no model client, no spend.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.evals import rollout
from tests.evals.corpus import _TAG_LINE_RE, Observation, build_corpus
from tests.evals.north_star_report import grade_correctness, tag_followed
from tests.evals.rollout import (
    _PULL_API_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    REFERENCE_TAG_INSTRUCTION,
    Arm,
    RolloutRecord,
    TurnTokenUsage,
    _native_grep_system_prompt,
    _native_index_system_prompt,
    build_native_argv,
    build_pull_argv,
)

CORPUS_SCALE = "core"
_CORPUS = build_corpus(scale=CORPUS_SCALE)


def _probe(probe_id: str):
    for probe in _CORPUS.probes:
        if probe.id == probe_id:
            return probe
    raise KeyError(probe_id)


def _record(*, arm: Arm, probe_id: str, probe_class: str, answer: str) -> RolloutRecord:
    return RolloutRecord(
        arm=arm,
        probe_id=probe_id,
        probe_class=probe_class,
        corpus_scale=CORPUS_SCALE,
        answer=answer,
        turn_tokens=[TurnTokenUsage(turn=1, input_tokens=10, output_tokens=10)],
        turn_count=1,
    )


def _cli_append_system_prompt(argv: list[str]) -> str:
    """The value ``claude -p`` would actually receive, read back out of the
    built argv rather than assumed -- a CLI arm's system prompt has no
    ``system=`` kwarg to inspect, so the argv IS the prompt."""
    assert "--append-system-prompt" in argv
    return argv[argv.index("--append-system-prompt") + 1]


def _arm_system_prompts(memory_dir: Path) -> dict[str, str]:
    """Every arm's real system-prompt text, DERIVED from the producers the
    rollout code itself calls -- never copied as literals. That is what makes
    the coverage test below meaningful: mutating any one arm's prompt to drop
    the instruction changes what this function returns, and the test fails.
    """
    return {
        "single_shot": _SYSTEM_PROMPT,
        "pull_api": _PULL_API_SYSTEM_PROMPT,
        "native_index_api": _native_index_system_prompt(memory_dir),
        "native_grep_api": _native_grep_system_prompt(memory_dir),
        "pull_cli": _cli_append_system_prompt(
            build_pull_argv("claude", Path("/tmp/example/mcp-config.json"), "claude-haiku-4-5")
        ),
        "native_cli": _cli_append_system_prompt(
            build_native_argv(
                "claude",
                Path("/tmp/example/settings.json"),
                Path("/tmp/example/mcp-config.json"),
                memory_dir,
                "claude-haiku-4-5",
            )
        ),
    }


def test_every_arm_system_prompt_carries_the_reference_tag_instruction(tmp_path: Path) -> None:
    """AC1: the instruction is present in EVERY arm's prompt -- single-shot,
    the tool-using api-mode PULL prompt, both native api-mode prompts, and
    both ``claude -p`` CLI paths."""
    prompts = _arm_system_prompts(tmp_path / "memory")
    missing = [name for name, text in prompts.items() if REFERENCE_TAG_INSTRUCTION not in text]
    assert not missing, f"arms missing the reference-tag instruction: {missing}"


def test_the_instruction_is_byte_identical_across_arms(tmp_path: Path) -> None:
    """The whole reason the instruction does not bias the comparison is that
    every arm gets the SAME text. One shared constant, quoted verbatim -- not
    six per-arm paraphrases that drifted."""
    prompts = _arm_system_prompts(tmp_path / "memory")
    assert len({text.count(REFERENCE_TAG_INSTRUCTION) for text in prompts.values()}) == 1
    for name, text in prompts.items():
        assert text.count(REFERENCE_TAG_INSTRUCTION) == 1, name


def test_the_instruction_plants_no_real_corpus_token() -> None:
    """An ``abstention`` answer grades incorrect the moment ANY planted token
    appears in it. If the instruction illustrated itself with a real tag, a
    model echoing the example would fail the abstention class by construction.
    """
    every_token = {tok for probe in _CORPUS.probes for tok in probe.answer_tokens}
    assert every_token  # the corpus really does plant tokens
    leaked = sorted(tok for tok in every_token if tok.lower() in REFERENCE_TAG_INSTRUCTION.lower())
    assert not leaked, f"instruction leaks planted corpus tokens: {leaked}"


def test_cli_arms_append_rather_than_replace_the_system_prompt() -> None:
    """``--system-prompt`` would discard Claude Code's own instructions --
    for a native-memory arm that is the auto-memory behaviour being measured.
    """
    pull = build_pull_argv("claude", Path("/tmp/example/mcp-config.json"), "claude-haiku-4-5")
    native = build_native_argv(
        "claude",
        Path("/tmp/example/settings.json"),
        Path("/tmp/example/mcp-config.json"),
        Path("/tmp/example/memory"),
        "claude-haiku-4-5",
    )
    for argv in (pull, native):
        assert "--system-prompt" not in argv
        assert argv[argv.index("--append-system-prompt") + 1] == REFERENCE_TAG_INSTRUCTION


def test_build_native_argv_can_omit_the_instruction_on_request() -> None:
    """The opt-out :func:`run_native_writer` uses exists and works. This is
    only the mechanism; the test below pins that the writer actually takes
    it."""
    argv = build_native_argv(
        "claude",
        Path("/tmp/example/settings.json"),
        Path("/tmp/example/mcp-config.json"),
        Path("/tmp/example/memory"),
        "claude-haiku-4-5",
        append_system_prompt=None,
    )
    assert "--append-system-prompt" not in argv
    assert REFERENCE_TAG_INSTRUCTION not in argv


def test_native_writer_spawns_without_the_instruction(tmp_path: Path, monkeypatch) -> None:
    """The write path produces memory files, not graded answers, and telling
    a writer to append ``[ref: ...]`` would risk polluting what it saves.

    Pinned where the exclusion actually happens -- the argv
    :func:`run_native_writer` really spawns, captured through a monkeypatched
    ``subprocess.run`` -- not merely at the ``build_native_argv`` seam.
    Asserting the seam alone would still pass if the writer's call site
    dropped its ``append_system_prompt=None``, which is exactly the
    regression this guards.
    """
    monkeypatch.setattr(rollout.shutil, "which", lambda _binary: "/usr/bin/claude")
    spawned: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        spawned.append(argv)
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(rollout.subprocess, "run", fake_run)

    observation = Observation(
        uid="obs-001",
        page_uid="page-x",
        source="sessions",
        timestamp="20260101T000000Z",
        uuid8="aaaaaa01",
        body="a fact worth saving",
    )
    rollout.run_native_writer([observation], tmp_path)

    assert spawned, "run_native_writer spawned no session"
    for argv in spawned:
        assert "--append-system-prompt" not in argv
        assert REFERENCE_TAG_INSTRUCTION not in argv


# ---------------------------------------------------------------------------
# Grading: the contract is now satisfiable, and still refuses a bare answer
# ---------------------------------------------------------------------------


def test_oracle_answer_ending_in_the_reference_tag_grades_correct() -> None:
    """AC3, the positive control: a recorded-fixture ``oracle`` rollout whose
    answer ends ``[ref: Cinderquill]`` grades CORRECT -- the exact thing the
    first live grid could not produce."""
    probe = _probe("pto_allowance")
    tag = probe.answer_tokens[0]
    assert tag == "Cinderquill"  # the planted tag on policy-pto, read off the corpus

    tagged = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=(
            "The firm's PTO allowance is 25 days per year plus UK bank holidays.\n\n"
            f"[ref: {tag}]"
        ),
    )
    assert grade_correctness(tagged, probe, _CORPUS) is True


def test_correctness_no_longer_needs_the_tag_but_tag_followed_still_does() -> None:
    """Issue athenaeum#1831 (operator ruling on athenaeum#1791 comment
    5732689494): correctness is graded on CONTENT now, never the tag -- so a
    substantively identical, factually correct answer with no tag grades
    RIGHT, not wrong. This is the exact inversion of what this test asserted
    before athenaeum#1831 (it used to pin the old tag-only contract's
    negative control); the tag's OWN satisfaction survives unchanged as the
    separate, report-only :func:`tag_followed` diagnostic, which still grades
    this untagged answer ``False``."""
    probe = _probe("pto_allowance")
    untagged = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer="The firm's PTO allowance is 25 days per year plus UK bank holidays.",
    )
    assert grade_correctness(untagged, probe, _CORPUS) is True
    assert tag_followed(untagged, probe) is False


def test_follow_through_tag_followed_requires_a_tag_from_every_page() -> None:
    """Why the instruction is worded in the plural. A ``follow_through``
    probe plants a token on EACH of two pages -- the breadcrumb page and the
    one reachable only by a link from it. Issue athenaeum#1831: this is now
    a :func:`tag_followed` assertion, not :func:`grade_correctness` -- the
    tag is report-only, and correctness for this shape is covered by
    ``test_follow_through_and_multi_hop_content_marker_counter_examples`` in
    ``test_north_star_report.py`` instead."""
    probe = _probe("fenwick_relationship_history")
    first, second = probe.answer_tokens

    body = "Fenwick Systems' relationship is coordinated as the pages describe."
    one_tag = _record(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"{body}\n\n[ref: {first}]",
    )
    assert tag_followed(one_tag, probe) is False

    both_tags = _record(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=f"{body}\n\n[ref: {first}]\n[ref: {second}]",
    )
    assert tag_followed(both_tags, probe) is True


def test_multi_hop_tag_followed_plants_a_single_tag() -> None:
    """The counterpart to the test above, pinned because the distinction is
    easy to misremember: ``multi_hop``'s second hop lives in the RETRIEVAL,
    not in the ground truth, so it plants ONE token and one cited tag
    suffices. Only ``follow_through`` splits tokens across pages. Issue
    athenaeum#1831: retargeted to :func:`tag_followed` -- see the sibling
    test above."""
    probe = _probe("spend_approver_named")
    assert len(probe.answer_tokens) == 1
    record = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=(
            "Amir Osei approves discretionary spend above 500 GBP.\n\n"
            f"[ref: {probe.answer_tokens[0]}]"
        ),
    )
    assert tag_followed(record, probe) is True


@pytest.mark.parametrize(
    "probe_id",
    ["abstain_unknown_client", "abstain_unknown_policy", "abstain_unknown_person"],
)
def test_abstention_with_ref_none_still_grades_correct(probe_id: str) -> None:
    """AC2: declining language plus ``[ref: none]`` is still correct
    abstention -- ``none`` is not a planted token, so the new instruction
    cannot break the class it never applied to."""
    probe = _probe(probe_id)
    assert not probe.answer_tokens
    record = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer="I don't know — the knowledge base has no record of that.\n\n[ref: none]",
    )
    assert grade_correctness(record, probe, _CORPUS) is True


def test_abstention_citing_a_real_tag_grades_right_when_it_declines() -> None:
    """athenaeum#1753 AC2's other half, AMENDED by athenaeum#1836 (operator
    ruling on athenaeum#1791, applied on athenaeum#1736 from run
    35399179014): a decline that also cites the neighbouring page it DID find
    -- tag and all -- is a correct abstention, not confabulation-by-retrieval.
    Every arm's honest medium-scale abstention on the parental-leave probe
    quoted the adjacent PTO fact this way, and the old deny-list graded them
    all wrong. A confident value with no declining language still fails."""
    probe = _probe("abstain_unknown_policy")
    declining_with_neighbour = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=(
            "I could not find a parental leave policy, but the leave policy says "
            "25 days.\n\n[ref: Cinderquill]"
        ),
    )
    assert grade_correctness(declining_with_neighbour, probe, _CORPUS) is True

    confident = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer="Parental leave is 25 days.\n\n[ref: Cinderquill]",
    )
    assert grade_correctness(confident, probe, _CORPUS) is False


# ---------------------------------------------------------------------------
# Why the PULL arms need `read_entity`: the two DISTINCT ways a planted tag is
# unreachable from `recall`'s rendering alone (issue athenaeum#1756)
# ---------------------------------------------------------------------------


def test_the_tags_recall_alone_cannot_deliver_split_into_two_mechanisms(tmp_path: Path) -> None:
    """Measured, not asserted from memory, because the two mechanisms are easy
    to conflate and the count differs between them.

    A tag can be missing from a ``recall`` response for either of two reasons,
    and only the FIRST is a snippet-window truncation:

    1. The tag-bearing page IS returned, and the tag still does not appear --
       the page is longer than the 400-character window
       ``athenaeum.mcp_server._snippet`` gives every hit, and the corpus puts
       the tag on the page's last line. Three probes on pages of
       587/566/434 characters, plus (issue athenaeum#1779) four more on the
       long-page tier's 1,500-3,000-character pages, authored specifically
       to land in this bucket -- see
       ``tests/evals/data/corpus/core/12-long-pages.yaml``.
    2. The tag-bearing page is not returned at all at ``top_k=5``. Those pages
       are SHORT -- ``portal_design_reviewer``'s is 164 characters, so no
       window could have cut it. This is a ranking outcome, not truncation.

    ``read_entity`` rescues both, by different routes: it returns the whole
    page for (1), and it is reachable by uid from a first-hop page for (2).
    Writing "four tags fall outside the snippet window" would merge one
    instance of (2) into (1) and state something this test disproves.
    """
    from athenaeum.mcp_server import recall_search
    from athenaeum.search import get_backend

    wiki_root = _CORPUS.materialize(tmp_path)
    cache_dir = tmp_path / "cache"
    get_backend("fts5").build_index(wiki_root, cache_dir)

    truncated: dict[str, int] = {}
    unretrieved: dict[str, int] = {}
    for probe in _CORPUS.probes:
        if not probe.answer_tokens:
            continue
        if probe.probe_class == "unprompted_push":
            # athenaeum#1778: this class plants its answer token on a
            # decision page DESIGNED to share no query vocabulary at all
            # (validate_core's own lexical-unreachability check) -- a plain
            # FTS5 recall_search over the query can never rank it into
            # top_k regardless of page length, which is the property the
            # class exists to measure, not a truncation-vs-ranking outcome
            # this test's two-mechanism taxonomy is scoped to.
            continue
        response = recall_search(
            wiki_root,
            probe.query,
            top_k=5,
            search_backend="fts5",
            cache_dir=cache_dir,
        )
        for token in probe.answer_tokens:
            if token in response:
                continue
            for page in _CORPUS.pages:
                if token not in page.body:
                    continue
                bucket = truncated if f"**Uid:** {page.uid}" in response else unretrieved
                bucket[probe.id] = len(page.body)

    # Re-pinned under athenaeum#1839: `core/16-redundancy.yaml` added
    # `driftgate_migration_funding` and `dual_signoff_threshold` to this
    # bucket (each has a 400+-character expected page carrying its own
    # `Internal reference tag:` line, same shape as `keelbridge_programme_scope`
    # above). `mira_castellane_role`'s pages are both under 400 characters and
    # land in `unretrieved` instead -- asserted separately below, not pinned
    # by name.
    assert sorted(truncated) == [
        "bramfield_retainer_renewal",
        "driftgate_migration_funding",
        "dual_signoff_threshold",
        "keelbridge_programme_scope",
        "lighthouse_migration_rollback",
        "person_not_repo",
        "remote_equipment_stipend_cap",
        "repo_not_person",
        "triform_vendor_consolidation",
    ]
    # Every one of them is longer than the window -- which is what makes
    # "outside the snippet window" the right description of this bucket.
    assert all(length > 400 for length in truncated.values())

    # The other bucket is real and larger, and `read_entity` helps there too --
    # but never by widening a snippet.
    assert "portal_design_reviewer" in unretrieved
    assert all(length < 400 for length in unretrieved.values())
    assert not set(truncated) & set(unretrieved)


# ---------------------------------------------------------------------------
# The two athenaeum#1759 defects: a fixture wording drift, and the uid
# competing with the tag as a citation target.
# ---------------------------------------------------------------------------


def test_the_instruction_names_the_same_line_prefix_every_fixture_carries() -> None:
    """AC1 for athenaeum#1759. The instruction tells the model the tag is
    the value on the page's ``Internal reference tag:`` line; every
    token-bearing core page must carry that EXACT prefix on some line of its
    body, not a synonym like ``Internal reference code:``. This is the fixed
    counterpart of the drift that capped ``follow_through`` at zero: the
    fixture used a different word than the instruction named, so the
    contract's own wording was unsatisfiable even though the token still
    occurred somewhere in the page body.
    """
    assert "Internal reference tag:" in rollout.REFERENCE_TAG_INSTRUCTION
    for page in _CORPUS.pages:
        every_token = {tok for probe in _CORPUS.probes for tok in probe.answer_tokens}
        if not any(token in page.body for token in every_token):
            continue
        assert _TAG_LINE_RE.search(page.body), (
            f"page {page.uid!r} carries a planted token but no line beginning "
            "'Internal reference tag:'"
        )


def test_every_tagged_core_page_renders_exactly_one_tag_line() -> None:
    """AC3 for athenaeum#1759, the rendering half: ANY page carrying an
    ``Internal reference tag:`` line -- whether or not that tag is itself a
    planted ``answer_tokens`` value -- must render EXACTLY ONE such line
    (bolded by ``Page.to_markdown``, issue athenaeum#1759's uid-competition
    fix), not a stray second identifier line. Scoped to every tagged page,
    not only token-bearing ones, since athenaeum#1766 defect 2 gave several
    ``expected_uids`` pages a citable tag with no answer token of their own
    (`validate_core` requires this of every page a probe names).
    """
    checked = 0
    for page in _CORPUS.pages:
        body_matches = _TAG_LINE_RE.findall(page.body)
        if not body_matches:
            continue
        checked += 1
        assert len(body_matches) == 1, f"page {page.uid!r} has {len(body_matches)} tag lines"

        rendered = page.to_markdown()
        rendered_matches = re.findall(r"\*\*Internal reference tag:\*\* (.+)", rendered)
        assert (
            len(rendered_matches) == 1
        ), f"page {page.uid!r} rendered {len(rendered_matches)} bold tag lines"
        assert rendered_matches[0] == body_matches[0]
    assert checked, "no tagged core pages were found to check"


def test_every_token_bearing_core_page_tag_is_a_planted_token() -> None:
    """AC3 for athenaeum#1759, the token-fidelity half: a page whose BODY
    plants an ``answer_tokens`` value must carry that value on its own
    ``Internal reference tag:`` line -- not a stray second identifier line,
    and not a value that drifted from the corpus's own bookkeeping. Scoped
    to token-bearing pages only (see the test above for the broader
    every-tagged-page rendering invariant): a page tagged for citation
    purposes alone (athenaeum#1766 defect 2) has no answer token to check
    here.
    """
    every_token = {tok for probe in _CORPUS.probes for tok in probe.answer_tokens}
    checked = 0
    for page in _CORPUS.pages:
        if not any(token in page.body for token in every_token):
            continue
        checked += 1
        body_matches = _TAG_LINE_RE.findall(page.body)
        assert len(body_matches) == 1, f"page {page.uid!r} has {len(body_matches)} tag lines"
        value = body_matches[0].rstrip(".")
        assert value in every_token, f"page {page.uid!r} tag {value!r} is not a planted token"
    assert checked, "no token-bearing core pages were found to check"


def test_the_instruction_excludes_the_uid_as_a_citation_target() -> None:
    """AC2 for athenaeum#1759: 17 oracle cells in the first live grid cited
    the page's frontmatter ``uid:`` instead of its tag. The instruction must
    say, in one sentence, that the tag is never the uid, title, or filename,
    so the model has an explicit reason to prefer the tag line over the
    other identifier-shaped strings on the page.

    Pinned as a single regex over the actual clause (issue athenaeum#1762),
    not four independent substring checks: those pass equally on a prompt
    that mentions "uid", "never", "title", and "filename" in four unrelated
    sentences, which would not exclude the uid as a citation target at all.
    """
    pattern = re.compile(
        r"never the page'?s `?uid`?,\s*its title,\s*or its filename",
        re.IGNORECASE,
    )
    assert pattern.search(rollout.REFERENCE_TAG_INSTRUCTION), (
        "REFERENCE_TAG_INSTRUCTION must contain a single clause of the shape "
        "'never the page's uid, its title, or its filename'; got: "
        f"{rollout.REFERENCE_TAG_INSTRUCTION!r}"
    )


def test_the_tag_line_is_the_final_non_blank_line_of_every_token_bearing_page() -> None:
    """AC1 for athenaeum#1762. ``Page.to_markdown``'s docstring says the
    ``Internal reference tag:`` line is rendered "BOLD and alone on its own
    final line", and the athenaeum#1759 rationale for that (out-competing
    the frontmatter ``uid:`` line as the most identifier-shaped string a
    model reads) depends on the tag line actually being the LAST thing on
    the page, not merely present somewhere in the body.
    ``test_every_token_bearing_core_page_tag_is_a_planted_token`` pinned
    count and value only; this pins position, for both the raw fixture body
    and the rendered markdown a model actually sees.

    Scoped to every token-bearing page across the whole materialized corpus
    -- every core fixture file, wave-2 probe classes included -- since the
    check reads pages off ``_CORPUS``, not off a probe-class allowlist, so a
    fixture landing under any future probe class is covered the moment it
    plants an answer token. Pinned, not merely claimed: the loop below also
    collects which token-bearing pages named by an ``aggregation`` probe's
    ``expected_uids`` (the wave-2 class already authored today, in
    ``data/corpus/core/15-aggregation.yaml``) got checked, and asserts that
    set is non-empty and exhaustive -- so this test could not silently pass
    with every wave-2 fixture excluded by an over-eager filter.
    """
    every_token = {tok for probe in _CORPUS.probes for tok in probe.answer_tokens}
    aggregation_expected_uids = {
        uid
        for probe in _CORPUS.probes
        if probe.probe_class == "aggregation"
        for uid in probe.expected_uids
    }
    checked_uids: set[str] = set()
    for page in _CORPUS.pages:
        if not any(token in page.body for token in every_token):
            continue
        checked_uids.add(page.uid)

        body_lines = [line for line in page.body.splitlines() if line.strip()]
        assert body_lines, f"page {page.uid!r} has an empty body"
        assert _TAG_LINE_RE.match(body_lines[-1]), (
            f"page {page.uid!r}'s last non-blank body line is {body_lines[-1]!r}, not an "
            "'Internal reference tag:' line"
        )

        rendered_lines = [line for line in page.to_markdown().splitlines() if line.strip()]
        assert rendered_lines[-1].startswith("**Internal reference tag:**"), (
            f"page {page.uid!r}'s rendered markdown ends on {rendered_lines[-1]!r}, not the "
            "bold tag line"
        )
    assert checked_uids, "no token-bearing core pages were found to check"

    checked_aggregation_uids = aggregation_expected_uids & checked_uids
    assert checked_aggregation_uids, (
        "no token-bearing aggregation-probe page was checked -- the wave-2 fixture coverage "
        "this test claims did not actually exercise any aggregation page"
    )
