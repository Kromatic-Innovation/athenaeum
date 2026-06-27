# SPDX-License-Identifier: Apache-2.0
"""Athenaeum configuration loader.

Reads ``athenaeum.yaml`` from the knowledge directory root to control
sidecar behavior: auto-recall toggle, search backend selection, etc.

Missing config or missing keys fall back to sensible defaults.
"""

from __future__ import annotations

import copy
import logging
import os
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
    # NOTE (issue #231): only seed a key here when this dict is its single
    # source of truth. Keys whose defaults live next to their consumer code
    # (librarian.cluster_threshold / cluster_output, contradiction.*) must
    # NOT be seeded: load_config() would merge the seed into every config,
    # the resolver would see it as "user-set", and the module-level code
    # default — plus any future change to it — becomes unreachable. That is
    # how the #187 resolver-cap raise (50 -> 250) was silently reverted to
    # 50 through the config path.
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

    # Merge user config over defaults (one level deep). User keys absent
    # from _DEFAULTS pass through untouched so module-level code defaults
    # (and their env > yaml > default precedence chains) stay live and
    # user-set sections like ``contradiction:`` or ``resolve:`` are not
    # dropped (issue #231). Deep-copy the seed so callers mutating nested
    # values (e.g. ``recall.extra_intake_roots``) cannot corrupt _DEFAULTS
    # process-wide.
    result: dict[str, Any] = copy.deepcopy(_DEFAULTS)
    for key, user_val in config.items():
        default_val = result.get(key)
        if isinstance(default_val, dict) and isinstance(user_val, dict):
            result[key] = {**default_val, **user_val}
        else:
            result[key] = user_val

    return result


