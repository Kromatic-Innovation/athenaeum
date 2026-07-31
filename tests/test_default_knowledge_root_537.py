# SPDX-License-Identifier: Apache-2.0
"""Guard tests for the single ``DEFAULT_KNOWLEDGE_ROOT`` constant (issue #537).

Issue #537 collapsed 38 copies of the literal ``Path("~/knowledge")`` — argparse
``--path`` defaults and resolver fallbacks scattered across the ``_cmd_*``
modules — into one constant in :mod:`athenaeum.config`. These tests are the
change-amplification guard the issue's "Suggested verification" section asks
for: they fail if the literal ever creeps back into a second module, or if the
config/librarian defaults drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from athenaeum import config, librarian

# The exact source literal the sweep eliminated. Written without the parens so
# this test file itself is not counted by the scan below.
_LITERAL = re.compile(r'Path\(\s*"~/knowledge"\s*\)')

_PKG_DIR = Path(config.__file__).resolve().parent


def _modules_containing_literal() -> list[str]:
    hits = []
    for py in sorted(_PKG_DIR.rglob("*.py")):
        if _LITERAL.search(py.read_text(encoding="utf-8")):
            hits.append(py.name)
    return hits


def test_tilde_literal_appears_in_exactly_one_module() -> None:
    """The ``Path("~/knowledge")`` literal must live in exactly one place."""
    hits = _modules_containing_literal()
    assert hits == ["config.py"], (
        "The default knowledge-root literal must live only in config.py "
        f"(issue #537); found it in: {hits}"
    )


def test_config_default_is_the_tilde_template() -> None:
    assert config.DEFAULT_KNOWLEDGE_ROOT == Path("~/knowledge")


def test_librarian_default_is_the_expanded_form_of_the_template() -> None:
    """librarian's runtime default is the pre-expanded config template."""
    assert (
        librarian.DEFAULT_KNOWLEDGE_ROOT
        == config.DEFAULT_KNOWLEDGE_ROOT.expanduser()
    )
    # Byte-for-byte identical to the former ``Path.home() / "knowledge"`` literal.
    assert librarian.DEFAULT_KNOWLEDGE_ROOT == Path.home() / "knowledge"
    assert librarian.DEFAULT_RAW_ROOT == Path.home() / "knowledge" / "raw"
    assert librarian.DEFAULT_WIKI_ROOT == Path.home() / "knowledge" / "wiki"
