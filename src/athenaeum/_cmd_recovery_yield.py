# SPDX-License-Identifier: Apache-2.0
"""``athenaeum recovery-yield`` — read-only readout of the recovery-yield
signal (issue athenaeum#1453).

Prints ONE JSON object on stdout and exits ``0`` unconditionally (see "Exit
code" below) — a read-only reading, no side effects, bounded output. Two
independent things are reported in the same object:

1. **The signal itself** — the persisted counters from
   :func:`athenaeum.recovery_yield.load_state` (``uncited``/``recovered``/
   ``write_cited``/``time_window``), the derived ``rate`` and ``within_threshold``
   (both ``null`` when ``uncited == 0`` — "no data", not "zero yield", see
   :mod:`athenaeum.recovery_yield`'s module docstring), the resolved
   ``threshold``, and the state's own ``updated`` timestamp. This half needs
   no corpus at all — it reads a small cache-dir sidecar.
2. **The corpus cross-check (AC4)** — a scan of the compiled wiki for
   ``type: auto-memory`` pages, counting how many carry an empty/absent
   ``sources`` list. This is what makes athenaeum#1452's AC2 ("most compiled
   auto-memory pages carry ``sources: []``") checkable by a READING instead
   of by manual inspection. Bounded to counts and a derived share — never a
   per-page list, so the output stays small regardless of corpus size. If
   ``--path`` names a knowledge dir with no ``wiki/`` (or none at all), the
   corpus fields report as ``null`` rather than erroring — a lane with no
   corpus mounted must still be able to read the signal half.

**Exit code stays 0 even when the signal is below threshold.** This readout
is consumed by hestia's generic ``athenaeum_readonly_subcommand`` evidence
probe (Kromatic-Innovation/hestia#2502), which treats a non-zero exit as a
FAILED reading, not as a successful reading of a bad number. The breach is
carried in ``within_threshold: false`` and in the intake-side
``log.warning`` (:mod:`athenaeum.intake`'s pass end), not in the exit code.
Non-zero is reserved for a genuine error (e.g. a malformed ``--path``
argument type, which argparse itself rejects before this function runs).

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring. This module owns presentation only: it reads
:mod:`athenaeum.recovery_yield`'s state/evaluation and
:mod:`athenaeum.auto_memory_prune`'s page discovery + :mod:`athenaeum.models`'s
frontmatter parser, and does no counting logic of its own beyond assembling
those into one JSON object.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from athenaeum.auto_memory_prune import AUTO_MEMORY_TYPE, discover_auto_pages
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config, resolve_cache_dir
from athenaeum.models import parse_frontmatter
from athenaeum.recovery_yield import STATE_NAME, evaluate, load_state, resolve_threshold


def add_recovery_yield_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``recovery-yield``."""
    parser = subparsers.add_parser(
        "recovery-yield",
        help="Read-only readout of the auto-memory origin-recovery yield "
        "signal (issue athenaeum#1453): recovered/uncited counters, basis "
        "split, resolved threshold, and (AC4) the corpus share of "
        "type:auto-memory pages with sources:[]. One JSON object on "
        "stdout, exit 0 always, no side effects.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory for the AC4 corpus scan (default: ~/knowledge). "
        "The signal fields are reported regardless of whether this exists.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory the signal sidecar lives under (default: ~/.cache/athenaeum)",
    )
    parser.set_defaults(func=cmd_recovery_yield)


def _scan_corpus(knowledge_root: Path) -> dict[str, object]:
    """AC4: count compiled ``type: auto-memory`` pages and their ``sources: []`` share.

    Reuses :func:`athenaeum.auto_memory_prune.discover_auto_pages` (the SAME
    ``wiki/auto-*.md`` discovery the prune driver uses) and
    :func:`athenaeum.models.parse_frontmatter` rather than hand-rolling a
    second YAML-frontmatter reader. Returns all-``None`` fields when
    ``wiki/`` does not exist — distinct from a real zero, which is a
    legitimate corpus reading.
    """
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        return {
            "auto_memory_pages": None,
            "auto_memory_pages_empty_sources": None,
            "auto_memory_empty_sources_share": None,
        }
    total = 0
    empty_sources = 0
    for path in discover_auto_pages(wiki_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if not isinstance(meta, dict) or meta.get("type") != AUTO_MEMORY_TYPE:
            continue
        total += 1
        sources = meta.get("sources")
        if not sources:
            empty_sources += 1
    share = (empty_sources / total) if total else None
    return {
        "auto_memory_pages": total,
        "auto_memory_pages_empty_sources": empty_sources,
        "auto_memory_empty_sources_share": share,
    }


def cmd_recovery_yield(args: argparse.Namespace) -> int:
    """Print one JSON document: the signal state + evaluation + AC4 corpus scan."""
    knowledge_root = args.path.expanduser().resolve()
    cache_dir = resolve_cache_dir(args.cache_dir).resolve()

    state = load_state(cache_dir)
    # ``load_config`` fails open to defaults for a missing/unreadable
    # athenaeum.yaml (and a missing knowledge_root entirely) — the signal
    # half of this readout must work with no corpus mounted at all.
    cfg = load_config(knowledge_root)
    threshold = resolve_threshold(cfg)
    evaluation = evaluate(state, threshold)

    payload: dict[str, object] = {
        "uncited": state["uncited"],
        "recovered": state["recovered"],
        "write_cited": state["write_cited"],
        "time_window": state["time_window"],
        "rate": evaluation.rate,
        "threshold": threshold,
        "within_threshold": evaluation.within_threshold,
        "verdict": evaluation.verdict,
        "updated": _load_updated(cache_dir),
    }
    payload.update(_scan_corpus(knowledge_root))

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _load_updated(cache_dir: Path) -> str | None:
    """Read the raw ``updated`` stamp straight from the sidecar, if present.

    :func:`athenaeum.recovery_yield.load_state` deliberately does not surface
    the timestamp (it is presentation, not state the evaluator needs) — this
    reads it the same fail-open way, missing/corrupt -> ``None``.
    """
    path = cache_dir / STATE_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    updated = data.get("updated")
    return updated if isinstance(updated, str) else None