def resolve_owner(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resolve the workspace owner identity from config (issue #263).

    The owner is the single canonical person the knowledge base belongs to.
    Athenaeum ships to PyPI, so the owner identity must NEVER be hardcoded in
    source — it comes entirely from ``athenaeum.yaml``::

        owner:
          uid: <owner-person-uid>                # canonical owner person UID
          google_contact: people/<contact-id>    # owner Google contact id
          aliases: ["<your_user_handle>", ...]   # optional name/handle aliases

    Aliases used for name matching must be FULL names (≥2 tokens); a
    single-token alias is ignored for name matching so it cannot absorb
    every stranger who shares that one name.

    Returns a normalized dict ``{"uid", "google_contact", "aliases"}`` when at
    least one usable field is set, else ``None``. A ``None`` return makes every
    owner-aware behavior (auto-bind, owner join keys, ``user_*`` routing) inert
    so the package works for any user with no owner configured. No default is
    seeded into ``_DEFAULTS`` (issue #231) — an unset owner is genuinely empty.
    """
    if not isinstance(config, dict):
        return None
    raw = config.get("owner")
    if not isinstance(raw, dict):
        return None

    def _clean_str(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    uid = _clean_str(raw.get("uid"))
    google_contact = _clean_str(raw.get("google_contact"))
    aliases_raw = raw.get("aliases")
    aliases: list[str] = []
    if isinstance(aliases_raw, list):
        aliases = [s for s in (_clean_str(a) for a in aliases_raw) if s]

    if not (uid or google_contact or aliases):
        return None  # blank/empty owner block is inert
    return {"uid": uid, "google_contact": google_contact, "aliases": aliases}


def resolve_model(
    knob: str,
    env_var: str,
    default: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Resolve a model id from env > yaml ``models.<knob>`` > code default.

    Issue #232. Mirrors :func:`athenaeum.librarian.librarian_max_api_calls`:
    the env var wins over the yaml key so an operator can swap a model for a
    single run without editing config, and the yaml key is read only when
    the operator actually set it — no seed in ``_DEFAULTS`` (issue #231).
    Non-string or blank yaml values fall through to *default*. The
    contradiction-resolver model is NOT routed through here; it stays at
    ``resolve.model`` (see :func:`athenaeum.resolutions._get_model`).
    """
    env = os.environ.get(env_var)
    if env:
        return env
    if isinstance(config, dict):
        models = config.get("models")
        if isinstance(models, dict):
            raw = models.get(knob)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return default


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

# Workspace owner identity (issue #263). Designates the single canonical
# person this knowledge base belongs to so the librarian keeps the owner a
# singleton instead of fragmenting across commit-authorship / footnote
# fragments and a parallel ``user_*`` alias family. ENTIRELY OPTIONAL — when
# unset, every owner-aware behavior (person auto-bind, owner dedup join keys,
# ``user_*`` reference routing) is inert. Set no personal identity in source;
# only the operator's athenaeum.yaml carries it.
#   uid: canonical owner person UID. Owner fragments auto-bind (merge) into
#     this page rather than persisting standalone.
#   google_contact: owner Google contact id; two person pages sharing it are
#     treated as duplicates.
#   aliases: optional name/handle aliases (display names, git author emails,
#     ``user_*`` handles). Pages whose name/handle/process-context author
#     matches an alias auto-bind to the owner. The ``user_*`` namespace is
#     always treated as an owner alias when an owner is configured. Name
#     aliases must be FULL names (>=2 tokens) — a single-token alias is
#     ignored for name matching so it cannot absorb every same-named stranger.
# owner:
#   uid: <owner-person-uid>
#   google_contact: people/<google-contact-id>
#   aliases:
#     - <your_user_handle>
#     - <Your Name>

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
#   issue #196). Higher = tighter clusters; 0.55 is tuned against the
#   voltaire/nanoclaw near-duplicate fixture.
# cluster_output: canonical JSONL output path (relative to knowledge
#   root). Each run also writes a timestamped sibling and atomically
#   replaces this path.
# max_files: per-run intake batch size — stop after processing this many
#   raw files (issue #232). Precedence: --max-files CLI flag, then
#   ATHENAEUM_MAX_FILES env, then this key, then 50.
# batch_mode: submit tier-2/tier-3 LLM calls via the Anthropic Messages
#   Batch API at a 50% token discount (issue #236). Latency-tolerant:
#   most batches finish within an hour, 24h worst case — intended for the
#   nightly run. Precedence: --batch-mode CLI flag, then
#   ATHENAEUM_BATCH_MODE env, then this key, then off.
# librarian:
#   cluster_threshold: 0.55
#   cluster_output: raw/_librarian-clusters.jsonl
#   max_files: 50
#   batch_mode: false

# Model selection (issue #232). Per knob: env var wins over the yaml key,
# which wins over the built-in default. Values are free-form model id
# strings passed to the Anthropic SDK.
# classify: Tier-2 classifier + C4 contradiction detector
#   (env: ATHENAEUM_CLASSIFY_MODEL).
# write: Tier-3 writer (env: ATHENAEUM_WRITE_MODEL).
# topic: recall query-topic extraction (env: ATHENAEUM_TOPIC_MODEL).
# The contradiction-resolver model is configured separately under
# ``resolve.model`` below (env: ATHENAEUM_RESOLVE_MODEL).
# models:
#   classify: claude-haiku-4-5-20251001
#   write: claude-sonnet-4-6
#   topic: claude-haiku-4-5-20251001

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
# Opus-backed resolver caps (issue #126).
# resolve_max_per_run: cap on resolver calls per ingest. Surplus contradictions
#   are escalated without a proposal (degraded mode). Default raised from
#   50 to 250 in issue #187. Env override: ATHENAEUM_RESOLVE_MAX_PER_RUN.
# contradiction:
#   cross_scope_mode: ancestor
#   cluster_size_cap: 25
#   similarity_threshold: 0.85
#   resolve_max_per_run: 250  # raised from 50 in #187
#   resolved_similarity_threshold: 0.83  # cosine threshold for decision-log matching (#211)
#   not_a_conflict_ttl_days: 0  # decay stale auto not_a_conflict (#251); 0 = off

# Contradiction resolver (issue #126). See docs/auto-resolve.md for the
# full knob set (auto_apply, auto_apply_threshold, full_body_token_cap).
# model: model used to propose a winner once Haiku flags a contradiction.
#   Defaults to claude-opus-4-7. Env override: ATHENAEUM_RESOLVE_MODEL.
# resolve:
#   model: claude-opus-4-7
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
