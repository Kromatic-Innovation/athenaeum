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
        # Issue #315 seam: the embedding model. Kept at the documented
        # default; incremental seeding (issue #348) is the one-time re-embed
        # that makes evaluating a stronger model cheap. Do NOT change this
        # default without a recorded eval — swapping it forces a full
        # re-embed of the whole corpus.
        "embedding_model": "all-MiniLM-L6-v2",
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


def _normalize_audience_roles(values: Any) -> set[str]:
    """Case-fold, trim, and drop empties from an iterable of role ids (#312)."""
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {v.strip().lower() for v in values if isinstance(v, str) and v.strip()}


def resolve_audience(
    config: dict[str, Any] | None,
    cli_value: str | None = None,
) -> set[str] | None:
    """Resolve the serve-time read-scope audience pin (issue #312).

    Returns the role set this ``serve`` / ``recall`` process is pinned to, or
    ``None`` for the owner / default caller (FULL access — every page,
    untagged included). ``None`` keeps existing single-user installs unchanged.

    Precedence follows the repo convention CLI > env > yaml > default:

    - ``cli_value`` — the ``--audience`` flag's comma-separated value.
    - ``ATHENAEUM_AUDIENCE`` — comma-separated env var.
    - ``serve.audience`` — a yaml list (or comma string).
    - ``None`` — owner, unfiltered.

    An explicitly EMPTY value at any tier (blank flag, ``ATHENAEUM_AUDIENCE=``,
    empty yaml list) resolves to ``None`` = owner: to RESTRICT a caller you must
    name at least one non-empty role. Role ids are opaque, case-folded, and
    whitespace-trimmed; athenaeum assigns them no meaning (they map onto the
    operator's external RBAC). No seed in ``_DEFAULTS`` (issue #231).
    """
    if cli_value is not None:
        roles = _normalize_audience_roles(cli_value.split(","))
        return roles or None

    env = os.environ.get("ATHENAEUM_AUDIENCE")
    if env is not None:
        roles = _normalize_audience_roles(env.split(","))
        return roles or None

    if isinstance(config, dict):
        serve_cfg = config.get("serve")
        if isinstance(serve_cfg, dict):
            raw = serve_cfg.get("audience")
            if isinstance(raw, str):
                roles = _normalize_audience_roles(raw.split(","))
                return roles or None
            roles = _normalize_audience_roles(raw)
            return roles or None
    return None


def resolve_google_contact_keys(config: dict[str, Any] | None) -> list[str]:
    """Resolve extra Google-contact dedup join-key field-names (issue #269).

    The dedupe merge always treats the generic ``google_contact`` frontmatter
    field as a join/merge key. Some operators carry the same Google contact id
    under additional namespace-specific field names (e.g. a separate field per
    Google Workspace account). Those EXTRA field names are operator-specific
    and must never be hardcoded in shipped source -- they come entirely from
    ``athenaeum.yaml``::

        dedupe:
          google_contact_keys:
            - google_contact_<namespace>

    Returns the configured list of extra field names (the base
    ``google_contact`` key is implicit and not included here). Returns an
    empty list when unset -- a fresh install dedups on the generic
    ``google_contact`` key only, with no personal namespace literal in source.
    No seed in ``_DEFAULTS`` (issue #231).
    """
    if not isinstance(config, dict):
        return []
    section = config.get("dedupe")
    if not isinstance(section, dict):
        return []
    raw = section.get("google_contact_keys")
    if not isinstance(raw, list):
        return []
    return [k.strip() for k in raw if isinstance(k, str) and k.strip()]


