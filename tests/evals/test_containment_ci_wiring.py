# SPDX-License-Identifier: Apache-2.0
"""Offline proof that nothing this issue adds runs in ci.yml, becomes a
required check, or is selected by evals.yml (issue athenaeum#1521 AC6).

UNMARKED — no network, no credential; reads the committed workflow/config
files directly (the "most direct way the repo already uses" — see
``tests/test_retrieval_golden_1420.py``'s docstring, which reasons about
the same `pyproject.toml` addopts/markers mechanism in prose; this module
asserts it mechanically instead).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pytest_ini_options() -> dict[str, object]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    result = data["tool"]["pytest"]["ini_options"]
    assert isinstance(result, dict)
    return result


def test_rollout_marker_is_registered() -> None:
    markers = _pytest_ini_options()["markers"]
    assert any(str(m).startswith("rollout:") for m in markers)


def test_default_addopts_deselects_rollout() -> None:
    addopts = str(_pytest_ini_options()["addopts"])
    assert "not rollout" in addopts
    # Same for its siblings, so a regression here would also be caught by
    # existing behaviour -- belt and suspenders on the one line that matters.
    assert "not eval" in addopts
    assert "not embedding" in addopts


def test_ci_yml_test_job_uses_default_addopts_no_marker_override() -> None:
    """ci.yml's `test` job must invoke plain `pytest tests/ ...` with no `-m`
    override -- that is what makes the default addopts (which excludes
    `rollout`, proven above) the actual selection CI runs, rather than an
    independent `-m` expression that could silently diverge from it."""
    ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"Run tests\s*\n\s*run:\s*(.+)", ci_yml)
    assert match, "could not find the ci.yml 'Run tests' step"
    command = match.group(1)
    assert "pytest tests/" in command
    assert " -m " not in command, (
        f"ci.yml's test step now overrides -m directly: {command!r} -- if it "
        "ever adds 'rollout' to a positive selection this test must fail"
    )


def test_ci_yml_never_mentions_rollout_or_containment() -> None:
    ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "rollout" not in ci_yml
    assert "containment" not in ci_yml


def test_evals_yml_never_selects_rollout_marker() -> None:
    """evals.yml's `eval` job selects `-m eval` only, and `embedding-suite`
    selects `-m embedding` only -- a rollout-marked test must never be
    reachable from EITHER, since neither is the required CI gate but both
    ARE workflows that spend real money."""
    evals_yml = (REPO_ROOT / ".github" / "workflows" / "evals.yml").read_text(encoding="utf-8")
    assert "-m rollout" not in evals_yml
    assert "rollout" not in evals_yml
    assert "containment" not in evals_yml


# --- athenaeum#1742: the split cannot drift back together -----------------
#
# PR athenaeum#1740 shipped a regression in test_render_report_leads_with_pull_no_call_rate
# (the athenaeum#1523 report-ordering contract) green, because the whole module
# carried pytest.mark.rollout even though it makes no LLM call and spawns no
# `claude` binary -- the marker deselected it from ci.yml's default job by
# construction. The two tests below pin the fix and guard against it drifting
# back: one proves every REMAINING rollout-marked module in this directory
# actually spends tokens, the other pins the specific modules that must NOT
# carry a deselecting marker.

_EVALS_DIR = REPO_ROOT / "tests" / "evals"

# A module carrying one of these markers is deselected by ci.yml's default
# `pytest tests/` invocation (proven above). Matches this directory's own
# convention -- a single module-level `pytestmark = pytest.mark.<name>` line,
# with nothing else on it (per-test/per-param marks, e.g. test_recall_eval.py's
# `pytest.param(..., marks=pytest.mark.embedding)`, are a second axis this
# helper deliberately does not chase -- the module-level mark already governs
# whether the file is reachable by default).
_MODULE_MARKER_RE = re.compile(
    r"^pytestmark\s*=\s*pytest\.mark\.(eval|embedding|rollout)\s*$", re.MULTILINE
)

# Textual proof a test file genuinely spends tokens or spawns the real
# `claude` binary, rather than merely sharing a module family with one that
# does. Kept narrow on purpose -- broadening it is how a future token-free
# test could slip back under `rollout` unnoticed.
_LIVE_SOURCE_SIGNALS = (
    "ANTHROPIC_API_KEY",
    "ATHENAEUM_LIVE_TESTS",
    'shutil.which("claude")',
    "anthropic.Anthropic(",
)


def test_rollout_deselected_tests_are_actually_live() -> None:
    """Every module still carrying `pytest.mark.rollout` must show, in its
    OWN source, that it constructs a live client, spawns the real `claude`
    binary, or gates on a live-test env var -- not merely that it lives
    beside a module that does. `eval`/`embedding` get no such per-source
    check here: those markers are pre-existing and out of this issue's
    scope, and their OWN pyproject.toml `markers` reason string already
    documents the live/real-model cost they mean (see
    test_rollout_marker_is_registered's sibling assertions above) -- exactly
    the "marker reason string" escape the issue names. `rollout` gets no
    such pass: a marker string alone, undischarged by the file's own
    content, is exactly what let athenaeum#1740's regression ship deselected
    without spending a token."""
    offenders = []
    for path in sorted(_EVALS_DIR.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        match = _MODULE_MARKER_RE.search(source)
        if match is None or match.group(1) != "rollout":
            continue
        if not any(signal in source for signal in _LIVE_SOURCE_SIGNALS):
            offenders.append(path.name)
    assert not offenders, (
        f"rollout-marked with no live-client/claude-binary/live-env-gate "
        f"signal found in source: {offenders} -- either the test genuinely "
        f"spends no tokens (drop pytest.mark.rollout) or it needs a "
        f"recognizable live signal added to _LIVE_SOURCE_SIGNALS"
    )


def test_token_free_report_modules_carry_no_deselecting_marker() -> None:
    """Pins the athenaeum#1740 regression class directly: these modules render
    reports, round-trip payloads, or drive a CLI or an api-mode tool loop
    from synthetic fixtures and stub clients -- no LLM call, no `claude`
    spawn -- so they run in ci.yml's default job and must never be silently
    deselected again.

    The last three landed `rollout`-marked with athenaeum#1743 while this
    issue was in flight, each one token-free by its own docstring's account:
    the drift this issue exists to stop recurred within a single PR, which
    is why they are pinned by name here and not merely unmarked.
    """
    token_free_modules = [
        "test_north_star_report.py",
        "test_rollout.py",
        "test_north_star_cli.py",
        "test_rollout_payload.py",
        "test_rollout_push_breadcrumb_spike.py",
        "test_north_star_verdicts.py",
        "test_rollout_api_mode.py",
        "test_rollout_api_mode_hardening.py",
        "test_rollout_mode_labelling.py",
        # athenaeum#1751: the grid's concurrency and partial-run tests. Both
        # drive `main()` through stub runners and `tests.conftest.FakeLLMClient`
        # only -- the drift risk is identical (they import
        # `tests.evals.rollout`, which reads like a reason to mark them), so
        # they are pinned by name for the same reason the eight above are.
        "test_north_star_concurrency.py",
        "test_north_star_partial_safety.py",
    ]
    for name in token_free_modules:
        source = (_EVALS_DIR / name).read_text(encoding="utf-8")
        match = _MODULE_MARKER_RE.search(source)
        assert match is None, (
            f"{name} carries pytest.mark.{match.group(1) if match else '?'} -- "
            "expected no deselecting marker"
        )
