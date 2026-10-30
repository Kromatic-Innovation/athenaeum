# SPDX-License-Identifier: Apache-2.0
"""Rot check for `.github/llm-surface.txt` (issues athenaeum#1731, athenaeum#2224).

The surface list is the ONE file `.github/workflows/eval-receipt-check.yml`
and this test both read. This test does not assert the list's exact contents
(it carries hand-curated entries no test derives — see the file's own header)
— it asserts a SUBSET: every symbol the codebase itself marks as
reasoning-facing must appear in the list, so the list cannot rot silently as
new reasoning-facing code is added.

Five independently-derived "must appear" sets, D1 to D5 of
`docs/design/eval-trigger-surface.md` §5.2. Every one of them is DERIVED from
the tree or from the prompt registry — never transcribed — and every one is a
membership check, never a count or an equality, so hand-curated entries stay
legal:

1. **D1 call sites.** Every enclosing top-level function or method of a call
   whose function is an attribute chain ending in ``.messages.create`` or
   ``.messages.batches.create``. This is what the pre-athenaeum#2224 oracle
   (the substring ``LLMBackend``) missed: `paste_cleanup.py` and
   `memory_class_backfill.py` call `client.messages.create` without ever
   naming `LLMBackend`, so neither was on the surface.
2. **D2 prompts.** Every `prompt_registry.PROMPT_META` row, as
   ``path::CONSTANT``. Replaces the old whole-module requirement.
3. **D3 routing seam.** `config.py::resolve_model`, every module-level
   ``DEFAULT_*_MODEL``, every ``_get_*model`` function,
   `provider.py::resolve_max_tokens`, `provider.py::resolve_thinking` and
   `prompt_registry.py::_META_ROWS` — found by AST name pattern.
4. **D4 LLMBackend importers.** Each importing module must carry at least one
   entry of ANY form (loosened from a whole-file requirement, since a module
   covered by symbol entries is covered).
5. **D5 no stale pointers.** Every ``path::symbol`` entry must resolve in the
   tree, so a rename or a deletion cannot leave a designation pointing at
   nothing (which `fire` would read as a permanent change).

Every derivation is a pure function of a source root (or of a row list), so
each has a counter-example test that runs it over a synthetic tree and
asserts it FAILS — a subset check that cannot fail is not a check.

**Honest limit, by design (§5.2).** Designation is not transitive, and D1 sees
only the two literal SDK call shapes. A function that takes an `LLMBackend`
and calls through the protocol is NOT derived by D1; those are hand-curated,
and the surface file's header records the query that found them so the
curation can be re-run rather than remembered.

Offline, zero network, no `eval`/`embedding` marker — runs in the default
suite.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from athenaeum import prompt_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
SURFACE_FILE = REPO_ROOT / ".github" / "llm-surface.txt"
SRC_ATHENAEUM = REPO_ROOT / "src" / "athenaeum"
PROMPTS_DIR = SRC_ATHENAEUM / "prompts"
SRC_PREFIX = "src/athenaeum"

_SCRIPT_PATH = REPO_ROOT / "scripts" / "check_llm_surface_receipt.py"
_spec = importlib.util.spec_from_file_location("check_llm_surface_receipt", _SCRIPT_PATH)
assert _spec and _spec.loader
_check_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _check_script
_spec.loader.exec_module(_check_script)
_matches = _check_script._matches
parse_surface_entry = _check_script.parse_surface_entry
_resolve_symbol = _check_script._resolve_symbol

#: The two SDK call shapes that mean "a model is being asked something".
_MODEL_CALL_SUFFIXES = (".messages.create", ".messages.batches.create")

#: D3's name patterns (design §5.2). `_FIXED_SEAM` is per-file and required
#: only when the symbol actually exists, so removing a seam function removes
#: its requirement instead of wedging CI.
_DEFAULT_MODEL_RE = re.compile(r"DEFAULT_.*_MODEL")
_GET_MODEL_RE = re.compile(r"_get_.*model")
_FIXED_SEAM: tuple[tuple[str, str], ...] = (
    ("config.py", "resolve_model"),
    ("provider.py", "resolve_max_tokens"),
    ("provider.py", "resolve_thinking"),
    ("prompt_registry.py", "_META_ROWS"),
)


def _load_surface(path: Path = SURFACE_FILE) -> set[str]:
    """Surface entries, verbatim: `path`, `dir/` or `path::symbol`.

    The `path::symbol` form arrived with athenaeum#2223. This reader accepts
    it the same way the gate's `load_surface` does — by keeping the entry
    whole and leaving interpretation to the ONE shared parser,
    `check_llm_surface_receipt.parse_surface_entry` (imported above, never
    re-implemented here: the surface file has two readers and exactly one
    grammar).
    """
    lines: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.add(line)
    return lines


def _surface_paths(surface: set[str]) -> set[str]:
    """The FILE each entry designates, dropping any `::symbol` part."""
    return {parse_surface_entry(entry).path for entry in surface}


def _covers(surface: set[str], entry: str) -> bool:
    """Is `entry` (a `path::symbol`) designated, by itself or by its file?

    A WHOLE-FILE or `dir/` entry covers every symbol in that file — which is
    what makes D4 a floor rather than a target, and what keeps a rollback to a
    whole-file list legal.

    A SIBLING SYMBOL entry in the same file covers nothing. `_matches` is
    path-level (it drops any `::symbol`), so matching every candidate with it
    would let `tiers.py::tier3_merge` vouch for a brand-new call site
    elsewhere in `tiers.py` — and since the migrated list gives almost every
    `src/athenaeum` module at least one symbol entry, D1/D2/D3 would have
    become vacuous for the whole tree. Caught by Seer review on PR
    athenaeum#2245; pinned by
    `test_a_sibling_symbol_entry_does_not_cover_a_new_symbol`.
    """
    if entry in surface:
        return True
    path = parse_surface_entry(entry).path
    return any(
        _matches(path, candidate)
        for candidate in surface
        if parse_surface_entry(candidate).symbol is None
    )


def _missing(surface: set[str], required: Iterable[str]) -> list[str]:
    return sorted(entry for entry in required if not _covers(surface, entry))


# --- D1: model call sites ---------------------------------------------------


def _attribute_chain(node: ast.expr) -> str:
    """`a.b.c` for an attribute chain, with a non-name root rendered opaquely."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    parts.append(current.id if isinstance(current, ast.Name) else "?")
    return ".".join(reversed(parts))


