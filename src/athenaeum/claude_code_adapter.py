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
import sys
from typing import Any

from athenaeum.config import resolve_cache_dir


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
        prompt, session_id = _read_hook_input(sys.stdin.read())
        if not prompt:
            return 0

        from athenaeum.context import build_context_for_turn

        envelope = build_context_for_turn(
            prompt,
            session_id,
            cache_dir=resolve_cache_dir(None),
        )
        text = envelope["render"]["text"]
        if text:
            print(_hook_output(text))
    except Exception:  # noqa: BLE001 — a recall failure must never block a prompt (AC3)
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
