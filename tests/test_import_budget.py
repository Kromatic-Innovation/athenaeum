# SPDX-License-Identifier: Apache-2.0
"""Guard over what `import athenaeum` and the retrieval modules actually load.

Issue athenaeum#1360. The package root used to eagerly import
:mod:`athenaeum.librarian`, which pulls the ``anthropic`` SDK, so *every*
``import athenaeum`` — and, because importing a submodule executes its package
root first, every ``import athenaeum.search`` and ``import
athenaeum.push_metrics`` — paid ~440 ms for an SDK the command might never
speak to. `docs/retrieval-entry-point-measurements.md` (issue athenaeum#1357)
measured it and showed the cost could not be dodged by importing the submodule
directly, nor by ``importlib.util.spec_from_file_location``: the submodule's own
absolute imports re-enter the root regardless.

**Why an exact pin rather than a "does it import anthropic?" assertion.** The
regression this guards is not "someone imports the SDK" — nobody would do that
deliberately here. It is "someone adds a module-scope import of a module that
transitively reaches it", which is invisible at the call site and was in fact
how the chain formed: `drain_advisor` imports one CONSTANT from `tiers`,
`rule_proposals` imports one private helper from it, and `tiers` imported the
SDK. Pinning the exact transitive set makes any new edge fail loudly with the
name of the module that was added, whether or not that edge happens to reach an
SDK today.

Every check runs in a **subprocess**. In-process assertions on ``sys.modules``
are worthless here: pytest has already imported most of the tree by the time a
test body runs, so an in-process check passes unconditionally.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# Third-party packages that must not appear on a retrieval-path import. Each is
# expensive and none is needed to answer an FTS5 query.
FORBIDDEN_THIRD_PARTY = frozenset({"anthropic", "chromadb", "onnxruntime", "mcp"})

# The pinned transitive sets. Regenerate deliberately -- see the failure message
# in `_assert_pinned` -- never by pasting whatever the run happened to produce.
PINNED_IMPORTS: dict[str, frozenset[str]] = {
    # The lazy root imports nothing at all: every public name resolves through
    # PEP 562 `__getattr__` on first access.
    "athenaeum": frozenset({"athenaeum"}),
    "athenaeum.search": frozenset(
        {
            "athenaeum",
            "athenaeum.atomic_io",
            "athenaeum.authority",
            "athenaeum.config",
            "athenaeum.models",
            "athenaeum.pii",
            "athenaeum.search",
            "athenaeum.storage",
            "athenaeum.store",
        }
    ),
    "athenaeum.push_metrics": frozenset(
        {
            "athenaeum",
            "athenaeum.atomic_io",
            "athenaeum.config",
            "athenaeum.models",
            "athenaeum.push_metrics",
            "athenaeum.store",
        }
    ),
}

_PROBE = textwrap.dedent(
    """
    import importlib, json, sys
    target = sys.argv[1]
    importlib.import_module(target)
    print(json.dumps({
        "athenaeum": sorted(
            m for m in sys.modules
            if m == "athenaeum" or m.startswith("athenaeum.")
        ),
        "third_party": sorted(
            m for m in sys.modules if m in %(forbidden)r
        ),
    }))
    """
)


def _probe(target: str, *, extra_path: Path | None = None) -> dict[str, list[str]]:
    """Import `target` in a clean interpreter and report what came with it."""
    env_path = [str(_REPO / "src")]
    if extra_path is not None:
        env_path.insert(0, str(extra_path))
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE % {"forbidden": set(FORBIDDEN_THIRD_PARTY)}, target],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": ":".join(env_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, f"probe failed for {target}:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("target", sorted(PINNED_IMPORTS))
class TestPinnedImportSets:
    def test_no_forbidden_third_party(self, target: str) -> None:
        """athenaeum#1360 AC1/AC2: the LLM chain is off the retrieval path.

        The criterion's own counter-example: this assertion must fail against
        the tree before the fix, where `athenaeum/__init__.py` imported
        `athenaeum.librarian` at module scope.
        """
        loaded = _probe(target)["third_party"]
        assert loaded == [], (
            f"`import {target}` pulled in {loaded}. These are expensive and "
            f"nothing on the retrieval path needs them; import them inside the "
            f"function that actually calls out, or under TYPE_CHECKING if the "
            f"use is annotation-only."
        )

    def test_transitive_athenaeum_set_is_pinned(self, target: str) -> None:
        """athenaeum#1360 AC3: no new module-scope edge appears unnoticed."""
        actual = frozenset(_probe(target)["athenaeum"])
        expected = PINNED_IMPORTS[target]
        added, removed = sorted(actual - expected), sorted(expected - actual)
        assert actual == expected, (
            f"`import {target}` no longer loads the pinned module set.\n"
            f"  newly loaded: {added or 'none'}\n"
            f"  no longer loaded: {removed or 'none'}\n"
            f"If an addition is deliberate, check what it drags in before "
            f"updating PINNED_IMPORTS -- this guard exists because the "
            f"expensive edges were added one innocuous-looking constant import "
            f"at a time (issue athenaeum#1360)."
        )


