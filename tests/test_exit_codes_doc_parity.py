# SPDX-License-Identifier: Apache-2.0
"""Doc/code parity for `EXIT_LOCK_HELD` producers (issue athenaeum#1383).

`docs/reference/exit-codes.md` names, in prose, every `_cmd_*.py` module that can
return `EXIT_LOCK_HELD` (75) because it calls
`athenaeum._cli_shared._acquire_or_exit`. That list is only trustworthy if a
future `_cmd_*` module adding a call to `_acquire_or_exit` is caught here
before it ships — a doc that silently falls behind the tree is worse than no
doc, per athenaeum#1383's motivation.

``_modules_calling_acquire_or_exit`` / ``_modules_named_in_doc`` are plain,
reusable functions (not tied to pytest) so both:

- ``TestLiveTreeParity`` — the REAL `src/athenaeum` tree against the REAL
  `docs/reference/exit-codes.md`, and
- ``TestSyntheticFixtureCatchesDrift`` — a synthetic temp-directory `_cmd_x.py`
  plus a doc text that omits it

go through the exact same derivation code. A test that only compared the
live tree to the live doc would trivially pass if the derivation itself just
echoed back the doc's own list — the synthetic fixture is what proves the
derivation is actually reading `src/`, not the doc.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src" / "athenaeum"
EXIT_CODES_DOC = REPO_ROOT / "docs" / "reference" / "exit-codes.md"

_CMD_MODULE_NAME_RE = re.compile(r"`(_cmd_[A-Za-z0-9_]+\.py)`")
_CALL_RE = re.compile(r"\b_acquire_or_exit\s*\(")


def _modules_calling_acquire_or_exit(src_dir: Path) -> set[str]:
    """Return the `.py` module filenames under ``src_dir`` that contain a
    real CALL to ``_acquire_or_exit(...)`` — not merely an import of the
    name, a re-export, a docstring/comment mention, or the function's own
    ``def`` line.

    A line counts as a call when, after stripping leading/trailing
    whitespace, it is not a comment (does not start with ``#``) and is not
    the `_acquire_or_exit` definition itself (does not start with
    ``def _acquire_or_exit``), and it matches ``_acquire_or_exit(`` as a
    whole word followed by an opening paren.
    """
    modules: set[str] = set()
    for path in sorted(src_dir.glob("*.py")):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("def _acquire_or_exit"):
                continue
            if _CALL_RE.search(stripped):
                modules.add(path.name)
                break
    return modules


def _modules_named_in_doc(doc_text: str) -> set[str]:
    """Return the `_cmd_*.py` module names ``doc_text`` credits as
    `EXIT_LOCK_HELD` producers.

    Only backtick-quoted ``_cmd_*.py`` mentions inside a paragraph that also
    mentions ``_acquire_or_exit`` count — this excludes unrelated `_cmd_*.py`
    mentions elsewhere in the same doc (e.g. a different command's own
    section) that have nothing to do with the lock. Paragraphs are blocks of
    text separated by a blank line; a markdown table with no blank lines
    between rows counts as one paragraph, which is why a single row naming
    both ``_acquire_or_exit`` and the producer modules is sufficient.
    """
    modules: set[str] = set()
    for paragraph in re.split(r"\n\s*\n", doc_text):
        if "_acquire_or_exit" not in paragraph:
            continue
        modules.update(_CMD_MODULE_NAME_RE.findall(paragraph))
    return modules


class TestLiveTreeParity:
    def test_doc_names_exactly_the_modules_that_call_it(self) -> None:
        derived_from_src = _modules_calling_acquire_or_exit(SRC_DIR)
        derived_from_doc = _modules_named_in_doc(EXIT_CODES_DOC.read_text())

        assert derived_from_src == derived_from_doc, (
            "docs/reference/exit-codes.md's EXIT_LOCK_HELD producer list has drifted "
            f"from src/athenaeum: modules calling _acquire_or_exit but not "
            f"named in the doc: {sorted(derived_from_src - derived_from_doc)}; "
            f"modules named in the doc but not actually calling it: "
            f"{sorted(derived_from_doc - derived_from_src)}"
        )

    def test_derivation_is_non_trivial(self) -> None:
        """Guard against a derivation that degenerates to an empty set (which
        would make the equality check above vacuous)."""
        derived_from_src = _modules_calling_acquire_or_exit(SRC_DIR)
        assert len(derived_from_src) == 10
        assert derived_from_src == {
            "_cmd_curate.py",
            "_cmd_decay.py",
            "_cmd_drain.py",
            "_cmd_index.py",
            "_cmd_pending.py",
            "_cmd_pii_restore.py",
            "_cmd_reconcile.py",
            "_cmd_repair.py",
            "_cmd_run.py",
            "_cmd_storage.py",
        }


class TestSyntheticFixtureCatchesDrift:
    """Prove the derivation actually reads `src/`, per athenaeum#1383's AC
    that a live-tree-vs-live-doc comparison alone is not sufficient (it would
    also pass for a derivation that just echoed the doc's own list back)."""

    def test_module_calling_it_but_missing_from_doc_is_reported(
        self, tmp_path: Path
    ) -> None:
        fixture_src = tmp_path / "athenaeum"
        fixture_src.mkdir()
        (fixture_src / "_cmd_x.py").write_text(
            "from athenaeum._cli_shared import _acquire_or_exit\n\n\n"
            "def cmd_x(args):\n"
            "    lock = _acquire_or_exit(knowledge_root, args, cfg)\n"
            "    return 0\n"
        )
        # A second module that merely imports/mentions the name, to prove the
        # derivation doesn't over-report every importer as a caller.
        (fixture_src / "_cmd_y.py").write_text(
            "from athenaeum._cli_shared import _acquire_or_exit  # noqa: F401\n"
        )

        doc_text = (
            "| `75` | `EXIT_LOCK_HELD` | returned by `_acquire_or_exit` when "
            "`_cmd_x.py` cannot acquire the run lock. |\n"
        )
        # Deliberately omit `_cmd_x.py` from the doc to prove the mismatch is
        # caught, then also test the doc naming it correctly.
        doc_text_missing = (
            "| `75` | `EXIT_LOCK_HELD` | returned by `_acquire_or_exit` when "
            "the lock cannot be acquired. |\n"
        )

        derived_from_src = _modules_calling_acquire_or_exit(fixture_src)
        assert derived_from_src == {"_cmd_x.py"}

        derived_from_doc_correct = _modules_named_in_doc(doc_text)
        assert derived_from_doc_correct == {"_cmd_x.py"}
        assert derived_from_src == derived_from_doc_correct

        derived_from_doc_missing = _modules_named_in_doc(doc_text_missing)
        assert derived_from_doc_missing == set()
        assert derived_from_src != derived_from_doc_missing, (
            "the synthetic fixture's mismatch (a real caller omitted from "
            "the doc) was NOT reported — the derivation is not actually "
            "reading src/, or is echoing the doc's own list back"
        )

    def test_module_named_in_doc_but_not_a_real_caller_is_reported(
        self, tmp_path: Path
    ) -> None:
        """The inverse drift direction: the doc claims a producer that the
        tree does not actually have (e.g. left over after a module stopped
        calling `_acquire_or_exit`)."""
        fixture_src = tmp_path / "athenaeum"
        fixture_src.mkdir()
        (fixture_src / "_cmd_real.py").write_text(
            "def cmd_real(args):\n"
            "    lock = _acquire_or_exit(knowledge_root, args, cfg)\n"
            "    return 0\n"
        )

        doc_text = (
            "returned by `_acquire_or_exit` when `_cmd_real.py` or "
            "`_cmd_stale.py` cannot acquire the run lock.\n"
        )

        derived_from_src = _modules_calling_acquire_or_exit(fixture_src)
        derived_from_doc = _modules_named_in_doc(doc_text)

        assert derived_from_src == {"_cmd_real.py"}
        assert derived_from_doc == {"_cmd_real.py", "_cmd_stale.py"}
        assert derived_from_src != derived_from_doc


class TestDerivationExcludesNonCalls:
    """Targeted regression coverage for the specific non-`_cmd_*` mentions
    investigated during athenaeum#1383: `cli.py` re-exports the name (import,
    no call), and `decision_answers.py` / `name_collisions.py` /
    `pending_merges.py` / `verdicts.py` only mention it in prose. None of
    these are call sites and none should show up in the derived set."""

    def test_import_only_module_is_not_a_caller(self, tmp_path: Path) -> None:
        fixture_src = tmp_path / "athenaeum"
        fixture_src.mkdir()
        (fixture_src / "cli.py").write_text(
            "from athenaeum._cli_shared import (\n"
            "    _acquire_or_exit,  # noqa: F401 — re-exported\n"
            ")\n"
        )
        assert _modules_calling_acquire_or_exit(fixture_src) == set()

    def test_docstring_mention_only_is_not_a_caller(self, tmp_path: Path) -> None:
        fixture_src = tmp_path / "athenaeum"
        fixture_src.mkdir()
        (fixture_src / "verdicts.py").write_text(
            '"""writers trust the CLI\'s ``_acquire_or_exit`` to already hold\n'
            'the lock."""\n'
        )
        assert _modules_calling_acquire_or_exit(fixture_src) == set()

    def test_definition_itself_is_not_a_caller(self, tmp_path: Path) -> None:
        fixture_src = tmp_path / "athenaeum"
        fixture_src.mkdir()
        (fixture_src / "_cli_shared.py").write_text(
            "def _acquire_or_exit(knowledge_root, args, config=None):\n"
            "    ...\n"
        )
        assert _modules_calling_acquire_or_exit(fixture_src) == set()