def _enclosing_designations(module: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every top-level function, and every method, paired with its designation name.

    These are exactly the units a surface entry can name: `func` and
    `Class.method`. A nested function is attributed to the top-level unit that
    contains it, since that is the smallest thing the grammar can designate.
    """
    units: list[tuple[str, ast.AST]] = []
    for stmt in module.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            units.append((stmt.name, stmt))
        elif isinstance(stmt, ast.ClassDef):
            for sub in stmt.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    units.append((f"{stmt.name}.{sub.name}", sub))
    return units


def _makes_a_model_call(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call) and _attribute_chain(sub.func).endswith(_MODEL_CALL_SUFFIXES)
        for sub in ast.walk(node)
    )


def model_call_sites(root: Path, rel_prefix: str = SRC_PREFIX) -> set[str]:
    """D1, derived: `path::symbol` for every unit that calls the SDK directly."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - ordinary CI fails on this first
            continue
        rel = f"{rel_prefix}/{path.relative_to(root).as_posix()}"
        for name, node in _enclosing_designations(module):
            if _makes_a_model_call(node):
                found.add(f"{rel}::{name}")
    return found


# --- D2: prompt constants ---------------------------------------------------


def prompt_constant_entries(metas: Iterable[object]) -> set[str]:
    """D2, derived from `PROMPT_META` itself: `path::CONSTANT` per registered row."""
    entries: set[str] = set()
    for meta in metas:
        module = getattr(meta, "module")
        constant = getattr(meta, "constant")
        entries.add("src/" + module.replace(".", "/") + f".py::{constant}")
    return entries


# --- D3: the routing seam ---------------------------------------------------


def routing_seam_entries(root: Path, rel_prefix: str = SRC_PREFIX) -> set[str]:
    """D3, derived by AST name pattern — never a transcribed list of symbols."""
    entries: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        rel = f"{rel_prefix}/{path.relative_to(root).as_posix()}"
        basename = path.name
        for stmt in module.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and _DEFAULT_MODEL_RE.fullmatch(target.id):
                        entries.add(f"{rel}::{target.id}")
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if _DEFAULT_MODEL_RE.fullmatch(stmt.target.id):
                    entries.add(f"{rel}::{stmt.target.id}")
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                if _GET_MODEL_RE.fullmatch(stmt.name):
                    entries.add(f"{rel}::{stmt.name}")
        for seam_file, seam_symbol in _FIXED_SEAM:
            if basename == seam_file and _resolve_symbol(module, seam_symbol) is not None:
                entries.add(f"{rel}::{seam_symbol}")
    return entries


# --- D4: LLMBackend importers ----------------------------------------------


def llm_backend_importers(root: Path, rel_prefix: str = SRC_PREFIX) -> set[str]:
    """`grep -l LLMBackend <root>/*.py`, expressed as repo-relative paths."""
    hits = set()
    for path in sorted(root.glob("*.py")):
        if "LLMBackend" in path.read_text(encoding="utf-8"):
            hits.add(f"{rel_prefix}/{path.name}")
    return hits


# --- D5: no stale pointers -------------------------------------------------


def unresolved_symbol_entries(surface: set[str], repo_root: Path = REPO_ROOT) -> list[str]:
    """D5: `path::symbol` entries that do not resolve in the tree.

    The tree IS head here — the rot check runs from the checkout, which is
    what `fire` will read at the head commit.
    """
    stale: list[str] = []
    for raw in sorted(surface):
        entry = parse_surface_entry(raw)
        if entry.symbol is None:
            continue
        target = repo_root / entry.path
        if not target.is_file():
            stale.append(raw)
            continue
        try:
            module = ast.parse(target.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            stale.append(raw)
            continue
        if _resolve_symbol(module, entry.symbol) is None:
            stale.append(raw)
    return stale


# --- the live checks -------------------------------------------------------


def test_surface_file_exists_and_is_non_empty() -> None:
    surface = _load_surface()
    assert surface, f"{SURFACE_FILE} must list at least one path"


def test_d1_every_model_call_site_is_designated() -> None:
    surface = _load_surface()
    missing = _missing(surface, model_call_sites(SRC_ATHENAEUM))
    assert not missing, (
        "these functions call the Anthropic SDK directly but are not on the "
        f"reasoning surface: {missing} — add them to {SURFACE_FILE} (as "
        "`path::symbol`), or list their file whole."
    )


def test_d2_every_prompt_registry_row_is_designated_by_constant() -> None:
    surface = _load_surface()
    missing = _missing(surface, prompt_constant_entries(prompt_registry.PROMPT_META.values()))
    assert not missing, (
        "these prompt constants are registered in prompt_registry.py's "
        f"PROMPT_META but are not on the reasoning surface: {missing} — add "
        f"them to {SURFACE_FILE}."
    )


def test_d3_the_routing_seam_is_designated() -> None:
    surface = _load_surface()
    missing = _missing(surface, routing_seam_entries(SRC_ATHENAEUM))
    assert not missing, (
        "these model-routing symbols are not on the reasoning surface: "
        f"{missing} — they decide WHICH model is asked, or with what knobs, "
        f"so add them to {SURFACE_FILE}."
    )


def test_d4_every_llm_backend_importer_has_at_least_one_entry() -> None:
    surface = _load_surface()
    importers = llm_backend_importers(SRC_ATHENAEUM)
    missing = sorted(importers - _surface_paths(surface))
    assert not missing, (
        "the following src/athenaeum/*.py files import LLMBackend but have no "
        f"entry at all in {SURFACE_FILE}: {missing} — add at least one entry "
        "(a symbol entry is enough; a whole-file entry is also legal)."
    )


def test_d5_every_symbol_entry_resolves() -> None:
    stale = unresolved_symbol_entries(_load_surface())
    assert not stale, (
        f"these entries in {SURFACE_FILE} name something that no longer "
        f"exists: {stale} — a designation pointing at nothing fingerprints as "
        "permanently changed, so fix or remove it in the PR that renamed it."
    )


def test_prompt_md_files_are_covered_by_the_surface() -> None:
    surface = _load_surface()
    md_files = sorted(f"src/athenaeum/prompts/{p.name}" for p in PROMPTS_DIR.glob("*.md"))
    assert md_files, f"expected at least one .md prompt file under {PROMPTS_DIR}"
    uncovered = [f for f in md_files if not any(_matches(f, entry) for entry in surface)]
    assert not uncovered, (
        "the following .md files under src/athenaeum/prompts/ are not "
        f"covered by any entry in {SURFACE_FILE}: {uncovered} — add "
        "`src/athenaeum/prompts/` (or the specific file) to that file."
    )


def test_the_two_call_sites_the_old_oracle_missed_are_now_on_the_surface() -> None:
    """athenaeum#2224's motivating gap, pinned so it cannot silently reopen.

    Neither module mentions `LLMBackend`, so the pre-athenaeum#2224 oracle
    never required them even though both call `client.messages.create`.
    """
    surface = _load_surface()
    for entry in (
        "src/athenaeum/paste_cleanup.py::propose_page",
        "src/athenaeum/paste_cleanup.py::verify_page",
        "src/athenaeum/memory_class_backfill.py::classify_residual",
    ):
        assert _covers(surface, entry), entry
    assert "LLMBackend" not in (SRC_ATHENAEUM / "paste_cleanup.py").read_text(encoding="utf-8")


def test_librarian_orchestration_is_designated_not_covered_by_one_token_entry() -> None:
    """athenaeum#2224: D4 is a floor, and `librarian.py` is where that matters.

    `process_one` decides which tier calls are made and when a model call is
    skipped (the R5 class, exercised by the whole-chain Attachment and
    Person-hint eval layers). A single nominal entry for the module would
    satisfy D4 while designating none of that, so it is named explicitly —
    and `librarian.py` must NOT be listed whole, or the 12k-line module is
    back to firing the gate on any edit.
    """
    surface = _load_surface()
    assert "src/athenaeum/librarian.py" not in surface
    assert "src/athenaeum/librarian.py::process_one" in surface


def test_the_large_modules_are_not_listed_whole() -> None:
    """athenaeum#2224: the migration's whole point.

    These are the modules whose whole-file entries made 68 of 141 PRs in 30
    days trip the gate. If one comes back as a whole-file entry, the gate is
    back to path granularity for it.
    """
    surface = _load_surface()
    for module in ("config.py", "librarian.py", "models.py", "tiers.py", "search.py"):
        assert f"src/athenaeum/{module}" not in surface, module


def test_generated_prompts_doc_is_not_on_the_surface() -> None:
    """`docs/design/prompts.md` is generated AND byte-pinned by test_prompt_goldens.py.

    Designating it made the gate fire on a file no human edits and no eval
    reads.
    """
    assert "docs/design/prompts.md" not in _load_surface()


def entries_without_a_reason_comment(path: Path = SURFACE_FILE) -> list[str]:
    """Entries with no `#` comment line anywhere above them in their block.

    The surface file's convention (athenaeum#2224): a derived block carries
    one comment naming its derivation; a hand-curated entry carries its own
    one-line reason. An entry with no comment above it is an entry nobody can
    audit, so it is treated as undocumented.
    """
    seen_comment = False
    orphans: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            seen_comment = True
            continue
        if not seen_comment:
            orphans.append(line)
    return orphans


def test_every_entry_carries_a_reason_comment() -> None:
    orphans = entries_without_a_reason_comment()
    assert not orphans, (
        f"these entries in {SURFACE_FILE} have no `#` comment above them "
        f"saying why they are designated: {orphans}"
    )


# --- counter-examples: a subset check that cannot fail is not a check ------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_d1_counter_example_an_undesignated_call_site_fails(tmp_path: Path) -> None:
    _write(
        tmp_path / "sneaky.py",
        "def asks_a_model(client):\n    return client.messages.create(model='m')\n",
    )
    required = model_call_sites(tmp_path)
    assert required == {"src/athenaeum/sneaky.py::asks_a_model"}
    assert _missing({"src/athenaeum/other.py"}, required) == [
        "src/athenaeum/sneaky.py::asks_a_model"
    ]
    # ...and designating it, or its file, clears the check.
    assert _missing({"src/athenaeum/sneaky.py::asks_a_model"}, required) == []
    assert _missing({"src/athenaeum/sneaky.py"}, required) == []


def test_a_sibling_symbol_entry_does_not_cover_a_new_symbol() -> None:
    """The rot check must not be vouched for by a DIFFERENT symbol in the same file.

    Seer review on PR athenaeum#2245. `_matches` is path-level, so before the
    fix `tiers.py::tier3_merge` made any new `tiers.py` symbol read as
    designated — which would have made D1, D2 and D3 vacuous across almost the
    whole migrated list, since nearly every module now carries at least one
    symbol entry. This is the single assertion that distinguishes a live
    subset check from a decorative one.
    """
    sibling_only = {"src/athenaeum/tiers.py::tier3_merge"}
    assert not _covers(sibling_only, "src/athenaeum/tiers.py::brand_new_call_site")
    assert _missing(sibling_only, ["src/athenaeum/tiers.py::brand_new_call_site"]) == [
        "src/athenaeum/tiers.py::brand_new_call_site"
    ]
    # Its own entry covers it, and so does a WHOLE-FILE entry — both still true.
    assert _covers(sibling_only, "src/athenaeum/tiers.py::tier3_merge")
    assert _covers({"src/athenaeum/tiers.py"}, "src/athenaeum/tiers.py::brand_new_call_site")
    # ...and a `dir/` prefix entry still covers everything under it.
    assert _covers({"src/athenaeum/prompts/"}, "src/athenaeum/prompts/one.md")


def test_d1_finds_a_batches_call_and_a_method(tmp_path: Path) -> None:
    _write(
        tmp_path / "mod.py",
        "class Boundary:\n"
        "    def send(self, client):\n"
        "        return client.messages.batches.create(requests=[])\n"
        "\n"
        "def untouched():\n"
        "    return 1\n",
    )
    assert model_call_sites(tmp_path) == {"src/athenaeum/mod.py::Boundary.send"}


def test_d1_attributes_a_nested_helper_to_its_top_level_unit(tmp_path: Path) -> None:
    """The grammar cannot name a closure, so the enclosing unit is the designation."""
    _write(
        tmp_path / "mod.py",
        "def outer(client):\n"
        "    def inner():\n"
        "        return client.messages.create(model='m')\n"
        "    return inner()\n",
    )
    assert model_call_sites(tmp_path) == {"src/athenaeum/mod.py::outer"}


def test_d2_counter_example_an_unlisted_registry_row_fails() -> None:
    class _Meta:
        module = "athenaeum.newthing"
        constant = "NEW_SYSTEM"

    required = prompt_constant_entries([_Meta()])
    assert required == {"src/athenaeum/newthing.py::NEW_SYSTEM"}
    assert _missing(set(), required) == ["src/athenaeum/newthing.py::NEW_SYSTEM"]
    assert _missing({"src/athenaeum/newthing.py::NEW_SYSTEM"}, required) == []


def test_d3_counter_example_an_unlisted_routing_symbol_fails(tmp_path: Path) -> None:
    _write(
        tmp_path / "newmod.py",
        "DEFAULT_SUMMARY_MODEL = 'claude-haiku-4-5'\n\n\ndef _get_summary_model():\n    return 1\n",
    )
    _write(tmp_path / "config.py", "def resolve_model(config):\n    return 'm'\n")
    required = routing_seam_entries(tmp_path)
    assert required == {
        "src/athenaeum/newmod.py::DEFAULT_SUMMARY_MODEL",
        "src/athenaeum/newmod.py::_get_summary_model",
        "src/athenaeum/config.py::resolve_model",
    }
    assert _missing(set(), required) == sorted(required)


def test_d3_does_not_require_a_seam_symbol_that_does_not_exist(tmp_path: Path) -> None:
    """Removing a seam function removes its requirement, rather than wedging CI."""
    _write(tmp_path / "config.py", "def something_else():\n    return 1\n")
    assert routing_seam_entries(tmp_path) == set()


def test_d4_counter_example_an_importer_with_no_entry_fails(tmp_path: Path) -> None:
    _write(tmp_path / "newmod.py", "from athenaeum.provider import LLMBackend\n")
    importers = llm_backend_importers(tmp_path)
    assert importers == {"src/athenaeum/newmod.py"}
    assert sorted(importers - _surface_paths(set())) == ["src/athenaeum/newmod.py"]
    # A SYMBOL entry is enough — D4 is a floor, not a whole-file requirement.
    assert not importers - _surface_paths({"src/athenaeum/newmod.py::only_one_function"})


def test_d5_counter_example_an_entry_naming_a_deleted_function_fails(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "athenaeum" / "mod.py", "def kept():\n    return 1\n")
    surface = {
        "src/athenaeum/mod.py::kept",
        "src/athenaeum/mod.py::deleted_last_week",
        "src/athenaeum/gone.py::anything",
        "src/athenaeum/mod.py",
    }
    assert unresolved_symbol_entries(surface, tmp_path) == [
        "src/athenaeum/gone.py::anything",
        "src/athenaeum/mod.py::deleted_last_week",
    ]


def test_reason_comment_counter_example_an_undocumented_entry_is_reported(
    tmp_path: Path,
) -> None:
    surface_file = tmp_path / "llm-surface.txt"
    surface_file.write_text("src/a.py\n# why b\nsrc/b.py\n", encoding="utf-8")
    assert entries_without_a_reason_comment(surface_file) == ["src/a.py"]
    surface_file.write_text("# why\nsrc/a.py\n\nsrc/b.py\n", encoding="utf-8")
    assert entries_without_a_reason_comment(surface_file) == []


def test_both_readers_accept_the_symbol_grammar(tmp_path: Path) -> None:
    """athenaeum#2223 AC1: the gate's reader and this one parse the same grammar."""
    surface_file = tmp_path / "llm-surface.txt"
    surface_file.write_text(
        "# a comment\n"
        "\n"
        "src/athenaeum/prompts/\n"
        "src/athenaeum/tiers.py\n"
        "src/athenaeum/tiers.py::parse_merge_ops_response\n"
        "src/athenaeum/search.py::VectorBackend.build_index\n"
        "src/athenaeum/prompt_registry.py::_META_ROWS\n",
        encoding="utf-8",
    )

    from_rot_check = _load_surface(surface_file)
    from_gate = set(_check_script.load_surface(surface_file))
    assert from_rot_check == from_gate

    parsed = {(e.path, e.symbol) for e in map(parse_surface_entry, from_gate)}
    assert parsed == {
        ("src/athenaeum/prompts/", None),
        ("src/athenaeum/tiers.py", None),
        ("src/athenaeum/tiers.py", "parse_merge_ops_response"),
        ("src/athenaeum/search.py", "VectorBackend.build_index"),
        ("src/athenaeum/prompt_registry.py", "_META_ROWS"),
    }
    # A whole-file and a symbol entry for the SAME path are both legal, and
    # collapse to one path for the subset checks above.
    assert _surface_paths(from_gate) == {
        "src/athenaeum/prompts/",
        "src/athenaeum/tiers.py",
        "src/athenaeum/search.py",
        "src/athenaeum/prompt_registry.py",
    }


def test_every_derivation_finds_something_in_the_real_tree() -> None:
    """A positive control: an empty derivation would make its check vacuous."""
    assert len(model_call_sites(SRC_ATHENAEUM)) >= 20
    assert len(prompt_constant_entries(prompt_registry.PROMPT_META.values())) >= 20
    assert len(routing_seam_entries(SRC_ATHENAEUM)) >= 20
    assert len(llm_backend_importers(SRC_ATHENAEUM)) >= 15


@pytest.mark.parametrize(
    "entry",
    [
        "src/athenaeum/config.py::resolve_model",
        "src/athenaeum/provider.py::resolve_max_tokens",
        "src/athenaeum/provider.py::resolve_thinking",
        "src/athenaeum/prompt_registry.py::_META_ROWS",
    ],
)
def test_the_named_routing_seam_symbols_are_derived_from_the_real_tree(entry: str) -> None:
    """The four symbols athenaeum#2224 D3 names explicitly, found by the derivation."""
    assert entry in routing_seam_entries(SRC_ATHENAEUM)


# --- the committed replay measurement (athenaeum#2224) ----------------------

MEASUREMENT = REPO_ROOT / "docs" / "measurements" / "eval-fire-replay-2026-10-30.md"


def _measurement_row(pr_number: int) -> str:
    """The committed replay's row for one PR, or '' when it has none."""
    for line in MEASUREMENT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"| athenaeum#{pr_number} |"):
            return line
    return ""


def test_the_replay_measurement_is_committed() -> None:
    text = MEASUREMENT.read_text(encoding="utf-8")
    assert "PRs that tripped the old gate" in text
    assert "scripts/replay_fire_over_merged_prs.py" in text


@pytest.mark.parametrize(
    ("pr_number", "symbol"),
    [
        # athenaeum#2224 names this one as a MUST: prompt `.md` edits have to fire.
        (2046, "src/athenaeum/prompts/"),
        # These two carried free-text `not needed` lines the design read as
        # reasoning-relevant. The replay must RECORD an outcome for each; that
        # both fire is the design's §2 claim, measured.
        (2111, "src/athenaeum/tiers.py::tier3_merge"),
        (2036, "src/athenaeum/search.py::VectorBackend.build_index"),
    ],
)
def test_the_replay_records_the_three_named_prs_as_firing(pr_number: int, symbol: str) -> None:
    row = _measurement_row(pr_number)
    assert row, f"the replay measurement has no row for athenaeum#{pr_number}"
    assert "**yes**" in row, row
    assert symbol in row, row
