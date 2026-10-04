#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail if a shipped Claude Code hook example reimplements recall
(issue athenaeum#1363).

``examples/claude-code/`` is force-included in the wheel
(``pyproject.toml``, issue athenaeum#793), so every file in it is shipped
code, not scratch material. For years one of those files --
``user-prompt-recall.sh`` -- was a ~1650-line bash/awk reimplementation of
per-turn recall: its own FTS5 ``SELECT``, its own BM25 ordering, its own
slot cap, its own budget packing. A second implementation of a contract
drifts from the first, and that one did, repeatedly and silently.

Issue athenaeum#1361 cut the live hook over to the packaged adapter
(``athenaeum.claude_code_adapter``) and athenaeum#1363 retired the shell
body. This check is the part of athenaeum#1363 that makes the fork
STRUCTURALLY hard to reintroduce rather than merely discouraged: prose in
a doc does not fail a pull request.

Two rules, deliberately different in kind:

**Rule A -- no recall search SQL in any shipped hook example.** A
reimplementation of recall cannot avoid querying the index, so the query
is the load-bearing signal: a ``sqlite3`` invocation, a ``SELECT ...
FROM``, an ``ORDER BY``, a ``LIMIT n``, an FTS5 ``MATCH``/``bm25()``, or a
``USING fts5`` table definition. A shipped hook example must reach the
index through the packaged ``athenaeum`` package, never through its own
SQL. This rule is pattern-based and so is scoped to shapes that are
decidable: it does NOT try to pattern-match "ranking logic" in the
abstract, which would be both leaky (ranking has no single syntax) and
false-positive-prone.

**Rule B -- the hooks a packaged adapter already covers must stay thin and
must delegate.** :data:`DELEGATING_HOOKS` names them. Each must (1)
actually invoke the packaged adapter, and (2) stay under
:data:`MAX_DELEGATING_HOOK_CODE_LINES` lines of executable shell.
That size budget is what catches the failure mode Rule A alone would
miss: not a clean re-fork with SQL in it, but the "re-add the old hook
temporarily" commit. Comments are not counted -- an example SHOULD
explain itself at length; what it must not do is compute.

Both rules read COMMENT-STRIPPED source: a full-line ``#`` comment is
prose and is skipped, so this file's own prose (and the hook's own header,
which names ``SELECT`` and ``ORDER BY`` to explain what is forbidden)
cannot trip the scan it describes. Inline trailing comments ARE scanned --
the alternative is parsing shell quoting to tell a ``#`` in a string from
a ``#`` starting a comment. If a trailing comment trips this check, make
it a full-line comment.

Exit codes: 0 clean, 1 violations found, 2 the scan itself is broken
(zero hook files discovered -- a moved directory or a bad glob would
otherwise pass green, the silent-pass trap ``scripts/check_env_docs.py``
documents at length).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "examples" / "claude-code"

#: Hook examples whose job is fully covered by a packaged adapter, and
#: which must therefore delegate to it instead of carrying an
#: implementation. Keyed by filename within :data:`HOOKS_DIR`.
DELEGATING_HOOKS = {"user-prompt-recall.sh"}

#: Executable (non-comment, non-blank) line budget for a hook in
#: :data:`DELEGATING_HOOKS`. The thin launcher athenaeum#1363 shipped is
#: 13 lines; the budget leaves room for a resolution fallback or two
#: without leaving room for an implementation.
MAX_DELEGATING_HOOK_CODE_LINES = 40

#: Shapes that prove a hook is querying the recall index itself. Ordered
#: most-specific first so the reported violation names the clearest
#: signal.
SEARCH_SQL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fts5 virtual table", re.compile(r"USING\s+fts5", re.IGNORECASE)),
    ("bm25() ranking call", re.compile(r"\bbm25\s*\(")),
    ("FTS5 MATCH predicate", re.compile(r"\bMATCH\b")),
    ("sqlite3 invocation", re.compile(r"(?<![\w./-])sqlite3\b")),
    ("SQL SELECT ... FROM", re.compile(r"\bSELECT\b.{0,400}?\bFROM\b", re.DOTALL)),
    ("SQL ORDER BY", re.compile(r"\bORDER\s+BY\b")),
    ("SQL LIMIT n", re.compile(r"\bLIMIT\s+\d")),
)

#: What counts as delegating to the packaged adapter: the console script,
#: or the module behind it.
DELEGATION_PATTERN = re.compile(r"athenaeum-claude-hook|athenaeum\.claude_code_adapter")


class Violation(NamedTuple):
    """One rule failure, with enough detail to fix it without re-running.

    A :class:`~typing.NamedTuple` rather than a dataclass deliberately: this
    module is loaded by its own test through
    ``importlib.util.spec_from_file_location`` (the convention
    ``tests/test_env_docs.py`` set for every ``scripts/check_*.py`` guard), and
    ``@dataclass`` resolves annotations through ``sys.modules[cls.__module__]``,
    which that loader does not populate.
    """

    path: Path
    rule: str
    detail: str
    line: int | None = None

    def render(self, root: Path) -> str:
        try:
            where = self.path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - defensive
            where = str(self.path)
        at = f":{self.line}" if self.line else ""
        return f"{where}{at}: [{self.rule}] {self.detail}"


