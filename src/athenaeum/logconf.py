# SPDX-License-Identifier: Apache-2.0
"""Central logging configuration + per-run correlation id (issue athenaeum#540, M25).

Before this module the CLI configured logging in three near-duplicate
``logging.basicConfig`` calls whose format carried neither a date
(``datefmt="%H:%M:%S"`` only, so a run spanning midnight is unorderable) nor a
logger name (``%(name)s`` absent, so a line can't be attributed to a module),
and ``mcp_server`` configured nothing at all. There was no run/correlation id
anywhere, so two overlapping runs produced interleaved lines that could not be
untangled.

:func:`configure_logging` is the single entry point — one ISO-dated format with
``%(name)s`` and a per-run correlation id — shared by every process (CLI and the
MCP server). The correlation id lives in a :class:`contextvars.ContextVar` so it
follows the run without threading a parameter through every call site; a
:class:`logging.Filter` injects it onto every record so ``%(run_id)s`` in the
format always resolves.

Layering: L0 primitive (leaf). May import only stdlib (``contextvars``,
``logging``, ``uuid``). Factoring rule: this module owns ONLY logging
format/wiring and the run-id contextvar; it must never itself decide WHEN a
run starts (that is :func:`athenaeum.librarian.run` calling
:func:`new_run_id`) or read any athenaeum config.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

#: The active run's correlation id. ``"-"`` until a run stamps one via
#: :func:`new_run_id`, so a stray log line before a run starts still formats.
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "athenaeum_run_id", default="-"
)

# ISO-8601 date + time so lines from a run spanning midnight stay orderable
# (the old ``%H:%M:%S``-only format could not), and the logger name so each
# line is attributable to the module that emitted it.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [run:%(run_id)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


class _RunIdFilter(logging.Filter):
    """Stamp every record with the active run's correlation id.

    Attached to the root handler (not a logger) so it runs for records from
    every logger in the tree, keeping ``%(run_id)s`` resolvable everywhere.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        return True


def new_run_id() -> str:
    """Mint a fresh short correlation id, set it as the active run id, return it.

    Called at the start of a run (e.g. :func:`athenaeum.librarian.run`) so every
    line that run emits — even in a long-lived process that performs several
    runs — is attributable to exactly one run.
    """
    rid = uuid.uuid4().hex[:8]
    run_id_var.set(rid)
    return rid


def configure_logging(*, verbose: bool = False, force: bool = False) -> None:
    """Configure root logging with the shared ISO format + run-id filter.

    ``verbose`` selects ``DEBUG`` vs ``INFO``. ``force`` is passed straight to
    :func:`logging.basicConfig`: the default ``False`` preserves the prior
    behavior of the three CLI ``basicConfig`` calls this replaces — a no-op when
    the root logger already has handlers (e.g. under pytest's log-capture
    plugin, so ``caplog`` keeps working; or a second call in one process). In a
    real CLI/MCP-server process the root starts clean, so the first call
    installs this handler + format. Pass ``force=True`` only to deliberately
    reconfigure (e.g. an isolated unit test of this module).
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    handler.addFilter(_RunIdFilter())
    logging.basicConfig(level=level, handlers=[handler], force=force)
