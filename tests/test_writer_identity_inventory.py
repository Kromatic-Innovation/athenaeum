# SPDX-License-Identifier: Apache-2.0
"""Bind ``docs/design/writer-identity.md`` to the code by membership (issue athenaeum#2041).

The design doc inventories every code path that makes an authority decision
about a writer. This test AST-scans ``src/athenaeum/`` for those call sites
and fails if any call site's ``module:function`` is not cited in the doc as
``src/athenaeum/<module>.py:<function>``. It checks MEMBERSHIP, not a count,
so adding a call site without documenting it fails, and documenting an extra
symbol does not.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "athenaeum"
DOC = REPO_ROOT / "docs" / "design" / "writer-identity.md"

#: Calls that decide authority about a writer, wherever they appear in src/.
AUTHORITY_CALLS = frozenset(
    {
        "compare_authority",
        "strictly_greater_authority",
        "treated_as_equal_authority",
        "classify_for_correct_gate",
    }
)
#: ``compare_asserters`` is only an authority decision inside supersession.py;
#: other uses are classified in the doc but not required by this scan.
ASSERTER_CALL = "compare_asserters"
ASSERTER_CALL_MODULES = frozenset({"supersession"})
RECORD_CALL = "record_resolution"

_CITATION = re.compile(r"src/athenaeum/(\w+)\.py:(\w+)")


def cited_symbols(doc_text: str) -> set[tuple[str, str]]:
    """``(module, symbol)`` pairs cited as ``src/athenaeum/<module>.py:<symbol>``."""
    return {(m.group(1), m.group(2)) for m in _CITATION.finditer(doc_text)}


def _call_name(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def authority_call_sites(root: Path) -> set[tuple[str, str, str]]:
    """``(module, enclosing function, call name)`` for every authority call under *root*."""
    sites: set[tuple[str, str, str]] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.stem
        stack: list[str] = []

        def visit(node: ast.AST) -> None:
            is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_fn:
                stack.append(node.name)  # type: ignore[union-attr]
            if isinstance(node, ast.Call):
                name = _call_name(node)
                wanted = (
                    name in AUTHORITY_CALLS
                    or (name == ASSERTER_CALL and module in ASSERTER_CALL_MODULES)
                    or (
                        name == RECORD_CALL and any(kw.arg == "resolved_by" for kw in node.keywords)
                    )
                )
                if wanted and name is not None:
                    sites.add((module, stack[-1] if stack else "<module>", name))
            for child in ast.iter_child_nodes(node):
                visit(child)
            if is_fn:
                stack.pop()

        visit(tree)
    return sites


def uncited_call_sites(root: Path, doc_text: str) -> list[tuple[str, str, str]]:
    cited = cited_symbols(doc_text)
    return sorted(s for s in authority_call_sites(root) if (s[0], s[1]) not in cited)


class TestInventoryMembership:
    def test_every_authority_call_site_is_cited_in_the_doc(self) -> None:
        missing = uncited_call_sites(SRC, DOC.read_text(encoding="utf-8"))
        assert not missing, (
            "authority call sites missing from docs/design/writer-identity.md "
            f"(cite as src/athenaeum/<module>.py:<function>): {missing}"
        )

    def test_scan_finds_the_known_sites(self) -> None:
        """Guard against the scan silently matching nothing."""
        sites = authority_call_sites(SRC)
        for expected in [
            ("supersession", "decide_supersession", "compare_authority"),
            ("supersession", "decide_supersession", "strictly_greater_authority"),
            ("supersession", "decide_supersession", "treated_as_equal_authority"),
            ("supersession", "_compare_asserters", "compare_asserters"),
            ("asserter_authority", "strictly_greater_authority", "compare_authority"),
            ("answers", "ingest_answers", "record_resolution"),
            ("tiers", "_record_auto", "record_resolution"),
            ("resolutions", "_transcript_authorizes_correct", "classify_for_correct_gate"),
        ]:
            assert expected in sites, expected

    def test_counter_example_uncited_call_fails(self, tmp_path: Path) -> None:
        """An uncited ``compare_authority`` call in a fixture module must be caught."""
        fixture_root = tmp_path / "athenaeum"
        fixture_root.mkdir()
        (fixture_root / "rogue_consumer.py").write_text(
            "from athenaeum.asserter_authority import compare_authority\n"
            "\n"
            "def sneaky_promotion(a, b):\n"
            "    return compare_authority(a, b)\n",
            encoding="utf-8",
        )
        missing = uncited_call_sites(fixture_root, DOC.read_text(encoding="utf-8"))
        assert missing == [("rogue_consumer", "sneaky_promotion", "compare_authority")]

    def test_citing_the_fixture_site_clears_it(self, tmp_path: Path) -> None:
        fixture_root = tmp_path / "athenaeum"
        fixture_root.mkdir()
        (fixture_root / "rogue_consumer.py").write_text(
            "def sneaky_promotion(a, b):\n    return compare_authority(a, b)\n",
            encoding="utf-8",
        )
        doc = "see `src/athenaeum/rogue_consumer.py:sneaky_promotion`"
        assert uncited_call_sites(fixture_root, doc) == []

    def test_record_resolution_without_resolved_by_is_not_an_authority_site(
        self, tmp_path: Path
    ) -> None:
        fixture_root = tmp_path / "athenaeum"
        fixture_root.mkdir()
        (fixture_root / "m.py").write_text(
            "def f():\n    record_resolution(root, fingerprint='x', verdict='v')\n",
            encoding="utf-8",
        )
        assert authority_call_sites(fixture_root) == set()


class TestDocCarriesSentence:
    def test_labels_sentence_in_doc_and_module_docstring(self) -> None:
        from athenaeum import writer_registry

        sentence = (
            "A writer-registry entry labels a writer; it does not verify one, "
            "and it never confers authority."
        )
        assert writer_registry.LABELS_NOT_VERIFIES_SENTENCE == sentence
        assert sentence in DOC.read_text(encoding="utf-8")
        assert sentence in (writer_registry.__doc__ or "")
