# SPDX-License-Identifier: Apache-2.0
"""Athenaeum configuration loader.

Reads ``athenaeum.yaml`` from the knowledge directory root to control
sidecar behavior: auto-recall toggle, search backend selection, etc.

Missing config or missing keys fall back to sensible defaults.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "auto_recall": True,
    "search_backend": "fts5",
    "vector": {
        "provider": "chromadb",
        "collection": "wiki",
    },
    "recall": {
        # Extra intake roots scanned recursively alongside the wiki. Paths
        # are resolved relative to ``knowledge_root``. The default points
        # at the agent-auto-memory intake tree so that raw memories
        # written via ``remember`` (and per-scope agent-written notes)
        # show up in recall without separate plumbing. Set to an empty
        # list to restrict recall to the compiled wiki only.
        "extra_intake_roots": ["raw/auto-memory"],
    },
    "librarian": {
        # Cluster pass (issue #196, C2). ``cluster_threshold`` is the
        # cosine-similarity cutoff used by single-linkage clustering over
        # auto-memory files. Empirically tuned against the voltaire
        # fixture (5 files sharing a nanoclaw/voltaire token and one typo
        # clone land in a single cluster at 0.6 without dragging in
        # unrelated singletons). ``cluster_output`` is the canonical
        # JSONL report path (resolved relative to the knowledge root);
        # every run rotates a timestamped sibling and atomically replaces
        # the canonical file.
        "cluster_threshold": 0.55,
        "cluster_output": "raw/_librarian-clusters.jsonl",
    },
    "contradiction": {
        # Cross-scope contradiction detection (issue #125, #81-A).
        # Modes: off | ancestor | similarity | both. Default ``ancestor``
        # pools each per-scope cluster with members from any ancestor scope
        # (e.g. ``-Users-tristankromer-Code-foo``'s pool also includes
        # ``-Users-tristankromer-Code``). ``similarity`` runs a cosine
        # cross-product over the recall index (raw + wiki). ``both`` runs
        # ancestor pooling AND the similarity sweep. Env override:
        # ``ATHENAEUM_CROSS_SCOPE_MODE``.
        "cross_scope_mode": "ancestor",
        # Hard cap on pooled-cluster size before splitting into newest-first
        # chunks for the detector.
        "cluster_size_cap": 25,
        # Cosine similarity threshold for the cross-scope sweep.
        "similarity_threshold": 0.85,
        # Issue #126: Opus-backed resolver between Haiku detection and
        # tier4_escalate. ``resolve_model`` overrides the default Opus
        # model so users can pick a cheaper resolver. Env var
        # ``ATHENAEUM_RESOLVE_MODEL`` wins over this setting.
        "resolve_model": "claude-opus-4-7",
        # Per-run cap on Opus calls. Surplus contradictions get escalated
        # without a proposal (degraded mode). Env var
        # ``ATHENAEUM_RESOLVE_MAX_PER_RUN`` wins over this setting.
        "resolve_max_per_run": 50,
    },
}


def load_config(knowledge_root: Path | None = None) -> dict[str, Any]:
    """Load athenaeum config from *knowledge_root*/athenaeum.yaml.

    Falls back to ``~/knowledge/athenaeum.yaml`` if *knowledge_root* is None.
    Returns defaults merged with any values found in the file.
    """
    if knowledge_root is None:
        knowledge_root = Path.home() / "knowledge"

    config_path = knowledge_root / "athenaeum.yaml"
    config: dict[str, Any] = {}

    if config_path.is_file():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config = raw
        except (yaml.YAMLError, OSError):
            pass  # fall back to defaults

    # Merge with defaults (one level deep)
    result = dict(_DEFAULTS)
    for key, default_val in _DEFAULTS.items():
        if key in config:
            if isinstance(default_val, dict) and isinstance(config[key], dict):
                result[key] = {**default_val, **config[key]}
            else:
                result[key] = config[key]

    return result


_DEFAULT_CONFIG_CONTENT = """\
# Athenaeum sidecar configuration
# See https://github.com/Kromatic-Innovation/athenaeum for docs.

