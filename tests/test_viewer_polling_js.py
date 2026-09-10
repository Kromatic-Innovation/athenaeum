# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the polling JS shipped in ``viewer_static/index.html``
(issue athenaeum#1539).

Nothing in ``athenaeum``'s own test suite executes browser JS today, and this
repo holds to "zero new dependencies" (module docstring,
``src/athenaeum/_cmd_viewer.py``), so these tests do not pull in a headless
browser or a Python JS engine. Instead they run the REAL, shipped
``<script>`` body (extracted verbatim from the packaged HTML, not
reimplemented) under plain Node.js -- present on every GitHub-hosted
``ubuntu-latest`` runner, and already the interpreter this project's
contributors have locally for any other tooling. A small hand-rolled DOM/
``fetch``/``window`` shim (see :data:`_DOM_SHIM_JS` below) stands in for the
browser; it implements only the handful of DOM operations the script
actually calls.

**AC2 is the mandatory counter-example test in this file:**
``test_ac2_new_record_appears_via_poll_and_fails_when_polling_disabled``
runs the identical scenario twice -- once with polling enabled, once with it
disabled via the same ``POLL_INTERVAL_MS`` knob ``_cmd_viewer.py`` embeds in
the page -- and asserts a NEW record appears in the enabled run and does NOT
appear in the disabled run. Run manually with ``--poll-interval`` effectively
forced off (see the module docstring of that test) to confirm the assertion
actually goes red without the feature; it does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_INDEX_HTML = (
    Path(__file__).resolve().parent.parent / "src" / "athenaeum" / "viewer_static" / "index.html"
)

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None,
    reason="node not on PATH -- these tests execute the shipped viewer JS under Node "
    "rather than reimplementing it in Python (see module docstring)",
)


def _extract_script(*, poll_interval_ms: int) -> str:
    """Pull the real ``<script>...</script>`` body out of the shipped HTML and
    substitute the same placeholders ``_cmd_viewer.py._serve_html`` does, so
    this test exercises the exact code a browser would receive."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert match is not None, "viewer_static/index.html has no inline <script> to extract"
    script = match.group(1)
    script = script.replace("__VIEWER_NONCE_PLACEHOLDER__", "test-nonce")
    script = script.replace("__VIEWER_POLL_INTERVAL_MS_PLACEHOLDER__", str(poll_interval_ms))
    return script


#: Minimal DOM/window/fetch shim. Implements exactly the operations
#: index.html's script calls (createElement, getElementById against a fixed
#: set of ids the real page defines, appendChild/removeChild -- including
#: "appendChild on an already-attached node moves it", which morphRows()
#: relies on -- classList, dataset, textContent, innerHTML-as-clear,
#: addEventListener/click dispatch, and fetch/setTimeout/scrollTo).
_DOM_SHIM_JS = r"""
'use strict';

