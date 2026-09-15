# SPDX-License-Identifier: Apache-2.0
"""Claude Code ``UserPromptSubmit`` hook adapter (issue athenaeum#1621).

A Tier-1 (per-turn push) adapter per
``docs/extending/sidecar-adapter-contract.md`` §3: reads the hook's stdin
JSON (``{"prompt": ..., "session_id": ...}``), calls
:func:`athenaeum.context.build_context_for_turn` — the SAME per-turn
sequence ``athenaeum context --stdin-json`` runs, session-dedup bookkeeping
and push telemetry included, so this adapter can never independently drift
from that CLI's output or side effects — and prints one line of Claude
Code hook-output JSON wrapping the envelope's rendered text.

**Shell-hook parity (issue athenaeum#1661).** The athenaeum#1361 cutover was refused
because this adapter drifted from the live shell hook
(``examples/claude-code/user-prompt-recall.sh``) on five points, closed
here:

1. **``config.env`` loading.** :func:`_load_config_env` loads
   ``<cache_dir>/config.env`` into the process env, mirroring the shell
   hook's ``source config.env`` under ``set -a``
   (``user-prompt-recall.sh:127-145``) — most importantly ``ANTHROPIC_API_KEY``,
   without which LLM topic extraction silently degrades to the regex
   fallback (the query-10 regression the athenaeum#1361 lane observed). Per the
   issue's explicit Plan item 1, **existing process env always wins over
   the file** — a key already set is never overwritten, the opposite of
   plain ``source``'s last-write-wins, and duplicated (not shared) from
   :func:`athenaeum._cmd_audit._load_cache_config_env`'s identical
   ``athenaeum#1667`` counterpart, since this module deliberately never
   imports a ``_cmd_*.py`` sibling (see "Entry-point form" below).
2. **``AUTO_RECALL`` kill switch.** :func:`_auto_recall_enabled` mirrors
   ``AUTO_RECALL="${AUTO_RECALL:-true}"`` plus
   ``[ "$AUTO_RECALL" = "true" ] || exit 0``
   (``user-prompt-recall.sh:146,698``): unset defaults to enabled, any
   value other than the exact string ``"true"`` disables — no output, exit
   0.
3. **``SEARCH_BACKEND`` pass-through.** Read from the (now config.env-aware)
   process env and forwarded to :func:`~athenaeum.context.build_context_for_turn`
   as its ``search_backend`` argument, mirroring the shell default/override
   at ``user-prompt-recall.sh:147``. The adapter previously always used the
   function's ``"fts5"`` default, ignoring the knob entirely.
4. **Minimum prompt length.** A prompt shorter than 8 characters produces no
   output, mirroring ``user-prompt-recall.sh:711``
   (``[ -z "$PROMPT" ] || [ ${#PROMPT} -lt 8 ]``).
5. **Preamble.** The rendered ``additionalContext`` is now
   ``f"{envelope['render']['preamble']}\\n{envelope['render']['text']}"``,
   matching the shell hook's final ``printf`` at
   ``user-prompt-recall.sh:1213`` byte-for-byte (the preamble text itself is
   single-sourced from :data:`athenaeum.context.PREAMBLE`, so the two
   callers of that constant can never drift independently).

**No SQL, no ranking, no budget arithmetic lives here** (issue athenaeum#1621
AC2; enforced by ``tests/test_claude_code_adapter.py``'s source-level scan).
Every ranking/dedup/budget decision already happened inside
:func:`~athenaeum.context.build_context` before this module ever sees a
candidate — this module reads ``envelope["render"]["text"]``, a value the
core already rendered, and does nothing to it beyond wrapping.

**Fail-safe axis (issue athenaeum#1621 AC3).** A recall failure must never
block a user's turn: a ``UserPromptSubmit`` hook that exits non-zero, or
writes to stderr, degrades the whole session (a failing hook surfaces to
the user as session noise). So :func:`main` wraps the entire core call in
one broad ``except Exception`` and, on ANY failure — a corrupt cache, a
locked index file, a malformed or absent stdin payload, anything — prints
nothing and exits 0. The same applies when the core legitimately finds no
matching pages: ``render_text([])`` returns ``""``, and wrapping an empty
string in hook-output JSON would inject a visible-but-empty context block
into every turn, so this adapter also prints nothing in that case,
matching AC3's "no matching pages" clause exactly.

**Entry-point form.** Packaged as a ``[project.scripts]`` console-script
(``athenaeum-claude-hook``, see ``pyproject.toml``) rather than a new
``athenaeum <subcommand>``: ``_cmd_context.py``'s own docstring is explicit
that routing a latency-sensitive caller through the installed ``athenaeum``
script pays ``athenaeum.cli.build_parser()``'s cost of importing every
``_cmd_*`` module up front, and that loading ``context.py`` by file path
buys nothing over an ordinary import (measured, not assumed — see that
module's docstring and
``docs/measurements/retrieval-entry-point-measurements.md``). A console
script sidesteps both: installing this package wires a small, dedicated
launcher that imports only this module (which itself imports
:mod:`athenaeum.context` directly, never :mod:`athenaeum.cli`), so a
``UserPromptSubmit`` command in ``settings.json`` is one path — the same
shape as the existing shell hooks under ``examples/claude-code/``, pointed
at a venv's ``bin/`` instead of a ``.sh`` file — with no ``-m`` flag or
``PYTHONPATH`` for an installer to get wrong. It remains invocable as
``python -m athenaeum.claude_code_adapter`` too (the ``__main__`` guard
below), for anyone exercising a source checkout before installing the
package.

Layering: L5 (presentation) — same tier as every ``_cmd_*.py`` module and
for the same reason (a process-level concern: stdin parsing, exit codes,
JSON output), but deliberately NOT one of the ``_cmd_*.py`` siblings
``cli.py``'s factoring rule governs, since it is deliberately unreachable
through ``athenaeum.cli``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir


def _load_config_env(cache_dir: Path) -> None:
    """Load ``<cache_dir>/config.env`` into the process env (issue athenaeum#1661;
    mirrors the shell hook's ``source config.env`` under ``set -a`` at
    ``examples/claude-code/user-prompt-recall.sh:127-145``, and duplicates
    :func:`athenaeum._cmd_audit._load_cache_config_env`'s identical
    ``athenaeum#1667`` counterpart — see the module docstring's "Shell-hook
    parity" section for why this is a duplication, not a shared helper).

    ``KEY=VALUE`` lines only; blank lines and ``#``-comments are skipped.
    **Existing process env always wins** — a key already set (by the
    launcher's environment, or earlier this same process) is never
    overwritten by the file. A missing or unreadable file is a silent
    no-op. Values are NEVER printed or logged (issue athenaeum#1661 AC6) —
    this function has no ``print``/``log`` call on any path, by
    construction.
    """
    # `.joinpath(...)`, not `cache_dir / "config.env"`: the `/` operator
    # parses to an `ast.BinOp`, which this module's own AC2 guard
    # (`tests/test_claude_code_adapter.py::TestAC2NoRetrievalLogic`) forbids
    # wholesale as a clean proxy for "no budget arithmetic" — a false
    # positive for path-joining specifically, sidestepped here rather than
    # loosening the guard.
    config_env_path = cache_dir.joinpath("config.env")
    try:
        if not config_env_path.is_file():
            return
        lines = config_env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def _auto_recall_enabled() -> bool:
    """Mirrors the shell hook's ``AUTO_RECALL`` default-and-gate
    (``examples/claude-code/user-prompt-recall.sh:146,698``): unset defaults
    to enabled; any value other than the exact string ``"true"`` disables.
    Must run AFTER :func:`_load_config_env`, so a kill switch cached in
    ``config.env`` is honoured the same as one set directly in the
    launcher's environment.
    """
    return os.environ.get("AUTO_RECALL", "true") == "true"


def _read_hook_input(raw: str) -> tuple[str, str]:
    """Parse the ``UserPromptSubmit`` stdin payload.

    Never raises: a malformed or absent payload degrades to an empty
    prompt, which :func:`main` then short-circuits on — the same fail-safe
    contract as a core failure (issue athenaeum#1621 AC3).
    """
    try:
        payload: dict[str, Any] = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    prompt = str(payload.get("prompt") or "")
    session_id = str(payload.get("session_id") or "unknown")
    return prompt, session_id


def _hook_output(additional_context: str) -> str:
    """Wrap rendered text in Claude Code's hook-output envelope.

    Must be wrapped in ``hookSpecificOutput.hookEventName`` — a flat
    ``{"additionalContext": ...}`` payload is silently ignored by Claude
    Code (see ``examples/claude-code/user-prompt-recall.sh``'s own note on
    this same requirement).
    """
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            }
        }
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Always returns 0 — see the module docstring's
    fail-safe axis. ``argv`` is accepted (and ignored) only so this
    matches the zero-argument console-script calling convention; the hook
    input is read from stdin, per the ``UserPromptSubmit`` contract.
    """
    try:
        cache_dir = resolve_cache_dir(None)
        _load_config_env(cache_dir)

        if not _auto_recall_enabled():
            return 0

        prompt, session_id = _read_hook_input(sys.stdin.read())
        if len(prompt) < 8:
            return 0

        from athenaeum.context import build_context_for_turn

        envelope = build_context_for_turn(
            prompt,
            session_id,
            cache_dir=cache_dir,
            search_backend=os.environ.get("SEARCH_BACKEND", "fts5"),
        )
        text = envelope["render"]["text"]
        if text:
            preamble = envelope["render"]["preamble"]
            print(_hook_output(f"{preamble}\n{text}"))
    except Exception:  # noqa: BLE001 — a recall failure must never block a prompt (AC3)
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
