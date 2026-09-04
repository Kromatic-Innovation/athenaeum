# SPDX-License-Identifier: Apache-2.0
"""Guard for issue athenaeum#1360.

``src/athenaeum/__init__.py`` used to eagerly ``from athenaeum.librarian import
(...)`` at module top level. Because ``athenaeum.librarian`` transitively
imports the ``anthropic`` SDK (via ``athenaeum.tiers``/``athenaeum.batch``),
that one line cost ~520ms on EVERY ``import athenaeum`` — paid by
``athenaeum --version`` as much as by anything that actually talks to an LLM.

The fix moved those names to a PEP 562 module ``__getattr__`` so they still
resolve (``athenaeum.run``, ``from athenaeum import ingest``, etc. still
work — this is a laziness fix, not an API removal) but only pay the
librarian/anthropic import cost the first time one of them is actually
touched, not on package import.

This test is a subprocess guard (like ``test_import_graph_acyclic.py``'s
in-process walk, but here we need a *fresh* interpreter each time since
``sys.modules`` is a process-global cache) so it can't be fooled by another
test in the same process having already imported ``anthropic``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout.strip()


def test_import_athenaeum_excludes_anthropic() -> None:
    """Counter-example this must catch: a top-level
    ``from athenaeum.librarian import run`` reappearing in __init__.py."""
    out = _run("import sys; import athenaeum; print('anthropic' in sys.modules)")
    assert out == "False", (
        "`import athenaeum` pulled `anthropic` into sys.modules — the "
        "eager librarian import has regressed. See issue athenaeum#1360."
    )


def test_import_athenaeum_excludes_chromadb() -> None:
    out = _run("import sys; import athenaeum; print('chromadb' in sys.modules)")
    assert out == "False"


def test_search_importable_without_llm_chain() -> None:
    """athenaeum.search must not pull anthropic in — importing a submodule
    always runs the parent package's __init__ first, so this also re-checks
    the __init__.py fix, plus that search.py itself stays import-light."""
    out = _run("import sys; import athenaeum.search; print('anthropic' in sys.modules)")
    assert out == "False"


def test_push_metrics_importable_without_llm_chain() -> None:
    out = _run("import sys; import athenaeum.push_metrics; print('anthropic' in sys.modules)")
    assert out == "False"


def test_lazy_names_still_resolve() -> None:
    """Public-API compatibility: `from athenaeum import ingest` (and the
    other librarian re-exports) must still work — this is a laziness fix,
    not a breaking removal."""
    out = _run(
        "import athenaeum\n"
        "names = ['run', 'ingest', 'process_one', 'rebuild_index', "
        "'reindex', 'session_end', 'discover_raw_files', "
        "'IngestResult', 'SessionEndResult']\n"
        "for n in names:\n"
        "    assert hasattr(athenaeum, n), n\n"
        "print('ok')"
    )
    assert out == "ok"


def test_touching_lazy_name_does_import_librarian() -> None:
    """The cost isn't gone, just deferred: actually using `athenaeum.run`
    must still import the librarian chain (this fix does not stub it out)."""
    out = _run(
        "import sys; import athenaeum; athenaeum.run; print('athenaeum.librarian' in sys.modules)"
    )
    assert out == "True"


def test_unknown_attribute_still_raises_attribute_error() -> None:
    out = _run(
        "import athenaeum\n"
        "try:\n"
        "    athenaeum.definitely_not_a_real_export\n"
        "    print('no-raise')\n"
        "except AttributeError:\n"
        "    print('raised')\n"
    )
    assert out == "raised"
