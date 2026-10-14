# SPDX-License-Identifier: Apache-2.0
"""Component-boundary pins for the Maya PII-hazard classifier (issue athenaeum#2063, AC11).

Maya is never registered as a :mod:`athenaeum.sensitivity` recognizer, and
no redaction-site module imports either new module
(:mod:`athenaeum.pii_hazard_classifier` or :mod:`athenaeum.yesno`).
"""

from __future__ import annotations

import ast
from pathlib import Path

from athenaeum.sensitivity import available_recognizers

_SRC = Path(__file__).resolve().parents[1] / "src" / "athenaeum"

#: Issue athenaeum#2063's Vitruvius design, "where it hooks (and where it
#: does not)": every module whose job is redaction, migration, or
#: conformance -- Maya is permissive-direction and must never reach any of
#: these.
_REDACTION_SITE_MODULES = (
    "outbound_pii",
    "sensitivity_routing",
    "storage_migrate",
    "pending_merges_pii",
    "bounce_contract",
)

_FORBIDDEN_IMPORTS = ("athenaeum.pii_hazard_classifier", "athenaeum.yesno")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestNotARecognizer:
    def test_available_recognizers_has_no_maya_entry(self) -> None:
        recognizers = available_recognizers(None)
        for name in recognizers:
            assert "maya" not in name.lower()

    def test_importing_the_classifier_module_does_not_register_anything(self) -> None:
        before = set(available_recognizers(None))
        import athenaeum.pii_hazard_classifier
        import athenaeum.yesno  # noqa: F401

        after = set(available_recognizers(None))
        assert before == after


class TestRedactionSitesNeverImportTheClassifier:
    def test_no_redaction_site_module_imports_pii_hazard_classifier_or_yesno(self) -> None:
        for name in _REDACTION_SITE_MODULES:
            path = _SRC / f"{name}.py"
            assert path.is_file(), f"expected redaction-site module missing: {path}"
            imported = _imported_modules(path)
            for forbidden in _FORBIDDEN_IMPORTS:
                assert forbidden not in imported, f"{name}.py imports {forbidden}"

    def test_pii_module_is_not_touched(self) -> None:
        # athenaeum.pii (L1) is deliberately not touched by this issue.
        pii_source = (_SRC / "pii.py").read_text(encoding="utf-8")
        for forbidden in _FORBIDDEN_IMPORTS:
            assert forbidden not in pii_source
