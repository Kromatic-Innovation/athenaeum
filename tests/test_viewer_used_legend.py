# SPDX-License-Identifier: Apache-2.0
"""The viewer must say what its ``used`` column actually means (athenaeum#1575 AC5).

The demo audience reads ``used`` as "the sidecar helped". Today it means only
that the page's recorded push id (the full uid on the MCP path, an 8-hex uid
prefix on the sidecar path) appears as a substring in the transcript — a proxy
that both misses content-only use and counts a bare echo (measured in
``docs/measurements/used-column-heuristic-accuracy-*.md``). The legend has to
say so in the browser, which is where the overclaim would happen; a module
docstring in ``_cmd_viewer.py`` is not that surface.

UNMARKED — reads the packaged static asset directly, no browser, no network.
"""

from __future__ import annotations

from pathlib import Path

INDEX_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "athenaeum" / "viewer_static" / "index.html"
)


def test_legend_defines_used_as_a_uid_appearance() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    legend = html.split('<div class="legend">', 1)[1].split("</div>", 1)[0]
    assert "used" in legend
    assert "recorded push id" in legend
    assert "substring" in legend
    assert "session transcript" in legend


def test_legend_does_not_overclaim() -> None:
    """The sentence must name the limit, not just restate the mechanism."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    legend = html.split('<div class="legend">', 1)[1].split("</div>", 1)[0]
    assert "a proxy for use, not a measure of it" in legend