def strip_full_line_comments(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, text)`` for every line that is not blank and
    not a full-line ``#`` comment. Line numbers are 1-based and refer to
    the ORIGINAL file, so a reported violation is directly navigable.
    """
    kept: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append((lineno, raw))
    return kept


def find_search_sql(text: str) -> list[tuple[str, int, str]]:
    """Rule A. Return ``(pattern name, line number, offending line)`` for
    every recall-SQL shape in the comment-stripped body of *text*.
    """
    code_lines = strip_full_line_comments(text)
    # Rebuild a body that preserves line numbers so a multi-line heredoc
    # SELECT ... FROM is still matchable, while comment prose is not.
    by_line = {lineno: line for lineno, line in code_lines}
    body = "\n".join(line for _, line in code_lines)
    offsets: list[int] = []
    for lineno, _ in code_lines:
        offsets.append(lineno)

    found: list[tuple[str, int, str]] = []
    for name, pattern in SEARCH_SQL_PATTERNS:
        for match in pattern.finditer(body):
            # Which kept-line does this match start on?
            index = body.count("\n", 0, match.start())
            lineno = offsets[index] if index < len(offsets) else 0
            found.append((name, lineno, by_line.get(lineno, "").strip()))
    return found


def count_code_lines(text: str) -> int:
    """Rule B's measure: executable shell lines, comments excluded."""
    return len(strip_full_line_comments(text))


def delegates(text: str) -> bool:
    """Whether *text* actually invokes the packaged adapter.

    Searches the COMMENT-STRIPPED body, not the raw file. A thin launcher's
    header necessarily names the adapter in prose to explain what it
    delegates to, so a raw search would accept a hook whose only remaining
    mention of it is a commented-out ``# exec athenaeum-claude-hook`` — the
    exact "temporarily" edit Rule B exists to catch.
    """
    body = "\n".join(line for _, line in strip_full_line_comments(text))
    return bool(DELEGATION_PATTERN.search(body))


def check_hook(path: Path) -> list[Violation]:
    """Apply both rules to one hook example."""
    text = path.read_text(encoding="utf-8")
    violations: list[Violation] = []

    for name, lineno, line in find_search_sql(text):
        violations.append(
            Violation(
                path=path,
                rule="no-recall-sql",
                detail=(
                    f"{name} in a shipped hook example: {line!r}. A shipped hook "
                    "must reach the recall index through the packaged athenaeum "
                    "package (athenaeum-claude-hook / athenaeum context), never "
                    "through its own query. If this is explanatory prose, make it "
                    "a full-line comment."
                ),
                line=lineno or None,
            )
        )

    if path.name in DELEGATING_HOOKS:
        if not delegates(text):
            violations.append(
                Violation(
                    path=path,
                    rule="must-delegate",
                    detail=(
                        "this hook's job is covered by the packaged adapter, but it "
                        "never invokes it. Expected a call to "
                        "athenaeum-claude-hook or athenaeum.claude_code_adapter in "
                        "executable shell — a mention in a comment does not count."
                    ),
                )
            )
        code_lines = count_code_lines(text)
        if code_lines > MAX_DELEGATING_HOOK_CODE_LINES:
            violations.append(
                Violation(
                    path=path,
                    rule="thin-example-budget",
                    detail=(
                        f"{code_lines} lines of executable shell exceeds the "
                        f"{MAX_DELEGATING_HOOK_CODE_LINES}-line budget for a hook "
                        "that delegates to a packaged adapter. Comments are not "
                        "counted: explain at length, compute elsewhere."
                    ),
                )
            )

    return violations


def check_dir(hooks_dir: Path = HOOKS_DIR) -> tuple[list[Violation], int]:
    """Check every ``*.sh`` under *hooks_dir*. Returns the violations and
    the number of files scanned -- the caller FAILS LOUDLY on zero, rather
    than reporting a vacuous pass.
    """
    paths = sorted(hooks_dir.glob("*.sh"))
    violations: list[Violation] = []
    for path in paths:
        violations.extend(check_hook(path))
    return violations, len(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hooks-dir",
        type=Path,
        default=HOOKS_DIR,
        help="directory of shipped hook examples (default: examples/claude-code)",
    )
    args = parser.parse_args(argv)

    violations, scanned = check_dir(args.hooks_dir)

    if scanned == 0:
        print(
            f"hook-examples: ERROR no *.sh files found under {args.hooks_dir} — the "
            "scan is broken (moved examples/claude-code/? bad glob?); refusing to "
            "report a false pass.",
            file=sys.stderr,
        )
        return 2

    if violations:
        print(
            f"hook-examples: {len(violations)} violation(s) across {scanned} shipped "
            "hook example(s) — recall belongs to the packaged adapter "
            "(issue athenaeum#1363):",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation.render(REPO_ROOT)}", file=sys.stderr)
        return 1

    print(f"hook-examples: OK ({scanned} shipped hook example(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