# Toggle per-turn auto-recall (UserPromptSubmit hook).
# When false, the hook exits immediately — recall is only via explicit MCP tool calls.
auto_recall: true

# Search backend for recall queries: "fts5" (keyword) or "vector" (semantic).
# fts5: SQLite FTS5 with BM25 ranking and porter stemming. No extra dependencies.
# vector: Chromadb with local embeddings. Requires: pip install athenaeum[vector]
search_backend: fts5

# Vector backend settings (only used when search_backend: vector)
# vector:
#   provider: chromadb
#   collection: wiki

# Recall configuration.
# extra_intake_roots: additional directories (resolved relative to the
# knowledge root) that the index build will scan recursively alongside
# wiki/. Intended for agent-written raw memory trees. Set to [] to
# disable and restrict recall to the compiled wiki only.
# recall:
#   extra_intake_roots:
#     - raw/auto-memory

# Librarian pipeline configuration.
# cluster_threshold: cosine cutoff for auto-memory clustering (C2,
#   issue #196). Higher = tighter clusters; 0.6 is tuned against the
#   voltaire/nanoclaw near-duplicate fixture.
# cluster_output: canonical JSONL output path (relative to knowledge
#   root). Each run also writes a timestamped sibling and atomically
#   replaces this path.
# librarian:
#   cluster_threshold: 0.55
#   cluster_output: raw/_librarian-clusters.jsonl

# Cross-scope contradiction detection (issue #125).
# cross_scope_mode: off | ancestor (default) | similarity | both.
#   - off: per-scope cluster only.
#   - ancestor: pool each cluster with ancestor scopes (-Users-foo-bar
#     also includes -Users-foo, -Users) before running the detector.
#   - similarity: per-scope pass + cosine sweep over raw + wiki.
#   - both: ancestor pooling THEN similarity sweep.
# cluster_size_cap: pooled-cluster size cap; oversized pools are split
#   into newest-first chunks before detection.
# similarity_threshold: cosine cutoff for the cross-scope sweep.
# Env override: ATHENAEUM_CROSS_SCOPE_MODE.
# Opus-backed resolver (issue #126).
# resolve_model: model used to propose a winner once Haiku flags a contradiction.
#   Defaults to claude-opus-4-7. Env override: ATHENAEUM_RESOLVE_MODEL.
# resolve_max_per_run: cap on resolver calls per ingest. Surplus contradictions
#   are escalated without a proposal (degraded mode).
#   Env override: ATHENAEUM_RESOLVE_MAX_PER_RUN.
# contradiction:
#   cross_scope_mode: ancestor
#   cluster_size_cap: 25
#   similarity_threshold: 0.85
#   resolve_model: claude-opus-4-7
#   resolve_max_per_run: 50
"""


def write_default_config(knowledge_root: Path) -> Path:
    """Write the default config file if it doesn't exist. Returns the path."""
    config_path = knowledge_root / "athenaeum.yaml"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_CONFIG_CONTENT, encoding="utf-8")
    return config_path


def resolve_extra_intake_roots(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> list[Path]:
    """Resolve configured extra intake roots to absolute :class:`Path` values.

    Values under ``recall.extra_intake_roots`` that are relative are
    resolved against ``knowledge_root``; absolute paths are passed through.
    Missing directories are dropped (with a warning) — a half-initialized
    knowledge base (no ``raw/auto-memory`` yet) should not break index
    rebuild, but operators should see a diagnostic when a configured
    root is typo'd or unmounted. Returns an empty list when no extras
    are configured.
    """
    if config is None:
        config = load_config(knowledge_root)

    recall_cfg = config.get("recall") or {}
    raw_roots = recall_cfg.get("extra_intake_roots") or []
    if not isinstance(raw_roots, list):
        return []

    resolved: list[Path] = []
    for item in raw_roots:
        if not isinstance(item, str) or not item.strip():
            continue
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = knowledge_root / candidate
        candidate = candidate.expanduser()
        if candidate.is_dir():
            resolved.append(candidate.resolve())
        else:
            logger.warning("extra_intake_root not found: %s", candidate)
    return resolved
