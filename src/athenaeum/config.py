# SPDX-License-Identifier: Apache-2.0
"""Athenaeum configuration loader.

Reads ``athenaeum.yaml`` from the knowledge directory root to control
sidecar behavior: auto-recall toggle, search backend selection, etc.

Missing config or missing keys fall back to sensible defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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
    Missing directories are silently dropped — a half-initialized
    knowledge base (no ``raw/auto-memory`` yet) should not break index
    rebuild. Returns an empty list when no extras are configured.
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
    return resolved
