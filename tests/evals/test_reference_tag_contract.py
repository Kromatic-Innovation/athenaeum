# SPDX-License-Identifier: Apache-2.0
"""The reference-tag grading contract (issue athenaeum#1753).

Correctness is a deterministic substring match against each probe's planted
``answer_tokens``, and those tokens are the corpus pages' own
``Internal reference tag:`` values. No model repeats an unasked-for tag, so
before this contract the match was unsatisfiable by a correct answer and even
``oracle`` -- handed the ground-truth page verbatim -- graded 0 on every
class. These tests pin the fix from both ends: every arm's system prompt
carries one IDENTICAL instruction, and grading actually reads the tag that
instruction asks for.

Token-free: no live rollout, no model client, no spend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.evals.corpus import build_corpus
from tests.evals.north_star_report import grade_correctness
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


def test_native_writer_sessions_are_outside_the_contract() -> None:
    """The write path produces memory files, not graded answers. Telling a
    writer to append ``[ref: ...]`` would only risk polluting what it saves,
    so :func:`build_native_argv` takes ``append_system_prompt=None`` there --
    a deliberate exclusion, pinned so it reads as one."""
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


def test_the_same_oracle_answer_without_the_tag_grades_wrong() -> None:
    """The negative half of the same control: a substantively identical --
    and factually correct -- answer with no tag grades WRONG. This is the
    failure mode that put ``oracle`` at 0/18, pinned so it cannot be
    mistaken for a grading bug later."""
    probe = _probe("pto_allowance")
    untagged = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer="The firm's PTO allowance is 25 days per year plus UK bank holidays.",
    )
    assert grade_correctness(untagged, probe, _CORPUS) is False


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


def test_abstention_citing_a_real_tag_still_grades_wrong() -> None:
    """The other half of AC2: a planted token in an abstention answer is a
    confabulation-by-retrieval signal and stays incorrect, even when it
    arrives inside a well-formed ``[ref: ...]`` citation and is wrapped in
    declining language."""
    probe = _probe("abstain_unknown_policy")
    confabulated = _record(
        arm=Arm.ORACLE,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        answer=(
            "I could not find a parental leave policy, but the leave policy says "
            "25 days.\n\n[ref: Cinderquill]"
        ),
    )
    assert grade_correctness(confabulated, probe, _CORPUS) is False