function Element(tag) {
  this.tagName = tag;
  this.id = "";
  this._className = "";
  this.children = [];
  this.parentNode = null;
  this.dataset = {};
  this.style = {};
  this._text = "";
  this._listeners = {};
  this.disabled = false;
  this.checked = false;
  this.colSpan = null;
  this.__placeholder = false;
  var self = this;
  this.classList = {
    add: function (c) {
      if (self._classes().indexOf(c) === -1) {
        self._className = (self._className + " " + c).trim();
      }
    },
    remove: function (c) {
      self._className = self._classes()
        .filter(function (x) { return x !== c; })
        .join(" ");
    },
    contains: function (c) { return self._classes().indexOf(c) !== -1; }
  };
}
Element.prototype._classes = function () { return this._className.split(/\s+/).filter(Boolean); };
Object.defineProperty(Element.prototype, "className", {
  get: function () { return this._className; },
  set: function (v) { this._className = v; }
});
Object.defineProperty(Element.prototype, "textContent", {
  get: function () { return this._text; },
  set: function (v) { this._text = v; this.children = []; }
});
Object.defineProperty(Element.prototype, "innerHTML", {
  get: function () { return ""; },
  set: function (v) {
    // Only ever set to "" by this app (to clear a container) -- assert that
    // invariant rather than silently no-op on anything else.
    if (v !== "") { throw new Error("shim only supports innerHTML = \"\" (clear), got: " + v); }
    this.children.forEach(function (c) { c.parentNode = null; });
    this.children = [];
    this._text = "";
  }
});
Element.prototype.appendChild = function (child) {
  if (child.parentNode && child.parentNode !== this) {
    var old = child.parentNode.children;
    var idx = old.indexOf(child);
    if (idx !== -1) { old.splice(idx, 1); }
  } else if (child.parentNode === this) {
    var i2 = this.children.indexOf(child);
    if (i2 !== -1) { this.children.splice(i2, 1); }
  }
  child.parentNode = this;
  this.children.push(child);
  return child;
};
Element.prototype.removeChild = function (child) {
  var idx = this.children.indexOf(child);
  if (idx !== -1) { this.children.splice(idx, 1); }
  child.parentNode = null;
  return child;
};
Object.defineProperty(Element.prototype, "firstChild", {
  get: function () { return this.children.length ? this.children[0] : null; }
});
Element.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
Element.prototype.dispatch = function (type) {
  (this._listeners[type] || []).forEach(function (fn) { fn(); });
};
// Recursively find the first descendant carrying dataset.rowId === id --
// enough for the tests to locate one page row without implementing
// querySelector.
Element.prototype.findByRowId = function (id) {
  if (this.dataset && this.dataset.rowId === id) { return this; }
  for (var i = 0; i < this.children.length; i++) {
    var found = this.children[i].findByRowId(id);
    if (found) { return found; }
  }
  return null;
};
Element.prototype.allText = function () {
  var out = this._text || "";
  this.children.forEach(function (c) { out += " " + c.allText(); });
  return out;
};

var _ids = {};
["session-meta", "poll-toggle", "poll-toggle-label", "poll-freshness", "notice-area",
 "turn-count", "turn-meta", "rows-turn", "count-pages", "rows-pages", "toast"
].forEach(function (id) { _ids[id] = new Element("div"); _ids[id].id = id; });

global.document = {
  createElement: function (tag) { return new Element(tag); },
  getElementById: function (id) {
    if (!_ids[id]) { throw new Error("shim: unknown element id " + id); }
    return _ids[id];
  }
};

global.__fetchCallCount = 0;
global.__responses = [];  // queue of {payload}|{reject}|{ok:false,status}; last entry repeats
global.fetch = function (url) {
  global.__fetchCallCount += 1;
  var item = global.__responses.length > 1 ? global.__responses.shift() : global.__responses[0];
  if (!item) { return Promise.reject(new Error("no mock response configured")); }
  if (item.reject) { return Promise.reject(new Error(item.reject)); }
  return Promise.resolve({
    ok: item.ok !== false,
    status: item.status || 200,
    json: function () { return Promise.resolve(item.payload); }
  });
};

global.__scrollToCalls = [];
global.window = {
  scrollY: 0,
  scrollTo: function (x, y) { global.__scrollToCalls.push(y); global.window.scrollY = y; },
  setTimeout: setTimeout,
  clearTimeout: clearTimeout
};
"""


def _payload(*, ids: list[str]) -> dict:
    return {
        "session_id": "s1",
        "has_reference_determination": True,
        "last_turn": {"present": False},
        "pages": [
            {
                "id": item_id,
                "resolved": True,
                "name": item_id,
                "description": "",
                "memory_tier": "warm",
                "token_cost": 5,
                "classification": "pulled-cold",
            }
            for item_id in ids
        ],
    }


def _run_node(script: str, *, timeout: float = 15.0) -> dict:
    result = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    last_line = [line for line in result.stdout.splitlines() if line.strip()][-1]
    return json.loads(last_line)


def _scenario(*, poll_interval_ms: int, wait_ms: int = 260) -> dict:
    """Load with only page 'x' present, then have every subsequent poll
    return 'x' PLUS a brand new page 'y' -- mirrors AC2's "append a record to
    a stubbed stream while the view is open" -- and report whether 'y' ever
    made it into the rendered page-rows table."""
    app_script = _extract_script(poll_interval_ms=poll_interval_ms)
    driver = f"""
