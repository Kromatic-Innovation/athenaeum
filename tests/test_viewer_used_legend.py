# SPDX-License-Identifier: Apache-2.0
"""The viewer must say what its ``used`` column actually means (athenaeum#1575 AC5).

The demo audience reads ``used`` as "the sidecar helped". Since athenaeum#1585
it means the page's recorded push id appears as a whole token in what the
operator or the assistant WROTE (an echo inside a tool result does not count),
or the assistant's own text reproduces distinctive phrasing from the page. That
is a better proxy than the substring rule it replaced — measured in
``docs/measurements/used-column-heuristic-accuracy-*.md`` — but still a proxy.
The legend has to say so in the browser, which is where the overclaim would
happen; a module docstring in ``_cmd_viewer.py`` is not that surface.

UNMARKED — reads the packaged static asset directly, no browser, no network.
"""

from __future__ import annotations

from pathlib import Path

INDEX_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "athenaeum" / "viewer_static" / "index.html"
)


def test_legend_defines_used_as_the_rule_that_is_actually_implemented() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    legend = html.split('<div class="legend">', 1)[1].split("</div>", 1)[0]
    assert "used" in legend
    assert "recorded push id" in legend
    assert "whole token" in legend
    assert "session transcript" in legend


def test_legend_names_both_signals_and_the_echo_exclusion() -> None:
    """A legend that named only the id half would still understate the column,
    and one that omitted the echo exclusion would still overstate it."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    legend = html.split('<div class="legend">', 1)[1].split("</div>", 1)[0]
    assert "tool result" in legend
    assert "distinctive phrasing" in legend
    assert "breadcrumb" in legend


def test_legend_does_not_overclaim() -> None:
    """The sentence must name the limit, not just restate the mechanism."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    legend = html.split('<div class="legend">', 1)[1].split("</div>", 1)[0]
    assert "a proxy for use, not a measure of it" in legend
