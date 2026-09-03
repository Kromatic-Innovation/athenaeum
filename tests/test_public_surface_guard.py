# SPDX-License-Identifier: Apache-2.0
"""The semver guard over athenaeum's public surface (issue athenaeum#1335).

The defect: `0.19.45` removed four public entry points relative to the last
PUBLISHED release `v0.19.0` while moving only the patch digit. The CHANGELOG
documented every removal; the version number contradicted it. Two zenodotus
reviewers caught it by reading, and nothing in CI did.

**These tests are written so the guard is exercised in BOTH directions on every
run.** A guard that only ever asserts "the current tree is fine" becomes
unfalsifiable the moment the version is corrected: everything passes, and
nobody can tell whether it passes because the guard works or because the guard
never fires. So `TestTheHistoricalDefect` pins the literal `0.19.45` against the
real committed baseline and the real current surface and asserts it is REJECTED,
alongside `0.20.0` being accepted — real data, both verdicts, permanently.
"""

from __future__ import annotations

import asyncio
import inspect
import tomllib
from pathlib import Path

import pytest

from tests.public_surface import (
    BASELINE_PATH,
    bump_is_at_least_minor,
    check_surface_against_version,
    extract_surface,
    load_baseline,
    mcp_tool_names,
    parse_version,
    removed_names,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The version the defect shipped as. Kept as a literal on purpose — once
#: pyproject moves to 0.20.0 this is the only remaining witness that the guard
#: rejects what it was built to reject.
DEFECTIVE_VERSION = "0.19.45"


def _pyproject_version() -> str:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


class TestVersionArithmetic:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.19.0", (0, 19, 0)),
            ("0.19.45", (0, 19, 45)),
            ("1.0.0", (1, 0, 0)),
            ("0.20.0rc1", (0, 20, 0)),
            ("0.20.0+local", (0, 20, 0)),
            ("0.20.0-rc.1", (0, 20, 0)),
        ],
    )
    def test_parses(self, raw: str, expected: tuple[int, int, int]) -> None:
        assert parse_version(raw) == expected

    def test_rejects_a_two_part_version(self) -> None:
        with pytest.raises(ValueError, match="three-part"):
            parse_version("0.19")

    @pytest.mark.parametrize(
        ("baseline", "current", "expected"),
        [
            ("0.19.0", "0.19.45", False),  # the defect: patch only
            ("0.19.0", "0.19.1", False),
            ("0.19.0", "0.19.0", False),
            ("0.19.0", "0.20.0", True),  # the fix
            ("0.19.0", "0.21.3", True),
            ("0.19.0", "1.0.0", True),
        ],
    )
    def test_minor_or_greater(self, baseline: str, current: str, expected: bool) -> None:
        assert bump_is_at_least_minor(baseline, current) is expected

    def test_zero_x_gets_no_exemption(self) -> None:
        """semver's 0.x allowance permits breakage in a MINOR bump, not a patch.

        Worth pinning: "it's 0.x, anything goes" is the reading that produced
        the defect, and this project's CHANGELOG header claims semver adherence.
        """
        assert bump_is_at_least_minor("0.19.0", "0.19.99") is False


class TestGuardLogic:
    """The rule, over synthetic surfaces: removals need a minor-or-greater bump."""

    BEFORE = {"cli_subcommands": ["alpha", "beta"], "python_all": ["A"], "mcp_tools": ["t"]}

    def _check(self, current_surface: dict[str, list[str]], version: str) -> str | None:
        return check_surface_against_version(
            baseline_version="0.19.0",
            baseline_surface=self.BEFORE,
            current_version=version,
            current_surface=current_surface,
        )

    def test_removal_on_a_patch_bump_is_rejected(self) -> None:
        shrunk = {**self.BEFORE, "cli_subcommands": ["alpha"]}
        message = self._check(shrunk, "0.19.45")
        assert message is not None
        assert "beta" in message
        assert "0.19.45" in message and "0.19.0" in message

    def test_the_same_removal_on_a_minor_bump_is_accepted(self) -> None:
        shrunk = {**self.BEFORE, "cli_subcommands": ["alpha"]}
        assert self._check(shrunk, "0.20.0") is None

    def test_additions_are_always_fine(self) -> None:
        grown = {**self.BEFORE, "cli_subcommands": ["alpha", "beta", "gamma"]}
        assert self._check(grown, "0.19.45") is None

    def test_an_unchanged_surface_is_fine(self) -> None:
        assert self._check(dict(self.BEFORE), "0.19.45") is None

    def test_growth_does_not_mask_a_removal(self) -> None:
        """The anti-count test.

        Between v0.19.0 and the candidate the CLI grew 39 -> 48 and ``__all__``
        grew 21 -> 32, so every count-based check passes while four named
        entry points are gone. Only a set difference sees it.
        """
        swapped = {**self.BEFORE, "cli_subcommands": ["alpha", "gamma", "delta"]}
        assert len(swapped["cli_subcommands"]) > len(self.BEFORE["cli_subcommands"])
        message = self._check(swapped, "0.19.45")
        assert message is not None
        assert "beta" in message

    def test_every_dimension_is_checked(self) -> None:
        for dimension in ("cli_subcommands", "python_all", "mcp_tools"):
            shrunk = {**self.BEFORE, dimension: []}
            message = self._check(shrunk, "0.19.45")
            assert message is not None, f"{dimension} removal went unnoticed"
            assert dimension in message

    def test_an_unknown_dimension_is_not_an_error(self) -> None:
        """Adding a fourth dimension later must not invalidate a committed baseline."""
        assert removed_names({"future_dimension": ["x"]}, self.BEFORE) == {}


