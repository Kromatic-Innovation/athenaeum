# SPDX-License-Identifier: Apache-2.0
"""Ingestion gate: intake gated on push-metrics precision staying healthy (issue athenaeum#968).

Part 3 of #968 (memory-model v6's reshaped #430): the "measured-precision
hook" memory-model §6.5 names — "so intake cannot silently degrade push
quality." The push-precision instrument (:mod:`athenaeum.push_metrics`,
issue athenaeum#711) is itself an optional, config-gated measurement; if an
operator (or a bug) turns it off, or it never produces a single
reference-determination record, the pipeline would otherwise keep compiling
new claims into the wiki with NO visibility into whether push quality is
holding up. This module is the health check that stops that from being
silent.

**"Healthy" is deliberately a liveness check, not a quality bar.** The issue
names no minimum precision VALUE anywhere — memory-model §6.5 says intake is
"gated on the precision instrumentation staying healthy", which this module
reads as "the instrument is alive and producing data", not "precision is
above some threshold". Concretely, :func:`check_ingestion_gate` considers the
instrumentation healthy when BOTH:

1. push-metrics instrumentation is enabled
   (:func:`athenaeum.config.resolve_push_metrics_enabled`), and
2. at least one reference-determination record exists in the ledger ever
   (precision is therefore COMPUTABLE, even if the computed value is low).

A fresh install with the gate turned on and zero sessions run yet is
"unhealthy" under this definition until its first session completes — this
is intentional and self-healing, not a bug: the gate itself defaults OFF
(:func:`athenaeum.config.resolve_ingestion_gate_enabled`), so an operator who
opts in accepts this bootstrap window. This is a genuine product decision the
issue's text does not pin down further; see the athenaeum#968 PR description for
the explicit call-out.

**What "gated" means when unhealthy.** No partial-volume throttle is
implemented (the issue specifies no throttle curve/threshold to throttle
against) — when the gate is enabled and unhealthy, the caller
(:mod:`athenaeum.librarian`'s ``_run_auto_memory_phase``) skips auto-memory
compilation ENTIRELY for that run. Nothing is deleted: the raw intake stays
on disk and is re-evaluated (and, if still unhealthy, skipped again)
idempotently on the next run — same non-destructive shape as
:mod:`athenaeum.never_ingest`'s own refusal.

Layering: L3 service, alongside :mod:`athenaeum.push_metrics`. Imports
:mod:`athenaeum.config` (leaf) and :mod:`athenaeum.push_metrics` (L3 peer,
its two public ledger readers only) — must never import
:mod:`athenaeum.librarian` back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IngestionGateStatus:
    """The ingestion gate's verdict for one check call.

    ``enabled=False`` always implies ``healthy=True`` — a disabled gate never
    blocks anything, so its status is trivially "healthy" in the sense that
    it imposes no constraint. Callers should branch on ``enabled and not
    healthy`` to decide whether to actually skip ingestion, never on
    ``healthy`` alone.
    """

    enabled: bool
    healthy: bool
    reason: str
    push_metrics_enabled: bool
    reference_record_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "healthy": self.healthy,
            "blocked": self.blocked,
            "reason": self.reason,
            "push_metrics_enabled": self.push_metrics_enabled,
            "reference_record_count": self.reference_record_count,
        }

    @property
    def blocked(self) -> bool:
        """True when a caller should skip ingestion this run."""
        return self.enabled and not self.healthy


def check_ingestion_gate(
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
) -> IngestionGateStatus:
    """Check whether push-metrics precision instrumentation is healthy.

    See the module docstring for the exact "healthy" definition and what
    "gated" means downstream. Read-only — never writes anything, never
    deletes anything.
    """
    from athenaeum.config import (
        resolve_ingestion_gate_enabled,
        resolve_push_metrics_enabled,
    )
    from athenaeum.push_metrics import read_reference_records

    enabled = resolve_ingestion_gate_enabled(config)
    pm_enabled = resolve_push_metrics_enabled(config)
    ref_count = len(read_reference_records(cache_dir))

    if not enabled:
        return IngestionGateStatus(
            enabled=False,
            healthy=True,
            reason="ingestion gate disabled (librarian.ingestion_gate_enabled)",
            push_metrics_enabled=pm_enabled,
            reference_record_count=ref_count,
        )
    if not pm_enabled:
        return IngestionGateStatus(
            enabled=True,
            healthy=False,
            reason="push-metrics instrumentation is disabled -- precision "
            "cannot be measured",
            push_metrics_enabled=False,
            reference_record_count=ref_count,
        )
    if ref_count == 0:
        return IngestionGateStatus(
            enabled=True,
            healthy=False,
            reason="no reference-determination records yet -- precision is "
            "not computable",
            push_metrics_enabled=True,
            reference_record_count=0,
        )
    return IngestionGateStatus(
        enabled=True,
        healthy=True,
        reason="healthy",
        push_metrics_enabled=True,
        reference_record_count=ref_count,
    )


__all__ = ["IngestionGateStatus", "check_ingestion_gate"]
