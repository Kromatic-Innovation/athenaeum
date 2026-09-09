#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate ``docs/reference/cli.md`` from the REAL ``argparse`` tree built by
``athenaeum.cli.build_parser()`` (issue athenaeum#1424).

Why the parser object rather than shelling out to ``--help``: ``build_parser()``
is already the documented single source of truth for every subcommand
(``tests/test_cli.py`` walks its entire tree to assert every leaf binds
``func`` and every leaf's ``--help`` renders) — introspecting the same
``argparse.ArgumentParser`` objects directly is strictly more deterministic
than parsing N subprocess outputs (no terminal-width-dependent line
wrapping, no subprocess startup cost, no risk of one subcommand's ``--help``
crashing the whole generation run).

Structure: athenaeum's subcommands are registered TOP-LEVEL — ``cli.py``
builds ONE ``parser.add_subparsers()`` and each sibling ``_cmd_*.py`` module
hangs its subparser(s) off it directly. There is no ``query`` group despite
what a couple of docstrings and a stderr deprecation notice claim — this
generator walks the actual parser tree instead of trusting that prose. A
handful of top-level commands (``dedupe``, ``auto-memory``, ``questions``,
``merges``, ``decisions``, ``authority``, ``axiom``, ``calibration``,
``storage``, ``push-metrics``, ``measure``, ``memory-class``,
``description``, ``verdicts``, ``dimensions``, ``subject``) DO have their own
nested sub-subparsers (e.g. ``athenaeum merges revalidate``) — the walk
recurses into those.

One flat, alphabetically-sorted section per node (group or leaf) in the tree,
keyed by its full dotted command path (``athenaeum.merges.revalidate``) so
sort order is stable regardless of registration order. A GROUP node (one
that itself has subparsers) lists its children with their one-line help; a
LEAF node lists every flag and positional argument with its default and help
text, read directly off the ``argparse.Action`` objects.

Determinism: no timestamps, no absolute paths (verified empirically — every
``argparse`` default in this tree is either a scalar or a relative/
tilde-prefixed ``Path``; see the issue for how that was checked), and no
sorting of anything whose native order is unstable (``dict``/``set``) without
an explicit ``sorted()``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
OUTPUT = REPO_ROOT / "docs" / "reference" / "cli.md"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_ISSUE_PAREN_RE = re.compile(
    r"\(\s*(?:see\s+)?(?:issues?\s+)?athenaeum#\d+(?:\s*[-,/]\s*(?:athenaeum)?#\d+)*\s*\)",
    re.IGNORECASE,
)
_ISSUE_INLINE_RE = re.compile(
    r"\bissues?\s+athenaeum#\d+(?:\s*[-,/]\s*(?:athenaeum)?#\d+)*\b", re.IGNORECASE
)
_ISSUE_BARE_RE = re.compile(r"athenaeum#\d+(?:\s*[-,/]\s*(?:athenaeum)?#\d+)*")
_PAREN_LEADING_PUNCT_RE = re.compile(r"\(\s*[:,]\s*")
_PAREN_TRAILING_PUNCT_RE = re.compile(r"[,;]\s*\)")
_PAREN_INNER_OPEN_RE = re.compile(r"\(\s+")
_PAREN_INNER_CLOSE_RE = re.compile(r"\s+\)")
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([.,;:)])")


def _sanitize(text: str | None) -> str:
    """Strip issue references from one-line ``--help``/description text.

    Simpler than ``gen_config_reference.py``'s ``_strip_issue_refs`` — CLI
    help strings are one line each with no RST literal blocks to protect —
    but the same cleanup idioms (dangling paren punctuation after a bare
    ref is removed) apply, so the regex shapes are deliberately identical.
    """
    if not text:
        return ""
    text = _ISSUE_PAREN_RE.sub("", text)
    text = _ISSUE_INLINE_RE.sub("", text)
    text = _ISSUE_BARE_RE.sub("", text)
    text = _PAREN_LEADING_PUNCT_RE.sub("(", text)
    text = _PAREN_TRAILING_PUNCT_RE.sub(")", text)
    text = _PAREN_INNER_OPEN_RE.sub("(", text)
    text = _PAREN_INNER_CLOSE_RE.sub(")", text)
    text = _EMPTY_PARENS_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return text.strip()


def _format_default(value: Any) -> str:
    if value is None:
        return "—"
    if value == argparse.SUPPRESS:
        return "—"
    if isinstance(value, bool):
        return "`" + str(value) + "`"
    if isinstance(value, (list, tuple)) and not value:
        return "`[]`"
    return "`" + str(value) + "`"


class _Node:
    def __init__(
        self,
        path: tuple[str, ...],
        parser: argparse.ArgumentParser,
        help_text: str,
    ) -> None:
        self.path = path
        self.parser = parser
        self.help_text = help_text
        self.children: dict[str, "_Node"] = {}

    @property
    def dotted(self) -> str:
        return ".".join(self.path)

    @property
    def command_line(self) -> str:
        return " ".join(("athenaeum",) + self.path)


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _build_tree(parser: argparse.ArgumentParser, path: tuple[str, ...], help_text: str) -> _Node:
    node = _Node(path, parser, help_text)
    sub_action = _subparsers_action(parser)
    if sub_action is not None:
        # `_choices_actions` carries the `help=` text passed to `add_parser`,
        # in REGISTRATION order; `choices` maps name -> the child parser.
        help_by_name = {pseudo.dest: (pseudo.help or "") for pseudo in sub_action._choices_actions}
        for name, child_parser in sub_action.choices.items():
            node.children[name] = _build_tree(
                child_parser, path + (name,), help_by_name.get(name, "")
            )
    return node


def _flatten(node: _Node, out: list[_Node]) -> None:
    if node.path:  # skip the synthetic root
        out.append(node)
    for child in node.children.values():
        _flatten(child, out)


def _render_leaf_flags(parser: argparse.ArgumentParser) -> list[str]:
    lines: list[str] = []
    positionals = [
        a
        for a in parser._actions
        if not a.option_strings
        and not isinstance(a, argparse._SubParsersAction)
        and not isinstance(a, argparse._HelpAction)
    ]
    options = [
        a
        for a in parser._actions
        if a.option_strings and not isinstance(a, argparse._HelpAction)
    ]
    if positionals:
        lines.append("**Positional arguments:**")
        lines.append("")
        for a in positionals:
            help_text = _sanitize(a.help)
            choices = f" (choices: {', '.join(str(c) for c in a.choices)})" if a.choices else ""
            bullet = f"- `{a.dest}`{choices}"
            lines.append(f"{bullet} — {help_text}" if help_text else bullet)
        lines.append("")
    if options:
        lines.append("| Flag | Default | Choices | Help |")
        lines.append("|---|---|---|---|")
        for a in sorted(options, key=lambda a: a.option_strings[0]):
            flags = ", ".join(f"`{s}`" for s in a.option_strings)
            default = _format_default(a.default)
            choices = ", ".join(str(c) for c in a.choices) if a.choices else "—"
            help_text = _sanitize(a.help).replace("|", "\\|") or "—"
            lines.append(f"| {flags} | {default} | {choices} | {help_text} |")
        lines.append("")
    if not positionals and not options:
        lines.append("No flags beyond `-h`/`--help`.")
        lines.append("")
    return lines


def generate() -> str:
    import athenaeum.cli as cli_mod

    root_parser = cli_mod.build_parser()
    root = _build_tree(root_parser, (), "")
    nodes: list[_Node] = []
    _flatten(root, nodes)
    nodes.sort(key=lambda n: n.dotted)

    lines: list[str] = []
    lines.append("# CLI Reference")
    lines.append("")
    lines.append(
        "This page is GENERATED from `athenaeum.cli.build_parser()`'s real "
        "`argparse` tree by `scripts/gen_cli_reference.py` — do not hand-edit "
        "it. CI (`tests/test_generated_docs_parity.py`) regenerates it and "
        "fails the build on any diff. To change an entry, change the "
        "subcommand's own `add_argument(...)` call in its owning `_cmd_*.py` "
        "module and regenerate:"
    )
    lines.append("")
    lines.append("```")
    lines.append("python scripts/gen_cli_reference.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "Every subcommand is registered top-level on one `parser.add_subparsers()` "
        "in `cli.py` — there is no `query` group. A handful of commands (`dedupe`, "
        "`auto-memory`, `questions`, `merges`, `decisions`, `authority`, `axiom`, "
        "`calibration`, `storage`, `push-metrics`, `measure`, `memory-class`, "
        "`description`, `verdicts`, `dimensions`, `subject`) have their own nested "
        "sub-subcommands, listed under their own section below."
    )
    lines.append("")
    lines.append("## Command index")
    lines.append("")
    for n in nodes:
        kind = "group" if n.children else "command"
        help_text = _sanitize(n.help_text)
        anchor = n.command_line.replace(" ", "-").replace("`", "")
        suffix = f" — {help_text}" if help_text else ""
        lines.append(f"- [`{n.command_line}`](#{anchor}) ({kind}){suffix}")
    lines.append("")

    for n in nodes:
        lines.append(f"## `{n.command_line}`")
        lines.append("")
        help_text = _sanitize(n.help_text)
        if help_text:
            lines.append(help_text)
            lines.append("")
        if n.children:
            lines.append("Subcommands:")
            lines.append("")
            for name in sorted(n.children.keys()):
                child = n.children[name]
                child_help = _sanitize(child.help_text)
                lines.append(
                    f"- `{child.command_line}`" + (f" — {child_help}" if child_help else "")
                )
            lines.append("")
        else:
            lines.extend(_render_leaf_flags(n.parser))

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    OUTPUT.write_text(generate(), encoding="utf-8")
    print(f"gen_cli_reference: wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
