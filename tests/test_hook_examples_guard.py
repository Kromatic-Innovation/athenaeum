# SPDX-License-Identifier: Apache-2.0
"""CI gate + unit tests for the shipped-hook-example guard (issue athenaeum#1363).

`test_no_recall_logic_in_shipped_hook_examples` is the enforcement gate: it
fails the suite if any `examples/claude-code/*.sh` file -- all of which ship in
the wheel, since `examples/` is force-included -- reimplements recall instead of
delegating to the packaged adapter. `test_shipped_recall_hook_delegates` and
`test_shipped_recall_hook_is_thin` assert the positive shape of the one hook a
packaged adapter already covers, so the gate cannot be satisfied by deleting
the delegation along with the SQL.

The rest prove the checker itself works, because a guard nobody has seen fail
is indistinguishable from a guard that cannot fail. Two directions matter
equally here:

* **It catches the thing it exists to catch.** `test_catches_reintroduced_*`
  feeds it the literal counter-example the issue names -- "someone re-adds a
  200-line hook temporarily" -- plus a compact re-fork that only queries the
  index, and asserts each is rejected.
* **It does not catch prose.** The retired hook's replacement explains at
  length what is forbidden, naming `SELECT` and `ORDER BY` in its header; a
  scanner that read its own documentation as a violation would have to be
  disabled on the first commit after this one. `test_full_line_comment_prose_*`
  pins that comment-stripping behaviour, and `test_legitimate_awk_*` pins that
  the frontmatter-parsing `awk` other shipped hooks use is not mistaken for
  ranking.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_hook_examples.py"

_spec = importlib.util.spec_from_file_location("check_hook_examples", _SCRIPT)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "examples" / "claude-code"
_RECALL_HOOK = _HOOKS_DIR / "user-prompt-recall.sh"


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------


def test_no_recall_logic_in_shipped_hook_examples() -> None:
    violations, scanned = guard.check_dir(_HOOKS_DIR)
    assert scanned > 0, (
        "no *.sh found under examples/claude-code/ — the scan surface moved and "
        "this gate would pass vacuously"
    )
    rendered = [v.render(guard.REPO_ROOT) for v in violations]
    assert not violations, (
        "a shipped hook example reimplements recall; it must go through the "
        "packaged adapter instead:\n" + "\n".join(rendered)
    )


def test_main_returns_zero_on_current_tree(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.main([]) == 0
    assert "OK" in capsys.readouterr().out


def test_shipped_recall_hook_delegates() -> None:
    # The negative rule (no SQL) is satisfiable by an empty file. This is the
    # positive half: the shipped hook must actually reach the adapter.
    assert guard.DELEGATION_PATTERN.search(_RECALL_HOOK.read_text(encoding="utf-8"))


def test_shipped_recall_hook_is_thin() -> None:
    code_lines = guard.count_code_lines(_RECALL_HOOK.read_text(encoding="utf-8"))
    assert code_lines <= guard.MAX_DELEGATING_HOOK_CODE_LINES, (
        f"{_RECALL_HOOK.name} carries {code_lines} lines of executable shell"
    )


# --------------------------------------------------------------------------
# It catches what it exists to catch
# --------------------------------------------------------------------------


def _write_hook(tmp_path: Path, body: str, name: str = "user-prompt-recall.sh") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_catches_reintroduced_two_hundred_line_hook(tmp_path: Path) -> None:
    # The issue's own counter-example: "someone re-adds a 200-line hook
    # 'temporarily'". Padded with real statements, not blank lines, so the
    # budget rule is measured against executable shell.
    padding = "\n".join(f'VAR_{i}="value {i}"' for i in range(200))
    body = (
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + padding
        + '\nRESULTS=$(sqlite3 "$DB_FILE" "SELECT filename, rank FROM wiki '
        + "WHERE wiki MATCH '$QUERY' ORDER BY rank LIMIT 3\")\n"
        + 'printf "%s" "$RESULTS"\n'
    )
    violations = guard.check_hook(_write_hook(tmp_path, body))
    rules = {v.rule for v in violations}
    assert "no-recall-sql" in rules
    assert "must-delegate" in rules
    assert "thin-example-budget" in rules


def test_catches_compact_refork_that_only_queries(tmp_path: Path) -> None:
    # A re-fork does not have to be large to be a fork: Rule A alone must
    # reject a hook that queries the index itself, even inside the budget and
    # even while also calling the adapter.
    body = (
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        'ROWS=$(sqlite3 "$DB" "SELECT name FROM wiki ORDER BY rank LIMIT 3")\n'
        'if [ -z "$ROWS" ]; then exec athenaeum-claude-hook; fi\n'
    )
    violations = guard.check_hook(_write_hook(tmp_path, body))
    assert {v.rule for v in violations} == {"no-recall-sql"}
    assert any("sqlite3" in v.detail for v in violations)


def test_catches_recall_sql_in_a_non_delegating_hook(tmp_path: Path) -> None:
    # Rule A covers EVERY shipped hook example, not only the recall one — a
    # re-fork under a new filename must not escape the guard.
    body = (
        "#!/usr/bin/env bash\n"
        'sqlite3 "$DB" "SELECT filename FROM wiki WHERE wiki MATCH \'$Q\' LIMIT 3"\n'
    )
    violations = guard.check_hook(_write_hook(tmp_path, body, name="my-own-recall.sh"))
    assert [v.rule for v in violations] == ["no-recall-sql"] * len(violations)
    assert violations


def test_catches_a_delegating_hook_that_stops_delegating(tmp_path: Path) -> None:
    body = "#!/usr/bin/env bash\nset -uo pipefail\nexit 0\n"
    violations = guard.check_hook(_write_hook(tmp_path, body))
    assert [v.rule for v in violations] == ["must-delegate"]


def test_catches_a_fat_body_with_no_sql_at_all(tmp_path: Path) -> None:
    # The budget rule is independent of Rule A: shell can grow an
    # implementation without ever naming SQL (grep, awk, sort).
    padding = "\n".join(f'VAR_{i}="value {i}"' for i in range(60))
    body = "#!/usr/bin/env bash\nexec athenaeum-claude-hook\n" + padding + "\n"
    violations = guard.check_hook(_write_hook(tmp_path, body))
    assert [v.rule for v in violations] == ["thin-example-budget"]


def test_fails_loudly_on_an_empty_scan(tmp_path: Path) -> None:
    # A moved directory must not read as "nothing wrong here".
    assert guard.main(["--hooks-dir", str(tmp_path)]) == 2


def test_main_returns_one_on_violations(tmp_path: Path) -> None:
    _write_hook(tmp_path, '#!/usr/bin/env bash\nsqlite3 "$DB" "SELECT 1 FROM wiki"\n')
    assert guard.main(["--hooks-dir", str(tmp_path)]) == 1


# --------------------------------------------------------------------------
# It does not catch prose
# --------------------------------------------------------------------------


def test_full_line_comment_prose_is_not_a_violation(tmp_path: Path) -> None:
    body = (
        "#!/usr/bin/env bash\n"
        "# This hook used to run its own SELECT ... FROM wiki WHERE wiki MATCH\n"
        "# '$QUERY' ORDER BY rank LIMIT 3, via sqlite3, with a bm25() call.\n"
        "# It does not any more; all of that lives in the packaged adapter.\n"
        "exec athenaeum-claude-hook\n"
    )
    assert guard.check_hook(_write_hook(tmp_path, body)) == []


def test_the_shipped_hooks_own_header_prose_is_scanned_clean() -> None:
    # Narrower than the gate above and deliberately redundant with it: the
    # replacement hook's header NAMES the forbidden shapes to explain them, so
    # this is the specific regression that would make the guard self-defeating.
    text = _RECALL_HOOK.read_text(encoding="utf-8")
    assert "SQL" in text, "expected the thin hook to explain what it no longer does"
    assert guard.find_search_sql(text) == []


def test_comments_do_not_count_against_the_budget(tmp_path: Path) -> None:
    body = (
        "#!/usr/bin/env bash\n"
        + "\n".join(f"# explanatory line {i}" for i in range(300))
        + "\nexec athenaeum-claude-hook\n"
    )
    assert guard.check_hook(_write_hook(tmp_path, body)) == []


def test_legitimate_awk_frontmatter_parsing_is_not_a_violation(tmp_path: Path) -> None:
    # The shape `stop-hook-validate.sh` and `wiki-context-inject.sh` really use:
    # awk slicing a YAML frontmatter block. Not search, not ranking.
    body = (
        "#!/usr/bin/env bash\n"
        'fm="$(awk \'BEGIN{c=0} /^---$/{c++; next} c==1{print} c>1{exit}\' "$f")"\n'
        'printf "%s" "$fm"\n'
    )
    assert guard.check_hook(_write_hook(tmp_path, body, name="stop-ish.sh")) == []


def test_delegating_to_the_module_form_counts(tmp_path: Path) -> None:
    body = '#!/usr/bin/env bash\nexec "$PYTHON" -m athenaeum.claude_code_adapter\n'
    assert guard.check_hook(_write_hook(tmp_path, body)) == []


def test_violation_line_numbers_point_at_the_original_file(tmp_path: Path) -> None:
    # Comment-stripping must not shift reported line numbers, or the error
    # message sends the reader to the wrong line.
    body = (
        "#!/usr/bin/env bash\n"
        "# comment\n"
        "# comment\n"
        "exec athenaeum-claude-hook\n"
        'sqlite3 "$DB" "SELECT 1 FROM wiki"\n'
    )
    violations = guard.check_hook(_write_hook(tmp_path, body))
    assert violations
    assert {v.line for v in violations} == {5}
