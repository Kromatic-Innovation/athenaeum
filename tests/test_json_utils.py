# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.json_utils` (issue #219).

The 2026-06-11 nightly run silently dropped 38 clusters because the
detector wrapped its JSON in markdown code fences and the greedy
first-``{``-to-last-``}`` regex swallowed trailing prose/braces,
producing ``json.JSONDecodeError: Extra data``. The shared
:func:`extract_json_object` helper must tolerate fences, surrounding
prose, and nested braces — and return ``None`` when no object exists so
callers keep their existing warning + fallback behavior.

Contract hardening (QA round): fenced content is preferred over unfenced
text, exactly one unfenced object parses, and multiple unfenced objects
are ambiguous → ``None`` (never guess between an example and the answer).
Clause-2 amendment (QA triage round, F1): fences present but yielding no
balanced object fall back to the whole-text exactly-one scan, recovering
the "fenced plan/diff + unfenced answer object" shape without
reintroducing silent wrong-object extraction.
"""

from __future__ import annotations

from athenaeum.json_utils import extract_json_object


def test_plain_strict_json() -> None:
    """Regression: strict bare-object output keeps parsing."""
    assert extract_json_object('{"detected": true}') == {"detected": True}


def test_json_fenced_object() -> None:
    text = '```json\n{"detected": true, "rationale": "r"}\n```'
    assert extract_json_object(text) == {"detected": True, "rationale": "r"}


def test_bare_fenced_object() -> None:
    text = '```\n{"action": "keep_a"}\n```'
    assert extract_json_object(text) == {"action": "keep_a"}


def test_prose_around_object() -> None:
    text = 'Here is my analysis:\n{"detected": false}\nHope that helps!'
    assert extract_json_object(text) == {"detected": False}


def test_fenced_object_with_nested_braces_in_string() -> None:
    text = (
        "```json\n"
        '{"rationale": "the body uses {curly} braces", "detected": true}\n'
        "```"
    )
    assert extract_json_object(text) == {
        "rationale": "the body uses {curly} braces",
        "detected": True,
    }


def test_extra_data_after_object() -> None:
    """The nightly failure shape: fenced object + later brace span."""
    text = (
        '```json\n{"detected": true}\n```\n'
        'A non-conflicting cluster would instead be {"detected": false}.'
    )
    assert extract_json_object(text) == {"detected": True}


def test_stray_brace_before_object() -> None:
    text = 'prose with a stray { brace\n{"detected": true}'
    assert extract_json_object(text) == {"detected": True}


def test_no_json_returns_none() -> None:
    assert extract_json_object("no json here, just prose") is None


def test_empty_string_returns_none() -> None:
    assert extract_json_object("") is None


def test_top_level_array_returns_none() -> None:
    """Only objects count — a brace-free array has no ``{`` to anchor on."""
    assert extract_json_object('["a", "b"]') is None


def test_object_inside_top_level_array_is_extracted() -> None:
    """The scan anchors on ``{``, so an object wrapped in a top-level
    array still counts as the (single) object — pinned behavior."""
    assert extract_json_object('[{"detected": true}]') == {"detected": True}


def test_nested_object_returned_whole() -> None:
    """A genuinely nested object is one top-level object, not two."""
    assert extract_json_object('{"a": {"b": 1}}') == {"a": {"b": 1}}


def test_example_object_then_real_object_unfenced_is_ambiguous() -> None:
    """Two unfenced top-level objects → ``None``; never guess which is
    the answer. Callers log + safe-fallback, matching old-regex safety."""
    text = (
        'Example: {"detected": false}. '
        'Actual: {"detected": true, "conflict_type": "factual"}'
    )
    assert extract_json_object(text) is None


def test_fenced_answer_wins_over_unfenced_example() -> None:
    """A fence marks the deliberate answer — unfenced prose objects
    (e.g. a preceding example) are ignored when any fence is present."""
    text = (
        'An example of a non-conflict is {"detected": false}.\n'
        '```json\n{"detected": true}\n```'
    )
    assert extract_json_object(text) == {"detected": True}


def test_fence_without_object_falls_back_to_unfenced_scan() -> None:
    """Clause-2 fallback (F1): fences present but yielding no balanced
    object → the whole text is scanned, so a single unfenced object is
    still extracted. (Deliberate amendment of the earlier fences-only
    pin, which dropped this recoverable shape.)"""
    text = '```\nnot json at all\n```\n{"detected": true}'
    assert extract_json_object(text) == {"detected": True}


def test_fence_without_object_two_unfenced_objects_ambiguous() -> None:
    """Clause-2 fallback keeps the exactly-one rule: fenced non-JSON plus
    TWO unfenced objects is still ambiguous → ``None``, never a guess."""
    text = (
        "```\nnot json at all\n```\n"
        'Example: {"detected": false}. Actual: {"detected": true}'
    )
    assert extract_json_object(text) is None


def test_fenced_diff_preview_with_unfenced_edits_object() -> None:
    """F1 probe shape from the freetext-writeback call site: the model
    fences a diff preview and leaves the ``{"edits": [...]}`` answer
    unfenced — the clause-2 fallback recovers it."""
    text = (
        "Plan:\n"
        "```diff\n- old line\n+ new line\n```\n"
        'Applying:\n{"edits": [{"path": "notes.md", "changed": true}]}'
    )
    assert extract_json_object(text) == {
        "edits": [{"path": "notes.md", "changed": True}]
    }


def test_unterminated_fence_object_extracted_via_fallback() -> None:
    """F2a: an opening fence with no closer is not a fenced block, so the
    complete object after it is found by the whole-text scan."""
    text = '```json\n{"detected": true}'
    assert extract_json_object(text) == {"detected": True}


def test_complete_fenced_example_beats_unterminated_answer_fence() -> None:
    """F2b: a complete fenced example object followed by an unterminated
    answer fence — the example is the only well-formed fenced block, so
    clause 1 returns it. Pinned as the safe-or-equal parity outcome: the
    old greedy regex would not have recovered the answer here either."""
    text = '```json\n{"detected": false}\n```\n' 'Answer:\n```json\n{"detected": true}'
    assert extract_json_object(text) == {"detected": False}


def test_one_space_indented_fence_recognized() -> None:
    """F3: CommonMark allows fence delimiters indented up to 3 spaces — a
    1-space-indented fenced object wins clause 1 over the unfenced decoy
    (without fence recognition the scan would be ambiguous → ``None``)."""
    text = ' ```json\n {"detected": true}\n ```\nExample: {"detected": false}'
    assert extract_json_object(text) == {"detected": True}


def test_four_space_indented_fence_is_not_a_fence() -> None:
    """F3: 4 spaces is an indented code block, not a fence (CommonMark) —
    the object inside it is just another unfenced object, so two unfenced
    objects are ambiguous → ``None``. Were the 4-space block treated as a
    fence, clause 1 would return ``{"a": 1}``."""
    text = '    ```json\n    {"a": 1}\n    ```\n{"b": 2}'
    assert extract_json_object(text) is None


def test_crlf_fenced_object() -> None:
    text = '```json\r\n{"detected": true}\r\n```'
    assert extract_json_object(text) == {"detected": True}


def test_uppercase_fence_tag() -> None:
    text = '```JSON\n{"detected": true}\n```'
    assert extract_json_object(text) == {"detected": True}


def test_truncated_object_returns_none() -> None:
    """Simulates a max_tokens cutoff mid-object."""
    text = '{"detected": true, "rationale": "cut off mid-sent'
    assert extract_json_object(text) is None


def test_pathologically_deep_nesting_returns_none() -> None:
    """``RecursionError`` inside ``raw_decode`` is contained — the
    helper's returns-``None`` contract holds instead of raising."""
    assert extract_json_object('{"a":' * 5000) is None


def test_malformed_json_logs_debug(caplog) -> None:  # type: ignore[no-untyped-def]
    """Observability: the last decode error (message + position) is
    debug-logged so malformed-JSON vs no-JSON is diagnosable."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="athenaeum.json_utils"):
        assert extract_json_object('{"detected": tru') is None
    assert any("last decode error" in r.message for r in caplog.records)


def test_inline_backtick_run_before_real_fence() -> None:
    """Issue #222 fence-pairing probe: a stray inline ``` run in prose
    must not pair with the real fence opener — only line-leading ```
    delimits a fence. The fenced object must still be extracted."""
    text = (
        "Wrap your answer in ``` fences, e.g. ```json is fine.\n"
        '```json\n{"detected": true}\n```\n'
    )
    assert extract_json_object(text) == {"detected": True}


def test_fences_without_object_log_debug(caplog) -> None:  # type: ignore[no-untyped-def]
    """Issue #222 observability: fences present but yielding no balanced
    object (no decode error occurs — there is no ``{`` at all) must emit
    the fences-branch debug message. Pinned on a substring unique to the
    fences-present branch (F5) — not "no JSON object", which both debug
    branches share."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="athenaeum.json_utils"):
        assert extract_json_object("```\nno braces at all\n```") is None
    assert any("falling back to whole-text scan" in r.message for r in caplog.records)


def test_fenced_recursion_only_not_reported_as_no_object(caplog) -> None:  # type: ignore[no-untyped-def]
    """F6: a fenced block whose only parse attempt raised
    ``RecursionError`` (so ``last_err`` is ``None``) must not be described
    as containing no JSON object — the recursion case logs distinctly."""
    import logging

    text = '```json\n{"a": ' + "[" * 5000 + "\n```"
    with caplog.at_level(logging.DEBUG, logger="athenaeum.json_utils"):
        assert extract_json_object(text) is None
    assert any("recursion" in r.message for r in caplog.records)
    assert not any("contains no JSON object" in r.message for r in caplog.records)


def test_real_object_then_example_object_unfenced_is_ambiguous() -> None:
    """Reverse ordering of the example-then-answer case: answer first,
    example second — still two unfenced top-level objects → ``None``."""
    text = (
        '{"detected": true, "conflict_type": "factual"} '
        'For contrast, a non-conflict looks like {"detected": false}.'
    )
    assert extract_json_object(text) is None


def test_malformed_fenced_answer_falls_back_to_unfenced_object() -> None:
    """N1 — accepted clause-2 contract (issue #222 triage): a malformed
    fenced ANSWER (here a trailing comma) yields no balanced fenced
    object, so the whole-text fallback fires and extracts the only
    well-formed object — the unfenced EXAMPLE. This wrong-object risk is
    the accepted trade for recovering the "fenced plan + unfenced
    answer" shape: downstream call sites shape-guard (action allowlist /
    edits-list validation), so a wrong-shaped fallback object is
    contained rather than acted on."""
    text = (
        'Example: {"detected": false}.\n' 'Answer:\n```json\n{"detected": true,}\n```'
    )
    assert extract_json_object(text) == {"detected": False}


def test_two_objects_within_single_fence_first_wins() -> None:
    """N2 — a single fenced block containing TWO top-level objects
    returns the FIRST: the exactly-one ambiguity rule (clauses 3-4)
    applies only to whole-text scans, never within a fence (pinned
    asymmetry, issue #222 triage)."""
    text = '```json\n{"first": 1}\n{"second": 2}\n```'
    assert extract_json_object(text) == {"first": 1}


def test_multi_object_top_level_array_is_ambiguous() -> None:
    """Two objects inside a top-level array count as two top-level
    objects under the ``{``-anchored scan → ``None`` via the
    exactly-one rule (pinned behavior)."""
    assert extract_json_object('[{"a": 1}, {"b": 2}]') is None