class TestGuardActuallyDetects:
    """A guard that has never been seen to fail is not evidence of anything.

    Both checks above are asserted to FIND something, against deliberately
    constructed offenders, so a broken probe cannot masquerade as a clean tree.
    """

    def test_a_module_scope_librarian_import_is_detected(self, tmp_path: Path) -> None:
        """The exact counter-example athenaeum#1360 AC3 names, made executable.

        A module whose top level does `from athenaeum.librarian import run` must
        be seen by the transitive-set probe. This is the edge-detection control
        for `test_transitive_athenaeum_set_is_pinned`.
        """
        offender = tmp_path / "athenaeum_edge_probe.py"
        offender.write_text(
            "from athenaeum.librarian import run  # noqa: F401\n", encoding="utf-8"
        )
        loaded = _probe("athenaeum_edge_probe", extra_path=tmp_path)["athenaeum"]
        assert "athenaeum.librarian" in loaded, (
            "the probe did not see `athenaeum.librarian` even though the module "
            "under test imports it at module scope -- the pinned-set guard "
            "cannot be trusted until this passes"
        )
        # And it is genuinely a new edge relative to every pinned set, which is
        # what makes the guard above fail rather than shrug.
        for target, pinned in PINNED_IMPORTS.items():
            assert "athenaeum.librarian" not in pinned, target

    def test_a_module_scope_sdk_import_is_detected(self, tmp_path: Path) -> None:
        """Control for `test_no_forbidden_third_party`.

        Note this is a SEPARATE control from the one above, because on the fixed
        tree those two conditions have come apart: `athenaeum.librarian` no
        longer drags `anthropic` in either (it reached the SDK through
        `athenaeum.tiers`, whose import is now annotation-only). Using the
        librarian import as the SDK control would therefore silently stop
        testing anything -- which is exactly how it first failed.
        """
        offender = tmp_path / "athenaeum_sdk_probe.py"
        offender.write_text("import anthropic  # noqa: F401\n", encoding="utf-8")
        loaded = _probe("athenaeum_sdk_probe", extra_path=tmp_path)["third_party"]
        assert loaded == ["anthropic"], (
            f"the probe reported {loaded} for a module that imports `anthropic` "
            f"directly -- the forbidden-package guard cannot be trusted"
        )


class TestCliParserStaysOffTheLlmChain:
    """The CLI is where this cost was actually paid, so it gets its own guard.

    `cli.build_parser()` imports all ~31 `_cmd_*` modules to assemble the
    subcommand tree, so any one of them reaching the SDK puts it on the path of
    every invocation -- `athenaeum --version` included. No exact pin here: the
    parser's module set is large and legitimately churns as subcommands are
    added. The durable invariant is which expensive packages it must not touch.
    """

    def test_build_parser_does_not_import_the_sdk(self) -> None:
        probe = textwrap.dedent(
            """
            import json, sys
            from athenaeum.cli import build_parser
            build_parser()
            print(json.dumps(sorted(m for m in sys.modules if m in %r)))
            """
        ) % set(FORBIDDEN_THIRD_PARTY)
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        loaded = json.loads(proc.stdout)
        assert loaded == [], (
            f"building the CLI parser imported {loaded}. Every `athenaeum` "
            f"invocation pays this, including `--version` and `--help`. A "
            f"subcommand module that needs an SDK should import it inside its "
            f"handler, not at module scope (issue athenaeum#1360)."
        )


class TestPublicApiSurvivesLazyReExport:
    """athenaeum#1360 AC4: laziness must not be a silent breaking change."""

    def test_every_public_name_still_resolves(self) -> None:
        probe = textwrap.dedent(
            """
            import athenaeum
            missing = [n for n in athenaeum.__all__ if not hasattr(athenaeum, n)]
            assert not missing, missing
            print(len(athenaeum.__all__))
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) == 32

    def test_from_import_still_works_for_an_external_installer(self) -> None:
        """The criterion's counter-example: `from athenaeum import ingest`."""
        probe = (
            "from athenaeum import ingest, parse_frontmatter, FilesystemStore, "
            "init_knowledge_dir; print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"

    def test_dir_still_reports_the_whole_surface(self) -> None:
        """Surface-extraction tooling reads `dir()`; laziness must not shrink it."""
        probe = (
            "import athenaeum; "
            "print(set(athenaeum.__all__) <= set(dir(athenaeum)) "
            "and '__version__' in dir(athenaeum))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "True"

    def test_unknown_attribute_still_raises_attribute_error(self) -> None:
        """`__getattr__` must not turn a typo into something other than AttributeError."""
        probe = textwrap.dedent(
            """
            import athenaeum
            try:
                athenaeum.definitely_not_a_real_name
            except AttributeError as exc:
                assert "definitely_not_a_real_name" in str(exc), str(exc)
                print("ok")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"

    def test_version_resolves_lazily_without_importing_the_tree(self) -> None:
        """Reading `__version__` must work and must not drag the package in."""
        probe = textwrap.dedent(
            """
            import json, sys
            import athenaeum
            v = athenaeum.__version__
            assert v and isinstance(v, str), v
            print(json.dumps(sorted(
                m for m in sys.modules
                if m == "athenaeum" or m.startswith("athenaeum.")
            )))
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == ["athenaeum"]

    def test_repeated_access_is_cached_not_reimported(self) -> None:
        """First access caches into globals(); the second must be a plain lookup."""
        probe = textwrap.dedent(
            """
            import athenaeum
            first = athenaeum.ingest
            assert "ingest" in vars(athenaeum), "not cached into globals()"
            assert athenaeum.ingest is first
            print("ok")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(_REPO / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"
