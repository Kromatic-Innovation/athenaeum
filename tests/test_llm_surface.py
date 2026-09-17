# SPDX-License-Identifier: Apache-2.0
"""Rot check for `.github/llm-surface.txt` (issue athenaeum#1731).

The surface list is the ONE file `.github/workflows/eval-receipt-check.yml`
and this test both read. This test does not assert the list's exact
contents (it carries hand-curated entries no test derives — see the file's
own header) — it asserts a SUBSET: every file the codebase itself marks as
LLM-facing must appear in the list, so the list cannot rot silently as new
LLM-facing modules are added.

Three independently-derived "must appear" sets, mirroring athenaeum#1731 AC1:

1. Every `src/athenaeum/*.py` file whose source contains the literal
   ``LLMBackend`` (the same `grep -l LLMBackend src/athenaeum/*.py`
   semantics the acceptance criteria names — substring presence, not an
   AST import analysis).
2. Every module registered in `athenaeum.prompt_registry.PROMPT_META` (a
   prompt constant's home module), derived via `prompt_registry`'s own
   `_display_path`-equivalent mapping rather than re-implemented here.
3. Every `.md` file under `src/athenaeum/prompts/` — `tiers.py` loads these
   as live system prompts via `importlib.resources.files("athenaeum.prompts")`
   (``name_resolution_confirm.md``, ``create_name_variant_decision.md``), so
   a new file dropped in that directory is exactly as much a prompt edit as
   a `PROMPT_META` row and must be just as visible to this gate. Checked
   with the same directory-prefix semantics the check script uses
   (`check_llm_surface_receipt._matches`), imported directly rather than
   re-implemented, so the two cannot drift apart.

Offline, zero network, no `eval`/`embedding` marker — runs in the default
suite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from athenaeum import prompt_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
SURFACE_FILE = REPO_ROOT / ".github" / "llm-surface.txt"
SRC_ATHENAEUM = REPO_ROOT / "src" / "athenaeum"
PROMPTS_DIR = SRC_ATHENAEUM / "prompts"

_SCRIPT_PATH = REPO_ROOT / "scripts" / "check_llm_surface_receipt.py"
_spec = importlib.util.spec_from_file_location("check_llm_surface_receipt", _SCRIPT_PATH)
assert _spec and _spec.loader
_check_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _check_script
_spec.loader.exec_module(_check_script)
_matches = _check_script._matches


def _load_surface() -> set[str]:
    lines: set[str] = set()
    for raw in SURFACE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.add(line)
    return lines


def _llm_backend_importers() -> set[str]:
    """`grep -l LLMBackend src/athenaeum/*.py`, expressed as repo-relative paths."""
    hits = set()
    for path in sorted(SRC_ATHENAEUM.glob("*.py")):
        if "LLMBackend" in path.read_text(encoding="utf-8"):
            hits.add(f"src/athenaeum/{path.name}")
    return hits


def _prompt_registry_modules() -> set[str]:
    """Every module `PROMPT_META` indexes, as repo-relative `.py` paths."""
    modules = {meta.module for meta in prompt_registry.PROMPT_META.values()}
    return {"src/" + module.replace(".", "/") + ".py" for module in modules}


def test_surface_file_exists_and_is_non_empty() -> None:
    surface = _load_surface()
    assert surface, f"{SURFACE_FILE} must list at least one path"


def test_llm_backend_importers_are_on_the_surface() -> None:
    surface = _load_surface()
    importers = _llm_backend_importers()
    missing = importers - surface
    assert not missing, (
        "the following src/athenaeum/*.py files import LLMBackend but are "
        f"missing from {SURFACE_FILE}: {sorted(missing)} — add them to that "
        "file."
    )


def test_prompt_registry_modules_are_on_the_surface() -> None:
    surface = _load_surface()
    modules = _prompt_registry_modules()
    missing = modules - surface
    assert not missing, (
        "the following modules are registered in prompt_registry.py's "
        f"PROMPT_META but missing from {SURFACE_FILE}: {sorted(missing)} — "
        "add them to that file."
    )


def test_prompt_md_files_are_covered_by_the_surface() -> None:
    surface = _load_surface()
    md_files = sorted(
        f"src/athenaeum/prompts/{p.name}" for p in PROMPTS_DIR.glob("*.md")
    )
    assert md_files, f"expected at least one .md prompt file under {PROMPTS_DIR}"
    uncovered = [f for f in md_files if not any(_matches(f, entry) for entry in surface)]
    assert not uncovered, (
        "the following .md files under src/athenaeum/prompts/ are not "
        f"covered by any entry in {SURFACE_FILE}: {uncovered} — add "
        "`src/athenaeum/prompts/` (or the specific file) to that file."
    )