global.__responses = [
  {{ payload: {json.dumps(_payload(ids=["x"]))} }},
  {{ payload: {json.dumps(_payload(ids=["x", "y"]))} }}
];
{app_script}
setTimeout(function () {{
  var rowsTbody = document.getElementById("rows-pages");
  var sawY = !!rowsTbody.findByRowId("y");
  console.log(JSON.stringify({{
    sawNewRecord: sawY,
    fetchCallCount: global.__fetchCallCount
  }}));
  process.exit(0);
}}, {wait_ms});
"""
    return _run_node(_DOM_SHIM_JS + driver)


# ---------------------------------------------------------------------------
# AC1 / AC2 (mandatory counter-example)
# ---------------------------------------------------------------------------


def test_ac2_new_record_appears_via_poll_and_fails_when_polling_disabled() -> None:
    """AC2, verbatim: append a record to a stubbed stream while the view is
    open and assert it appears (polling ENABLED) -- and assert the identical
    scenario does NOT show that record (i.e. this same check FAILS) when
    polling is disabled. Confirmed red manually: running only the
    poll_interval_ms=0 half of this scenario with the pre-athenaeum#1539 assertion
    (`assert result["sawNewRecord"]`) fails, because the disabled page never
    issues a second fetch at all (`fetchCallCount` stays 1)."""
    enabled = _scenario(poll_interval_ms=30)
    assert enabled["sawNewRecord"] is True, "AC1: a new record must appear without manual reload"
    assert enabled["fetchCallCount"] >= 2

    disabled = _scenario(poll_interval_ms=0)
    assert disabled["sawNewRecord"] is False, (
        "a view with polling disabled must NOT pick up a new record -- if this "
        "assertion fails, polling-off has silently stopped disabling anything"
    )
    assert disabled["fetchCallCount"] == 1, "polling disabled must mean exactly one fetch, ever"


# ---------------------------------------------------------------------------
# AC4: scroll position and row state survive a poll
# ---------------------------------------------------------------------------


def test_ac4_scroll_position_restored_after_poll() -> None:
    app_script = _extract_script(poll_interval_ms=30)
    driver = f"""
global.__responses = [
  {{ payload: {json.dumps(_payload(ids=["x"]))} }},
  {{ payload: {json.dumps(_payload(ids=["x", "y"]))} }}
];
{app_script}
setTimeout(function () {{ global.window.scrollY = 777; }}, 10);
setTimeout(function () {{
  console.log(JSON.stringify({{ scrollToCalls: global.__scrollToCalls }}));
  process.exit(0);
}}, 260);
"""
    result = _run_node(_DOM_SHIM_JS + driver)
    assert 777 in result["scrollToCalls"], (
        "a poll-triggered re-render must restore the scroll position it observed "
        f"just before re-rendering; calls were {result['scrollToCalls']!r}"
    )


def test_ac4_row_element_identity_and_extra_class_survive_a_poll() -> None:
    """Simulates whatever a future per-row 'expanded' feature would do: mark
    a live <tr> with a class this script does not manage, then poll, and
    confirm morphRows() mutated that SAME element rather than replacing it."""
    app_script = _extract_script(poll_interval_ms=30)
    driver = f"""
global.__responses = [
  {{ payload: {json.dumps(_payload(ids=["x"]))} }},
  {{ payload: {json.dumps(_payload(ids=["x", "y"]))} }}
];
{app_script}
var rowXBefore;
setTimeout(function () {{
  rowXBefore = document.getElementById("rows-pages").findByRowId("x");
  rowXBefore.classList.add("test-expanded");
  rowXBefore.__marker = "same-node";
}}, 10);
setTimeout(function () {{
  var rowXAfter = document.getElementById("rows-pages").findByRowId("x");
  console.log(JSON.stringify({{
    sameNode: rowXAfter === rowXBefore,
    keptMarker: rowXAfter.__marker === "same-node",
    keptExpandedClass: rowXAfter.classList.contains("test-expanded")
  }}));
  process.exit(0);
}}, 260);
"""
    result = _run_node(_DOM_SHIM_JS + driver)
    assert result["sameNode"] is True
    assert result["keptMarker"] is True
    assert result["keptExpandedClass"] is True


# ---------------------------------------------------------------------------
# AC5: a failed poll keeps the last good payload, staleness visible
# ---------------------------------------------------------------------------


def test_ac5_failed_poll_keeps_last_good_payload_and_shows_staleness() -> None:
    app_script = _extract_script(poll_interval_ms=30)
    driver = f"""
