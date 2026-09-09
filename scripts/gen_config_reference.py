#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate ``docs/reference/configuration.md`` from ``athenaeum.config``'s
``resolve_*`` functions (issue athenaeum#1424).

Why generate rather than hand-maintain: at the time this script was written,
``docs/reference/configuration.md`` was 3,320 hand-written lines against 128
``resolve_*`` functions in ``src/athenaeum/config.py``, with nothing noticing
when a new resolver landed undocumented (see the issue for the two measured
consequences — ``librarian.corrections.fields`` getting one mention in 3,320
lines, and ``preserved_log_adapter`` getting zero). This script makes the doc
a deterministic function of the code so a drift can be CI-gated
(``tests/test_generated_docs_parity.py``) exactly as
``tests/test_exit_codes_doc_parity.py`` / ``tests/test_env_docs.py`` /
``tests/test_measurement_docs.py`` already gate narrower surfaces.

Design (see :func:`generate` for the pipeline):

1. **Enumerate** every ``resolve_*`` function actually DEFINED in
   ``athenaeum.config`` (not re-exported), via ``inspect.getmembers`` — the
   same non-hand-maintained enumeration
   ``tests/test_config_resolver_parity_generic.py`` uses.
2. **YAML path** — resolved via a three-step fallback, each one only
   consulted if the previous found nothing:

   a. An exact ``` ``dotted.key`` ``` backtick span in the function's own
      docstring (covers the large majority: these docstrings already state
      the key precisely, because ``config.py``'s own module docstring makes
      "resolver function + docs entry are two halves of one change" a
      FACTORING RULE).
   b. A looser dotted-identifier match anywhere in the docstring, accepted
      only when its first segment is a section name already seen via (a)
      elsewhere in the module (:data:`_KNOWN_SECTIONS`) — this catches a key
      mentioned inline without exact backtick wrapping (e.g. followed by
      ``: true`` or ``.<name>``) without accidentally matching an unrelated
      dotted reference like a module path (``athenaeum.foo.bar``) or a doc
      path, since those don't share a section name with any real yaml key.
   c. A static-analysis fallback: every string-literal argument passed to a
      ``.get(...)``-shaped call (or a private ``_resolve*`` helper call) IN
      THE RESOLVER'S OWN BODY, in source order, filtered to
      yaml-key-identifier shape. This is intentionally narrower than
      ``test_config_resolver_parity_generic.py``'s own literal walk (which
      also follows one level into a delegate's body and therefore reorders
      section-before-key ambiguously — fine for THAT module's job of proving
      "some nesting changes the result", wrong for THIS module's job of
      printing one correct nesting) — for every resolver this fallback
      actually fires on, the section+key are both literal arguments at the
      resolver's OWN call site, in the right order.

3. **Env var** — a ``ATHENAEUM_[A-Z0-9_]+`` literal found in the resolver's
   own source, falling back one level into a called private ``_resolve*``
   helper's source if the resolver's own body has none.
4. **Default** — actually INVOKED at generation time (config=None or an
   empty dict, every ``ATHENAEUM_*`` env var cleared first) for every
   resolver whose signature and return type make that safe and
   deterministic; :data:`_CURATED` hand-supplies the default/precedence text
   for the handful that don't (a generic per-knob/per-family helper with no
   single "the" default, or a ``Path`` return that would otherwise bake this
   MACHINE's home directory into a committed file). See :data:`_CURATED`'s
   own comments for why each entry is there.
5. **Precedence chain** — derived mechanically from which of (env var, yaml
   path) resolution actually found, e.g. "environment variable >
   `athenaeum.yaml` > code default" vs. "`athenaeum.yaml` > code default".

