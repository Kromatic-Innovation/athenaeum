# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.json_utils` (issue #219).

The 2026-06-11 nightly run silently dropped 38 clusters because the
detector wrapped its JSON in markdown code fences and the greedy
first-``{``-to-last-``}`` regex swallowed trailing prose/braces,
producing ``json.JSONDecodeError: Extra data``. The shared
:func:`extract_json_object` helper must tolerate fences, surrounding
prose, and nested braces — and return ``None`` when no object exists so
callers keep their existing warning + fallback behavior.
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
    """Only objects count — a bare array has no ``{`` to anchor on."""
    assert extract_json_object('["a", "b"]') is None