class TestTheHistoricalDefect:
    """The guard, against REAL data, in both directions — permanently.

    This is athenaeum#1335's third acceptance criterion: the guard must be
    demonstrated FAILING on the version the defect shipped as, not merely
    passing on the corrected one.
    """

    def test_the_real_removals_are_detected(self) -> None:
        removed = removed_names(load_baseline(), extract_surface())
        assert removed, (
            "the committed baseline and the working tree show no removed public "
            "surface at all — either the baseline is stale or it was hand-written"
        )
        flat = {name for names in removed.values() for name in names}
        # The named surfaces athenaeum#1328's zenodotus panel objected to.
        assert {"people", "bounce-divergence"} <= flat
        assert "read_person" in flat

    def test_0_19_45_is_rejected(self) -> None:
        baseline = load_baseline()
        message = check_surface_against_version(
            baseline_version=baseline["version"],
            baseline_surface=baseline,
            current_version=DEFECTIVE_VERSION,
            current_surface=extract_surface(),
        )
        assert message is not None, (
            f"{DEFECTIVE_VERSION} removes published public surface on a patch "
            "bump and MUST be rejected — the guard is inert"
        )
        assert "PATCH-only" in message

    def test_0_20_0_is_accepted(self) -> None:
        baseline = load_baseline()
        assert (
            check_surface_against_version(
                baseline_version=baseline["version"],
                baseline_surface=baseline,
                current_version="0.20.0",
                current_surface=extract_surface(),
            )
            is None
        )


class TestExtractorFidelity:
    """A static extractor that under-reports turns the guard into a rubber stamp.

    Not hypothetical: the first draft of :func:`mcp_tool_names` handled only the
    ``@mcp.tool()`` decorator and missed ``recall``, which is registered via an
    explicit ``mcp.tool()(recall)`` call — so it reported ``recall``, a tool
    that is very much still there, as REMOVED public surface.
    """

    def test_static_mcp_extraction_matches_the_live_registration(self, tmp_path: Path) -> None:
        pytest.importorskip("mcp", reason="MCP tool enumeration needs the mcp extra")
        from athenaeum.mcp_server import create_server

        (tmp_path / "wiki").mkdir()
        (tmp_path / "raw").mkdir()
        server = create_server(raw_root=tmp_path / "raw", wiki_root=tmp_path / "wiki")

        listed = server.list_tools()
        if inspect.isawaitable(listed):
            listed = asyncio.run(listed)
        runtime = sorted(tool.name for tool in listed)

        assert mcp_tool_names() == runtime, (
            "tests/public_surface.py::mcp_tool_names disagrees with the tools "
            "FastMCP actually registers; the guard would mis-report removals"
        )

    def test_recall_is_seen_despite_its_non_decorator_registration(self) -> None:
        assert "recall" in mcp_tool_names()


class TestBaselineHygiene:
    def test_baseline_is_committed_and_well_formed(self) -> None:
        assert BASELINE_PATH.exists(), f"missing baseline: {BASELINE_PATH}"
        baseline = load_baseline()
        assert parse_version(baseline["version"])
        for dimension in ("cli_subcommands", "python_all", "mcp_tools"):
            assert baseline[dimension], f"baseline {dimension} is empty"
            assert baseline[dimension] == sorted(baseline[dimension])

    def test_baseline_is_not_ahead_of_the_working_tree(self) -> None:
        """The baseline describes a PUBLISHED release, so it cannot lead us."""
        assert parse_version(load_baseline()["version"]) <= parse_version(_pyproject_version())


class TestWorkingTree:
    """The live assertion — the one that fails a real offending release."""

    def test_this_version_honours_its_surface_changes(self) -> None:
        baseline = load_baseline()
        message = check_surface_against_version(
            baseline_version=baseline["version"],
            baseline_surface=baseline,
            current_version=_pyproject_version(),
            current_surface=extract_surface(),
        )
        assert message is None, message