def resolve_retire(config: dict[str, Any] | None) -> bool:
    """Resolve the move-then-retire opt-out from yaml ``librarian.retire`` (#259).

    The move-then-retire pass (issue #261) moves non-contradictory raw
    auto-memory into the wiki and ``git rm``s it. It is DEFAULT-ON
    (owner-confirmed): only ``librarian.retire: false`` in ``athenaeum.yaml``
    turns it off, and the ``athenaeum run --no-retire`` CLI flag overrides to
    off at the call site. No seed in ``_DEFAULTS`` (issue #231) — the default
    lives here in code so it stays reachable. Non-bool yaml values fall through
    to the default (on).
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("retire")
            if isinstance(raw, bool):
                return raw
    return True


def resolve_push_after_run(config: dict[str, Any] | None) -> bool:
    """Resolve the post-run ``git push`` opt-in (issue #284).

    Closes the move-then-retire recovery gap: a scheduled nightly ``athenaeum
    run`` commits locally but, without this opt-in, never pushes — so the
    git-only retired-raw recovery story only holds on the machine that ran
    the librarian. With ``librarian.push_after_run: true`` (or the
    ``athenaeum run --push`` CLI override), the librarian invokes ``git push``
    after a successful run that produced at least one commit, using the
    operator's ambient git credentials. Default OFF: no push without explicit
    opt-in, and athenaeum itself handles no tokens/secrets. No seed in
    ``_DEFAULTS`` (issue #231). Non-bool yaml values fall through to off.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("push_after_run")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_push_remote(config: dict[str, Any] | None) -> str:
    """Resolve the post-run push remote from ``librarian.push_remote`` (#284).

    Defaults to ``origin`` — the conventional name the knowledge repo's
    remote will carry on every operator we ship to. A non-string or empty
    yaml value falls through to the default.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("push_remote")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "origin"


def resolve_push_branch(config: dict[str, Any] | None) -> str | None:
    """Resolve the post-run push branch from ``librarian.push_branch`` (#284).

    Returns ``None`` when unset (the librarian will push the knowledge repo's
    current branch, which is what nightly schedulers expect). A non-string
    or empty yaml value also returns ``None``.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("push_branch")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


# Default glob patterns for inherently-throwaway auto-memory scope dirs
# (issue #278). These live in the CONFIG LAYER on purpose: the discover /
# prune pipeline logic carries no host-specific scope literals, it only asks
# this resolver for the active glob set. An operator overrides or extends the
# set via ``athenaeum.yaml`` ``librarian.ephemeral_scopes``. Patterns are
# matched against the scope DIRECTORY NAME with :func:`fnmatch.fnmatch`.
# No seed in ``_DEFAULTS`` (issue #231) -- the default lives here so it stays
# reachable and a user-set key is treated as authoritative.
_DEFAULT_EPHEMERAL_SCOPES: tuple[str, ...] = (
    "*hestia-routine*",
    "*var-folders*",
    "*private-tmp*",
    # Anchored to the hyphenated throwaway form (`...-cctest-...`) on purpose:
    # a bare ``*cctest*`` would also catch a legitimately-named project dir
    # such as ``-Users-alice-Code-cctest-harness``.
    "*-cctest-*",
)


def resolve_ephemeral_scopes(config: dict[str, Any] | None) -> list[str]:
    """Resolve glob patterns for throwaway auto-memory scope dirs (issue #278).

    Returns the operator's ``librarian.ephemeral_scopes`` list when set
    (authoritative -- it REPLACES the defaults so an operator owns the full
    set), else the built-in :data:`_DEFAULT_EPHEMERAL_SCOPES`. A present-but-
    empty list disables scope-based ephemeral classification entirely.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict) and "ephemeral_scopes" in cfg:
            raw = cfg.get("ephemeral_scopes")
            if isinstance(raw, list):
                return [
                    str(g).strip() for g in raw if isinstance(g, str) and str(g).strip()
                ]
    return list(_DEFAULT_EPHEMERAL_SCOPES)


def resolve_operational_markers(config: dict[str, Any] | None) -> list[str]:
    """Resolve content markers for operational auto-memory families (issue #278).

    These are lower-cased substrings; the classifier requires a MULTI-SIGNAL
    match (>= 2 distinct markers present) before it will drop an intake on
    markers alone, so a single incidental word can never clobber a legit
    architecture note. DEFAULT-EMPTY: a fresh install never classifies on
    markers -- only the operator opts in via ``librarian.operational_markers``.
    No seed in ``_DEFAULTS`` (issue #231).
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("operational_markers")
            if isinstance(raw, list):
                return [
                    str(m).strip().lower()
                    for m in raw
                    if isinstance(m, str) and str(m).strip()
                ]
    return []


def resolve_min_cluster_cohesion(config: dict[str, Any] | None) -> float:
    """Resolve the cluster-cohesion floor from ``librarian.min_cluster_cohesion`` (#278).

    The cross-scope ``similarity`` clustering path over-clusters: single-linkage
    chains a coherent source doc together with vaguely-similar operational
    session-notes from many OTHER scopes into one LOW-COHESION blend page. The
    floor lets the merge pass refuse to materialize such a cluster into a
    durable ``wiki/auto-*.md`` page: a cluster whose ``cluster_centroid_score``
    (mean intra-cluster cosine) is strictly BELOW this floor AND which spans at
    least :func:`resolve_min_cluster_cohesion_scopes` distinct origin scopes is
    suppressed. Its raw members stay in place (not retired) for a coherent
    cluster to pick up later.

    DEFAULT 0.0 (OFF): athenaeum ships to PyPI, and the clean ~0.47 cohesion gap
    is specific to one corpus -- a baked-in non-zero floor could suppress
    legitimate clusters in a corpus with a different cohesion distribution.
    Operators opt in via ``athenaeum.yaml``. No seed in ``_DEFAULTS`` (#231) so
    the code default stays reachable. ``bool`` (an ``int`` subclass) and
    non-numeric / negative yaml values fall through to 0.0 (off).
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("min_cluster_cohesion")
            if raw is None or isinstance(raw, bool):
                return 0.0
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return 0.0
            if value > 0.0:
                return value
    return 0.0


def resolve_min_cluster_cohesion_scopes(config: dict[str, Any] | None) -> int:
    """Resolve the distinct-origin-scope floor for the cohesion gate (#278).

    The cohesion floor (:func:`resolve_min_cluster_cohesion`) only suppresses a
    cluster that ALSO spans at least this many distinct ``origin_scopes`` -- the
    cross-scope over-cluster signature. Gating on scope count too prevents
    false-suppression of a low-cohesion SINGLE-scope cluster (legitimately
    diverse intake from one project) or a small 2-3 scope coherent cluster.

    DEFAULT 4: observed over-clusters span 8-17 origin scopes while legitimate
    auto-memory pages span 1-3, so a floor of 4 sits in the clean margin. No
    seed in ``_DEFAULTS`` (#231). ``bool`` and non-int / ``< 2`` yaml values
    fall through to the default.
    """
    default = 4
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("min_cluster_cohesion_scopes")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 2:
                return raw
    return default


def resolve_lock_timeout(config: dict[str, Any] | None) -> float:
    """Resolve the default run-lock wait (seconds) from env > yaml > 0 (#309).

    The single-machine run lock (:mod:`athenaeum.runlock`) fails fast by default
    when another ``athenaeum run`` (or other mutating command) already holds
    ``<knowledge_root>/.athenaeum.lock``. Operators who prefer a mutating
    command to WAIT rather than exit — e.g. a manual run overlapping the nightly
    cron — can set a default block window::

        librarian:
          lock_timeout: 300   # seconds; 0 = fail-fast (default)

    Precedence: ``ATHENAEUM_LOCK_TIMEOUT`` env, then ``librarian.lock_timeout``
    yaml, then ``0`` (fail-fast). The per-command ``--wait`` flag overrides this.
    No seed in ``_DEFAULTS`` (#231) so the code default stays reachable. ``bool``
    and non-numeric / negative values fall through to 0.0.
    """
    env = os.environ.get("ATHENAEUM_LOCK_TIMEOUT")
    if env is not None:
        try:
            value = float(env)
        except ValueError:
            value = 0.0
        return value if value > 0 else 0.0
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("lock_timeout")
            if raw is None or isinstance(raw, bool):
                return 0.0
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return 0.0
            if value > 0:
                return value
    return 0.0


def _resolve_positive_int_knob(
    config: dict[str, Any] | None,
    key: str,
    env_var: str,
    default: int,
) -> int:
    """Resolve a positive-int ``librarian.<key>`` knob (env > yaml > default).

    Shared helper for the wiki page-size guardrails (issue #310). Mirrors
    :func:`athenaeum.clusters.resolve_rotation_retention`'s precedence and
    coercion contract: the ``env_var`` wins when it parses to a positive int,
    otherwise the yaml key is read, otherwise *default*. ``bool`` (an ``int``
    subclass) and non-int / ``<= 0`` values fall through so ``page_warn_bytes:
    yes`` cannot become ``1`` and a nonsensical zero/negative byte count cannot
    silently disable the guardrail. No seed in ``_DEFAULTS`` (issue #231).
    """
    env = os.environ.get(env_var)
    if env is not None:
        try:
            value = int(env)
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            return value

    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get(key)
            if raw is not None and not isinstance(raw, bool):
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and value > 0:
                    return value
    return default


def resolve_page_warn_bytes(config: dict[str, Any] | None) -> int:
    """Resolve the wiki-page soft-warn size threshold in bytes (issue #310).

    Precedence: ``ATHENAEUM_PAGE_WARN_BYTES`` env > ``librarian.page_warn_bytes``
    yaml > ``8192``. A page whose UTF-8 size (frontmatter + body) exceeds this
    is surfaced in ``status`` as a warn-level oversized page — a nudge to split,
    never a block. See :func:`_resolve_positive_int_knob` for the coercion
    contract.
    """
    return _resolve_positive_int_knob(
        config, "page_warn_bytes", "ATHENAEUM_PAGE_WARN_BYTES", 8192
    )


def resolve_page_flag_bytes(config: dict[str, Any] | None) -> int:
    """Resolve the wiki-page flag-for-split size threshold in bytes (issue #310).

    Precedence: ``ATHENAEUM_PAGE_FLAG_BYTES`` env > ``librarian.page_flag_bytes``
    yaml > ``16384``. A page over this is flagged more loudly (and logged during
    ``athenaeum run``) as one that should be broken into linked sub-entities.
    Kept comfortably below the tier-3 merge body cap so flagging precedes any
    hard merge-budget pressure. See :func:`_resolve_positive_int_knob`.
    """
    return _resolve_positive_int_knob(
        config, "page_flag_bytes", "ATHENAEUM_PAGE_FLAG_BYTES", 16384
    )


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


def resolve_screening(config: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Resolve intake-screening settings for ``remember()`` (issue #320).

    Returns ``{"medical": {"action", "access"}}``. This first slice screens
    only the ``medical`` category; the action is one of ``off`` (default) /
    ``label_restrict``. Precedence per the module convention (env > yaml >
    default, no seed in ``_DEFAULTS`` so the code default stays reachable):
    ``ATHENAEUM_SCREEN_MEDICAL`` env > ``screening.medical.action`` yaml >
    ``off``.

    Raises :class:`athenaeum.screening.ScreeningConfigError` on an invalid or
    unsupported setting (unknown action, ``drop`` on medical, or a bad access
    level) so a mis-configured operator gets a clear signal at serve time
    rather than a silent no-op. ``label_restrict`` is inert until content
    actually matches, so an ``off``/unset install never touches intake.
    """
    from athenaeum.screening import (
        _ACCESS_RANK,
        VALID_MEDICAL_ACTIONS,
        ScreeningConfigError,
    )

    action = "off"
    access = "personal"
    if isinstance(config, dict):
        screening = config.get("screening")
        if isinstance(screening, dict):
            medical = screening.get("medical")
            if isinstance(medical, dict):
                raw_action = medical.get("action")
                if isinstance(raw_action, str) and raw_action.strip():
                    action = raw_action.strip().lower()
                raw_access = medical.get("access")
                if isinstance(raw_access, str) and raw_access.strip():
                    access = raw_access.strip().lower()

    env = os.environ.get("ATHENAEUM_SCREEN_MEDICAL")
    if env is not None and env.strip():
        action = env.strip().lower()

    if action not in VALID_MEDICAL_ACTIONS:
        raise ScreeningConfigError(
            f"screening.medical.action={action!r} is invalid; expected one of "
            f"{VALID_MEDICAL_ACTIONS}."
        )
    if action == "drop":
        raise ScreeningConfigError(
            "screening.medical.action='drop' is not supported (medical is "
            "label-first); use label_restrict or off."
        )
    if access not in _ACCESS_RANK:
        raise ScreeningConfigError(
            f"screening.medical.access={access!r} is not a valid access level; "
            f"expected one of {tuple(_ACCESS_RANK)}."
        )

    return {"medical": {"action": action, "access": access}}


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

# Serve-time read-scope audience (issue #312). Pins the MCP `serve` process
# (and `athenaeum recall`) to a RESTRICTED read scope so a secondary agent or
# scheduled routine can recall operational knowledge but never PII /
# confidential / financial pages. Values are OPAQUE role/group ids the operator
# maps onto their external RBAC (a Microsoft AD group, an app role, a routine
# name). A restricted caller receives a page only when it is `access: open`
# (world-readable) OR carries an `audience:` list granting one of these roles;
# untagged and confidential/personal pages are withheld. UNSET / empty = owner
# = full access, so existing single-user installs are unchanged. Precedence:
# `serve --audience` flag > ATHENAEUM_AUDIENCE env > this key > owner.
# serve:
#   audience:
#     - operations
#     - voltaire

# Intake screening at remember() time (issue #320). The write-side complement
# to #312's read-time scoping: classifies sensitive raw intake and stamps a
# read-time `access:` label BEFORE the append-only write, so recall never
# surfaces regulated content to a restricted caller. UNSET / empty = no
# screening (existing installs unchanged). This first slice screens `medical`
# only. Action values: `label_restrict` (store but stamp `access:`, default
# `personal`), `off` (skip). `drop` is reserved for future pure-secret
# categories and is rejected as a config error here (medical is label-first,
# never dropped). Per-category action precedence:
# ATHENAEUM_SCREEN_MEDICAL env > this file > default (off).
# screening:
#   medical:
#     action: label_restrict   # label_restrict | off   (default: off)
#     access: personal         # access level stamped when action=label_restrict

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

# Person dedup join keys (issue #269). The merge always dedups on the
# generic ``google_contact`` field. Operators whose contacts carry the
# same Google contact id under additional namespace-specific field names
# can list those EXTRA field names here so the merge coalesces them too.
# Unset = dedup on ``google_contact`` only. Keep no personal contact
# namespace literal in source; only the operator's athenaeum.yaml carries it.
# dedupe:
#   google_contact_keys:
#     - google_contact_<namespace>

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
#   near-duplicate clustering fixture.
# cluster_output: canonical JSONL output path (relative to knowledge
#   root). Each run also writes a timestamped sibling and atomically
#   replaces this path.
# rotation_retention: number of timestamped cluster-report rotations to
#   keep; older ones are pruned after each run (issue #311). Rotations are
#   debugging artifacts, not recovery-critical (recovery is git-based).
#   Precedence: ATHENAEUM_ROTATION_RETENTION env, then this key, then 30.
#   0 (or negative) disables pruning (keep all).
# max_files: per-run intake batch size — stop after processing this many
#   raw files (issue #232). Precedence: --max-files CLI flag, then
#   ATHENAEUM_MAX_FILES env, then this key, then 50.
# batch_mode: submit tier-2/tier-3 LLM calls via the Anthropic Messages
#   Batch API at a 50% token discount (issue #236). Latency-tolerant:
#   most batches finish within an hour, 24h worst case — intended for the
#   nightly run. Precedence: --batch-mode CLI flag, then
#   ATHENAEUM_BATCH_MODE env, then this key, then off.
# retire: move-then-retire of raw auto-memory (issue #261). DEFAULT ON.
#   When on, `athenaeum run` MOVES non-contradictory raw/auto-memory facts
#   into their wiki entry and `git rm`s the raw (recovery is git-only).
#   Set false to disable; the --no-retire CLI flag overrides to off. See
#   README "Data lifecycle & upgrade impact".
# ephemeral_scopes: glob patterns (matched against the auto-memory scope
#   DIRECTORY NAME) for inherently-throwaway operational scopes whose
#   intake must NEVER become a durable wiki/auto-*.md page (issue #278).
#   A raw file in a matching scope -- or one carrying an explicit
#   `ephemeral: true` frontmatter flag -- is dropped before clustering.
#   Setting this key REPLACES the built-in defaults
#   (*hestia-routine*, *var-folders*, *private-tmp*, *-cctest-*); an empty
#   list disables scope-based dropping. Same set drives `athenaeum
#   auto-memory prune`.
# operational_markers: optional lower-cased content substrings for
#   operational families (issue #278). CONSERVATIVE: the classifier drops
#   an intake on markers ONLY when >= 2 distinct markers are present, so a
#   single incidental word never clobbers a legit note. DEFAULT-EMPTY.
#   Markers are SUBSTRING-matched: avoid <=3-char markers (e.g. "ci" would
#   match "decision"/"specific") -- prefer distinctive multi-word phrases.
# min_cluster_cohesion: cohesion floor that suppresses low-cohesion
#   cross-scope OVER-CLUSTERS (issue #278). A cluster whose
#   cluster_centroid_score (mean intra-cluster cosine) is strictly below
#   this value AND which spans >= min_cluster_cohesion_scopes distinct
#   origin scopes is NOT materialized into wiki/auto-*.md; its raw members
#   stay in place (not retired) for a coherent cluster to absorb later.
#   DEFAULT 0.0 (OFF) -- the ~0.47 gap that separates over-clusters
#   (<=0.46) from coherent pages (>=0.5) is corpus-specific, so a baked-in
#   floor could mis-suppress on a different corpus. Recommended opt-in for
#   the reference corpus: 0.47.
# min_cluster_cohesion_scopes: minimum distinct origin_scopes a cluster
#   must span for the cohesion floor to apply (issue #278). DEFAULT 4 --
#   observed over-clusters span 8-17 scopes, legitimate pages 1-3, so 4
#   sits in the clean margin and a low-cohesion single-/few-scope cluster
#   is never suppressed.
# page_warn_bytes: soft byte threshold above which a wiki entity page is
#   reported as a WARN-level oversized page in `athenaeum status` (#310).
#   Warn-only -- nothing is blocked or modified. Precedence:
#   ATHENAEUM_PAGE_WARN_BYTES env, then this key, then 8192. A long page
#   usually means poorly-factored knowledge to split into linked entities.
# page_flag_bytes: louder byte threshold above which a page is FLAGGED for
#   splitting -- surfaced in `status` and logged as a non-fatal WARNING
#   during `athenaeum run` (#310). Still warn-only (the tier-3 merge body
#   cap is separate and unchanged). Precedence: ATHENAEUM_PAGE_FLAG_BYTES
#   env, then this key, then 16384. Keep comfortably below the merge cap.
# librarian:
#   cluster_threshold: 0.55
#   cluster_output: raw/_librarian-clusters.jsonl
#   rotation_retention: 30
#   max_files: 50
#   batch_mode: false
#   retire: true
#   ephemeral_scopes:
#     - "*hestia-routine*"
#     - "*var-folders*"
#     - "*private-tmp*"
#     - "*-cctest-*"
#   operational_markers: []
#   min_cluster_cohesion: 0.0
#   min_cluster_cohesion_scopes: 4
#   page_warn_bytes: 8192
#   page_flag_bytes: 16384

# LLM provider selection (issue #330). Chooses the backend the librarian
# compile path (tiers, contradiction detector, resolver) talks to.
#   api (default): the Anthropic SDK. Requires ANTHROPIC_API_KEY; params
#     (incl. prompt caching and the Batch API) pass through unchanged.
#   claude-cli: the operator's ambient Claude Code SUBSCRIPTION login, via
#     `claude -p --system-prompt ... --output-format json`. No API key and no
#     credential handling (same ambient-auth stance as the git-push path).
#     cache_control is stripped; batch mode is NOT supported (loud error);
#     token counts are recorded but estimated_cost_usd reports $0
#     (subscription-covered). Precedence: env ATHENAEUM_LLM_PROVIDER > this
#     key > api. See docs/configuration.md "LLM provider selection".
# llm:
#   provider: api

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


def _resolve_glob_list(config: dict[str, Any] | None, key: str) -> list[str] | None:
    """Read a ``recall.<key>`` list of glob strings (issue #348).

    Returns ``None`` when unset (the default — index everything) so callers
    can pass it straight through to the search backend, which treats ``None``
    as "no scoping". Non-string / blank entries are dropped.
    """
    if not isinstance(config, dict):
        return None
    recall_cfg = config.get("recall") or {}
    raw = recall_cfg.get(key)
    if not isinstance(raw, list):
        return None
    globs = [g for g in raw if isinstance(g, str) and g.strip()]
    return globs or None


def resolve_index_globs(
    config: dict[str, Any] | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Resolve ``(include_globs, exclude_globs)`` for corpus scoping (issue #348).

    COULD-tier footprint/relevance knob. Default (unset) returns
    ``(None, None)`` — index everything — because the Apollo contact wikis
    are legitimate name-recall targets and must stay indexed by default.
    """
    return (
        _resolve_glob_list(config, "include_globs"),
        _resolve_glob_list(config, "exclude_globs"),
    )


def resolve_embedding_model(config: dict[str, Any] | None) -> str | None:
    """Resolve the configured vector embedding model (issue #315 seam).

    Returns ``None`` when unset so the VectorBackend uses its documented
    default (``all-MiniLM-L6-v2``) unchanged.
    """
    if not isinstance(config, dict):
        return None
    vector_cfg = config.get("vector") or {}
    if not isinstance(vector_cfg, dict):
        return None
    model = vector_cfg.get("embedding_model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None