global.__responses = [
  {{ payload: {json.dumps(_payload(ids=["x"]))} }},
  {{ reject: "boom: subprocess exited 1" }}
];
{app_script}
setTimeout(function () {{
  var rows = document.getElementById("rows-pages");
  var freshness = document.getElementById("poll-freshness");
  console.log(JSON.stringify({{
    stillShowsX: !!rows.findByRowId("x"),
    noticeIsError: document.getElementById("notice-area").allText()
      .indexOf("Could not load") !== -1,
    freshnessText: freshness.textContent,
    freshnessIsStale: freshness.className.indexOf("stale") !== -1
  }}));
  process.exit(0);
}}, 260);
"""
    result = _run_node(_DOM_SHIM_JS + driver)
    assert result["stillShowsX"] is True, "a failed poll must not blank the last good payload"
    assert result["noticeIsError"] is False, (
        "a failed poll must not paint the full-page error over good data"
    )
    assert result["freshnessIsStale"] is True
    assert "boom" in result["freshnessText"], "the failure reason must be visible, not swallowed"


def test_ac5_initial_load_failure_shows_error_when_nothing_good_yet() -> None:
    """The pre-existing behaviour for a FIRST-load failure (no good payload to
    fall back on) must be unchanged: render the full error, never a blank
    page pretending to be empty-but-fine."""
    app_script = _extract_script(poll_interval_ms=30)
    driver = f"""
global.__responses = [{{ reject: "network down" }}];
{app_script}
setTimeout(function () {{
  console.log(JSON.stringify({{
    noticeIsError: document.getElementById("notice-area").allText().indexOf("Could not load") !== -1
  }}));
  process.exit(0);
}}, 100);
"""
    result = _run_node(_DOM_SHIM_JS + driver)
    assert result["noticeIsError"] is True


# ---------------------------------------------------------------------------
# AC6: polling can be switched off from the page itself, live
# ---------------------------------------------------------------------------


def test_ac6_pause_toggle_stops_further_polls() -> None:
    app_script = _extract_script(poll_interval_ms=30)
    driver = f"""
global.__responses = [
  {{ payload: {json.dumps(_payload(ids=["x"]))} }},
  {{ payload: {json.dumps(_payload(ids=["x", "y"]))} }},
  {{ payload: {json.dumps(_payload(ids=["x", "y", "z"]))} }}
];
{app_script}
setTimeout(function () {{
  document.getElementById("poll-toggle").checked = false;
  document.getElementById("poll-toggle").dispatch("change");
}}, 10);
setTimeout(function () {{
  console.log(JSON.stringify({{ fetchCallCount: global.__fetchCallCount }}));
  process.exit(0);
}}, 260);
"""
    result_a = _run_node(_DOM_SHIM_JS + driver)
    calls_right_after_pause = result_a["fetchCallCount"]
    assert calls_right_after_pause <= 2, "toggling off must stop scheduling new polls"


def test_ac6_poll_interval_zero_disables_toggle_and_shows_disabled_state() -> None:
    app_script = _extract_script(poll_interval_ms=0)
    driver = f"""
global.__responses = [{{ payload: {json.dumps(_payload(ids=["x"]))} }}];
{app_script}
setTimeout(function () {{
  console.log(JSON.stringify({{
    fetchCallCount: global.__fetchCallCount,
    toggleDisabled: document.getElementById("poll-toggle").disabled,
    freshnessText: document.getElementById("poll-freshness").textContent
  }}));
  process.exit(0);
}}, 200);
"""
    result = _run_node(_DOM_SHIM_JS + driver)
    assert result["fetchCallCount"] == 1
    assert result["toggleDisabled"] is True
    assert "disabled" in result["freshnessText"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
