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
"""


def write_default_config(knowledge_root: Path) -> Path:
    """Write the default config file if it doesn't exist. Returns the path."""
    config_path = knowledge_root / "athenaeum.yaml"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_CONFIG_CONTENT, encoding="utf-8")
    return config_path
