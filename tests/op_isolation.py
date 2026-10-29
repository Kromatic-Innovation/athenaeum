# SPDX-License-Identifier: Apache-2.0
"""Shared `op` sentinel for hook/eval test isolation (athenaeum#2193).

Before this module, isolation from a REAL `op` (the 1Password CLI) was
per-test opt-in: a test had to remember to call ``_env_without_op`` (see
``tests/test_shell_hooks.py``'s retired helper of that name) before every
hook subprocess invocation, or the hook could shell out to whatever `op`
happens to be on the runner's PATH -- hanging on a real desktop-app
permission prompt, or silently reading a real credential.

This module flips the default: :func:`install_sentinel_op` writes a stub
`op` that is prepended to PATH by every shared env-builder
(``tests/test_shell_hooks.py::hook_env``,
``tests/evals/rollout.py::build_breadcrumb_hook_env``) so a hook under test
finds the sentinel FIRST unless a test explicitly prepends its own working
fake `op` afterward (which still wins -- PATH precedence is unchanged, this
module only supplies what sits behind it).

The sentinel itself exits non-zero and writes nothing to stdout -- it must
look to the hook exactly like a real, installed-but-signed-out `op` that
fails every `op read`, not like `op` is simply missing from PATH. Every
invocation it is actually asked to make is recorded, so tests that assert
"op must never be reached" (the whole point of athenaeum#2193's new gate)
have something concrete to assert against.
"""

from __future__ import annotations

import shlex
from pathlib import Path

#: Filename of the invocation log the stub `op` appends to, relative to the
#: *parent* directory passed to :func:`install_sentinel_op`.
OP_SENTINEL_LOG_NAME = "op-invocations.log"


def install_sentinel_op(parent: Path) -> Path:
    """Write a PATH-first stub `op` under *parent*.

    The stub records every invocation (its full argv, as a single line) to
    ``op_invocation_log(parent)`` and exits non-zero without writing
    anything to stdout. *parent* is created if it does not already exist
    (callers may pass a directory they do not yet own, e.g. a fixture's
    ``tmp_path`` before any subdirectory has been created under it).

    Returns the bin directory to prepend to ``PATH`` -- the stub itself
    lives at ``<returned dir>/op``.
    """
    parent.mkdir(parents=True, exist_ok=True)
    bin_dir = parent / "op-sentinel-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = op_invocation_log(parent)
    stub = bin_dir / "op"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log_path))}\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def op_invocation_log(parent: Path) -> Path:
    """The log path `install_sentinel_op(parent)`'s stub appends to."""
    return parent / OP_SENTINEL_LOG_NAME


def sentinel_op_invocations(parent: Path) -> list[str]:
    """The recorded argv lines (empty list when the log does not exist)."""
    log_path = op_invocation_log(parent)
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