Determinism (issue athenaeum#1424's staleness-gate requirement): no
timestamps, no absolute paths, no environment-dependent values, and every
container type that has no stable native ordering (set/frozenset, dict) is
sorted before formatting. Every ``ATHENAEUM_*`` env var is cleared for the
duration of :func:`generate` so an operator's ambient environment cannot
change the generated defaults.

Reader-facing hygiene (issue athenaeum#1424 AC6): this doc is added to
``tests/test_docs_structure.py``'s ``READER_FACING`` tuple, which bans issue
references ENTIRELY — :func:`_strip_issue_refs` removes every
``athenaeum#<N>`` token (and the shorthand ``#<N>`` continuation some
docstrings use, e.g. ``athenaeum#519/#528``) from docstring prose pulled
into the output.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
CONFIG_PY = SRC_DIR / "athenaeum" / "config.py"
OUTPUT = REPO_ROOT / "docs" / "reference" / "configuration.md"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_ENV_VAR_RE = re.compile(r"ATHENAEUM_[A-Z0-9_]+")
_YAML_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DOTTED_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_EXACT_BACKTICK_DOTTED = re.compile(r"``([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)``")

# Sphinx cross-reference roles -> plain markdown code spans.
_SPHINX_ROLE_RE = re.compile(r":(?:func|class|data|meth|mod|attr):`~?([^`]+)`")

# Issue-reference stripping (reader-facing hygiene, AC6). Order matters: strip
# the longest/most-specific shapes first so a shorthand continuation like
# ``athenaeum#519/#528`` doesn't leave a dangling ``/#528``.
_ISSUE_PAREN_RE = re.compile(
    r"\(\s*(?:see\s+)?(?:issues?\s+)?athenaeum#\d+(?:\s*[-,/]\s*#?\d+)*\s*\)", re.IGNORECASE
)
_ISSUE_INLINE_RE = re.compile(
    r"\bissues?\s+athenaeum#\d+(?:\s*[-,/]\s*#?\d+)*\b", re.IGNORECASE
)
_ISSUE_BARE_RE = re.compile(r"athenaeum#\d+(?:\s*[-,/]\s*(?:athenaeum)?#\d+)*")
_ISSUE_SHORTHAND_RE = re.compile(r"(?<![A-Za-z0-9_])#\d+")
# A "; see <refs> for X" / ", see <refs> for X" clause where <refs> has
# already been removed by the bare-ref pattern above, leaving "see" directly
# adjacent to "for" or the closing paren — e.g. "(issue athenaeum#1022; see
# athenaeum#1023-athenaeum#1025 for the slices that do)" -> "(for the slices
# that do)".
_DANGLING_SEE_RE = re.compile(r"[;,]?\s*\bsee\s+(?=for\b|\))", re.IGNORECASE)
# A cross-repo citation idiom this codebase uses, e.g. "see `athenaeum-adapters#151`"
# — not caught by `_ISSUE_BARE_RE` (different repo name) or `_ISSUE_SHORTHAND_RE`
# (the `#` is preceded by an alnum char, by design, to avoid eating unrelated
# `word#N` tokens). Strip the whole "; see `<ref>`" / ", see `<ref>`" clause so
# no dangling "see" / stray punctuation is left behind.
_SEE_REF_CLAUSE_RE = re.compile(r"[;,]\s*see\s+`[^`]*#\d+[^`]*`", re.IGNORECASE)
# Two common inline idioms ("Authority in athenaeum#715 is..." / "so athenaeum#1140
# provides...") where the bare token removal below would otherwise leave a
# dangling preposition ("in is...", "so provides..."). Handled BEFORE the
# generic strip so the replacement reads as a real word, not a collapsed gap.
_IN_ISSUE_RE = re.compile(r"\bin\s+athenaeum#\d+\b")
_SO_ISSUE_RE = re.compile(r"\bso\s+athenaeum#\d+\b")
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")
# After an inline "issue athenaeum#NNN" is removed, a parenthetical can be
# left with leading punctuation/whitespace it never had on its own, e.g.
# "(issue athenaeum#231: no seed ...)" -> "(: no seed ...)", or
# "(issue athenaeum#315 seam)" -> "( seam)". Trim both.
_PAREN_LEADING_PUNCT_RE = re.compile(r"\(\s*[:,]\s*")
_PAREN_TRAILING_PUNCT_RE = re.compile(r"[,;]\s*\)")
_PAREN_INNER_OPEN_RE = re.compile(r"\(\s+")
_PAREN_INNER_CLOSE_RE = re.compile(r"\s+\)")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([.,;:)])")

# A placeholder knowledge_root used only to invoke a `(knowledge_root, config)`
# resolver deterministically. Never expanded/resolved, never written anywhere
# in generated output (those resolvers are curated below, precisely because
# their return value embeds this path). Deliberately NOT a real absolute path
# like `/home/x/knowledge` so this script cannot leak a host-specific value.
_PLACEHOLDER_KNOWLEDGE_ROOT = Path("<knowledge_root>")


def _strip_issue_refs_prose(text: str) -> str:
    """Remove every issue reference from a PROSE chunk (never a fenced code
    block — see :func:`_strip_issue_refs`). Operates on the whole chunk, with
    ``\\s`` in the reference patterns matching a newline too, because the
    source wraps prose at arbitrary column widths and a citation like
    "...fired on any of them; see\\n`repo#151`)." can have its "see" and its
    backtick ref split across a line break — a per-line regex would never
    see the two halves together, and removing the match's own newline is
    exactly right here (it re-joins the sentence instead of leaving a
    dangling half-line).
    """
    text = _SEE_REF_CLAUSE_RE.sub("", text)
    text = _IN_ISSUE_RE.sub("here", text)
    text = _SO_ISSUE_RE.sub("so this", text)
    text = _ISSUE_PAREN_RE.sub("", text)
    text = _ISSUE_INLINE_RE.sub("", text)
    text = _ISSUE_BARE_RE.sub("", text)
    text = _ISSUE_SHORTHAND_RE.sub("", text)
    text = _DANGLING_SEE_RE.sub("", text)
    text = _PAREN_LEADING_PUNCT_RE.sub("(", text)
    text = _PAREN_TRAILING_PUNCT_RE.sub(")", text)
    text = _PAREN_INNER_OPEN_RE.sub("(", text)
    text = _PAREN_INNER_CLOSE_RE.sub(")", text)
    text = _EMPTY_PARENS_RE.sub("", text)
    # Collapse horizontal whitespace WITHIN each line only (never across the
    # newline itself, which would merge two originally-separate lines).
    text = "\n".join(_MULTI_SPACE_RE.sub(" ", line) for line in text.split("\n"))
    text = "\n".join(_SPACE_BEFORE_PUNCT_RE.sub(r"\1", line) for line in text.split("\n"))
    # A line that degenerated to nothing but stray punctuation after
    # stripping (a paragraph that was ONLY an issue citation) is dropped.
    # This can turn two real paragraph breaks into three blank lines in a
    # row; collapsing 2+ blank lines to exactly one normalizes that away
    # without needing to track which blank lines were "original".
    lines = [
        line.rstrip()
        for line in text.split("\n")
        if line.strip() not in ("()", ".", ",")
    ]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


_FENCE_SPLIT_RE = re.compile(r"(```[^\n]*\n.*?\n```)", re.DOTALL)


def _strip_issue_refs(text: str) -> str:
    """Remove every issue reference from *text* (AC6 reader-facing hygiene),
    leaving any fenced code block byte-for-byte untouched (a YAML example's
    leading indentation is meaningful and must not go through the prose
    whitespace-collapsing cleanup, and could coincidentally contain a `#`
    that isn't an issue reference at all)."""
    parts = _FENCE_SPLIT_RE.split(text)
    out: list[str] = []
    for part in parts:
        if part.startswith("```"):
            out.append(part)
        else:
            out.append(_strip_issue_refs_prose(part))
    return "".join(out)


def _sphinx_to_markdown(text: str) -> str:
    text = _SPHINX_ROLE_RE.sub(lambda m: f"`{m.group(1)}`", text)
    return text


def _render_literal_blocks(doc: str) -> str:
    """Convert RST-ish literal blocks (``.. code-block:: yaml`` or a line
    ending in ``::``) into fenced markdown code blocks.

    ``ast.get_docstring`` already dedents the docstring as a whole (via
    ``inspect.cleandoc``), so an example block nested inside the prose still
    carries MORE leading whitespace than the surrounding paragraph text —
    that relative indent is what this uses to find the block's extent.
    """
    lines = doc.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        is_codeblock_directive = stripped.startswith(".. code-block::")
        is_literal_marker = stripped.endswith("::") and stripped not in ("::",)
        is_bare_literal_marker = stripped == "::"
        if is_codeblock_directive or is_literal_marker or is_bare_literal_marker:
            lang = ""
            if is_codeblock_directive:
                lang = stripped.split("::", 1)[1].strip()
            elif is_literal_marker:
                out.append(line[: len(line) - len(stripped)] + stripped[:-1])
            else:
                pass  # bare `::` line: drop it, the block follows
            # Collect the indented block that follows (skipping one blank line).
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            block_lines: list[str] = []
            while j < len(lines) and (
                lines[j].strip() == "" or lines[j].startswith((" ", "\t"))
            ):
                next_is_dedented = j + 1 < len(lines) and not lines[j + 1].startswith(
                    (" ", "\t")
                )
                if lines[j].strip() == "" and next_is_dedented:
                    break
                block_lines.append(lines[j])
                j += 1
            if block_lines:
                # Dedent by the minimum indent among non-blank lines.
                indents = [len(bl) - len(bl.lstrip()) for bl in block_lines if bl.strip()]
                min_indent = min(indents) if indents else 0
                dedented = [bl[min_indent:] if bl.strip() else "" for bl in block_lines]
                out.append(f"```{lang}".rstrip())
                out.extend(dedented)
                out.append("```")
                i = j
                continue
            else:
                # No block actually followed (false trigger) — keep line as-is.
                if not is_bare_literal_marker and not is_literal_marker:
                    out[-1] = line if not out else out[-1]
                i += 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


#: A line that was originally "Issue athenaeum#232. Mirrors ..." (a standalone
#: bare-issue sentence immediately followed by the next sentence, no space
#: after the removed reference's own trailing period) degrades to ". Mirrors
#: ..." once "Issue athenaeum#232" is stripped — the trailing period belonged
#: to the removed sentence, not to what follows. Strip that leftover leading
#: ". " at the start of a line (never mid-sentence, where a real ". " is
#: just normal prose).
_LEADING_STRAY_PERIOD_RE = re.compile(r"^\.\s+", re.MULTILINE)


def _clean_docstring(doc: str) -> str:
    doc = _sphinx_to_markdown(doc)
    doc = _render_literal_blocks(doc)
    doc = _strip_issue_refs(doc)
    doc = _LEADING_STRAY_PERIOD_RE.sub("", doc)
    return doc.strip()


def _format_value(value: Any) -> str:
    """Render a resolved default deterministically — sorted, never a raw
    ``repr()`` of a hash-ordered container (set/frozenset/dict iteration
    order depends on ``PYTHONHASHSEED`` for str members)."""
    if isinstance(value, (set, frozenset)):
        return "`" + repr(sorted(value)) + "`" if value else "`" + repr(type(value)()) + "`"
    if isinstance(value, dict):
        if not value:
            return "`{}`"
        items = ", ".join(f"{k!r}: {value[k]!r}" for k in sorted(value.keys(), key=str))
        return "`{" + items + "}`"
    if isinstance(value, Path):
        return "`" + str(value) + "`"
    if isinstance(value, str):
        return "`" + repr(value) + "`"
    return "`" + repr(value) + "`"


# ---------------------------------------------------------------------------
# Curated entries: resolvers whose signature/return shape makes generic
# runtime invocation unsafe (bakes in a host path) or meaningless (a
# generic per-knob/per-family helper with no single "the" default). Each
# entry documents WHY it can't go through the generic path. Membership here
# is intentionally small and is exercised by
# ``tests/test_generated_docs_parity.py::test_curated_set_is_the_documented_exceptions_only``
# so a resolver cannot silently join it without that test changing too.
# ---------------------------------------------------------------------------
_CURATED: dict[str, dict[str, str]] = {
    "resolve_cache_dir": {
        "yaml_path": "—",
        "env_var": "`ATHENAEUM_CACHE_DIR`",
        "cli_flag": "`--cache-dir` (several subcommands)",
        "default": "`~/.cache/athenaeum`",
        "precedence": "explicit argument (CLI `--cache-dir`) > environment variable > code default",
        # Runtime invocation would call `.expanduser()` on the code default and
        # bake THIS machine's home directory into a committed file.
    },
    "resolve_model": {
        "yaml_path": "`models.<knob>`",
        "env_var": "caller-supplied (one per knob, e.g. `ATHENAEUM_WRITE_MODEL`)",
        "cli_flag": "—",
        "default": "caller-supplied",
        "precedence": "environment variable > `athenaeum.yaml` > caller-supplied default",
    },
    "resolve_recall_relevance_floor": {
        "yaml_path": "`recall.relevance_floor.<backend>` (see docstring — the `unprompted` "
        "call path resolves a sibling, push-specific key)",
        "env_var": "per-backend (see docstring)",
        "cli_flag": "—",
        "default": "`None` (no floor — today's behavior, unchanged, until an operator opts in)",
        "precedence": "environment variable > `athenaeum.yaml` > `None`",
    },
    "resolve_retention_policy": {
        "yaml_path": "`librarian.retention.families.<family>.policy` > "
        "`librarian.retention.defaults.policy`",
        "env_var": "—",
        "cli_flag": "—",
        "default": "`'truncate-top'`",
        "precedence": (
            "per-family `athenaeum.yaml` > shared-defaults `athenaeum.yaml` > "
            "code default"
        ),
    },
    "resolve_retention_max_bytes": {
        "yaml_path": "`librarian.retention.families.<family>.max_bytes` > "
        "`librarian.retention.defaults.max_bytes`",
        "env_var": "—",
        "cli_flag": "—",
        "default": "`1048576` (1 MiB)",
        "precedence": (
            "per-family `athenaeum.yaml` > shared-defaults `athenaeum.yaml` > "
            "code default"
        ),
    },
    "resolve_retention_destination": {
        "yaml_path": "`librarian.retention.families.<family>.destination` > "
        "`librarian.retention.defaults.destination`",
        "env_var": "—",
        "cli_flag": "—",
        "default": "`'in-repo'`",
        "precedence": (
            "per-family `athenaeum.yaml` > shared-defaults `athenaeum.yaml` > "
            "code default"
        ),
    },
    "resolve_extra_intake_roots": {
        "yaml_path": "`recall.extra_intake_roots`",
        "env_var": "—",
        "cli_flag": "—",
        "default": "`[]` (yaml default is `[\"raw/auto-memory\"]`; relative entries resolve "
        "against `knowledge_root` and entries that aren't real directories are dropped "
        "with a warning)",
        "precedence": "`athenaeum.yaml` > code default",
    },
    "resolve_authority_manifest_path": {
        "yaml_path": "`librarian.authority_manifest_path`",
        "env_var": "`ATHENAEUM_AUTHORITY_MANIFEST`",
        "cli_flag": "—",
        "default": "`<knowledge_root>/authority-manifest.yaml`",
        "precedence": (
            "environment variable > `athenaeum.yaml` (relative to `knowledge_root`) "
            "> code default"
        ),
    },
    "resolve_person_registry_root": {
        "yaml_path": "`person_registry.root`",
        "env_var": "—",
        "cli_flag": "—",
        "default": "`<knowledge_root>/wiki`",
        "precedence": "`athenaeum.yaml` (relative to `knowledge_root`) > code default",
    },
    "resolve_index_globs": {
        # Bundles two independent leaf keys under one function; documented
        # as two rows rather than shoehorned into one.
        "multi": "1",
    },
}

# The two synthetic rows resolve_index_globs expands to.
_INDEX_GLOBS_ROWS = [
    ("recall.include_globs", "—"),
    ("recall.exclude_globs", "—"),
]


def _own_call_site_literals(node: ast.FunctionDef) -> list[str]:
    literals: list[str] = []

    class _V(ast.NodeVisitor):
        def visit_Call(self, call: ast.Call) -> None:
            for arg in call.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.append(arg.value)
            for kw in call.keywords:
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    literals.append(kw.value.value)
            self.generic_visit(call)

    for stmt in node.body:
        _V().visit(stmt)
    return literals


def _called_private_helpers(node: ast.FunctionDef) -> list[str]:
    names: list[str] = []

    class _V(ast.NodeVisitor):
        def visit_Call(self, call: ast.Call) -> None:
            if isinstance(call.func, ast.Name) and call.func.id.startswith("_resolve"):
                if call.func.id not in names:
                    names.append(call.func.id)
            self.generic_visit(call)

    for stmt in node.body:
        _V().visit(stmt)
    return names


class _ResolverInfo:
    def __init__(self, name: str, node: ast.FunctionDef, fn: Any, docstring: str) -> None:
        self.name = name
        self.node = node
        self.fn = fn
        self.docstring = docstring
        self.yaml_path: str | None = None
        self.env_var: str | None = None


def _collect_resolvers() -> tuple[list[_ResolverInfo], dict[str, ast.FunctionDef]]:
    src = CONFIG_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    all_funcs: dict[str, ast.FunctionDef] = {
        n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    resolvers = [
        _ResolverInfo(n.name, n, None, ast.get_docstring(n) or "")
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("resolve_")
    ]
    return resolvers, all_funcs


def _find_env_var(info: _ResolverInfo, all_funcs: dict[str, ast.FunctionDef]) -> str | None:
    own_src = ast.unparse(info.node)
    found = _ENV_VAR_RE.findall(own_src)
    if found:
        return found[0]
    for helper_name in _called_private_helpers(info.node):
        helper_node = all_funcs.get(helper_name)
        if helper_node is None:
            continue
        helper_src = ast.unparse(helper_node)
        found = _ENV_VAR_RE.findall(helper_src)
        if found:
            return found[0]
    return None


def _find_yaml_path(
    info: _ResolverInfo, known_sections: set[str]
) -> str | None:
    m = _EXACT_BACKTICK_DOTTED.search(info.docstring)
    if m:
        return m.group(1)
    for m in _DOTTED_RE.finditer(info.docstring):
        candidate = m.group(0)
        first_seg = candidate.split(".")[0]
        if first_seg == "athenaeum":
            continue
        if first_seg in known_sections:
            return candidate
    own_literals = _own_call_site_literals(info.node)
    yaml_candidates = [
        s
        for s in own_literals
        if _YAML_KEY_RE.match(s) and not _ENV_VAR_RE.match(s)
    ]
    if yaml_candidates:
        return ".".join(yaml_candidates)
    return None


def _invoke_default(info: _ResolverInfo, config_mod: Any) -> Any:
    fn = getattr(config_mod, info.name)
    params = list(inspect.signature(fn).parameters.keys())
    if params and params[0] == "knowledge_root":
        return fn(_PLACEHOLDER_KNOWLEDGE_ROOT, {})
    return fn(None)


def _group_key(yaml_path: str | None) -> str:
    if not yaml_path or yaml_path == "—":
        return "zzz-other"
    return yaml_path.split(".")[0]


def _scan_env_vars_in_tree(root: Path) -> dict[str, list[str]]:
    """Every ``ATHENAEUM_*`` literal under *root*, mapped to the sorted
    relative paths of the ``*.py`` files that mention it.

    This is deliberately the SAME shape of scan
    ``scripts/check_env_docs.py``'s ``scan_tree`` runs (issue athenaeum#688/#1376)
    — that script gates "every `ATHENAEUM_*` read by `src/` must be documented
    in `docs/reference/configuration.md`" over the WHOLE tree, not just
    `config.py`. AC1 only asks this generator to document every `resolve_*`
    in `config.py`, which is a real subset: plenty of operator-facing env
    vars are read by a resolver in a DIFFERENT module (`cross_scope.py`,
    `clusters.py`, `batch_state.py`, ...), by the per-knob model-routing
    table, or directly by the CLI layer with no resolver at all. Rather than
    let generating this page regress the existing env-docs gate, every such
    var gets an entry in the "Other environment variables" appendix (see
    :func:`_render`) — naming the file(s) that reference it, which is exactly
    as much as a mechanical scan can honestly claim.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        names = set(_ENV_VAR_RE.findall(text))
        if not names:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for name in names:
            found.setdefault(name, []).append(rel)
    return found


#: The model-choosing knobs `resolve_model` is called for, each with its own
#: `DEFAULT_*_MODEL` constant in the calling module (issue athenaeum#1278's
#: `tests/test_configuration_doc_model_defaults.py` pins this exact set and
#: reads these same live constants — this table is generated to match that
#: pre-existing gate rather than duplicate a hand-typed copy of it). Every
#: value below is read from the live module, never a literal string, so a
#: model bump can't silently drift the doc from the code.
#: (label, knob, module_attr_path, used_by)
_MODEL_KNOBS: tuple[tuple[str, str, str, str], ...] = (
    ("Classifier", "classify", "athenaeum.config:DEFAULT_CLASSIFY_MODEL",
     "Tier-2 classifier and the C4 contradiction detector — one knob by design."),
    ("Writer", "write", "athenaeum.tiers:DEFAULT_WRITE_MODEL", "Tier-3 wiki writer."),
    ("Topic extractor", "topic", "athenaeum.query_topics:DEFAULT_TOPIC_MODEL",
     "`athenaeum query-topics` recall query rewriting."),
    ("Resolver", "resolve", "athenaeum.resolutions:DEFAULT_RESOLVE_MODEL",
     "Contradiction resolver (proposes a winner once the detector flags a conflict)."),
    ("Reasoning tier 1", "reasoning_t1", "athenaeum.reasoning_tiers:DEFAULT_T1_MODEL",
     "First-pass model for the reasoning-tier chain."),
    ("Reasoning tier 2", "reasoning_t2", "athenaeum.reasoning_tiers:DEFAULT_T2_MODEL",
     "Escalation model for the reasoning-tier chain."),
    ("Rule proposals", "rule_proposals", "athenaeum.rule_proposals:DEFAULT_RULE_PROPOSALS_MODEL",
     "Rule-proposal drafting call."),
)


def _models_table_lines() -> list[str]:
    import importlib

    lines = ["## Models", ""]
    lines.append(
        "All model values are free-form model-id strings passed to the Anthropic SDK. "
        "One row per model-choosing knob passed to `config.resolve_model` — see "
        "`resolve_model` above for the shared resolution mechanism. Each Default value "
        "below is read live from the knob's own module, never a hand-typed copy."
    )
    lines.append("")
    lines.append("| Knob | Env var | YAML key | Default | Used by |")
    lines.append("|---|---|---|---|---|")
    for label, knob, attr_path, used_by in _MODEL_KNOBS:
        module_name, attr_name = attr_path.split(":")
        module = importlib.import_module(module_name)
        default = getattr(module, attr_name)
        env_var = f"ATHENAEUM_{knob.upper()}_MODEL"
        lines.append(
            f"| {label} | `{env_var}` | `models.{knob}` | `{default}` | {used_by} |"
        )
    lines.append("")
    return lines


def generate() -> str:
    """Return the full generated ``docs/reference/configuration.md`` text."""
    # Clear ATHENAEUM_* env vars for the duration of default-invocation so an
    # operator's ambient environment cannot change the generated file.
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("ATHENAEUM_")}
    for k in saved_env:
        del os.environ[k]
    try:
        import athenaeum.config as config_mod

        resolvers, all_funcs = _collect_resolvers()
        # Pass 1: exact-backtick yaml paths only, to seed the known-section
        # vocabulary the loose-match fallback (step b) uses.
        known_sections: set[str] = set()
        for r in resolvers:
            m = _EXACT_BACKTICK_DOTTED.search(r.docstring)
            if m:
                known_sections.add(m.group(1).split(".")[0])

        rows: list[dict[str, str]] = []
        for r in resolvers:
            curated = _CURATED.get(r.name)
            if curated and curated.get("multi"):
                # resolve_index_globs: two synthetic rows, real docstring shared.
                effect = _clean_docstring(r.docstring)
                for yaml_path, env_var in _INDEX_GLOBS_ROWS:
                    leaf = yaml_path.rsplit(".", 1)[-1]
                    rows.append(
                        {
                            "name": f"{r.name} ({leaf})",
                            "yaml_path": f"`{yaml_path}`",
                            "env_var": env_var,
                            "cli_flag": "—",
                            "default": "`None` (unset — index everything)",
                            "precedence": "`athenaeum.yaml` > code default",
                            "effect": effect,
                        }
                    )
                continue
            if curated:
                rows.append(
                    {
                        "name": r.name,
                        "yaml_path": curated["yaml_path"],
                        "env_var": curated["env_var"],
                        "cli_flag": curated["cli_flag"],
                        "default": curated["default"],
                        "precedence": curated["precedence"],
                        "effect": _clean_docstring(r.docstring),
                    }
                )
                continue

            yaml_path = _find_yaml_path(r, known_sections)
            env_var = _find_env_var(r, all_funcs)
            default_value = _invoke_default(r, config_mod)

            has_env = env_var is not None
            has_yaml = yaml_path is not None
            if has_env and has_yaml:
                precedence = "environment variable > `athenaeum.yaml` > code default"
            elif has_yaml:
                precedence = "`athenaeum.yaml` > code default"
            elif has_env:
                precedence = "environment variable > code default"
            else:
                precedence = "code default only (no yaml key or env var of its own)"

            rows.append(
                {
                    "name": r.name,
                    "yaml_path": f"`{yaml_path}`" if yaml_path else "—",
                    "env_var": f"`{env_var}`" if env_var else "—",
                    "cli_flag": "—",
                    "default": _format_value(default_value),
                    "precedence": precedence,
                    "effect": _clean_docstring(r.docstring),
                }
            )

        # Group and sort deterministically: by yaml-path top-level section,
        # then by yaml path (or name, for the no-yaml-key group) within it.
        def sort_key(row: dict[str, str]) -> tuple[str, str]:
            yp = row["yaml_path"].strip("`")
            return (yp if yp != "—" else "~" + row["name"], row["name"])

        groups: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            yp = row["yaml_path"].strip("`") if row["yaml_path"] != "—" else None
            key = _group_key(yp)
            groups.setdefault(key, []).append(row)
        for key in groups:
            groups[key].sort(key=sort_key)

        covered_env_vars = {
            row["env_var"].strip("`") for row in rows if row["env_var"] != "—"
        }
        env_by_file = _scan_env_vars_in_tree(SRC_DIR)
        other_env_vars = {
            name: files
            for name, files in env_by_file.items()
            if name not in covered_env_vars
        }

        return _render(groups, other_env_vars)
    finally:
        for k in list(os.environ):
            if k.startswith("ATHENAEUM_"):
                del os.environ[k]
        os.environ.update(saved_env)


def _render(
    groups: dict[str, list[dict[str, str]]], other_env_vars: dict[str, list[str]]
) -> str:
    lines: list[str] = []
    lines.append("# Configuration Reference")
    lines.append("")
    lines.append(
        "This page is GENERATED from `src/athenaeum/config.py` by "
        "`scripts/gen_config_reference.py` — do not hand-edit it. CI "
        "(`tests/test_generated_docs_parity.py`) regenerates it and fails the "
        "build on any diff. To change an entry, change the resolver's own "
        "docstring (or its default/precedence, if that's what actually "
        "changed) and regenerate:"
    )
    lines.append("")
    lines.append("```")
    lines.append("python scripts/gen_config_reference.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "Every `resolve_*` function in `athenaeum.config` appears below, grouped by "
        "the top-level `athenaeum.yaml` section it reads (or under **Other** when it "
        "reads no yaml key of its own). `athenaeum.yaml` lives at the knowledge root "
        "(`<knowledge_root>/athenaeum.yaml`, default `~/knowledge/athenaeum.yaml`). An "
        "em dash (—) means that layer does not exist for a given knob."
    )
    lines.append("")
    lines.append(
        "Defaults were captured by actually invoking each resolver with no config and "
        "no `ATHENAEUM_*` environment variables set, except for a small, explicit set "
        "of resolvers whose signature takes a required non-config argument (a model "
        "knob, a retention family, a recall backend) or whose return value is a "
        "filesystem path relative to a knowledge root — those are hand-annotated "
        "because invoking them generically would either be meaningless (no single "
        "\"the\" default) or bake this generator's own machine into a committed file."
    )
    lines.append("")

    lines.extend(_models_table_lines())

    for key in sorted(groups.keys()):
        rows = groups[key]
        heading = "Other (no yaml key of its own)" if key == "zzz-other" else f"`{key}`"
        lines.append(f"## {heading}")
        lines.append("")
        for row in rows:
            lines.append(f"### `{row['name']}`")
            lines.append("")
            lines.append(f"- **YAML path:** {row['yaml_path']}")
            lines.append(f"- **Environment variable:** {row['env_var']}")
            lines.append(f"- **CLI flag:** {row['cli_flag']}")
            lines.append(f"- **Default:** {row['default']}")
            lines.append(f"- **Precedence:** {row['precedence']}")
            lines.append("")
            if row["effect"]:
                lines.append(row["effect"])
                lines.append("")

    if other_env_vars:
        lines.append("## Other environment variables")
        lines.append("")
        lines.append(
            "`ATHENAEUM_*` variables read somewhere in `src/` that are NOT sourced "
            "from a `config.py` `resolve_*` function — either a resolver in a "
            "different module (e.g. `cross_scope.py`, `clusters.py`, "
            "`batch_state.py`), a per-knob model-routing variable, or a variable "
            "read directly outside the resolver layer. Listed here (rather than "
            "omitted) so this page stays the complete answer to \"what "
            "`ATHENAEUM_*` variables exist\" — the same guarantee "
            "`scripts/check_env_docs.py` enforces in both directions."
        )
        lines.append("")
        lines.append("| Env var | Referenced in |")
        lines.append("|---|---|")
        for name in sorted(other_env_vars.keys()):
            files = ", ".join(f"`{f}`" for f in sorted(other_env_vars[name]))
            lines.append(f"| `{name}` | {files} |")
        lines.append("")

    text = "\n".join(lines)
    # Collapse any accidental run of 3+ blank lines produced by the literal-
    # block renderer, and guarantee a single trailing newline.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    OUTPUT.write_text(generate(), encoding="utf-8")
    print(f"gen_config_reference: wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
