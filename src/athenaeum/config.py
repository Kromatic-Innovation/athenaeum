# SPDX-License-Identifier: Apache-2.0
"""Athenaeum configuration resolution layer — L2.

Contract: this module is THE resolution layer for every operator-facing
knob in athenaeum (~50 ``resolve_*`` functions and counting) — not just
"sidecar behavior" as an early version of this docstring undersold it.
Every knob, without exception, resolves through the SAME precedence rule:
**env var > ``athenaeum.yaml`` > code default.** ``load_config`` reads
``athenaeum.yaml`` from the knowledge directory root; each ``resolve_*``
function then layers the env override and the default on top of whatever
``load_config`` returned (or on ``None``, for callers that skip disk
entirely). Missing config, missing keys, and a missing file all fall back
to sensible defaults rather than raising — the small set of deliberate
fail-loud exceptions is documented below.

FACTORING RULE (durable — enforce on every new knob): **a new knob is not
done until it is added here as a ``resolve_*`` function AND documented in
``docs/configuration.md``.** This module is the only place precedence is
implemented; do not hand-roll env/yaml lookups in another module, and do
not let a knob land without its docs entry — the resolver function and the
docs page are two halves of one change.

Layering (L2): sits above L1 (models/schemas/provenance/registry/
authority/storage) and L0 primitives, and below L3 (search/pii/
fingerprint/...) and everything above. May import L0/L1 freely. It must
NOT import L3+ at module level — screening is L3 and screening does not
depend on config at import time — but ``resolve_screening`` (~line 1414)
does a FUNCTION-LOCAL ``from athenaeum.screening import (...)`` purely to
reuse validation constants/exception types (``_ACCESS_RANK``,
``VALID_MEDICAL_ACTIONS``, ``ScreeningConfigError``) so this module doesn't
duplicate them. That import is a deliberate, one-way "reach up" confined
to a single function body — it is not a real import cycle (screening never
imports config back) and must stay deferred so `import athenaeum.config`
itself never pulls in L3.

Malformed-env-value policy (issue athenaeum#519/#528)
--------------------------------------------
Every numeric env override (``ATHENAEUM_*``) is read through
:func:`_env_number`, which enforces ONE policy for a value that fails to
parse: **log a WARNING naming the variable, then fall back** to yaml/default.
This replaces four incompatible hand-rolled behaviours that used to depend on
which knob you mistyped — silent fall-through, silent return-default, a silent
hard-zero, and a hard crash — so the same typo now produces the same,
predictable, *visible* outcome everywhere.

The one deliberate exception is a small, enumerated set of **fail-loud** knobs
that raise :class:`ValueError` on a bad value BY DESIGN, because silently
falling back would hide an operator error with a safety cost:
``ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD`` (and its per-action siblings, in
:mod:`athenaeum.resolutions`) and ``ATHENAEUM_SCREEN_MEDICAL``. These are the
documented exceptions to the WARN-and-fall-back rule, not accidental
divergence. Separately, out-of-*range* validation (e.g. a ``[0.0, 1.0]``
bound, or a ``> 0`` guardrail) is a per-knob concern layered on top of the
shared parse and is unaffected by this policy.
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timezone, tzinfo
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from athenaeum.models import default_model_rates, model_has_price

logger = logging.getLogger(__name__)

# The single source of truth for the default knowledge-directory root (issue
# athenaeum#537). This is the *tilde template* form used as the CLI ``--path`` default
# and as the fallback in resolver helpers; every consumer expands it via
# ``.expanduser()`` (directly, or through the argparse value it seeds). Keeping
# it here — rather than as 38 copies of ``Path("~/knowledge")`` scattered across
# the ``_cmd_*`` modules — means relocating the default is a one-line edit.
# ``librarian.py`` derives its own pre-expanded runtime default from this
# constant, so the ``~/knowledge`` literal lives in exactly one module.
DEFAULT_KNOWLEDGE_ROOT = Path("~/knowledge")

_T = TypeVar("_T")


def _env_number(name: str, cast: Callable[[str], _T]) -> _T | None:
    """Parse env var *name* via *cast*, or ``None`` if unset **or malformed**.

    The one place numeric env overrides are read (issue athenaeum#519/#524). A ``None``
    return means "no usable env value — fall through to yaml/default"; a
    non-``None`` return is the parsed override and is **authoritative over
    yaml** (M1), including a parsed ``0``.

    On a malformed value it logs a WARNING naming the variable (M2) and returns
    ``None`` instead of silently swallowing the typo — every numeric knob
    previously resolved a mistyped ``ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY=0.85x``
    to its default with no signal at any level.

    This settles the malformed-value policy for numeric knobs — WARN and fall
    back — that athenaeum#528 sweeps across the remaining hand-rolled resolvers.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring malformed %s=%r (expected %s); falling back to yaml/default.",
            name,
            raw,
            getattr(cast, "__name__", str(cast)),
        )
        return None


# Issue athenaeum#519/#521 (H9 + L3): the single canonical default cache-dir location
# and the single resolver honouring the ``ATHENAEUM_CACHE_DIR`` override.
#
# Before this, ``~/.cache/athenaeum`` was constructed by hand at ~13 sites
# across 8 modules — some consulting the env var first, most not — so "honours
# ``ATHENAEUM_CACHE_DIR``" was a per-site property new code got wrong by
# default (H9: ``serve`` wrote/read the wrong index). Every site now routes
# through :data:`DEFAULT_CACHE_DIR` / :func:`resolve_cache_dir`; a guard test
# asserts the literal is constructed nowhere else.
DEFAULT_CACHE_DIR: Path = Path("~/.cache/athenaeum")


def resolve_cache_dir(cache_dir: Path | None = None) -> Path:
    """Resolve the athenaeum cache dir: ``arg > ATHENAEUM_CACHE_DIR env > default``.

    Returns an ``expanduser()``-ed path (``~`` expanded); callers that need a
    fully-resolved absolute path call ``.resolve()`` on the result. This is the
    one place ``~/.cache/athenaeum`` is defaulted and the env override applied.
    """
    if cache_dir is not None:
        return Path(cache_dir).expanduser()
    env = os.environ.get("ATHENAEUM_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return DEFAULT_CACHE_DIR.expanduser()


_DEFAULTS: dict[str, Any] = {
    "auto_recall": True,
    "search_backend": "fts5",
    "vector": {
        "provider": "chromadb",
        # Issue athenaeum#315 seam: the embedding model. Kept at the documented
        # default; incremental seeding (issue athenaeum#348) is the one-time re-embed
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
    # NOTE (issue athenaeum#231): only seed a key here when this dict is its single
    # source of truth. Keys whose defaults live next to their consumer code
    # (librarian.cluster_threshold / cluster_output, contradiction.*) must
    # NOT be seeded: load_config() would merge the seed into every config,
    # the resolver would see it as "user-set", and the module-level code
    # default — plus any future change to it — becomes unreachable. That is
    # how the athenaeum#187 resolver-cap raise (50 -> 250) was silently reverted to
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
    # dropped (issue athenaeum#231). Deep-copy the seed so callers mutating nested
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
    """Resolve the workspace owner identity from config (issue athenaeum#263).

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
    seeded into ``_DEFAULTS`` (issue athenaeum#231) — an unset owner is genuinely empty.
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


def resolve_owner_asserter(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the owner's OIDC ``asserter`` identity block, or ``None`` (athenaeum#328).

    Read from ``owner.asserter`` in ``athenaeum.yaml``. Used by
    ``repair --backfill-sources`` to stamp ``on_behalf_of`` on a
    ``user-stated`` upgrade WHEN a durable identity is configured. Transcripts
    carry no OIDC identity, so an unset block leaves ``on_behalf_of`` absent
    (the athenaeum#327 fallback). Returns the raw dict unchanged for
    :func:`athenaeum.models.asserter_identity_key` to key on; a non-dict or
    empty block is inert.
    """
    if not isinstance(config, dict):
        return None
    owner = config.get("owner")
    if not isinstance(owner, dict):
        return None
    asserter = owner.get("asserter")
    if isinstance(asserter, dict) and asserter:
        return asserter
    return None


def _normalize_audience_roles(values: Any) -> set[str]:
    """Case-fold, trim, and drop empties from an iterable of role ids (athenaeum#312)."""
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {v.strip().lower() for v in values if isinstance(v, str) and v.strip()}


def resolve_audience(
    config: dict[str, Any] | None,
    cli_value: str | None = None,
) -> set[str] | None:
    """Resolve the serve-time read-scope audience pin (issue athenaeum#312).

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
    operator's external RBAC). No seed in ``_DEFAULTS`` (issue athenaeum#231).
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
    """Resolve extra Google-contact dedup join-key field-names (issue athenaeum#269).

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
    No seed in ``_DEFAULTS`` (issue athenaeum#231).
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
    """Resolve the move-then-retire opt-out from yaml ``librarian.retire`` (athenaeum#259).

    The move-then-retire pass (issue athenaeum#261) moves non-contradictory raw
    auto-memory into the wiki and ``git rm``s it. It is DEFAULT-ON
    (owner-confirmed): only ``librarian.retire: false`` in ``athenaeum.yaml``
    turns it off, and the ``athenaeum run --no-retire`` CLI flag overrides to
    off at the call site. No seed in ``_DEFAULTS`` (issue athenaeum#231) — the default
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
    """Resolve the post-run ``git push`` opt-in (issue athenaeum#284).

    Closes the move-then-retire recovery gap: a scheduled nightly ``athenaeum
    run`` commits locally but, without this opt-in, never pushes — so the
    git-only retired-raw recovery story only holds on the machine that ran
    the librarian. With ``librarian.push_after_run: true`` (or the
    ``athenaeum run --push`` CLI override), the librarian invokes ``git push``
    after a successful run that produced at least one commit, using the
    operator's ambient git credentials. Default OFF: no push without explicit
    opt-in, and athenaeum itself handles no tokens/secrets. No seed in
    ``_DEFAULTS`` (issue athenaeum#231). Non-bool yaml values fall through to off.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("push_after_run")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_push_remote(config: dict[str, Any] | None) -> str:
    """Resolve the post-run push remote from ``librarian.push_remote`` (athenaeum#284).

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
    """Resolve the post-run push branch from ``librarian.push_branch`` (athenaeum#284).

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


def resolve_pull_before_run(config: dict[str, Any] | None) -> bool:
    """Resolve the pre-run ``git pull`` opt-in (issue athenaeum#399).

    Symmetric to :func:`resolve_push_after_run` (athenaeum#284): with
    ``librarian.pull_before_run: true`` (or the ``athenaeum run --pull`` CLI
    override), the librarian invokes ``git pull --ff-only --autostash`` on
    the knowledge repo BEFORE the run starts, so the run compiles against
    origin's latest instead of a possibly-stale local checkout. Default OFF:
    a fresh install must never side-effect an operator's git remote, and
    athenaeum itself handles no credentials — pulls (like pushes) rely
    entirely on the operator's ambient git auth (credential helper / SSH).

    There is no shipped nightly cron wrapper in this repo, so pull and push
    both stay independently opt-in via yaml/CLI rather than being bundled
    into an assumed scheduler script. An operator wanting full bidirectional
    sync sets both ``pull_before_run: true`` and ``push_after_run: true`` in
    ``athenaeum.yaml``. Non-bool yaml values fall through to the default
    (off).
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("pull_before_run")
            if isinstance(raw, bool):
                return raw
    return False


# Default glob patterns for inherently-throwaway auto-memory scope dirs
# (issue athenaeum#278). These live in the CONFIG LAYER on purpose: the discover /
# prune pipeline logic carries no host-specific scope literals, it only asks
# this resolver for the active glob set. An operator overrides or extends the
# set via ``athenaeum.yaml`` ``librarian.ephemeral_scopes``. Patterns are
# matched against the scope DIRECTORY NAME with :func:`fnmatch.fnmatch`.
# No seed in ``_DEFAULTS`` (issue athenaeum#231) -- the default lives here so it stays
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
    """Resolve glob patterns for throwaway auto-memory scope dirs (issue athenaeum#278).

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
                return [str(g).strip() for g in raw if isinstance(g, str) and str(g).strip()]
    return list(_DEFAULT_EPHEMERAL_SCOPES)


def resolve_operational_markers(config: dict[str, Any] | None) -> list[str]:
    """Resolve content markers for operational auto-memory families (issue athenaeum#278).

    These are lower-cased substrings; the classifier requires a MULTI-SIGNAL
    match (>= 2 distinct markers present) before it will drop an intake on
    markers alone, so a single incidental word can never clobber a legit
    architecture note. DEFAULT-EMPTY: a fresh install never classifies on
    markers -- only the operator opts in via ``librarian.operational_markers``.
    No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("operational_markers")
            if isinstance(raw, list):
                return [
                    str(m).strip().lower() for m in raw if isinstance(m, str) and str(m).strip()
                ]
    return []


def resolve_non_intake_sources(config: dict[str, Any] | None) -> set[str]:
    """Resolve `raw/<source>/` dirs excluded from entity intake (issue athenaeum#843).

    A source directory named here is skipped WHOLE by
    :func:`athenaeum.intake.discover_raw_files` — none of its files become
    entity intake. This is for a tool that writes its own OPERATIONAL
    artifacts into ``raw/<source>/`` (the same tree ``remember()``-authored
    content uses): action logs, launchd logs, state dumps. Those match the
    ``*.md`` / ``*.jsonl`` glob and are not correction-batch envelopes, so
    without this knob they enter ``tier2_classify`` → ``tier3_write`` as if
    they were memory content, and a multi-megabyte log gets read whole and
    handed to the classifier.

    Generalizes the hardcoded ``source == "answers"`` skip (issue
    athenaeum#414), which stays as-is: this is a SECOND, operator-controlled
    mechanism alongside it, so the next occurrence is a config change rather
    than another patch to ``discover_raw_files``.

    Matched against ``source_dir.name`` exactly (no globbing, no case folding
    — a directory name on disk is what it is). DEFAULT-EMPTY: a fresh install
    excludes nothing, so unconfigured discovery is byte-identical to
    pre-athenaeum#843 behaviour. No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("non_intake_sources")
            if isinstance(raw, list):
                return {s.strip() for s in raw if isinstance(s, str) and s.strip()}
    return set()


def resolve_preserved_log_dir(config: dict[str, Any] | None) -> str | None:
    """Resolve the preserved-log area from ``librarian.preserved_log_dir`` (issue athenaeum#837).

    A **preserved log is a source document, not intake.** The operator names a
    folder under the knowledge root here — e.g. ``preserved_log_dir: logs`` —
    and declares that its contents are artifacts to be kept whole, referenced
    as provenance, and never compiled into wiki prose. The `preserve`
    disposition (:mod:`athenaeum.rules`) MOVES a matching raw file into it.

    Why a directory OUTSIDE ``raw/`` rather than another flag on a file that
    stays put. `retain` (athenaeum#903) already covers "mark it exempt where it
    lies", and that is the weaker guarantee: the file remains in the intake
    tree, so every future mechanism that walks ``raw/`` must remember to
    consult the exempt manifest, and a manifest that fails open (by design —
    see :mod:`athenaeum.compiled_exempt`) silently re-offers it. Moving the
    file makes the guarantee structural instead of advisory: a preserved log
    is not skipped by discovery, it is *not discoverable*, because
    :func:`athenaeum.intake.discover_raw_files` only ever walks ``raw/``.

    Returns the operator's value as a **relative POSIX path string**, or
    ``None`` when unset or unusable. Rejected (with a ``log.warning``, never a
    raise — an unusable value must not take the nightly run down): an absolute
    path, and any value escaping the knowledge root via ``..``.

    This key is deliberately scoped to the LOCAL, in-repo case only — an
    operator who needs a preserved artifact to land outside the knowledge
    root uses ``librarian.preserved_log_adapter`` instead (see
    :func:`resolve_preserved_log_adapter`, issue athenaeum#1132), which routes
    through a registered :mod:`athenaeum.storage` adapter whose resolved root
    may be absolute. Before athenaeum#1132 the absolute/escaping rejection here was
    reasoned as a blanket prohibition — "outside the repo it is neither
    versioned nor recoverable" — but S3 (issue athenaeum#978) replaced the old
    ``.git``-existence gate with a declared, per-store
    ``Store.capabilities.versioned``/recoverability capability that a
    non-git surface can satisfy on its own terms. "Outside the repo" is
    therefore no longer categorically unrecoverable, it is a property of
    whichever store an artifact is routed through — checked there, not
    assumed impossible here. This resolver still refuses an absolute or
    escaping value, but now simply because THIS key's contract is "a
    directory under the knowledge root": a scoping rule, not a
    recoverability argument.

    DEFAULT-NONE: a fresh install has no preserved area, so a `preserve` rule
    is inert until an operator configures one (the feature is opt-in twice
    over — the area AND a rule). No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    if not isinstance(config, dict):
        return None
    cfg = config.get("librarian")
    if not isinstance(cfg, dict):
        return None
    raw = cfg.get("preserved_log_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw.strip()
    if PurePosixPath(candidate).is_absolute() or Path(candidate).is_absolute():
        logger.warning(
            "librarian.preserved_log_dir %r is an absolute path — this key is "
            "scoped to the local, in-repo preserved area; use "
            "librarian.preserved_log_adapter (issue athenaeum#1132) to route "
            "outside the knowledge root. Ignoring (issue athenaeum#837).",
            candidate,
        )
        return None
    parts = PurePosixPath(candidate).parts
    if any(p == ".." for p in parts):
        logger.warning(
            "librarian.preserved_log_dir %r escapes the knowledge root via "
            "'..' — ignoring (issue athenaeum#837).",
            candidate,
        )
        return None
    normalized = PurePosixPath(candidate).as_posix().strip("/")
    return normalized or None


def resolve_preserved_log_adapter(config: dict[str, Any] | None) -> str | None:
    """Resolve the preserved-log routing adapter from
    ``librarian.preserved_log_adapter`` (issue athenaeum#1132).

    Names a registered ``storage.adapters.<name>`` (see
    :mod:`athenaeum.storage`) that the `preserve` disposition
    (:mod:`athenaeum.rules`) should route through INSTEAD of the local,
    in-repo area :func:`resolve_preserved_log_dir` names — the seam that lets
    a preserved log land outside the knowledge git repo (a different
    filesystem, a mounted corpus, an operator-defined adapter whose
    ``surface_root`` is absolute), which ``preserved_log_dir`` structurally
    cannot do. When both keys are set, the adapter wins and the rules engine
    logs a warning that it shadows ``preserved_log_dir`` — see
    :mod:`athenaeum.rules`'s `preserve` branch.

    This resolver only reads the operator's raw string; it does **not**
    validate that the named adapter actually exists — that check belongs to
    :mod:`athenaeum.storage` (:func:`athenaeum.storage.available_adapters`),
    which this L2 config module must not import (it would cycle back:
    ``storage.py`` already imports ``config.py`` to resolve its own adapter
    definitions). The caller resolving an unknown adapter name raises
    :class:`athenaeum.storage.StorageConfigError` loudly rather than
    silently falling back to the local directory.

    Returns ``None`` when unset or blank — DEFAULT-NONE, matching
    :func:`resolve_preserved_log_dir`: a fresh install has no adapter
    override configured. No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    if not isinstance(config, dict):
        return None
    cfg = config.get("librarian")
    if not isinstance(cfg, dict):
        return None
    raw = cfg.get("preserved_log_adapter")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def resolve_min_cluster_cohesion(config: dict[str, Any] | None) -> float:
    """Resolve the cluster-cohesion floor from ``librarian.min_cluster_cohesion`` (athenaeum#278).

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
    Operators opt in via ``athenaeum.yaml``. No seed in ``_DEFAULTS`` (athenaeum#231) so
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
    """Resolve the distinct-origin-scope floor for the cohesion gate (athenaeum#278).

    The cohesion floor (:func:`resolve_min_cluster_cohesion`) only suppresses a
    cluster that ALSO spans at least this many distinct ``origin_scopes`` -- the
    cross-scope over-cluster signature. Gating on scope count too prevents
    false-suppression of a low-cohesion SINGLE-scope cluster (legitimately
    diverse intake from one project) or a small 2-3 scope coherent cluster.

    DEFAULT 4: observed over-clusters span 8-17 origin scopes while legitimate
    auto-memory pages span 1-3, so a floor of 4 sits in the clean margin. No
    seed in ``_DEFAULTS`` (athenaeum#231). ``bool`` and non-int / ``< 2`` yaml values
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


def resolve_max_merge_sources(config: dict[str, Any] | None) -> int:
    """Resolve the resolver merge-proposal source-count cap (athenaeum#400).

    The resolver's merge-proposal path (``propose_merge`` → ``_pending_merges.md``)
    had no size cap, so a degenerate over-cluster — 1,600+ source memories folded
    into one proposed page at ~0.33 confidence — was emitted (and re-emitted every
    run) to the human queue. A merge above this many sources is by definition not
    the pairwise / small-group refinement a merge proposal is for, so it is
    suppressed before it reaches ``_pending_merges.md`` (neither proposed nor
    escalated as a pending question).

    DEFAULT 5 (active) — tightened from 25 (athenaeum#421, settled design). A merge
    PROPOSAL is a pairwise / small-group refinement; a fold of more than ~5
    sources is not that shape, and complete-linkage (athenaeum#421) means the members of
    a genuine small merge are mutually similar, so 5 sits well inside the
    legitimate-merge margin while excluding the observed 1,600-1,700-source
    degenerates decisively. (The wider size-25 cap still governs the pooled
    contradiction-cluster path via :func:`athenaeum.cross_scope.resolve_cluster_size_cap`
    — this cap is specifically the merge-PROPOSAL fan-in.)
    Env ``ATHENAEUM_MAX_MERGE_SOURCES`` > yaml ``librarian.max_merge_sources`` >
    this default; ``0`` (or negative) disables the cap. No seed in ``_DEFAULTS``
    (athenaeum#231) so the code default stays reachable. ``bool`` and non-numeric yaml
    values fall through to the default.
    """
    default = 5
    # Issue athenaeum#524 (M2): a parsed env value is authoritative (0/negative disables
    # the cap); a malformed value now logs a WARNING instead of silently
    # falling back to the default.
    value = _env_number("ATHENAEUM_MAX_MERGE_SOURCES", int)
    if value is not None:
        return value
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("max_merge_sources")
            if isinstance(raw, int) and not isinstance(raw, bool):
                return raw
    return default


def resolve_min_merge_confidence(config: dict[str, Any] | None) -> float:
    """Resolve the resolver merge-proposal confidence floor (athenaeum#400).

    A second, opt-in gate on the merge-proposal path: a proposal whose resolver
    confidence is strictly below this floor is suppressed before it reaches
    ``_pending_merges.md``. Complements :func:`resolve_max_merge_sources` — the
    size cap catches the degenerate over-clusters by shape, this lets an operator
    additionally keep low-confidence small merges out of the human queue.

    DEFAULT 0.0 (OFF): a baked-in confidence floor is a corpus-specific product
    call (what confidence a human wants to review is deployment-dependent), so it
    ships disabled and is opt-in via ``athenaeum.yaml`` — mirroring
    :func:`resolve_min_cluster_cohesion`. Env ``ATHENAEUM_MIN_MERGE_CONFIDENCE`` >
    yaml ``librarian.min_merge_confidence`` > this default. No seed in
    ``_DEFAULTS`` (athenaeum#231). Issue athenaeum#524 (M1): a parsed env value is authoritative
    over yaml — ``ATHENAEUM_MIN_MERGE_CONFIDENCE=0`` (or negative) disables the
    floor even when yaml sets one, instead of silently falling through. A
    malformed env value logs a WARNING (M2, via :func:`_env_number`) and falls
    back to yaml. A ``bool`` / non-numeric / ``<= 0`` yaml value falls through
    to 0.0 (off).
    """
    value = _env_number("ATHENAEUM_MIN_MERGE_CONFIDENCE", float)
    if value is not None:
        # Issue athenaeum#524 (M1): the parsed env value is authoritative over yaml.
        # ATHENAEUM_MIN_MERGE_CONFIDENCE=0 disables the floor even when yaml
        # sets one — an emergency override that previously failed the `> 0.0`
        # guard and silently fell through to the yaml value. A negative value
        # clamps to 0.0 (off). This reconciles the knob with its neighbour
        # resolve_max_merge_sources, where 0 already disables authoritatively.
        return max(0.0, value)
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("min_merge_confidence")
            if raw is None or isinstance(raw, bool):
                return 0.0
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return 0.0
            if value > 0.0:
                return value
    return 0.0


def resolve_intake_runtime_floor(config: dict[str, Any] | None) -> float:
    """Resolve ``librarian.intake_runtime_floor`` (issue athenaeum#1102).

    Reserves a MINIMUM share of ``max_runtime`` for the intake path that
    feeds C4 (auto-memory C2 cluster / C3 merge / C4 contradiction-detect /
    resolve) — the phase :func:`athenaeum.librarian.librarian_entity_runtime_share`
    (athenaeum#440) *caps* the entity phase against, but never itself
    *guarantees* anything to the phases after it. athenaeum#608 needs an
    honest per-contract LLM schema-mismatch rate and cannot compute one: the
    resolution contract had 7 observations because the resolver made ~1 call
    on the 2026-08-06 run, and the entity phase's wall-clock overrun (93.6%
    of a 3944s window on 3 files) is why. This floor is the lever an operator
    can arm to reserve intake a guaranteed minimum of the SAME wall-clock
    window ``entity_runtime_share`` already caps the entity phase against —
    :func:`athenaeum.librarian._arm_run_deadline` combines the two by taking
    the EARLIER (tighter) of the two candidate entity deadlines, so whichever
    constraint binds actually wins.

    **Unit (deliberate choice, athenaeum#1102):** a fraction of ``max_runtime``
    WALL-CLOCK, mirroring ``entity_runtime_share`` exactly — not an LLM-call
    count, even though calls are the resource athenaeum#608 ultimately counts.
    The nightly window itself is wall-clock, the athenaeum#1102 motivating
    data (entity consuming 93.6% of wall-clock while nowhere near
    ``max_api_calls``) is a wall-clock-shaped failure, and the entity phase
    already stops independently on the run-level call ceiling
    (``ctx.usage.api_calls >= ctx.max_api_calls``) regardless of this floor —
    a calls-based floor would duplicate a cap that already exists. What nothing
    guarantees today is that the entity phase leaves intake any WALL-CLOCK
    TIME to spend its own calls in; that is exactly what this floor reserves.

    DEFAULT 0.0 (OFF, issue athenaeum#1102 AC4): arming this needs a value
    chosen against measured figures (athenaeum#608's own review) — an operator
    decision, out of scope for this issue. No seed in ``_DEFAULTS`` (athenaeum#231)
    so the code default stays reachable. With the key unset, phase scheduling
    is byte-for-byte identical to before this issue.

    Only ``0 < floor < 1`` reserves anything (AC6: a non-positive or malformed
    value falls through to disabled, matching :func:`resolve_max_merge_sources`'s
    own "0 disables" convention — env authoritative including a parsed zero or
    negative value, via :func:`_env_number`, which WARNs on a genuinely
    malformed value rather than swallowing it silently). AC7: a floor ``>= 1.0``
    (reserving the WHOLE window or more) is REFUSED, not clamped — it falls
    through to disabled exactly like any other out-of-range value, mirroring
    :func:`athenaeum.librarian.librarian_entity_runtime_share`'s own
    ``0 < share < 1`` guard. Refusing (rather than clamping to some
    less-than-1 ceiling) means a misconfigured floor can never invert the
    starvation this issue fixes by starving the ENTITY phase instead — the
    reserve simply does not arm, which is the same as never having set the
    key.

    Env ``ATHENAEUM_INTAKE_RUNTIME_FLOOR`` > yaml
    ``librarian.intake_runtime_floor`` > this default (``0.0``).
    """
    default = 0.0

    def _in_range(value: float) -> float:
        return value if 0.0 < value < 1.0 else default

    env_value = _env_number("ATHENAEUM_INTAKE_RUNTIME_FLOOR", float)
    if env_value is not None:
        return _in_range(env_value)
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("intake_runtime_floor")
            if raw is None or isinstance(raw, bool):
                return default
            try:
                yaml_value = float(raw)
            except (TypeError, ValueError):
                return default
            return _in_range(yaml_value)
    return default


def _resolve_sample_rate(
    config: dict[str, Any] | None, *, env_var: str, key: str, default: float
) -> float:
    """Resolve a bounded [0.0, 1.0] sampling rate (env > yaml > default).

    Shared by the tier-audit sampler knobs (athenaeum#438). Out-of-range values are
    clamped into ``[0.0, 1.0]`` (a rate above 1 samples everything, below 0
    nothing); ``bool`` / non-numeric values fall through to *default*. No seed
    in ``_DEFAULTS`` (athenaeum#231) — the sampler is ON by default, so *default* is a
    real non-zero rate the resolver owns, not a disabled floor.
    """

    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    # Issue athenaeum#528: malformed env now WARNs + falls back (was silent fall-through).
    value = _env_number(env_var, float)
    if value is not None:
        return _clamp(value)
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get(key)
            if raw is not None and not isinstance(raw, bool):
                try:
                    return _clamp(float(raw))
                except (TypeError, ValueError):
                    pass
    return default


def resolve_audit_sample_rate_t2_approvals(config: dict[str, Any] | None) -> float:
    """Resolve the share of T2 approvals sampled for human audit (athenaeum#438).

    The calibration loop catches false-APPROVES: a random share of T2's
    approve verdicts is surfaced for a human to confirm or overturn. Env
    ``ATHENAEUM_AUDIT_SAMPLE_RATE_T2_APPROVALS`` > yaml
    ``librarian.audit_sample_rate_t2_approvals`` > default ``0.075`` (7.5%,
    the midpoint of the settled 5-10% band). Clamped to ``[0.0, 1.0]``.
    """
    return _resolve_sample_rate(
        config,
        env_var="ATHENAEUM_AUDIT_SAMPLE_RATE_T2_APPROVALS",
        key="audit_sample_rate_t2_approvals",
        default=0.075,
    )


def resolve_audit_sample_rate_t1_rejects(config: dict[str, Any] | None) -> float:
    """Resolve the share of T1 rejects sampled for human audit (athenaeum#438).

    The calibration loop catches false-REJECTS: a random share of T1's reject
    verdicts is surfaced for a human to confirm or overturn. Env
    ``ATHENAEUM_AUDIT_SAMPLE_RATE_T1_REJECTS`` > yaml
    ``librarian.audit_sample_rate_t1_rejects`` > default ``0.075`` (7.5%,
    the midpoint of the settled 5-10% band). Clamped to ``[0.0, 1.0]``.
    """
    return _resolve_sample_rate(
        config,
        env_var="ATHENAEUM_AUDIT_SAMPLE_RATE_T1_REJECTS",
        key="audit_sample_rate_t1_rejects",
        default=0.075,
    )


def resolve_reasoning_tier_auditing_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the T1 reasoning-tier screen's opt-in (issue athenaeum#518). DEFAULT OFF.

    **Re-read as T1-ONLY as of issue athenaeum#1200.** Before athenaeum#1200 this single
    key gated BOTH the harmless T1 screen (reject-or-pass-up, no write
    authority) and T2's UNREVIEWED AUTO-APPLY (safe-class merges written to
    the wiki with no human review) together — one key arming two very
    different blast radii. athenaeum#1200 split them: this key now gates ONLY

    - the T1 reasoning screen in the merge path
      (:func:`athenaeum.merge.merge_clusters_to_wiki`) — a confident T1 reject
      drops a merge proposal before it reaches the human queue.

    T2's auto-apply authority now requires its OWN, separate, explicit
    opt-in — see :func:`resolve_reasoning_tier_t2_auto_apply_enabled`, which
    defaults OFF independent of this key's value (issue athenaeum#1200 AC3). The
    calibration display surface (``athenaeum calibration summary`` / the
    ``calibration_summary`` MCP tool) checks BOTH keys via
    :func:`resolve_reasoning_tier_any_screen_enabled`, not this function
    alone, so it stays accurate for a (T1 off, T2 on) config too.

    **Migration note for an existing config (issue athenaeum#1200 AC4/AC5):** a
    config that already sets ``librarian.reasoning_tier_auditing_enabled:
    true`` keeps its T1 screen exactly as before, but as of this change no
    longer also arms T2's auto-apply — T2 now requires the new key below to
    be set as well. This is a change in what the EXISTING key's value means,
    and it changes it in the safe direction only: it can only ever REMOVE
    auto-apply authority an old config previously had, never grant new
    authority a config didn't already have. To restore the exact
    pre-athenaeum#1200 combined behavior, add ONE line:
    ``librarian.reasoning_tier_t2_auto_apply_enabled: true``. See
    ``docs/configuration.md``'s "Reasoning-tier screening" section for the
    full migration story.

    Env ``ATHENAEUM_REASONING_TIER_AUDITING_ENABLED`` (``1``/``true``/``yes``/``on``,
    case-insensitive) > yaml ``librarian.reasoning_tier_auditing_enabled`` >
    default ``False``. No seed in ``_DEFAULTS`` (issue athenaeum#231). Default OFF is
    deliberate: wiring the T1 screen changes what reaches the human merge
    queue, so it stays opt-in until an operator turns it on — production merge
    behavior is byte-identical to today until then. Non-bool yaml values and
    unrecognized env strings fall through to off.
    """
    env = os.environ.get("ATHENAEUM_REASONING_TIER_AUDITING_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("reasoning_tier_auditing_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_merge_worthiness_gate_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve ``librarian.merge_worthiness_gate_enabled`` (issue athenaeum#1172). DEFAULT OFF.

    Gates the deterministic, zero-LLM merge-worthiness containment check in
    :func:`athenaeum.tiers.check_merge_worthiness_gate`: when armed, a
    Tier-3 update whose raw file offers no fact absent from the target
    entity's existing page is suppressed before the merge prompt is built
    or any model call is made. Checked at the call site in
    :func:`athenaeum.tiers.tier3_derive_actions` (mirroring how
    :func:`athenaeum.merge.merge_clusters_to_wiki` gates the reasoning-tier
    screen) so a disabled knob costs one bool call and nothing else.

    Mirrors :func:`resolve_reasoning_tier_auditing_enabled`'s precedence
    contract exactly: env ``ATHENAEUM_MERGE_WORTHINESS_GATE_ENABLED``
    (``1``/``true``/``yes``/``on``, case-insensitive) > yaml
    ``librarian.merge_worthiness_gate_enabled`` (bool only; non-bool falls
    through) > default ``False``. No seed in ``_DEFAULTS``. Default OFF is
    deliberate: a false suppression permanently destroys a fact (raw files
    are unlinked after processing, with no re-derivation path), so the gate
    stays opt-in until an operator turns it on — production merge behavior
    is byte-identical to today until then.
    """
    env = os.environ.get("ATHENAEUM_MERGE_WORTHINESS_GATE_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("merge_worthiness_gate_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_reasoning_tier_t2_auto_apply_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve T2's unreviewed-auto-apply opt-in (issue athenaeum#1200). DEFAULT OFF.

    Split out of :func:`resolve_reasoning_tier_auditing_enabled` (issue
    athenaeum#1200): T1 (a harmless reject-or-pass-up screen with no write
    authority) and T2 (which can auto-apply a safe-class merge into the wiki
    with NO human review, via ``pending_merges.resolve_merge(...,
    auto_applied=True)``) used to share one flag. This key is T2's OWN,
    independent opt-in — resolved separately from, and NOT implied by,
    :func:`resolve_reasoning_tier_auditing_enabled` (T1's key). A config can
    set either key alone, both, or neither; T1 being on does not turn T2 on,
    and T2 being on does not require T1 (see
    :func:`athenaeum.merge.merge_clusters_to_wiki` — T2's screen call is
    gated by this value directly, exactly as T1's is gated by its own).

    Env ``ATHENAEUM_REASONING_TIER_T2_AUTO_APPLY_ENABLED``
    (``1``/``true``/``yes``/``on``, case-insensitive) > yaml
    ``librarian.reasoning_tier_t2_auto_apply_enabled`` > default ``False``.
    No seed in ``_DEFAULTS`` (issue athenaeum#231). **Default OFF regardless of
    the T1 key's value (issue athenaeum#1200 AC3)** — an operator who already
    has ``reasoning_tier_auditing_enabled: true`` in a live config does NOT
    get T2 auto-apply for free; it must be armed explicitly. Non-bool yaml
    values and unrecognized env strings fall through to off.
    """
    env = os.environ.get("ATHENAEUM_REASONING_TIER_T2_AUTO_APPLY_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("reasoning_tier_t2_auto_apply_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_reasoning_tier_any_screen_enabled(config: dict[str, Any] | None) -> bool:
    """Whether EITHER reasoning-tier screen is armed (issue athenaeum#1200).

    ``resolve_reasoning_tier_auditing_enabled(config) or
    resolve_reasoning_tier_t2_auto_apply_enabled(config)`` — the calibration
    display surface (``athenaeum calibration summary``, the
    ``calibration_summary`` / ``review_audit_item`` MCP tools) uses this
    rather than the T1 key alone, so a (T1 off, T2 on) config — unusual, but
    not forbidden — still shows T2's sampled audit items instead of a
    misleading "tier auditing not enabled" that would hide an ACTIVELY
    auto-applying tier from the one loop meant to catch it being wrong.
    """
    return resolve_reasoning_tier_auditing_enabled(
        config
    ) or resolve_reasoning_tier_t2_auto_apply_enabled(config)


def resolve_min_merge_mean_similarity(config: dict[str, Any] | None) -> float:
    """Resolve the merge-proposal mean-pairwise-similarity floor (athenaeum#421).

    A merge proposal whose cluster mean pairwise cosine
    (``cluster_centroid_score``) is strictly below this floor is suppressed
    before it reaches ``_pending_merges.md``. This is the ACTIVE-by-default
    cohesion gate the athenaeum#421 settled design calls for: unlike the corpus-specific
    :func:`resolve_min_cluster_cohesion` (which suppresses durable wiki pages
    and so ships OFF), the merge-PROPOSAL path is a human review queue — a
    low-mean-similarity fold is noise there regardless of corpus, so a modest
    floor ships on.

    DEFAULT 0.6 (ACTIVE) — a genuine small merge's members are mutually
    similar; 0.6 sits below tight near-duplicate clusters (~0.7+) while
    excluding the vague ~0.33-mean over-clusters. Complements the complete-
    linkage MIN-pairwise gate (a chain can have high mean but a sub-threshold
    min) and the size cap. Env ``ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY`` > yaml
    ``librarian.min_merge_mean_similarity`` > this default; ``0`` (or negative)
    disables the floor. No seed in ``_DEFAULTS`` (athenaeum#231) so the code default
    stays reachable. ``bool`` and non-numeric yaml values fall through to the
    default.
    """
    default = 0.6
    # Issue athenaeum#528: malformed env now WARNs + falls back (was silent fall-through).
    value = _env_number("ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY", float)
    if value is not None:
        return value
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("min_merge_mean_similarity")
            if raw is None or isinstance(raw, bool):
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default
    return default


def resolve_delta_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the delta-scoped-compile opt-in (athenaeum#370 PR2) from ``librarian.delta``.

    When TRUE (the default), the deterministic ``client=None`` compile path
    (session_end / ingest tier0) may scope the cluster + merge passes to only
    the changed files and their affected clusters instead of re-clustering and
    re-merging the whole auto-memory corpus. This is a pure SPEED optimization
    that is proven byte-equivalent to the whole-corpus path
    (``tests/test_delta_compile_equivalence.py``); the nightly LLM ``run`` (a
    live client with cross-scope contradiction detection) always stays
    whole-corpus regardless of this flag. Set ``librarian.delta.enabled: false``
    to force the whole-corpus path everywhere. ``bool`` yaml values are honored;
    anything else falls through to the TRUE default.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            delta_cfg = cfg.get("delta")
            if isinstance(delta_cfg, dict):
                raw = delta_cfg.get("enabled")
                if isinstance(raw, bool):
                    return raw
    return True


def resolve_delta_max_affected_clusters(config: dict[str, Any] | None) -> int:
    """Resolve the delta closure's affected-cluster cap (athenaeum#370 PR2, default 8).

    When the change-closure fixpoint pulls in MORE than this many clusters, the
    delta is no longer a small local update — the run falls back to a full
    whole-corpus compile (fallback trigger D2) rather than churning most of the
    corpus through the "delta" path. ``librarian.delta.max_affected_clusters``;
    ``bool`` and non-positive / non-int values fall through to the default.
    """
    default = 8
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            delta_cfg = cfg.get("delta")
            if isinstance(delta_cfg, dict):
                raw = delta_cfg.get("max_affected_clusters")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return default


def resolve_delta_max_affected_members(config: dict[str, Any] | None) -> int:
    """Resolve the delta closure's pooled-member cap (athenaeum#370 PR2, default 200).

    Companion to :func:`resolve_delta_max_affected_clusters`: when the pool of
    files entering the delta re-cluster exceeds this many members, fall back to
    a full compile (fallback trigger D2). Bounds the worst-case
    re-cluster cost so a pathological closure can never do MORE work than a full
    run. ``librarian.delta.max_affected_members``; ``bool`` and non-positive /
    non-int values fall through to the default.
    """
    default = 200
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            delta_cfg = cfg.get("delta")
            if isinstance(delta_cfg, dict):
                raw = delta_cfg.get("max_affected_members")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return default


def resolve_live_delta_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the live-client delta-scoped-compile opt-in from
    ``librarian.delta.live_client`` (issue athenaeum#463, slice D of athenaeum#460).

    When TRUE (the default), the nightly LLM ``run`` (a live client) MAY also
    take the delta-scoped cluster + merge path — previously (athenaeum#370 PR2) delta
    was gated to the deterministic ``client is None`` path ONLY (fallback
    trigger D5). The live-client delta path is additionally gated by
    ``full_compile_due`` (the periodic whole-corpus reconciliation, see
    :func:`athenaeum.config.resolve_full_compile_every_days`) regardless of
    this flag — see :func:`athenaeum.librarian._compile_auto_memory`. Set
    ``librarian.delta.live_client: false`` to keep the nightly run
    whole-corpus-only (the pre-athenaeum#463 behaviour) even when a live client is
    present. ``bool`` yaml values are honored; anything else falls through to
    the TRUE default.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            delta_cfg = cfg.get("delta")
            if isinstance(delta_cfg, dict):
                raw = delta_cfg.get("live_client")
                if isinstance(raw, bool):
                    return raw
    return True


def resolve_full_compile_every_days(config: dict[str, Any] | None) -> int:
    """Resolve the periodic whole-corpus reconciliation cadence (issue athenaeum#463,
    default 7 days) from ``librarian.full_compile_every_days``.

    The live-client delta path (athenaeum#463) is a corpus-consistency optimization
    over the auto-memory C2-C4 compile; this cadence is its backstop. When the
    last successful whole-corpus compile (:func:`athenaeum.librarian.
    _load_full_compile_stamp`) is at least this many days old — or there has
    never been one — the next run forces a whole-corpus compile regardless of
    the delta gate, re-entering any TTL-decayed auto-suppressions and
    resolving drift a delta pass could not see. Note this key lives directly
    under ``librarian``, NOT under ``librarian.delta`` (it also bounds the
    non-live delta path indirectly via the stamp, but is a run-cadence
    setting, not a delta-mechanism setting). ``bool`` and non-positive /
    non-int values fall through to the default.
    """
    default = 7
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("full_compile_every_days")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return default


# ---------------------------------------------------------------------------
# Reasoning-tier triggers (issue athenaeum#909): configurable backlog-depth,
# elapsed-interval, and nightly-backstop knobs consumed by
# :mod:`athenaeum.reasoning_triggers`'s pure evaluator. Every resolver here
# copies :func:`resolve_full_compile_every_days`'s exact int/bool/positive-int
# guard shape. Deliberately NOT seeded into ``_DEFAULTS`` (see the NOTE at
# ``_DEFAULTS``'s definition above) — each trigger's "disabled" state (``None``,
# not a magic sentinel int) is only representable when the key is absent, and
# seeding a default here would make "operator explicitly configured this"
# indistinguishable from "unset".
# ---------------------------------------------------------------------------


def resolve_reasoning_trigger_backlog_files(config: dict[str, Any] | None) -> int | None:
    """Resolve the backlog-depth-by-file-count trigger threshold (issue athenaeum#909)
    from ``librarian.reasoning_triggers.backlog_files``.

    When the pending-reasoning raw-intake backlog (:func:`athenaeum.intake.
    discover_raw_files`) reaches or exceeds this many files, the backlog-depth
    trigger fires (see :mod:`athenaeum.reasoning_triggers`). ``None`` (the
    default — key unset) DISABLES this trigger entirely; it never fires on
    file count. ``bool`` and non-positive / non-int values fall through to
    disabled, same as an unset key.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            triggers_cfg = cfg.get("reasoning_triggers")
            if isinstance(triggers_cfg, dict):
                raw = triggers_cfg.get("backlog_files")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return None


def resolve_reasoning_trigger_backlog_bytes(config: dict[str, Any] | None) -> int | None:
    """Resolve the backlog-depth-by-byte-count trigger threshold (issue athenaeum#909)
    from ``librarian.reasoning_triggers.backlog_bytes``.

    ``M bytes`` is the LITERAL on-disk size of pending raw intake (sum of
    :func:`athenaeum.intake.discover_raw_files`'s files' ``stat().st_size``,
    via :func:`athenaeum.intake.discover_raw_backlog_bytes`) — not a cost or
    token estimate. When the backlog reaches or exceeds this many bytes, the
    backlog-depth trigger fires (see :mod:`athenaeum.reasoning_triggers`).
    ``None`` (the default — key unset) DISABLES this trigger entirely.
    ``bool`` and non-positive / non-int values fall through to disabled, same
    as an unset key.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            triggers_cfg = cfg.get("reasoning_triggers")
            if isinstance(triggers_cfg, dict):
                raw = triggers_cfg.get("backlog_bytes")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return None


def resolve_reasoning_trigger_interval_hours(config: dict[str, Any] | None) -> int | None:
    """Resolve the elapsed-interval trigger threshold in hours (issue athenaeum#909)
    from ``librarian.reasoning_triggers.interval_hours``.

    When at least this many hours have elapsed since the last completed
    triggered reasoning run (see :mod:`athenaeum.reasoning_triggers` and the
    reasoning-trigger last-run stamp), the interval trigger fires regardless
    of backlog depth — so a quiet night still gets a bounded, incremental
    look rather than going dark until the nightly backstop. ``None`` (the
    default — key unset) DISABLES this trigger entirely. ``bool`` and
    non-positive / non-int values fall through to disabled, same as an unset
    key.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            triggers_cfg = cfg.get("reasoning_triggers")
            if isinstance(triggers_cfg, dict):
                raw = triggers_cfg.get("interval_hours")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return None


def resolve_reasoning_trigger_nightly_backstop_hours(
    config: dict[str, Any] | None,
) -> int:
    """Resolve the nightly-backstop trigger threshold in hours (issue athenaeum#909,
    default 24) from ``librarian.reasoning_triggers.nightly_backstop_hours``.

    Unlike the other three reasoning triggers, the backstop is always ON —
    tying reasoning to a single nightly window is exactly the failure mode
    athenaeum#909 removes (a bad night goes invisible for 24h). The backstop fires
    when at least this many hours have elapsed since the last completed
    triggered reasoning run AND no other trigger fired this evaluation (see
    :mod:`athenaeum.reasoning_triggers`) — it is the demoted fallback, not the
    primary path. ``bool`` and non-positive / non-int values fall through to
    the default.
    """
    default = 24
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            triggers_cfg = cfg.get("reasoning_triggers")
            if isinstance(triggers_cfg, dict):
                raw = triggers_cfg.get("nightly_backstop_hours")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return default


def resolve_drain_warn_days(config: dict[str, Any] | None) -> int:
    """Resolve the backlog-drain ETA warning threshold in days (issue athenaeum#470,
    default 3) from ``librarian.drain_warn_days``.

    At the end of any run that leaves raw intake undrained (and in ``athenaeum
    status``), the backlog-drain advisor (:func:`athenaeum.drain_advisor.build_advisory`)
    projects time-to-drain from observed throughput and emits a WARNING — naming
    the one-command ``athenaeum drain`` remedy — only when that projection
    EXCEEDS this many days. Below the threshold the run stays silent. Lives
    directly under ``librarian`` (a run-cadence advisory, not a delta/merge
    knob). ``bool`` and non-positive / non-int values fall through to the
    default.
    """
    default = 3
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("drain_warn_days")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return default


def resolve_reindex_full_rehash_max_age_days(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> float:
    """Resolve the periodic full-re-hash backstop age in days (athenaeum#373, default 7).

    The athenaeum#370 stat pre-filter reuses a stored content hash whenever a file's
    ``(mtime_ns, size)`` match the manifest, so a content edit that preserved
    BOTH would slip past until a full re-hash. On an INCREMENTAL build, when the
    manifest has not recorded a full re-hash within this many days, the search
    backend re-reads and re-hashes EVERY file for one build (still applying the
    change delta incrementally — no full re-embed / FTS5 rebuild). Read from
    ``librarian.reindex.full_rehash_max_age_days``.

    ``0`` or negative => always re-hash; a very large value => effectively never.
    ``bool`` (an ``int`` subclass) and non-numeric values fall through to the
    default so ``full_rehash_max_age_days: yes`` cannot read as ``1``.
    """
    default = 7.0
    if config is None:
        config = load_config(knowledge_root)
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            reindex_cfg = cfg.get("reindex")
            if isinstance(reindex_cfg, dict):
                raw = reindex_cfg.get("full_rehash_max_age_days")
                if not isinstance(raw, bool) and isinstance(raw, (int, float)):
                    return float(raw)
    return default


def resolve_lock_timeout(config: dict[str, Any] | None) -> float:
    """Resolve the default run-lock wait (seconds) from env > yaml > 0 (athenaeum#309).

    The single-machine run lock (:mod:`athenaeum.runlock`) fails fast by default
    when another ``athenaeum run`` (or other mutating command) already holds
    ``<knowledge_root>/.athenaeum.lock``. Operators who prefer a mutating
    command to WAIT rather than exit — e.g. a manual run overlapping the nightly
    cron — can set a default block window::

        librarian:
          lock_timeout: 300   # seconds; 0 = fail-fast (default)

    Precedence: ``ATHENAEUM_LOCK_TIMEOUT`` env, then ``librarian.lock_timeout``
    yaml, then ``0`` (fail-fast). The per-command ``--wait`` flag overrides this.
    No seed in ``_DEFAULTS`` (athenaeum#231) so the code default stays reachable. ``bool``
    and non-numeric / negative values fall through to 0.0.
    """
    # Issue athenaeum#528: malformed env now WARNs + falls back (was a silent hard-zero).
    value = _env_number("ATHENAEUM_LOCK_TIMEOUT", float)
    if value is not None:
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


def resolve_heartbeat_interval(config: dict[str, Any] | None) -> float:
    """Resolve the progress-heartbeat emit interval (seconds) (athenaeum#398).

    The dark-zone phases (the entity phase, issue athenaeum#800; T3 merge; C4
    contradiction detection; the athenaeum#290 wiki-dedup pass; the athenaeum#188
    re-resolve pass) emit a periodic ``librarian-heartbeat`` progress line via
    :class:`athenaeum.progress.PhaseHeartbeat`
    so a stall in one of these phases is visible in the log and detectable by
    a watchdog. This resolves how often (in seconds) a slow/wedged phase
    emits a tick::

        librarian:
          heartbeat_interval: 60   # seconds; <= 0 = emit every tick

    Precedence: ``ATHENAEUM_HEARTBEAT_INTERVAL`` env, then
    ``librarian.heartbeat_interval`` yaml, then ``60.0`` (default). ``bool``
    and non-numeric values fall through to the default. A value ``<= 0``
    means "emit every tick" and returns ``0.0`` (NOT the default — 0 is a
    valid, distinct configuration, unlike ``resolve_lock_timeout``'s
    fail-fast collapse).
    """
    default = 60.0
    # Issue athenaeum#528: malformed env now WARNs + falls back to yaml/default (was a
    # silent return-default that skipped yaml).
    value = _env_number("ATHENAEUM_HEARTBEAT_INTERVAL", float)
    if value is not None:
        return value if value > 0 else 0.0
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("heartbeat_interval")
            if raw is None or isinstance(raw, bool):
                return default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else 0.0
    return default


def resolve_lock_break_stale_after(config: dict[str, Any] | None) -> float | None:
    """Resolve the auto-break staleness threshold in seconds (athenaeum#397, default 6h).

    A contended :meth:`~athenaeum.runlock.RunLock.acquire` auto-breaks a
    wedged-but-alive holder's lock — WITHOUT requiring a human to pass
    ``--force`` — once the holder's heartbeat age exceeds this many seconds.
    Six hours is comfortably above any healthy librarian run (and well below
    the pathological multi-hour wedge seen in issue athenaeum#396); operators can
    lower it once the librarian reliably refreshes the lock heartbeat::

        librarian:
          lock_break_stale_after: 21600   # seconds; <= 0 disables auto-break

    Precedence: ``ATHENAEUM_LOCK_BREAK_STALE_AFTER`` env, then
    ``librarian.lock_break_stale_after`` yaml, then ``21600.0`` (6h). ``bool``
    and non-numeric values fall through to the default. A value ``<= 0``
    disables auto-break entirely (returns ``None``).
    """
    default = 21600.0
    # Issue athenaeum#528: malformed env now WARNs + falls back (was silent return-default).
    value = _env_number("ATHENAEUM_LOCK_BREAK_STALE_AFTER", float)
    if value is not None:
        return value if value > 0 else None
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("lock_break_stale_after")
            if raw is None:
                return default
            if isinstance(raw, bool):
                return default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else None
    return default


def resolve_lock_warn_stale_after(config: dict[str, Any] | None) -> float | None:
    """Resolve the loud-warning staleness threshold in seconds (athenaeum#397, default 2h).

    A contended :meth:`~athenaeum.runlock.RunLock.acquire` logs a prominent
    "likely wedged" warning naming the holder once its heartbeat age exceeds
    this many seconds — independent of (and typically lower than) the
    auto-break threshold, so an operator gets an early heads-up even when
    auto-break has not yet fired::

        librarian:
          lock_warn_stale_after: 7200   # seconds; <= 0 disables the warning

    Precedence: ``ATHENAEUM_LOCK_WARN_STALE_AFTER`` env, then
    ``librarian.lock_warn_stale_after`` yaml, then ``7200.0`` (2h). ``bool``
    and non-numeric values fall through to the default. A value ``<= 0``
    disables the warning entirely (returns ``None``).
    """
    default = 7200.0
    # Issue athenaeum#528: malformed env now WARNs + falls back (was silent return-default).
    value = _env_number("ATHENAEUM_LOCK_WARN_STALE_AFTER", float)
    if value is not None:
        return value if value > 0 else None
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("lock_warn_stale_after")
            if raw is None:
                return default
            if isinstance(raw, bool):
                return default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else None
    return default


#: Default pending-batch raw-file lease, in seconds (issue athenaeum#1143).
#:
#: 72h. Shorter than the Batch API's 29-day result retention, so a
#: still-collectible batch is always preferred over re-claiming its raw files.
#: Longer than the API's 24h processing ceiling, so a slow batch is not
#: abandoned mid-flight and its intake re-submitted at full price alongside it.
DEFAULT_BATCH_LEASE_SECONDS = 259200.0


def resolve_batch_lease_seconds(config: dict[str, Any] | None) -> float | None:
    """Resolve the pending-batch raw-file lease in seconds (athenaeum#1143, default 72h).

    :func:`athenaeum.batch_state.record_handle` leases the raw files a
    submitted-but-uncollected batch was built from for this many seconds, and
    the entity-phase claim loop skips a leased file until the lease expires —
    so a submit-and-exit run cannot have its own intake rediscovered and
    re-submitted, at full price, by the next run::

        librarian:
          batch_lease_seconds: 259200   # seconds; <= 0 disables leasing

    Precedence: ``ATHENAEUM_BATCH_LEASE_SECONDS`` env, then
    ``librarian.batch_lease_seconds`` yaml, then
    :data:`DEFAULT_BATCH_LEASE_SECONDS` (72h). ``bool`` and non-numeric values
    fall through to the default. A value ``<= 0`` disables leasing entirely
    (returns ``None``) — the explicit operator opt-out, matching the
    ``max_runtime`` escape-hatch convention and mirroring
    :func:`resolve_lock_break_stale_after`'s shape exactly.
    """
    default = DEFAULT_BATCH_LEASE_SECONDS
    value = _env_number("ATHENAEUM_BATCH_LEASE_SECONDS", float)
    if value is not None:
        return value if value > 0 else None
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("batch_lease_seconds")
            if raw is None:
                return default
            if isinstance(raw, bool):
                return default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else None
    return default


def _resolve_positive_int_knob(
    config: dict[str, Any] | None,
    key: str,
    env_var: str,
    default: int,
) -> int:
    """Resolve a positive-int ``librarian.<key>`` knob (env > yaml > default).

    Shared helper for the wiki page-size guardrails (issue athenaeum#310). Mirrors
    :func:`athenaeum.clusters.resolve_rotation_retention`'s precedence and
    coercion contract: the ``env_var`` wins when it parses to a positive int,
    otherwise the yaml key is read, otherwise *default*. ``bool`` (an ``int``
    subclass) and non-int / ``<= 0`` values fall through so ``page_warn_bytes:
    yes`` cannot become ``1`` and a nonsensical zero/negative byte count cannot
    silently disable the guardrail. No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    # Issue athenaeum#524 (M2): malformed env values now WARN (via _env_number) instead
    # of silently falling through. The `> 0` rejection is deliberate and kept
    # (a zero/negative byte count must not disable the guardrail), so 0 is NOT
    # authoritative here — unlike the disable-semantics knobs (M1).
    value = _env_number(env_var, int)
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
    """Resolve the wiki-page soft-warn size threshold in bytes (issue athenaeum#310).

    Precedence: ``ATHENAEUM_PAGE_WARN_BYTES`` env > ``librarian.page_warn_bytes``
    yaml > ``8192``. A page whose UTF-8 size (frontmatter + body) exceeds this
    is surfaced in ``status`` as a warn-level oversized page — a nudge to split,
    never a block. See :func:`_resolve_positive_int_knob` for the coercion
    contract.
    """
    return _resolve_positive_int_knob(config, "page_warn_bytes", "ATHENAEUM_PAGE_WARN_BYTES", 8192)


def resolve_page_flag_bytes(config: dict[str, Any] | None) -> int:
    """Resolve the wiki-page flag-for-split size threshold in bytes (issue athenaeum#310).

    Precedence: ``ATHENAEUM_PAGE_FLAG_BYTES`` env > ``librarian.page_flag_bytes``
    yaml > ``16384``. A page over this is flagged more loudly (and logged during
    ``athenaeum run``) as one that should be broken into linked sub-entities.
    Kept comfortably below the tier-3 merge body cap so flagging precedes any
    hard merge-budget pressure. See :func:`_resolve_positive_int_knob`.
    """
    return _resolve_positive_int_knob(config, "page_flag_bytes", "ATHENAEUM_PAGE_FLAG_BYTES", 16384)


def resolve_raw_file_max_bytes(config: dict[str, Any] | None) -> int:
    """Resolve the per-raw-file byte bound in bytes (issue athenaeum#898).

    Precedence: ``ATHENAEUM_RAW_FILE_MAX_BYTES`` env > ``librarian.raw_file_max_bytes``
    yaml > ``5242880`` (5 MiB). Enforced by :attr:`athenaeum.models.RawFile.content`
    — a raw intake file over this is refused BEFORE it is read into memory or
    handed to the classifier (:class:`~athenaeum.models.RawFileTooLargeError`).
    The default sits comfortably below the 9.7MB dry-run artifact that
    motivated this bound (it accounted for 93% of timed entity-phase LLM
    calls for roughly three months) while staying generous for a legitimately
    large note or document dump — ordinary `remember()`-authored intake is KB-
    sized. See :func:`_resolve_positive_int_knob` for the coercion contract.
    """
    return _resolve_positive_int_knob(
        config, "raw_file_max_bytes", "ATHENAEUM_RAW_FILE_MAX_BYTES", 5 * 1024 * 1024
    )


def resolve_raw_file_max_api_calls(config: dict[str, Any] | None) -> int:
    """Resolve the per-raw-file LLM-call bound (issue athenaeum#898, recalibrated athenaeum#994).

    Precedence: ``ATHENAEUM_RAW_FILE_MAX_API_CALLS`` env >
    ``librarian.raw_file_max_api_calls`` yaml > ``60``. Checked
    INCREMENTALLY by :func:`athenaeum.tiers.tier3_derive_actions`, after each
    entity action a raw file drives, against the running count of LLM calls
    THAT ONE FILE has consumed so far (``usage.api_calls`` before the file
    started vs. now) — see :class:`~athenaeum.models.RawFileOverBudgetError`'s
    docstring for why the check moved from "once, after the whole file" to
    "after every action".

    The original ``8`` default assumed an ordinary file costs roughly 1-3
    calls (tier-2 classify plus one tier-3 action or two). Measured reality
    on the live deployment (2026-08-15/16 nightly logs, api provider) put an
    ordinary file at **20-46 calls** — un-batched ``tier3_write`` spends one
    call per entity action, and a file with several entities easily clears a
    dozen — so ``8`` sat 3-6x below the median file and rejected normal
    input rather than catching loopers. ``60`` covers the measured
    distribution with headroom while still catching a file whose action set
    genuinely loops. See :func:`_resolve_positive_int_knob` for the coercion
    contract.
    """
    return _resolve_positive_int_knob(
        config, "raw_file_max_api_calls", "ATHENAEUM_RAW_FILE_MAX_API_CALLS", 60
    )


def resolve_raw_file_max_runtime_seconds(config: dict[str, Any] | None) -> int:
    """Resolve the per-raw-file wall-clock bound, in seconds.

    Issue athenaeum#898, recalibrated athenaeum#994.

    Precedence: ``ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS`` env >
    ``librarian.raw_file_max_runtime_seconds`` yaml > ``900``. Checked
    alongside :func:`resolve_raw_file_max_api_calls`, incrementally, after
    each entity action — the wall-clock spent inside ONE file's processing
    so far, compared against this bound.

    The original ``120`` default assumed a single file's tier-2/tier-3
    round trip(s) stayed well under it. Measured reality on the live
    deployment (2026-08-15/16 nightly logs, api provider) put an ordinary
    file at **300-690 seconds** — in line with the same un-batched
    per-action call pattern that drove the call-count recalibration above —
    so ``120`` rejected normal input long before it caught anything
    pathological. ``900`` covers the measured distribution with headroom
    while still catching a file that genuinely hangs or loops. See
    :func:`_resolve_positive_int_knob` for the coercion contract.
    """
    return _resolve_positive_int_knob(
        config,
        "raw_file_max_runtime_seconds",
        "ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS",
        900,
    )


def resolve_merge_body_preview_chars(config: dict[str, Any] | None) -> int:
    """Resolve the ``list_pending_merges`` draft-body preview cap (issue athenaeum#431).

    Complements the write-path suppression in :func:`resolve_max_merge_sources`
    (athenaeum#400): that gate keeps a degenerate over-cluster from ever reaching
    ``_pending_merges.md``, but a single legitimate-looking proposal can still
    carry an oversized ``draft_merged_body`` (the withdrawn runaway that
    prompted this issue had a ~878 KB draft). The raw MCP tool returned that
    body in full, unbounded, on every ``list_pending_merges`` call — this caps
    it to a bounded preview by default. The full body stays retrievable via
    ``list_pending_merges(full_body=True)`` for a caller that actually needs it
    (e.g. immediately before ``resolve_merge`` writes it to disk).

    Precedence: ``ATHENAEUM_MERGE_BODY_PREVIEW_CHARS`` env > yaml
    ``librarian.merge_body_preview_chars`` > ``2000``. See
    :func:`_resolve_positive_int_knob` for the coercion contract (``bool`` /
    non-int / ``<= 0`` values fall through to the default).
    """
    return _resolve_positive_int_knob(
        config,
        "merge_body_preview_chars",
        "ATHENAEUM_MERGE_BODY_PREVIEW_CHARS",
        2000,
    )


def resolve_decisions_max_sources_per_merge(config: dict[str, Any] | None) -> int:
    """Resolve the decisions-view per-merge source fan-out cap (issue athenaeum#431).

    The ``decisions`` view (:func:`athenaeum.decisions.merge_to_decision`)
    rendered EVERY source of a pending merge with no cap — a merge proposal
    with a very large source list (or the pathological over-cluster shape
    athenaeum#400 targets on the write path) would blow out a single decision item's
    payload. This bounds the rendered source list to this many entries, with
    the remainder surfaced as an accurate ``sources_omitted`` count rather
    than silently dropped.

    Precedence: ``ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE`` env > yaml
    ``librarian.decisions_max_sources_per_merge`` > ``20``. See
    :func:`_resolve_positive_int_knob` for the coercion contract (``bool`` /
    non-int / ``<= 0`` values fall through to the default).
    """
    return _resolve_positive_int_knob(
        config,
        "decisions_max_sources_per_merge",
        "ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE",
        20,
    )


def _resolve_optional_positive_number(
    config: dict[str, Any] | None,
    block: str,
    key: str,
    env_var: str,
    *,
    cast: type,
) -> Any | None:
    """Resolve an OPTIONAL positive number ``<block>.<key>`` (env > yaml > None).

    Shared helper for the spend ceilings (issue athenaeum#378). Unlike
    :func:`_resolve_positive_int_knob`, an unset knob resolves to ``None`` — a
    ceiling is off unless the operator opts in — rather than a code default.
    ``env_var`` wins when it parses to a positive number, otherwise the yaml key
    is read, otherwise ``None``. ``bool`` (an ``int`` subclass) and non-numeric
    / ``<= 0`` values fall through to ``None`` so ``max_usd_per_day: yes`` cannot
    become ``1`` and a nonsensical zero/negative ceiling cannot silently pin the
    pass to a no-op. No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    # Issue athenaeum#524 (M2): malformed env values now WARN (via _env_number) instead
    # of silently falling through. The `> 0` rejection is deliberate and kept
    # (a zero/negative ceiling must not silently pin the pass to a no-op), so 0
    # is NOT authoritative here — unlike the disable-semantics knobs (M1).
    value = _env_number(env_var, cast)
    if value is not None and value > 0:
        return value

    if isinstance(config, dict):
        cfg = config.get(block)
        if isinstance(cfg, dict):
            raw = cfg.get(key)
            if raw is not None and not isinstance(raw, bool):
                try:
                    value = cast(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and value > 0:
                    return value
    return None


def resolve_spend_ledger_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve whether the spend ledger is written (env > yaml > True) (athenaeum#378).

    The durable LLM-spend ledger (``~/.cache/athenaeum/spend.jsonl``) is ON by
    default — it is append-only, crash-safe, and records only counts (never
    content or credentials), so the cost is negligible. Precedence:
    ``ATHENAEUM_SPEND_LEDGER_ENABLED`` env > ``spend.ledger_enabled`` yaml >
    ``True``. Any env value other than a falsey token (``0`` / ``false`` /
    ``no`` / ``off``, case-insensitive) is truthy; a non-bool yaml value falls
    through to the default. No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    env = os.environ.get("ATHENAEUM_SPEND_LEDGER_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(config, dict):
        cfg = config.get("spend")
        if isinstance(cfg, dict):
            raw = cfg.get("ledger_enabled")
            if isinstance(raw, bool):
                return raw
    return True


def resolve_push_metrics_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve whether push-precision/coverage instrumentation runs (athenaeum#711).

    ON by default: it is passive measurement — one small append-only JSONL
    row per recall push and per session-end reference determination, both
    under the cache dir, never inside the wiki corpus — and the whole point
    of the v6 memory-model epic's precision baseline is that it starts
    recording BEFORE any later slice changes what recall pushes. Precedence:
    ``ATHENAEUM_PUSH_METRICS_ENABLED`` env > ``push_metrics.enabled`` yaml >
    ``True``. Any env value other than a falsey token (``0`` / ``false`` /
    ``no`` / ``off``, case-insensitive) is truthy; a non-bool yaml value falls
    through to the default. No seed in ``_DEFAULTS`` (issue athenaeum#231) — mirrors
    :func:`resolve_spend_ledger_enabled`'s shape exactly.
    """
    env = os.environ.get("ATHENAEUM_PUSH_METRICS_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(config, dict):
        cfg = config.get("push_metrics")
        if isinstance(cfg, dict):
            raw = cfg.get("enabled")
            if isinstance(raw, bool):
                return raw
    return True


def resolve_ingestion_gate_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve whether the ingestion gate is enforced (issue athenaeum#968).

    OFF by default — this is a new, additive gate (part 3 of athenaeum#968) that can
    BLOCK ingestion when push-metrics precision instrumentation looks
    unhealthy, so it must not change behavior for any existing operator
    until they opt in (DoD: "lands dark behind a documented config key
    defaulting to off"). Precedence: ``ATHENAEUM_INGESTION_GATE_ENABLED`` env
    > ``librarian.ingestion_gate_enabled`` yaml > ``False``. Any env value
    other than a falsey token (``0`` / ``false`` / ``no`` / ``off``,
    case-insensitive) is truthy; a non-bool yaml value falls through to the
    default. No seed in ``_DEFAULTS`` (issue athenaeum#231) — mirrors
    :func:`resolve_push_metrics_enabled`'s shape, inverted default.
    """
    env = os.environ.get("ATHENAEUM_INGESTION_GATE_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("ingestion_gate_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_push_token_budget(config: dict[str, Any] | None) -> int:
    """Resolve the unprompted push budget in tokens-per-turn (issue athenaeum#718).

    The one documented dial for how much recall pushes into a turn
    unprompted (the "hot" retrieval-cost tier only — see
    :mod:`athenaeum.memory_tiers`, deliberately "the entire expensive-and-
    noisy dial" per that issue's AC). Enforced at
    :func:`athenaeum.mcp_server._recall_via_backend`'s ``unprompted=True``
    path (:func:`athenaeum.memory_tiers.select_for_push`): hits are ranked
    by relevance x tier x coordinate-fit and greedily included, in that
    order, while the running token total (:func:`athenaeum.push_metrics.estimate_tokens`)
    stays within this budget — a hit that would exceed it is skipped, never
    truncated.

    Precedence: ``ATHENAEUM_PUSH_TOKEN_BUDGET`` env > ``push_budget.tokens_per_turn``
    yaml > ``1200``. A malformed env value WARNs and falls through (see
    :func:`_env_number`); a non-int / ``<= 0`` yaml value falls through to
    the default. No seed in ``_DEFAULTS`` (issue athenaeum#231).
    """
    value = _env_number("ATHENAEUM_PUSH_TOKEN_BUDGET", int)
    if value is not None and value > 0:
        return value
    if isinstance(config, dict):
        cfg = config.get("push_budget")
        if isinstance(cfg, dict):
            raw = cfg.get("tokens_per_turn")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return 1200


def resolve_memory_tier_sweep_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve whether the automatic memory-tier sweep runs (issue athenaeum#718).

    OFF by default — a new, additive librarian phase
    (:func:`athenaeum.librarian._run_memory_tier_sweep_phase`) that can
    rewrite a page's ``memory_tier:`` frontmatter field (demote hot -> warm,
    promote warm -> hot; see :mod:`athenaeum.memory_tiers`), so it must not
    change the nightly run's behavior for any existing operator until they
    opt in (DoD: "lands dark behind a documented config key defaulting to
    off"). Precedence: ``ATHENAEUM_MEMORY_TIER_SWEEP_ENABLED`` env >
    ``librarian.memory_tier_sweep_enabled`` yaml > ``False``. Any env value
    other than a falsey token (``0`` / ``false`` / ``no`` / ``off``,
    case-insensitive) is truthy; a non-bool yaml value falls through to the
    default. No seed in ``_DEFAULTS`` (issue athenaeum#231) — mirrors
    :func:`resolve_ingestion_gate_enabled`'s shape.
    """
    env = os.environ.get("ATHENAEUM_MEMORY_TIER_SWEEP_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("memory_tier_sweep_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_memory_tier_demote_after_days(config: dict[str, Any] | None) -> int:
    """Resolve the age-without-use / precision-grace window in days (issue athenaeum#718).

    Shared threshold :func:`athenaeum.memory_tiers.evaluate_tier_movement`
    uses for two of its three automatic hot -> warm demotion triggers: a hot
    claim with no usage record at all after this many days, or a hot claim
    that HAS been pushed but never referenced and whose last push is older
    than this many days. The third trigger (class-default: superseded/
    deprecated) is unconditional and ignores this knob.

    Precedence: ``ATHENAEUM_MEMORY_TIER_DEMOTE_AFTER_DAYS`` env >
    ``memory_tiers.demote_after_days`` yaml > ``60``. A malformed env value
    WARNs and falls through (see :func:`_env_number`); a non-int / ``<= 0``
    yaml value falls through to the default. No seed in ``_DEFAULTS``
    (issue athenaeum#231).
    """
    value = _env_number("ATHENAEUM_MEMORY_TIER_DEMOTE_AFTER_DAYS", int)
    if value is not None and value > 0:
        return value
    if isinstance(config, dict):
        cfg = config.get("memory_tiers")
        if isinstance(cfg, dict):
            raw = cfg.get("demote_after_days")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return 60


def resolve_spend_ledger_path(config: dict[str, Any] | None) -> Path | None:
    """Resolve an explicit spend-ledger path override (env > yaml > None) (athenaeum#378).

    ``None`` means "use the default" — ``<cache_dir>/spend.jsonl`` under
    ``~/.cache/athenaeum`` (see :func:`athenaeum.spend.default_ledger_path`).
    Precedence: ``ATHENAEUM_SPEND_LEDGER`` env > ``spend.ledger_path`` yaml >
    ``None``. Chiefly a test/relocation seam. No seed in ``_DEFAULTS`` (athenaeum#231).
    """
    env = os.environ.get("ATHENAEUM_SPEND_LEDGER")
    if env is not None and env.strip():
        return Path(env).expanduser()
    if isinstance(config, dict):
        cfg = config.get("spend")
        if isinstance(cfg, dict):
            raw = cfg.get("ledger_path")
            if isinstance(raw, str) and raw.strip():
                return Path(raw).expanduser()
    return None


def resolve_spend_max_tokens_per_run(config: dict[str, Any] | None) -> int | None:
    """Resolve the per-run SUBSCRIPTION token ceiling (env > yaml > None) (athenaeum#378).

    A run served by the ``claude-cli`` provider consumes subscription quota
    rather than dollars, so its ceiling is a TOKEN count. When set and the
    run-level total tokens reach it, the pass stops early and loudly (the
    remaining intake defers to the next run, exactly like the ``max_api_calls``
    budget). Precedence: ``ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN`` env >
    ``spend.max_tokens_per_run`` yaml > ``None`` (no ceiling).
    """
    return _resolve_optional_positive_number(
        config,
        "spend",
        "max_tokens_per_run",
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN",
        cast=int,
    )


def _system_local_timezone() -> tzinfo:
    """Best-effort resolve the host's local timezone as a stdlib ``tzinfo``.

    Tries ``/etc/localtime`` first — the standard Linux/macOS symlink into
    the system zoneinfo database (``/usr/share/zoneinfo/<Region>/<City>``)
    — which yields a NAMED :class:`~zoneinfo.ZoneInfo` that correctly
    observes DST transitions on either side of a day boundary. Falls back to
    ``datetime.now().astimezone().tzinfo`` — a fixed-offset ``timezone``
    object, correct for "right now" but blind to a future/past DST shift —
    when the symlink trick doesn't resolve (e.g. a container with no
    zoneinfo symlink, or a platform that lays out its tz data differently).
    Never raises: an unresolvable local zone falls all the way through to
    UTC, matching this module's "never crash a run over a config/environment
    quirk" contract for every other spend knob.
    """
    try:
        link = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        idx = link.find(marker)
        if idx != -1:
            return ZoneInfo(link[idx + len(marker) :])
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def resolve_spend_accounting_timezone(config: dict[str, Any] | None) -> tzinfo:
    """Resolve the timezone the per-day spend ceilings account against (athenaeum#1136).

    Every per-day ceiling (:func:`resolve_spend_max_tokens_per_day`,
    :func:`resolve_spend_max_usd_per_day`, and the weekly-percent derivation
    in :func:`resolve_spend_max_pct_per_day`) is enforced by
    :func:`athenaeum.spend.ceiling_tripped` against
    :func:`athenaeum.spend.spend_today`'s "since the start of the accounting
    day" window — this is the knob that decides where that day BEGINS.

    **Why this defaults to the system's local timezone, not UTC** (issue
    athenaeum#1136): a per-day ceiling is an OPERATOR budget — "how much may be
    spent today" means the operator's today. A UTC-midnight default silently
    opens the accounting window mid-evening for any operator west of UTC:
    for an operator in US Eastern time (UTC-4/-5), UTC midnight lands at
    20:00/19:00 local — squarely inside a typical evening working session —
    so that session can exhaust the WHOLE day's ceiling before a scheduled
    job firing after local midnight (but still inside the SAME UTC calendar
    day) ever gets a fresh window. That was observed in production: a
    nightly librarian run at 02:16 local inherited a ceiling an evening
    session had already exhausted three hours earlier, and compiled zero
    entities on every observed night. Defaulting to UTC would leave this
    starvation in place until an operator discovers the config key and sets
    it themselves — exactly what makes it a bug rather than a setting. An
    operator who already runs in UTC sees zero behavior change (their local
    day already equals the UTC day).

    Precedence: ``ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE`` env >
    ``spend.accounting_timezone`` yaml > the system's local timezone (see
    :func:`_system_local_timezone`). Both the env var and the yaml key take
    an IANA zone name (e.g. ``America/New_York``). A name
    :class:`~zoneinfo.ZoneInfo` cannot resolve — a typo, or a name absent
    from the running system's tzdata — WARNs and falls back to UTC rather
    than raising: a malformed timezone string must never crash a run any
    more than a malformed number does elsewhere in this module (see
    :func:`_env_number`).
    """
    env = os.environ.get("ATHENAEUM_SPEND_ACCOUNTING_TIMEZONE")
    name: str | None = None
    source = "env"
    if env is not None and env.strip():
        name = env.strip()
    elif isinstance(config, dict):
        cfg = config.get("spend")
        if isinstance(cfg, dict):
            raw = cfg.get("accounting_timezone")
            if isinstance(raw, str) and raw.strip():
                name = raw.strip()
                source = "yaml"
    if name is None:
        return _system_local_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.warning(
            "spend.accounting_timezone=%r (from %s) is not a valid IANA "
            "timezone name (%s) -- falling back to UTC for per-day spend "
            "accounting (issue athenaeum#1136)",
            name,
            source,
            exc,
        )
        return timezone.utc


def resolve_spend_max_tokens_per_day(config: dict[str, Any] | None) -> int | None:
    """Resolve the per-day SUBSCRIPTION token ceiling (env > yaml > None) (athenaeum#378).

    Summed across every ledger record on the subscription path since the
    start of the current ACCOUNTING day (issue athenaeum#1136 — see
    :func:`resolve_spend_accounting_timezone`; UTC by default only when the
    operator's own local timezone is UTC), plus the current run's accrued
    tokens. Precedence: ``ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY`` env >
    ``spend.max_tokens_per_day`` yaml > ``None`` (no ceiling).
    """
    return _resolve_optional_positive_number(
        config,
        "spend",
        "max_tokens_per_day",
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY",
        cast=int,
    )


def resolve_spend_max_usd_per_run(config: dict[str, Any] | None) -> float | None:
    """Resolve the per-run API DOLLAR ceiling (env > yaml > None) (athenaeum#378).

    A run served by the metered ``anthropic`` API path is constrained in real
    dollars. When set and the run's estimated USD reaches it, the pass stops
    early and loudly. Precedence: ``ATHENAEUM_SPEND_MAX_USD_PER_RUN`` env >
    ``spend.max_usd_per_run`` yaml > ``None`` (no ceiling).
    """
    return _resolve_optional_positive_number(
        config,
        "spend",
        "max_usd_per_run",
        "ATHENAEUM_SPEND_MAX_USD_PER_RUN",
        cast=float,
    )


def resolve_spend_max_usd_per_day(config: dict[str, Any] | None) -> float | None:
    """Resolve the per-day API DOLLAR ceiling (env > yaml > None) (athenaeum#378).

    Summed across every ledger record on the metered API path since the
    start of the current ACCOUNTING day (issue athenaeum#1136 — see
    :func:`resolve_spend_accounting_timezone`; UTC by default only when the
    operator's own local timezone is UTC), plus the current run's accrued
    USD. Precedence: ``ATHENAEUM_SPEND_MAX_USD_PER_DAY`` env >
    ``spend.max_usd_per_day`` yaml > ``None`` (no ceiling).
    """
    return _resolve_optional_positive_number(
        config,
        "spend",
        "max_usd_per_day",
        "ATHENAEUM_SPEND_MAX_USD_PER_DAY",
        cast=float,
    )


def resolve_spend_weekly_token_limit(config: dict[str, Any] | None) -> int | None:
    """Resolve the operator-declared SUBSCRIPTION weekly token limit (athenaeum#785).

    Claude Code subscription limits are rolling-window and are not exposed to
    athenaeum as a readable quota, so there is no denominator to derive a
    percentage ceiling from until the operator states one. This value is that
    denominator — combined with :func:`resolve_spend_max_pct_per_day` it
    produces an effective daily subscription token ceiling of
    ``weekly_token_limit / 7 * (max_pct_per_day / 100)`` (see
    :func:`athenaeum.spend.ceiling_tripped`). On its own (the other knob
    unset) it does nothing — strictly opt-in, like every other ceiling.
    Precedence: ``ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT`` env >
    ``spend.weekly_token_limit`` yaml > ``None`` (no ceiling).
    """
    return _resolve_optional_positive_number(
        config,
        "spend",
        "weekly_token_limit",
        "ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT",
        cast=int,
    )


def resolve_spend_max_pct_per_day(config: dict[str, Any] | None) -> float | None:
    """Resolve the max-percent-of-weekly-allowance-per-day knob (athenaeum#785).

    Paired with :func:`resolve_spend_weekly_token_limit`: this is the percentage
    taken OF that weekly figure to produce a daily subscription token ceiling.
    On its own (the weekly limit unset) it does nothing — there is no
    denominator to apply a percentage to, so setting only one of the two knobs
    leaves behavior unchanged, exactly like every other ceiling's opt-in
    contract. Precedence: ``ATHENAEUM_SPEND_MAX_PCT_PER_DAY`` env >
    ``spend.max_pct_per_day`` yaml > ``None`` (no ceiling).
    """
    return _resolve_optional_positive_number(
        config,
        "spend",
        "max_pct_per_day",
        "ATHENAEUM_SPEND_MAX_PCT_PER_DAY",
        cast=float,
    )


#: Default headroom-warning threshold (issue athenaeum#926): a run that ends at
#: or above this percentage of EITHER API dollar ceiling
#: (:func:`resolve_spend_max_usd_per_run` / :func:`resolve_spend_max_usd_per_day`)
#: gets a warning before the ceiling itself trips. See
#: :func:`resolve_spend_warning_threshold_pct`.
DEFAULT_SPEND_WARNING_THRESHOLD_PCT = 75.0


def resolve_spend_warning_threshold_pct(config: dict[str, Any] | None) -> float:
    """Resolve the spend-headroom warning threshold, as a percent of either
    API dollar cap (issue athenaeum#926).

    Unlike the ceilings above, this knob is NOT opt-in — it always resolves to
    a usable value (:data:`DEFAULT_SPEND_WARNING_THRESHOLD_PCT` when unset),
    because the warning it gates is meant to fire by default the first time a
    run gets close to a ceiling the operator already configured; there is
    nothing to warn about when neither ``max_usd_per_run`` nor
    ``max_usd_per_day`` is set (see :func:`athenaeum.spend.spend_headroom`,
    which reports a distinct "not configured" state for that case rather than
    reading as 0% or 100% consumed). Precedence:
    ``ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT`` env > ``spend.warning_threshold_pct``
    yaml > ``75.0``. A ``bool`` / non-numeric / ``<= 0`` value (env or yaml)
    falls through to the default — a zero/negative threshold would warn on
    every run, including one that spent nothing. No seed in ``_DEFAULTS``
    (issue athenaeum#231), matching every other spend knob in this module.
    """
    value = _env_number("ATHENAEUM_SPEND_WARNING_THRESHOLD_PCT", float)
    if value is not None and value > 0:
        return value
    if isinstance(config, dict):
        cfg = config.get("spend")
        if isinstance(cfg, dict):
            raw = cfg.get("warning_threshold_pct")
            if raw is not None and not isinstance(raw, bool):
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and value > 0:
                    return value
    return DEFAULT_SPEND_WARNING_THRESHOLD_PCT


#: Code default for the classify-model knob (env ``ATHENAEUM_CLASSIFY_MODEL`` >
#: yaml ``models.classify`` > this literal, via :func:`resolve_model`).
#: Single-sourced HERE (issue athenaeum#640) rather than in :mod:`athenaeum.tiers`:
#: ``contradictions``, ``reasoning_tiers``, ``query_topics`` and ``claim_kind``
#: all read it, and importing it top-level from the L4 ``tiers`` hub was the
#: ``contradictions`` -> ``tiers`` back-edge that pinned the
#: ``{answers, contradictions, resolutions, tiers}`` residual import SCC (athenaeum#545
#: audit M8). ``config`` is a low leaf every reader can depend on acyclically.
DEFAULT_CLASSIFY_MODEL = "claude-haiku-4-5-20251001"


def resolve_model(
    knob: str,
    env_var: str,
    default: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Resolve a model id from env > yaml ``models.<knob>`` > code default.

    Issue athenaeum#232. Mirrors :func:`athenaeum.librarian.librarian_max_api_calls`:
    the env var wins over the yaml key so an operator can swap a model for a
    single run without editing config, and the yaml key is read only when
    the operator actually set it — no seed in ``_DEFAULTS`` (issue athenaeum#231).
    Non-string or blank yaml values fall through to *default*. The
    contradiction-resolver model routes through here too, via
    :func:`athenaeum.resolutions._get_model` (knob ``resolve``); that
    wrapper threads the legacy ``resolve.model`` yaml key in as *default*
    so it sits below ``models.resolve`` but above the code default.
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


def resolve_model_rates(
    config: dict[str, Any] | None,
) -> dict[str, tuple[float, float]]:
    """Resolve the per-MTok pricing table from ``athenaeum.yaml``'s ``pricing:``
    section (issue athenaeum#783).

    ``pricing.<prefix>: [input_usd_per_mtok, output_usd_per_mtok]`` — the SAME
    longest-prefix-match convention :func:`athenaeum.models._rates_for_model`
    already uses for the code-default table (issue athenaeum#247), so a dated model id
    (``claude-sonnet-4-6-20260301``) still resolves via the shortest prefix
    that matches. Only a yaml layer exists for this knob — no env-var
    override (a whole pricing TABLE is not the single scalar the existing
    ``ATHENAEUM_*`` env convention fits) and, deliberately, no per-prefix code
    default merged in HERE: this function returns EXACTLY what
    ``athenaeum.yaml`` says, nothing more. See
    :func:`athenaeum.models.configure_model_rates` for what an empty return
    does at the call site (falls back to the code-default table WHOLESALE,
    not per missing prefix) and the athenaeum#783 issue's "Design decision" for why a
    per-prefix merge (yaml overlaying the code table, which stays the floor
    for anything yaml omits) was rejected: an omission in yaml would keep
    silently reading the code default — the invisible second source of truth
    the athenaeum#783 startup preflight (:func:`preflight_model_rates`) exists to kill
    for a model a run actually resolves to.

    Schema contract (athenaeum#783 "Schema note for the implementer"): **one rate per
    prefix, no mode dimension.** A prefix key cannot express a time-boxed
    promo rate (Sonnet 5's introductory $2/$10 through 2026-08-31 — Occam
    decision 2026-07-31, deliberately not encoded: a prefix-keyed rate cannot
    expire, so a promo would go silently wrong the day it ends) or a
    per-request-mode rate (Opus 5's ``speed: "fast"`` $10/$50 — athenaeum does
    not use fast mode). Both are explicitly out of scope for athenaeum#783; if either is
    ever needed, it is a schema change here (e.g. a mode-keyed sub-block), not
    a workaround layered on top of this function.

    Malformed entries WARN and are REJECTED (excluded from the returned
    dict — treated as unset for that prefix), mirroring
    :func:`athenaeum.provider.resolve_max_tokens`'s malformed-override
    convention rather than inventing a new one: wrong arity (not exactly 2
    elements), a non-numeric element, a ``bool`` element (``bool`` is an
    ``int`` subclass — ``[true, false]`` must not silently become
    ``[1.0, 0.0]``), or a negative rate.
    """
    rates: dict[str, tuple[float, float]] = {}
    if not isinstance(config, dict):
        return rates
    pricing_cfg = config.get("pricing")
    if not isinstance(pricing_cfg, dict):
        return rates
    for prefix, raw in pricing_cfg.items():
        if not isinstance(prefix, str) or not prefix.strip():
            continue
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or any(isinstance(v, bool) for v in raw)
            or not all(isinstance(v, (int, float)) for v in raw)
        ):
            logger.warning(
                "Ignoring malformed pricing entry for %r: expected "
                "[input_usd_per_mtok, output_usd_per_mtok], got %r",
                prefix,
                raw,
            )
            continue
        input_rate, output_rate = float(raw[0]), float(raw[1])
        if input_rate < 0 or output_rate < 0:
            logger.warning(
                "Ignoring pricing entry for %r with a negative rate: %r",
                prefix,
                raw,
            )
            continue
        rates[prefix.strip()] = (input_rate, output_rate)
    return rates


def preflight_model_rates(resolved_models: Iterable[tuple[str, str]]) -> str | None:
    """Return a startup error message if any RESOLVED model has no per-MTok
    price, else ``None`` (issue athenaeum#783).

    Mirrors :func:`athenaeum.provider.preflight_provider`'s pattern: validate
    at startup and fail LOUDLY (rc 1) rather than letting an unpriced model
    silently resolve to the blended fallback at cost-calculation time — the
    direction that errs DOWNWARD and disarms a spend ceiling (the athenaeum#777
    Fable/Mythos incident under-reported spend 6.67x this exact way, before it
    was caught and fixed by hand).

    *resolved_models* is ``(knob, model_id)`` pairs — e.g.
    ``[("classify", "claude-haiku-4-5-20251001"), ("write", "claude-sonnet-5"),
    ...]`` — the model id each LLM-serving knob resolves to for THIS run,
    via that knob's own env > yaml > default precedence, unchanged. The
    CALLER assembles this list (this module must not import the L3+
    knob-resolver modules — ``tiers``, ``contradictions``, ``query_topics``,
    ``resolutions``, ``reasoning_tiers`` — per the module docstring's
    layering rule; see :func:`athenaeum.librarian._resolve_run_models`),
    normally right after calling :func:`athenaeum.models.configure_model_rates`
    so this check runs against the table the run will actually price against.

    Checks :func:`athenaeum.models.model_has_price` — a real longest-prefix
    match against the ACTIVE table with NO blended fallback — the same test
    a miss would otherwise (silently) fail at cost-calculation time.
    """
    unpriced = [
        (knob, model) for knob, model in resolved_models if model and not model_has_price(model)
    ]
    if not unpriced:
        return None
    detail = "; ".join(
        f"knob {knob!r} resolved to model {model!r} — set pricing.{model} "
        "(or a shorter matching prefix) in athenaeum.yaml"
        for knob, model in unpriced
    )
    return (
        "the following resolved model(s) have no per-MTok price and would "
        f"silently under-report spend via the blended fallback: {detail}. "
        "Refusing to start (issue athenaeum#783)."
    )


def resolve_screening(config: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Resolve intake-screening settings for ``remember()`` (issue athenaeum#320).

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


def resolve_dimensions(config: dict[str, Any] | None) -> Any:
    """Resolve the ``dimensions:`` config block into a validated registry (athenaeum#714).

    Returns a :class:`athenaeum.dimensions.DimensionRegistry` — always
    non-empty: the six kernel dimensions (recorded-time, observed-time,
    valid-time, scope, subject, memory-class) are builtin and present
    regardless of config. ``dimensions:`` in ``athenaeum.yaml`` is a list of
    ADDITIONAL, deployment-declared dimensions (``engagement``, ``repo``,
    ``maturity``, ...) layered on top; a fresh install with no ``dimensions:``
    key gets the kernel-only registry, and ``athenaeum run`` behaves
    unchanged either way (nothing in the librarian pipeline consults a
    deployment dimension's ``applies_to`` unless one is declared).

    No env var: ``dimensions:`` is a structural block (a list of typed
    entries), not a scalar knob — there is no single value an env override
    could sensibly replace. Raises
    :class:`athenaeum.dimensions.DimensionRegistryError` on a malformed
    entry (unknown ``kind``/``null_means``/``state``, non-kebab-case name,
    duplicate name, an ``enum`` kind missing ``values``, or a name colliding
    with a kernel dimension) — a mis-declared dimension is a real config
    error, not something to silently drop, matching ``resolve_screening``'s
    fail-loud posture for a structural (not scalar) knob.
    """
    from athenaeum.dimensions import DimensionRegistryError, build_registry

    raw = config.get("dimensions") if isinstance(config, dict) else None
    try:
        return build_registry(raw)
    except DimensionRegistryError:
        raise


def resolve_dimension_registry_epoch(config: dict[str, Any] | None) -> int:
    """Resolve the dimension-registry epoch, for the verdict-ledger basis (athenaeum#714).

    Bump ``librarian.dimensions_registry_epoch`` in ``athenaeum.yaml``
    whenever a dimension's definition changes in a way that should
    invalidate verdicts justified by the old definition (issue athenaeum#714 AC:
    "Both must appear in the ledger basis of any verdict written after this
    issue" — see :class:`athenaeum.verdicts.Basis`). Namespaced under
    ``librarian.*`` alongside its athenaeum#712 sibling knobs
    (``verdict_ledger_enabled``, ``verdict_epoch_batch_interval_days``), same
    helper/coercion contract. Precedence:
    ``ATHENAEUM_DIMENSION_REGISTRY_EPOCH`` env > yaml
    ``librarian.dimensions_registry_epoch`` > ``1``. No seed in ``_DEFAULTS``
    (issue athenaeum#231).
    """
    return _resolve_positive_int_knob(
        config,
        "dimensions_registry_epoch",
        "ATHENAEUM_DIMENSION_REGISTRY_EPOCH",
        1,
    )


def resolve_dimension_tree_epoch(config: dict[str, Any] | None) -> int:
    """Resolve the scope-tree epoch, for the verdict-ledger basis (athenaeum#714).

    Bump ``librarian.dimensions_tree_epoch`` in ``athenaeum.yaml`` on a
    scope-tree reorg (renamed subtree) so athenaeum#712's targeted stale-marking
    (:func:`athenaeum.verdicts.select_stale_for_tree_epoch_bump`) can
    invalidate exactly the verdicts whose basis coordinates touch the
    renamed subtree. Precedence: ``ATHENAEUM_DIMENSION_TREE_EPOCH`` env >
    yaml ``librarian.dimensions_tree_epoch`` > ``1``. No seed in ``_DEFAULTS``
    (issue athenaeum#231).
    """
    return _resolve_positive_int_knob(
        config,
        "dimensions_tree_epoch",
        "ATHENAEUM_DIMENSION_TREE_EPOCH",
        1,
    )


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

# Serve-time read-scope audience (issue athenaeum#312). Pins the MCP `serve` process
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

# Intake screening at remember() time (issue athenaeum#320). The write-side complement
# to athenaeum#312's read-time scoping: classifies sensitive raw intake and stamps a
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

# Dimension registry (issue athenaeum#714). Athenaeum ships the six KERNEL
# dimensions (recorded-time, observed-time, valid-time, scope, subject,
# memory-class) unconditionally — they need no config. `dimensions:` here
# declares ADDITIONAL, deployment-specific axes on top; UNSET = kernel-only
# (existing installs unchanged). See docs/configuration.md's "Dimension
# registry" section for the full field reference.
# dimensions:
#   - name: engagement          # kebab-case, unique; must not collide with a
#                                # kernel dimension name
#     kind: identity             # interval | hierarchy | enum | identity
#     null_means: unknown        # universal | unknown
#     separates: true            # separator (may yield DISTINCT) vs sequencer
#     applies_to:                # selector bounding which claims carry this
#       memory_class: [entity]   # axis; {} / omitted = applies to every claim
#     state: backfill            # backfill | enforced
#     origin: operator           # builtin | operator | proposed:<id>
#     since: 2026-08-01
# librarian:
#   dimensions_registry_epoch: 1  # bump on a dimension-definition change
#   dimensions_tree_epoch: 1      # bump on a scope-tree reorg (renamed subtree)

# Workspace owner identity (issue athenaeum#263). Designates the single canonical
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

# Person dedup join keys (issue athenaeum#269). The merge always dedups on the
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
#   issue athenaeum#196). Higher = tighter clusters; 0.55 is tuned against the
#   near-duplicate clustering fixture.
# cluster_output: canonical JSONL output path (relative to knowledge
#   root). Each run also writes a timestamped sibling and atomically
#   replaces this path.
# rotation_retention: number of timestamped cluster-report rotations to
#   keep; older ones are pruned after each run (issue athenaeum#311). Rotations are
#   debugging artifacts, not recovery-critical (recovery is git-based).
#   Precedence: ATHENAEUM_ROTATION_RETENTION env, then this key, then 30.
#   0 (or negative) disables pruning (keep all).
# max_files: per-run intake batch size — stop after processing this many
#   raw files (issue athenaeum#232). Precedence: --max-files CLI flag, then
#   ATHENAEUM_MAX_FILES env, then this key, then 50.
# batch_mode: submit tier-2/tier-3 LLM calls via the Anthropic Messages
#   Batch API at a 50% token discount (issue athenaeum#236). Latency-tolerant:
#   most batches finish within an hour, 24h worst case — intended for the
#   nightly run. Precedence: --batch-mode CLI flag, then
#   ATHENAEUM_BATCH_MODE env, then this key, then off.
# batch: PER-KNOB batch selection (issue athenaeum#1175), under this
#   `librarian:` parent — deliberately NOT a new `llm.batch`, because batch
#   is a property of how the librarian RUN is executed, not of the LLM
#   routing layer, and it must fall back to `librarian.batch_mode`.
#     librarian:
#       batch:
#         classify: false
#         write: true
#   Only `classify` and `write` are ever batched. An absent knob key falls
#   back to the resolved `batch_mode`, so a config that sets only
#   `batch_mode` is unchanged. A knob set here turns the run into a batch run
#   even when `batch_mode` is off — but `--no-batch-mode` remains a hard off
#   that no yaml key can defeat. Setting a NON-batchable knob to true is a
#   config error (the run refuses), not a silent no-op.
# retire: move-then-retire of raw auto-memory (issue athenaeum#261). DEFAULT ON.
#   When on, `athenaeum run` MOVES non-contradictory raw/auto-memory facts
#   into their wiki entry and `git rm`s the raw (recovery is git-only).
#   Set false to disable; the --no-retire CLI flag overrides to off. See
#   README "Data lifecycle & upgrade impact".
# ephemeral_scopes: glob patterns (matched against the auto-memory scope
#   DIRECTORY NAME) for inherently-throwaway operational scopes whose
#   intake must NEVER become a durable wiki/auto-*.md page (issue athenaeum#278).
#   A raw file in a matching scope -- or one carrying an explicit
#   `ephemeral: true` frontmatter flag -- is dropped before clustering.
#   Setting this key REPLACES the built-in defaults
#   (*hestia-routine*, *var-folders*, *private-tmp*, *-cctest-*); an empty
#   list disables scope-based dropping. Same set drives `athenaeum
#   auto-memory prune`.
# operational_markers: optional lower-cased content substrings for
#   operational families (issue athenaeum#278). CONSERVATIVE: the classifier drops
#   an intake on markers ONLY when >= 2 distinct markers are present, so a
#   single incidental word never clobbers a legit note. DEFAULT-EMPTY.
#   Markers are SUBSTRING-matched: avoid <=3-char markers (e.g. "ci" would
#   match "decision"/"specific") -- prefer distinctive multi-word phrases.
# min_cluster_cohesion: cohesion floor that suppresses low-cohesion
#   cross-scope OVER-CLUSTERS (issue athenaeum#278). A cluster whose
#   cluster_centroid_score (mean intra-cluster cosine) is strictly below
#   this value AND which spans >= min_cluster_cohesion_scopes distinct
#   origin scopes is NOT materialized into wiki/auto-*.md; its raw members
#   stay in place (not retired) for a coherent cluster to absorb later.
#   DEFAULT 0.0 (OFF) -- the ~0.47 gap that separates over-clusters
#   (<=0.46) from coherent pages (>=0.5) is corpus-specific, so a baked-in
#   floor could mis-suppress on a different corpus. Recommended opt-in for
#   the reference corpus: 0.47.
# min_cluster_cohesion_scopes: minimum distinct origin_scopes a cluster
#   must span for the cohesion floor to apply (issue athenaeum#278). DEFAULT 4 --
#   observed over-clusters span 8-17 scopes, legitimate pages 1-3, so 4
#   sits in the clean margin and a low-cohesion single-/few-scope cluster
#   is never suppressed.
# max_merge_sources: merge-PROPOSAL fan-in cap (athenaeum#400, tightened athenaeum#421).
#   A propose_merge folding more than this many sources is suppressed before
#   it reaches wiki/_pending_merges.md (a merge proposal is a pairwise /
#   small-group refinement, not a mega-fold). DEFAULT 5 (active); env
#   ATHENAEUM_MAX_MERGE_SOURCES > this key > default; 0/negative disables.
# min_merge_mean_similarity: merge-PROPOSAL mean-pairwise-cohesion floor
#   (athenaeum#421). A proposal whose cluster mean pairwise cosine is strictly below
#   this is suppressed. DEFAULT 0.6 (ACTIVE) -- the human merge queue is
#   corpus-independent, so a modest floor ships on (unlike min_cluster_cohesion,
#   which gates durable pages and ships OFF). env
#   ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY > this key > default; 0/negative
#   disables. Complements the complete-linkage MIN-pairwise gate (athenaeum#421): a
#   single-linkage chain with a sub-threshold pair is suppressed even if its
#   mean clears this floor.
# min_merge_confidence: optional merge-PROPOSAL resolver-confidence floor
#   (athenaeum#400). DEFAULT 0.0 (OFF); opt-in. env ATHENAEUM_MIN_MERGE_CONFIDENCE >
#   this key > default.
# page_warn_bytes: soft byte threshold above which a wiki entity page is
#   reported as a WARN-level oversized page in `athenaeum status` (athenaeum#310).
#   Warn-only -- nothing is blocked or modified. Precedence:
#   ATHENAEUM_PAGE_WARN_BYTES env, then this key, then 8192. A long page
#   usually means poorly-factored knowledge to split into linked entities.
# page_flag_bytes: louder byte threshold above which a page is FLAGGED for
#   splitting -- surfaced in `status` and logged as a non-fatal WARNING
#   during `athenaeum run` (athenaeum#310). Still warn-only (the tier-3 merge body
#   cap is separate and unchanged). Precedence: ATHENAEUM_PAGE_FLAG_BYTES
#   env, then this key, then 16384. Keep comfortably below the merge cap.
# drain_warn_days: backlog-drain ETA threshold in days (issue athenaeum#470). At the
#   end of any run that leaves raw intake undrained (and in `athenaeum
#   status`), the advisor projects time-to-drain from OBSERVED throughput
#   (the athenaeum#378 spend ledger) and emits a WARNING naming the `athenaeum drain`
#   remedy only when the projection EXCEEDS this many days; below it stays
#   silent. Precedence: this key, then 3. bool/non-positive fall through.
# name_collision_scan: nightly deterministic name-collision detector opt-out
#   (issue athenaeum#1170). DEFAULT TRUE -- the scan is a glob + a dict grouping
#   (no LLM, no vectors, no network), so there is no cost reason to ship it
#   off. See resolve_name_collision_scan_enabled. bool values only; anything
#   else falls through to true.
# name_collision_automerge: auto-merge the UNAMBIGUOUS subset of collisions
#   the scan above finds (issue athenaeum#1170). DEFAULT FALSE, deliberately --
#   issue athenaeum#1170 was split from the one-time destructive repair sweep
#   over collisions ALREADY PRESENT in the operator's live corpus (issue
#   athenaeum#1246, ~operator-gated and blocked by this issue); shipping
#   auto-merge on by default would make the very next nightly run perform
#   that unattended sweep, defeating the split. An operator who sets this
#   true gets auto-merge of the unambiguous subset only -- an ambiguous
#   collision always queues for human review regardless -- and every
#   auto-merge is reversible via git by construction. See
#   resolve_name_collision_automerge_enabled. bool values only; anything
#   else falls through to false.
# librarian:
#   cluster_threshold: 0.55
#   cluster_output: raw/_librarian-clusters.jsonl
#   rotation_retention: 30
#   max_files: 50
#   batch_mode: false
#   batch:
#     classify: false
#     write: false
#   retire: true
#   ephemeral_scopes:
#     - "*hestia-routine*"
#     - "*var-folders*"
#     - "*private-tmp*"
#     - "*-cctest-*"
#   operational_markers: []
#   min_cluster_cohesion: 0.0
#   min_cluster_cohesion_scopes: 4
#   max_merge_sources: 5
#   min_merge_mean_similarity: 0.6
#   min_merge_confidence: 0.0
#   page_warn_bytes: 8192
#   page_flag_bytes: 16384
#   drain_warn_days: 3
#   name_collision_scan: true
#   name_collision_automerge: false

# LLM provider selection (issue athenaeum#330). Chooses the backend the librarian
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

# Model selection (issue athenaeum#232). Per knob: env var wins over the yaml key,
# which wins over the built-in default. Values are free-form model id
# strings passed to the Anthropic SDK.
# classify: Tier-2 classifier + C4 contradiction detector
#   (env: ATHENAEUM_CLASSIFY_MODEL).
# write: Tier-3 writer (env: ATHENAEUM_WRITE_MODEL).
# topic: recall query-topic extraction (env: ATHENAEUM_TOPIC_MODEL).
# resolve: contradiction resolver (env: ATHENAEUM_RESOLVE_MODEL). The legacy
#   ``resolve.model`` key below still works, but is checked only when
#   ``models.resolve`` is unset. Prefer this block for all four knobs.
# models:
#   classify: claude-haiku-4-5-20251001
#   write: claude-sonnet-5
#   topic: claude-haiku-4-5-20251001
#   resolve: claude-opus-5

# Per-MTok pricing table (issue athenaeum#783). UNLIKE the sections above, this
# block is ACTIVE (not commented out): athenaeum.yaml is the authoritative
# source for model pricing, and an unpriced model a run resolves to is a
# LOUD startup failure (exits non-zero, naming the model and this key) rather
# than a silent fall-through to the blended average — so a fresh install must
# ship priced correctly out of the box. Seeded here from the current
# athenaeum.models._MODEL_RATES_USD_PER_MTOK table (edit that constant, not
# this file, when a vendor price changes — this block is regenerated by
# write_default_config() from that single update site, never hand-copied).
# Each entry is ``<model-id-prefix>: [input_usd_per_mtok, output_usd_per_mtok]``,
# matched by LONGEST prefix so a dated id (e.g. claude-haiku-4-5-20251001)
# resolves to the right family. Schema contract: ONE rate per prefix, no mode
# dimension — a time-boxed promo rate or a per-request-mode rate (e.g.
# Opus 5 "fast" mode) cannot be expressed here by design; both are out of
# scope for athenaeum#783. Malformed entries (wrong arity, non-numeric, negative) warn
# and are ignored, same as every other yaml knob.
{{PRICING_YAML_BLOCK}}

# Cross-scope contradiction detection (issue athenaeum#125).
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
# Opus-backed resolver caps (issue athenaeum#126).
# resolve_max_per_run: cap on resolver calls per ingest. Surplus contradictions
#   are escalated without a proposal (degraded mode). Default raised from
#   50 to 250 in issue athenaeum#187. Env override: ATHENAEUM_RESOLVE_MAX_PER_RUN.
# contradiction:
#   cross_scope_mode: ancestor
#   cluster_size_cap: 25
#   similarity_threshold: 0.85
#   resolve_max_per_run: 250  # raised from 50 in athenaeum#187
#   resolved_similarity_threshold: 0.83  # cosine threshold, decision-log match (athenaeum#211)
#   not_a_conflict_ttl_days: 0  # decay stale auto not_a_conflict (athenaeum#251); 0 = off

# Contradiction resolver (issue athenaeum#126). See docs/auto-resolve.md for the
# full knob set (auto_apply, auto_apply_threshold, full_body_token_cap).
# model: LEGACY key for the model used to propose a winner once Haiku flags a
#   contradiction. Prefer ``models.resolve`` above; this key is read only when
#   ``models.resolve`` is unset, and is kept working for pre-existing configs.
#   Defaults to claude-opus-5. Env override: ATHENAEUM_RESOLVE_MODEL.
# resolve:
#   model: claude-opus-5

# Pluggable storage-surface layer (issue athenaeum#429). Maps each entity class (the
# wiki frontmatter `type`) onto a STORAGE ADAPTER — a backing store + a corpus
# policy (embedded / recallable / merge_eligible). UNSET = every class uses the
# built-in `wiki-markdown-embedded` surface (the flat wiki/, full corpus
# participation) — byte-identical to pre-athenaeum#429 behavior. Two adapters ship built
# in and need no definition here: `wiki-markdown-embedded` (default) and
# `excluded` (a surface OUTSIDE wiki/, no embed/recall/merge — what athenaeum#427's PII
# surface consumes). Adding a surface is config + a mapping, no core change.
# NOTE: this is a STORAGE-surface adapter, NOT the source→raw-intake adapter of
# docs/adapter-contract.md — different concept, opposite ends of the pipeline.
#
#   adapters:                      # custom adapters (built-ins are implicit)
#     contacts-excluded:
#       backing_store: markdown    # required
#       surface_root: contacts     # required; relative to knowledge root,
#                                  #   or absolute. Keep OUTSIDE wiki/ to be
#                                  #   excluded by construction.
#       corpus_policy:             # each key FAILS CLOSED (omitted => false)
#         embedded: false
#         recallable: false
#         merge_eligible: false
#   mapping:                       # entity `type` -> adapter name
#     pii: excluded                # route the pii class to the built-in surface
# storage:
#   mapping:
#     pii: excluded

# Authority manifest (issue athenaeum#426). Maps authoritative LIVE sources (skill
# files, code paths, config) to the topics/slugs they own, so a memory that
# merely duplicates content a live source already owns can be detected and
# converted to a one-line pointer stub instead of persisting a full copy.
# Precedence: ATHENAEUM_AUTHORITY_MANIFEST env > this key > default
# `<knowledge_root>/authority-manifest.yaml`. See docs/authority-manifest.md
# and `athenaeum authority --help`.
# librarian:
#   authority_manifest_path: authority-manifest.yaml
"""


def _render_pricing_yaml_block() -> str:
    """Render the code-default rate table as an active ``pricing:`` yaml
    block (issue athenaeum#783), for :func:`write_default_config`.

    Generated from :func:`athenaeum.models.default_model_rates` — the SAME
    literal a maintainer edits when a vendor price changes — never a
    hand-copied second list, so ``_DEFAULT_CONFIG_CONTENT``'s static
    surrounding prose stays the only thing anyone edits by hand.
    """
    lines = ["pricing:"]
    for prefix, (input_rate, output_rate) in default_model_rates().items():
        lines.append(f"  {prefix}: [{input_rate}, {output_rate}]")
    return "\n".join(lines)


def write_default_config(knowledge_root: Path) -> Path:
    """Write the default config file if it doesn't exist. Returns the path."""
    config_path = knowledge_root / "athenaeum.yaml"
    if not config_path.exists():
        content = _DEFAULT_CONFIG_CONTENT.replace(
            "{{PRICING_YAML_BLOCK}}", _render_pricing_yaml_block()
        )
        config_path.write_text(content, encoding="utf-8")
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
    """Read a ``recall.<key>`` list of glob strings (issue athenaeum#348).

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
    """Resolve ``(include_globs, exclude_globs)`` for corpus scoping (issue athenaeum#348).

    COULD-tier footprint/relevance knob. Default (unset) returns
    ``(None, None)`` — index everything — because the Apollo contact wikis
    are legitimate name-recall targets and must stay indexed by default.
    """
    return (
        _resolve_glob_list(config, "include_globs"),
        _resolve_glob_list(config, "exclude_globs"),
    )


def resolve_embedding_model(config: dict[str, Any] | None) -> str | None:
    """Resolve the configured vector embedding model (issue athenaeum#315 seam).

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


def resolve_authority_manifest_path(
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> Path:
    """Resolve the authority manifest path (issue athenaeum#426).

    The authority manifest maps authoritative LIVE sources (skill files, code
    paths, config) to the topics/slugs they own, so the librarian can detect a
    memory that merely duplicates content a live source already owns. Mirrors
    the module's standard precedence (env > yaml > default), matching
    :func:`resolve_spend_ledger_path`'s "explicit path override" shape:

    - ``ATHENAEUM_AUTHORITY_MANIFEST`` env — explicit path (highest).
    - ``librarian.authority_manifest_path`` yaml — relative values are
      resolved against ``knowledge_root``; absolute values pass through.
    - default: ``<knowledge_root>/authority-manifest.yaml`` — a sibling of
      ``athenaeum.yaml`` at the knowledge root, following the same "config
      lives at the root of the knowledge tree" convention.

    Does not check for existence — callers (:func:`athenaeum.authority.
    load_authority_manifest`) handle a missing file as "no manifest configured"
    (empty, not an error). No seed in ``_DEFAULTS`` (issue athenaeum#231) so this code
    default stays reachable.
    """
    env = os.environ.get("ATHENAEUM_AUTHORITY_MANIFEST")
    if env is not None and env.strip():
        return Path(env).expanduser()

    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("authority_manifest_path")
            if isinstance(raw, str) and raw.strip():
                candidate = Path(raw.strip()).expanduser()
                if not candidate.is_absolute():
                    candidate = knowledge_root / candidate
                return candidate

    return knowledge_root / "authority-manifest.yaml"


def resolve_storage_mapping(config: dict[str, Any] | None) -> dict[str, str]:
    """Resolve the ``storage.mapping`` entity-class → adapter-name table (athenaeum#429).

    Maps a wiki frontmatter ``type`` (``person``, ``pii``, …) onto the name of
    a storage adapter (``wiki-markdown-embedded``, ``excluded``, or a custom
    one). Returns an EMPTY dict when unset — the code default that keeps every
    class on the default wiki surface, so an unconfigured base is byte-identical
    (issue athenaeum#231: no seed in ``_DEFAULTS`` so this default stays reachable).
    Non-string keys/values and blank entries are dropped defensively.
    """
    if not isinstance(config, dict):
        return {}
    storage_cfg = config.get("storage") or {}
    if not isinstance(storage_cfg, dict):
        return {}
    raw = storage_cfg.get("mapping")
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for cls, adapter_name in raw.items():
        if not isinstance(cls, str) or not cls.strip():
            continue
        if not isinstance(adapter_name, str) or not adapter_name.strip():
            continue
        mapping[cls.strip()] = adapter_name.strip()
    return mapping


def resolve_storage_adapters(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Resolve the ``storage.adapters`` custom-adapter definitions (athenaeum#429).

    Returns the raw (still-primitive) per-adapter mapping dicts keyed by adapter
    name; :func:`athenaeum.storage.available_adapters` validates each and builds
    the :class:`~athenaeum.storage.StorageAdapter` objects. Returns an EMPTY
    dict when unset — the built-in ``wiki-markdown-embedded`` and ``excluded``
    adapters are always available regardless. Non-string keys and non-mapping
    values are dropped defensively (a malformed entry is surfaced loudly later,
    at build time, with the adapter name in the message).
    """
    if not isinstance(config, dict):
        return {}
    storage_cfg = config.get("storage") or {}
    if not isinstance(storage_cfg, dict):
        return {}
    raw = storage_cfg.get("adapters")
    if not isinstance(raw, dict):
        return {}
    adapters: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(definition, dict):
            continue
        adapters[name.strip()] = definition
    return adapters


def resolve_sensitivity_classes(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Resolve the ``sensitivity.classes`` class-definition blocks (athenaeum#910, S1b).

    Returns the raw (still-unvalidated) per-class mapping dicts keyed by class
    name; :func:`athenaeum.sensitivity.available_classes` validates each,
    resolves ``inherits`` chains, and builds the
    :class:`~athenaeum.sensitivity.SensitivityClass` objects. Returns an EMPTY
    dict when unset — the shipped built-in ``pii`` class (defined in
    :data:`athenaeum.sensitivity._BUILTIN_CLASSES`, not here — this dict's
    own source-of-truth rule, §2.4 of ``docs/sensitivity-class-vocabulary.md``)
    is still resolved by ``available_classes`` regardless, so this resolver is
    NOT seeded in ``_DEFAULTS``: seeding it here would make the code default
    unreachable, the exact athenaeum#187 regression that rule exists to
    prevent. Non-string keys and non-mapping values are dropped defensively
    (a malformed entry is surfaced loudly later, at build time, with the
    class name in the message — same posture as :func:`resolve_storage_adapters`).
    """
    if not isinstance(config, dict):
        return {}
    sensitivity_cfg = config.get("sensitivity") or {}
    if not isinstance(sensitivity_cfg, dict):
        return {}
    raw = sensitivity_cfg.get("classes")
    if not isinstance(raw, dict):
        return {}
    classes: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(definition, dict):
            continue
        classes[name.strip()] = definition
    return classes


VALID_SENSITIVITY_ROUTING_ACTIONS = ("route", "off")


class SensitivityRoutingConfigError(ValueError):
    """Raised when the ``sensitivity.routing`` config block is invalid.

    Mirrors :class:`athenaeum.screening.ScreeningConfigError` /
    :class:`athenaeum.storage.StorageConfigError` /
    :class:`athenaeum.sensitivity.SensitivityConfigError`: loud by design. A
    malformed ``enabled`` flag or an unknown per-class ``action`` must never
    silently fall back to a default — that could route (or fail to route) a
    sensitivity class the operator did not intend.
    """


def resolve_sensitivity_routing(config: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the ``sensitivity.routing`` config surface (athenaeum#949 design note §8).

    Slice 1/4 of athenaeum#949's design note (`docs/sensitivity-value-routing.md`).

    Returns ``{"enabled": bool, "classes": {<name>: {"action": "route"|"off"}}}``.
    A separate axis from :func:`resolve_sensitivity_classes` (athenaeum#910's
    own ``sensitivity.classes.*`` — the *definition* of a class): this block
    decides whether a matched class gets intercepted at intake, so a class
    can be defined without being routed. This slice adds no behavior on its
    own — nothing reads this resolver yet (issue athenaeum#1022; see
    athenaeum#1023-athenaeum#1025 for the slices that do).

    Precedence per the module convention (env > yaml > default, no seed in
    ``_DEFAULTS`` so the code default stays reachable):
    ``ATHENAEUM_SENSITIVITY_ROUTING_ENABLED`` env (``true``/``false``,
    case-insensitive) > ``sensitivity.routing.enabled`` yaml > ``False``
    (dark by default — the whole stage is a no-op, byte-identical to
    pre-athenaeum#949 behavior, until an operator opts in).

    Each entry under ``sensitivity.routing.classes.<name>`` may set
    ``action`` to ``"route"`` or ``"off"``; when a class block is present but
    ``action`` is unset, it defaults to ``"route"`` (defining a class and
    turning routing on is read as "protect it" unless the operator
    explicitly opts the class out).

    Raises :class:`SensitivityRoutingConfigError` on a malformed ``enabled``
    value (yaml value that isn't a bool, or an env value that isn't
    ``true``/``false``) or an unknown per-class ``action`` — fail loud, no
    silent fallback, matching :class:`athenaeum.screening.ScreeningConfigError`
    / :class:`athenaeum.storage.StorageConfigError`'s existing posture.
    """
    enabled = False
    classes: dict[str, dict[str, str]] = {}

    if isinstance(config, dict):
        sensitivity_cfg = config.get("sensitivity")
        if isinstance(sensitivity_cfg, dict):
            routing_cfg = sensitivity_cfg.get("routing")
            if isinstance(routing_cfg, dict):
                raw_enabled = routing_cfg.get("enabled")
                if isinstance(raw_enabled, bool):
                    enabled = raw_enabled
                elif raw_enabled is not None:
                    raise SensitivityRoutingConfigError(
                        f"sensitivity.routing.enabled={raw_enabled!r} is "
                        "invalid; expected a boolean."
                    )

                raw_classes = routing_cfg.get("classes")
                if isinstance(raw_classes, dict):
                    for name, definition in raw_classes.items():
                        if not isinstance(name, str) or not name.strip():
                            continue
                        class_name = name.strip()
                        action = "route"
                        if isinstance(definition, dict):
                            raw_action = definition.get("action")
                            if isinstance(raw_action, str) and raw_action.strip():
                                action = raw_action.strip().lower()
                        if action not in VALID_SENSITIVITY_ROUTING_ACTIONS:
                            raise SensitivityRoutingConfigError(
                                f"sensitivity.routing.classes.{class_name}."
                                f"action={action!r} is invalid; expected one "
                                f"of {VALID_SENSITIVITY_ROUTING_ACTIONS}."
                            )
                        classes[class_name] = {"action": action}

    env = os.environ.get("ATHENAEUM_SENSITIVITY_ROUTING_ENABLED")
    if env is not None and env.strip():
        env_value = env.strip().lower()
        if env_value == "true":
            enabled = True
        elif env_value == "false":
            enabled = False
        else:
            raise SensitivityRoutingConfigError(
                f"ATHENAEUM_SENSITIVITY_ROUTING_ENABLED={env!r} is invalid; "
                "expected 'true' or 'false'."
            )

    return {"enabled": enabled, "classes": classes}


def resolve_excluded_read_mapping(config: dict[str, Any] | None) -> dict[str, str]:
    """Resolve ``storage.excluded_read_mapping`` — page ``type:`` → surface class (athenaeum#885).

    The operator override for the mapping ``recall`` consults to know WHICH
    excluded surface a hit joins to. It is a different table from
    ``storage.mapping``: that one maps a class onto a storage ADAPTER, this one
    maps a wiki page's ``type:`` onto the SURFACE CLASS whose excluded record
    holds that page's excluded fields. The two names are distinct and the
    distinction is load-bearing — a page is ``type: person`` while its record
    lives on the ``pii`` surface.

    Returns an EMPTY dict when unset. The identity default plus the single
    shipped non-identity entry (``person: pii``) lives in
    :data:`athenaeum.pii.DEFAULT_EXCLUDED_READ_MAPPING`, not here, so this
    resolver reports only what the OPERATOR configured —
    ``resolve_storage_mapping``'s precedent. Non-string keys/values and blank
    entries are dropped defensively.
    """
    if not isinstance(config, dict):
        return {}
    storage_cfg = config.get("storage") or {}
    if not isinstance(storage_cfg, dict):
        return {}
    raw = storage_cfg.get("excluded_read_mapping")
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for page_class, surface_class in raw.items():
        if not isinstance(page_class, str) or not page_class.strip():
            continue
        if not isinstance(surface_class, str) or not surface_class.strip():
            continue
        mapping[page_class.strip()] = surface_class.strip()
    return mapping


def resolve_pii_scan_exclude(config: dict[str, Any] | None) -> list[str]:
    """Resolve ``storage.pii_scan_exclude`` — extra PII-scan filename exclusions (athenaeum#1273).

    ``storage lint-pii`` walks every file under ``wiki/`` (and, separately,
    ``raw/``) looking for inline emails/phones. A handful of files are
    machine-generated audit logs whose content is regenerated wholesale on a
    schedule — ``_shape_rule_dispositions.jsonl`` is the confirmed case: a
    341+ MB log of epoch-millisecond timestamps that the phone-axis detector
    misreads by the hundred-thousand, and whose distinct-value set never
    stabilises (fresh timestamps nightly), so no allowlist entry can ever
    absorb it. This is the OPERATOR'S list of ADDITIONAL filenames (matched
    by name only, not full path) to exclude beyond the shipped default —
    mirrors :func:`resolve_google_contact_keys`'s shape exactly: the code
    default (:data:`athenaeum.pii.DEFAULT_PII_SCAN_EXCLUDE_FILENAMES`) is
    additive and lives there, not here, so an unconfigured base still
    protects itself with no seed in ``_DEFAULTS`` (issue athenaeum#231).
    Returns an empty list when unset. Non-string entries and blank entries
    are dropped defensively.

    ::

        storage:
          pii_scan_exclude:
            - _some_other_machine_log.jsonl
    """
    if not isinstance(config, dict):
        return []
    storage_cfg = config.get("storage") or {}
    if not isinstance(storage_cfg, dict):
        return []
    raw = storage_cfg.get("pii_scan_exclude")
    if not isinstance(raw, list):
        return []
    return [n.strip() for n in raw if isinstance(n, str) and n.strip()]


def resolve_excluded_fields_config(
    config: dict[str, Any] | None,
) -> dict[str, tuple[str, ...]]:
    """Resolve ``storage.excluded_fields`` — surface class → data-field names (athenaeum#883).

    The explicit operator override at the top of
    :func:`athenaeum.pii.resolve_excluded_fields`'s resolution order: it names
    which frontmatter fields on an excluded record of a given SURFACE class
    (the ``storage.mapping`` key, e.g. ``pii`` — not a wiki page's ``type:``)
    hold data rather than the record's own bookkeeping.

    Returns an EMPTY dict when unset — the code default that leaves ``pii`` on
    its built-in :data:`athenaeum.pii.CONTACT_DATA_FIELDS` allowlist and every
    other class on the denylist-complement, so an unconfigured base is
    byte-identical (``resolve_storage_mapping``'s precedent: no seed in
    ``_DEFAULTS``, so this default stays reachable).

    A class mapped to an EMPTY list is honoured literally as "this class has no
    data fields" — that is an operator saying so, which is a different
    statement from not configuring the class at all, and collapsing the two
    would make the override unable to express it. Non-string keys, non-list
    values, and blank/non-string field names are dropped defensively.
    """
    if not isinstance(config, dict):
        return {}
    storage_cfg = config.get("storage") or {}
    if not isinstance(storage_cfg, dict):
        return {}
    raw = storage_cfg.get("excluded_fields")
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, tuple[str, ...]] = {}
    for surface_class, fields in raw.items():
        if not isinstance(surface_class, str) or not surface_class.strip():
            continue
        if not isinstance(fields, list):
            continue
        resolved[surface_class.strip()] = tuple(
            name.strip() for name in fields if isinstance(name, str) and name.strip()
        )
    return resolved


# ---------------------------------------------------------------------------
# Field corrections (issue athenaeum#797, docs/field-corrections.md §10.3)
# ---------------------------------------------------------------------------
#
# Every key lives under ``librarian.corrections``. The two structural
# (dict-shaped) keys below — ``fields`` and ``sensitive_fields`` — follow
# ``resolve_storage_mapping``'s precedent: no ``ATHENAEUM_*`` env override
# (a dict has no single scalar env encoding), EMPTY by default. The scalar
# bound resolvers further down follow ``resolve_delta_max_affected_clusters``'s
# precedent: each resolver's own named env var (see its docstring) wins
# over yaml, which wins over the code default.


def resolve_corrections_fields(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Resolve ``librarian.corrections.fields`` — the attribute allowlist
    that bounds what a correction may write CHEAPLY at tier 0
    (`docs/field-corrections.md` §6.3).

    Maps an attribute name to ``{"shape": "scalar"|"list", "writers":
    [...], "monotone": bool}``. **Empty by default** (§10.3) — with no
    config, no attribute is allowlisted, so every correction takes the
    reasoning-tier fallthrough (§8) and nothing is written cheaply. A fresh
    deployment cannot have its wiki written by a mechanical writer until an
    operator opts in per-attribute. Malformed entries (non-string attribute
    name, non-dict definition) are dropped defensively rather than raised —
    a config typo degrades to "this attribute reasons instead of writing
    cheaply," never a crash.
    """
    if not isinstance(config, dict):
        return {}
    librarian_cfg = config.get("librarian") or {}
    if not isinstance(librarian_cfg, dict):
        return {}
    corrections_cfg = librarian_cfg.get("corrections")
    if not isinstance(corrections_cfg, dict):
        return {}
    raw = corrections_cfg.get("fields")
    if not isinstance(raw, dict):
        return {}
    fields: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(definition, dict):
            continue
        fields[name.strip()] = definition
    return fields


def resolve_corrections_sensitive_fields(config: dict[str, Any] | None) -> dict[str, str]:
    """Resolve ``librarian.corrections.sensitive_fields`` — the §7.1
    sensitivity-routing table (`docs/field-corrections.md` §7.1).

    Maps an attribute name to a :mod:`athenaeum.storage` entity-CLASS name
    (resolved through the existing ``storage.mapping`` adapter layer, athenaeum#429
    — reused rather than reinvented) that a fact bearing on that attribute
    is routed to, REGARDLESS of the destination a correction named. Empty by
    default: **sensitivity classification is deployment configuration**,
    never shipped in this repo (docs/field-corrections.md §7.1, issue athenaeum#797
    out-of-scope list).
    """
    if not isinstance(config, dict):
        return {}
    librarian_cfg = config.get("librarian") or {}
    if not isinstance(librarian_cfg, dict):
        return {}
    corrections_cfg = librarian_cfg.get("corrections")
    if not isinstance(corrections_cfg, dict):
        return {}
    raw = corrections_cfg.get("sensitive_fields")
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for field_name, surface_class in raw.items():
        if not isinstance(field_name, str) or not field_name.strip():
            continue
        if not isinstance(surface_class, str) or not surface_class.strip():
            continue
        mapping[field_name.strip()] = surface_class.strip()
    return mapping


def resolve_corrections_schema_slots(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Resolve ``librarian.corrections.schema_slots`` — the §7.2 schema-
    evolution table (`docs/field-corrections.md` §7.2) for attributes that
    ARE on the :func:`resolve_corrections_fields` allowlist but that the
    deployment's schema has no dedicated slot for.

    Maps an attribute name to one of three shapes deciding which §7.2
    disposition an allowlisted-but-slot-less attribute takes:

    - ``{"alias_of": "<other-field>"}`` — a slot exists under a different
      name; the write is transparently redirected there.
    - ``{"propose_amendment": true}`` — no slot, and the deployment wants a
      human-decision schema-amendment proposal (``held-schema-proposal``,
      recorded on `_pending_questions.md`).
    - ``{"prose": true}`` — no slot, one-off; recorded as body prose on the
      entity (``recorded-as-prose``).

    An allowlisted attribute with NO entry here writes directly as ordinary
    frontmatter (schemas.py's per-type models already tolerate unknown keys
    via ``extra="allow"``, the same mechanism source-handle keys use) — §7.2
    only fires when the deployment explicitly asks for non-default routing.
    Empty by default, same rationale as :func:`resolve_corrections_sensitive_fields`.
    """
    if not isinstance(config, dict):
        return {}
    librarian_cfg = config.get("librarian") or {}
    if not isinstance(librarian_cfg, dict):
        return {}
    corrections_cfg = librarian_cfg.get("corrections")
    if not isinstance(corrections_cfg, dict):
        return {}
    raw = corrections_cfg.get("schema_slots")
    if not isinstance(raw, dict):
        return {}
    slots: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(definition, dict):
            continue
        slots[name.strip()] = definition
    return slots


def _resolve_corrections_int(
    config: dict[str, Any] | None,
    env_var: str,
    section: str,
    subsection: str,
    yaml_key: str,
    default: int,
) -> int:
    """Shared body for the four §10.2 integer volume bounds below.

    ``section``/``subsection`` are always ``"librarian"``/``"corrections"``
    in practice — passed as explicit literal arguments (not hard-coded in
    this helper's own body) so that
    ``tests/test_config_resolver_parity_generic.py``'s static discovery
    (which walks string-literal call arguments AT EACH RESOLVER'S OWN CALL
    SITE, in source order) sees the full ``env_var`` -> ``librarian`` ->
    ``corrections`` -> ``<key>`` chain from a single call, in the correct
    parent-to-leaf order — a three-level nesting the generic sentinel
    battery cannot otherwise synthesize from a delegated helper's own body.
    """
    value = _env_number(env_var, int)
    if value is not None and value > 0:
        return value
    if isinstance(config, dict):
        section_cfg = config.get(section) or {}
        if isinstance(section_cfg, dict):
            subsection_cfg = section_cfg.get(subsection)
            if isinstance(subsection_cfg, dict):
                raw = subsection_cfg.get(yaml_key)
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    return raw
    return default


def resolve_corrections_max_records_per_batch(config: dict[str, Any] | None) -> int:
    """§10.2 ``librarian.corrections.max_records_per_batch`` (default 5,000)."""
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_CORRECTIONS_MAX_RECORDS_PER_BATCH",
        "librarian",
        "corrections",
        "max_records_per_batch",
        5000,
    )


def resolve_corrections_max_records_per_run(config: dict[str, Any] | None) -> int:
    """§10.2 ``librarian.corrections.max_records_per_run`` (default 50,000)."""
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_CORRECTIONS_MAX_RECORDS_PER_RUN",
        "librarian",
        "corrections",
        "max_records_per_run",
        50000,
    )


def resolve_corrections_max_batch_bytes(config: dict[str, Any] | None) -> int:
    """§10.2 ``librarian.corrections.max_batch_bytes`` (default 32 MiB)."""
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_CORRECTIONS_MAX_BATCH_BYTES",
        "librarian",
        "corrections",
        "max_batch_bytes",
        32 * 1024 * 1024,
    )


def resolve_corrections_max_escalations_per_run(config: dict[str, Any] | None) -> int:
    """§10.2 ``librarian.corrections.max_escalations_per_run`` (default 50)."""
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_CORRECTIONS_MAX_ESCALATIONS_PER_RUN",
        "librarian",
        "corrections",
        "max_escalations_per_run",
        50,
    )


def resolve_corrections_runtime_share(config: dict[str, Any] | None) -> float:
    """§10.2 ``librarian.corrections.runtime_share`` (default 0.05).

    Mirrors :func:`athenaeum.librarian.librarian_entity_runtime_share`'s
    coercion rules: only ``0 < share < 1`` reserves anything; a bool
    (int-subclass guard), non-numeric, or out-of-range value falls back to
    the default rather than disabling the reserve — unlike the entity
    share, an operator who sets this key at all almost certainly wants SOME
    reserve, so a malformed value should not silently zero it out.
    """
    default = 0.05

    def _coerce(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            share = float(value)
        except (TypeError, ValueError):
            return None
        return share if 0.0 < share < 1.0 else None

    env = os.environ.get("ATHENAEUM_CORRECTIONS_RUNTIME_SHARE")
    if env is not None:
        resolved = _coerce(env)
        if resolved is not None:
            return resolved
    if isinstance(config, dict):
        librarian_cfg = config.get("librarian") or {}
        if isinstance(librarian_cfg, dict):
            corrections_cfg = librarian_cfg.get("corrections")
            if isinstance(corrections_cfg, dict):
                resolved = _coerce(corrections_cfg.get("runtime_share"))
                if resolved is not None:
                    return resolved
    return default


# ---------------------------------------------------------------------------
# Shape-rule engine (issue athenaeum#901, docs/field-corrections.md)
# ---------------------------------------------------------------------------
#
# The shape-rule engine compiles recognized foreign record shapes into
# `docs/field-corrections.md`-conformant correction batches (`emit`) or
# leaves them for the reasoning tiers (`fallthrough`). It runs in the same
# deterministic phase slot as the field-correction fast path above, and its
# per-run volume bound deliberately MIRRORS `librarian.corrections.
# max_records_per_run` / `runtime_share` (same key names, own
# `librarian.shape_rules` namespace) rather than sharing the corrections
# budget outright — the two phases cost different things (rule matching vs.
# applying an already-compiled batch) and a shared cap would let one starve
# the other silently.


def resolve_shape_rules_max_records_per_run(config: dict[str, Any] | None) -> int:
    """``librarian.shape_rules.max_records_per_run`` (default 50,000).

    Run-level cap on candidate raw files the engine evaluates against rules
    in one run. Mirrors ``librarian.corrections.max_records_per_run``
    (§10.2) — once the cap is hit, remaining candidates are left untouched
    for the next run (never dropped, never partially processed).
    """
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_SHAPE_RULES_MAX_RECORDS_PER_RUN",
        "librarian",
        "shape_rules",
        "max_records_per_run",
        50000,
    )


def resolve_shape_rules_log_no_match(config: dict[str, Any] | None) -> bool:
    """``librarian.shape_rules.log_no_match`` (default False). DEFAULT OFF.

    Issue athenaeum#1274: whether the shape-rules pass writes a per-record
    ``disposition: "no-match"`` row to ``wiki/_shape_rule_dispositions.jsonl``
    for every candidate no rule claimed. On a real deployment those rows were
    99.8% of a 341 MB ledger (1,485,942 of 1,488,689 rows over 9 days) --
    a negative result, regenerated on every nightly pass and re-derivable at
    any time by re-running the phase, sitting inside a git repo whose stated
    value is being small and diffable.

    **Default OFF is safe only because the sole consumer is also off by
    default.** ``no-match`` rows carry ``tier: None``, which is exactly what
    :func:`athenaeum.rule_proposals._grouped_deferred_rows` reads for
    athenaeum#905's shape-frequency detector -- so these rows are NOT inert.
    But that detector is reached only through
    ``librarian._run_rule_proposal_phase``, itself gated on
    :func:`resolve_rule_proposals_enabled` (also default False), which
    returns before any disposition-ledger read when off. With both at their
    defaults, suppressing the write loses nothing.

    **An operator turning on ``librarian.rule_proposals.enabled`` must turn
    this on too**, and must then wait
    :func:`resolve_rule_proposals_window_days` (default 30 days) for the
    detector to accumulate enough history to propose anything -- the ledger
    holds no ``no-match`` history from the period this was off. That coupling
    is deliberately NOT expressed as a derived default: every resolver in
    this module resolves to a literal, and a config value whose default is
    another config value would make "enable detection" quietly mean "wait a
    month" with nothing in config to show for it.

    Mirrors :func:`resolve_rule_proposals_enabled`'s shape: env
    ``ATHENAEUM_SHAPE_RULES_LOG_NO_MATCH`` (``1``/``true``/``yes``/``on``,
    case-insensitive) > yaml ``librarian.shape_rules.log_no_match`` > default
    ``False``. Non-bool yaml values and unrecognized env strings fall through
    to off.
    """
    env = os.environ.get("ATHENAEUM_SHAPE_RULES_LOG_NO_MATCH")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        librarian_cfg = config.get("librarian")
        if isinstance(librarian_cfg, dict):
            sr_cfg = librarian_cfg.get("shape_rules")
            if isinstance(sr_cfg, dict):
                raw = sr_cfg.get("log_no_match")
                if isinstance(raw, bool):
                    return raw
    return False


def resolve_shape_rules_dispositions_retention_days(config: dict[str, Any] | None) -> int:
    """``librarian.shape_rules.dispositions_retention_days`` (default 30).

    How many days of ``wiki/_shape_rule_dispositions.jsonl`` rows
    :func:`athenaeum.rules.prune_shape_rule_dispositions` keeps at the tail of
    every shape-rules phase -- the ledger's retention policy, and what gives
    it a bounded steady state instead of monotonic growth.

    Issue athenaeum#1274 split this out of
    :func:`resolve_rule_proposals_window_days`, which athenaeum#1229 had
    doing double duty. Those are two different questions: ``window_days`` is
    a READ window (how far back the athenaeum#905 detector counts), this is a
    RETENTION policy (how much history the file physically carries). Coupled,
    narrowing the detector's window silently deleted ledger history, and an
    operator who disabled ``librarian.rule_proposals`` outright still had
    retention governed by a key belonging to a phase they had turned off.
    The default matches athenaeum#1229's effective behaviour (``window_days``
    also defaults to 30), so this split is a no-op for existing deployments.
    """
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_SHAPE_RULES_DISPOSITIONS_RETENTION_DAYS",
        "librarian",
        "shape_rules",
        "dispositions_retention_days",
        30,
    )


def resolve_shape_rules_runtime_share(config: dict[str, Any] | None) -> float:
    """``librarian.shape_rules.runtime_share`` (default 0.05).

    Fraction of ``librarian.max_runtime`` the shape-rule phase may spend,
    mirroring :func:`resolve_corrections_runtime_share`'s mechanism exactly
    (own env var, own yaml key, same coercion rules: only ``0 < share < 1``
    reserves anything).
    """
    default = 0.05

    def _coerce(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            share = float(value)
        except (TypeError, ValueError):
            return None
        return share if 0.0 < share < 1.0 else None

    env = os.environ.get("ATHENAEUM_SHAPE_RULES_RUNTIME_SHARE")
    if env is not None:
        resolved = _coerce(env)
        if resolved is not None:
            return resolved
    if isinstance(config, dict):
        librarian_cfg = config.get("librarian") or {}
        if isinstance(librarian_cfg, dict):
            shape_rules_cfg = librarian_cfg.get("shape_rules")
            if isinstance(shape_rules_cfg, dict):
                resolved = _coerce(shape_rules_cfg.get("runtime_share"))
                if resolved is not None:
                    return resolved
    return default


def resolve_rule_proposals_threshold(config: dict[str, Any] | None) -> int:
    """``librarian.rule_proposals.threshold`` (default 50).

    Issue athenaeum#905 AC1/AC2, respecified by issue athenaeum#1229 part 2: the
    count of DISTINCT RECORDS (by ``source_ref``, never disposition ROWS --
    see :func:`athenaeum.rule_proposals._distinct_record_count`, the single
    function that makes this concrete) -- grouped by ``(source,
    key_fingerprint)``, restricted to rows the shape-rules pass deferred to
    the reasoning ladder (``tier is None`` in
    ``_shape_rule_dispositions.jsonl``; see :mod:`athenaeum.rule_proposals`)
    -- that must be crossed within :func:`resolve_rule_proposals_window_days`
    before the librarian drafts a candidate rule for that shape.

    Counting rows instead of records is a real, ~9.5x-consequential
    difference in practice: before the ledger deduped re-evaluations at
    write time (issue athenaeum#1229 part 1), a handful of records
    re-evaluated on every nightly run alone crossed a threshold of 50 by
    row count while their DISTINCT record count stayed far below it (57 of
    66 shapes crossed by row count vs. 6 by distinct record on the
    deployment that motivated athenaeum#1229). This docstring's "record count"
    was always the intent; :mod:`athenaeum.rule_proposals` now measures it,
    not rows.
    """
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_RULE_PROPOSALS_THRESHOLD",
        "librarian",
        "rule_proposals",
        "threshold",
        50,
    )


def resolve_rule_proposals_window_days(config: dict[str, Any] | None) -> int:
    """``librarian.rule_proposals.window_days`` (default 30).

    Issue athenaeum#905's "configurable window": the detector only counts
    ``_shape_rule_dispositions.jsonl`` rows whose ``at`` timestamp falls
    within this many days of "now".
    """
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_RULE_PROPOSALS_WINDOW_DAYS",
        "librarian",
        "rule_proposals",
        "window_days",
        30,
    )


def resolve_rule_proposals_exemplar_count(config: dict[str, Any] | None) -> int:
    """``librarian.rule_proposals.exemplar_count`` (default 5).

    Issue athenaeum#905 AC2's "K exemplars" -- how many readable raw records
    of a detected shape are embedded in the one drafting call.
    """
    return _resolve_corrections_int(
        config,
        "ATHENAEUM_RULE_PROPOSALS_EXEMPLAR_COUNT",
        "librarian",
        "rule_proposals",
        "exemplar_count",
        5,
    )


def resolve_rule_proposals_enabled(config: dict[str, Any] | None) -> bool:
    """``librarian.rule_proposals.enabled`` (default False). DEFAULT OFF.

    Issue athenaeum#1063: gates the wiring of
    :func:`athenaeum.rule_proposals.run_rule_proposal_detection` into the
    nightly ``athenaeum run`` loop (``librarian._run_rule_proposal_phase``).
    With this off (the default), the phase returns immediately -- zero LLM
    calls, no client constructed, no disposition-ledger read.

    Mirrors :func:`resolve_verdict_ledger_enabled`'s shape: env
    ``ATHENAEUM_RULE_PROPOSALS_ENABLED`` (``1``/``true``/``yes``/``on``,
    case-insensitive) > yaml ``librarian.rule_proposals.enabled`` > default
    ``False``. Default OFF is deliberate: this wiring adds a NEW unattended
    language-model call to the nightly run -- real recurring spend an
    operator must opt into rather than discover behind a detector issue (see
    athenaeum#1063's own text). Set ``librarian.rule_proposals.enabled: true``
    (or the env var) to turn it on. Non-bool yaml values and unrecognized env
    strings fall through to off.
    """
    env = os.environ.get("ATHENAEUM_RULE_PROPOSALS_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        librarian_cfg = config.get("librarian")
        if isinstance(librarian_cfg, dict):
            rp_cfg = librarian_cfg.get("rule_proposals")
            if isinstance(rp_cfg, dict):
                raw = rp_cfg.get("enabled")
                if isinstance(raw, bool):
                    return raw
    return False


def resolve_verdict_ledger_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the verdict-ledger opt-in (issue athenaeum#712). DEFAULT OFF.

    Gates the ENTIRE verdict-ledger subsystem (:mod:`athenaeum.verdicts`):
    with this off, ``athenaeum run`` never touches ``wiki/_verdicts/`` (no
    new file, no new run-summary phase, no exit-code change — byte-identical
    to before athenaeum#712), and a merge approve/reject via ``athenaeum
    ingest-answers`` never writes a verdict entry. Mirrors
    :func:`resolve_reasoning_tier_auditing_enabled`'s shape exactly: env
    ``ATHENAEUM_VERDICT_LEDGER_ENABLED`` (``1``/``true``/``yes``/``on``,
    case-insensitive) > yaml ``librarian.verdict_ledger_enabled`` > default
    ``False``. No seed in ``_DEFAULTS`` (issue athenaeum#231). Default OFF is
    deliberate — the comparator that would populate the ledger with real
    verdicts does not exist yet (a separate, future child of athenaeum#709);
    turning this on before then only exercises the store/schema/epoch
    machinery via the merge approve/reject decisions the pipeline already
    makes. Non-bool yaml values and unrecognized env strings fall through to
    off.
    """
    env = os.environ.get("ATHENAEUM_VERDICT_LEDGER_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("verdict_ledger_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_comparator_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the five-verdict comparator opt-in (issue athenaeum#715). DEFAULT OFF.

    Gates the comparator subsystem (:mod:`athenaeum.comparator`): with this
    off, nothing changes for any existing operator. As of athenaeum#715's
    cut-over, this IS a live pipeline gate:
    :func:`athenaeum.wiki_dedupe.propose_wiki_page_merges` (called from
    :func:`athenaeum.librarian._run_wiki_dedup_phase` every run) checks this
    first and returns immediately when it is off — the wiki-page dedup pass
    is otherwise a no-op, old algorithm and new both, since the old
    confidence/suppression-gate algorithm that pass used to run
    unconditionally was DELETED (not merely branched around) as part of the
    cut-over; there is exactly one implementation, gated by this one knob.
    ``athenaeum merges recompare`` (:mod:`athenaeum._cmd_merges`) remains
    the other live reader, unchanged. Mirrors
    :func:`resolve_verdict_ledger_enabled`'s shape exactly: env
    ``ATHENAEUM_COMPARATOR_ENABLED`` (``1``/``true``/``yes``/``on``,
    case-insensitive) > yaml ``librarian.comparator_enabled`` > default
    ``False``. No seed in ``_DEFAULTS`` (issue athenaeum#231). Non-bool yaml
    values and unrecognized env strings fall through to off.
    """
    env = os.environ.get("ATHENAEUM_COMPARATOR_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("comparator_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_name_collision_scan_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the nightly name-collision scan opt-out (issue athenaeum#1170). DEFAULT ON.

    Gates :func:`athenaeum.librarian._run_name_collision_phase` /
    :func:`athenaeum.name_collisions.resolve_name_collisions` — a
    deterministic, zero-cost, exact-``name:``-match scan over ``wiki/*.md``
    (no LLM, no vectors, no network). Unlike :func:`resolve_comparator_enabled`'s
    expensive comparator pass, there is no cost reason to ship this off by
    default; ``librarian.name_collision_scan: false`` exists only as an
    operator escape hatch. Mirrors :func:`resolve_delta_enabled`'s shape:
    yaml ``librarian.name_collision_scan`` (a plain ``bool``) overrides the
    ``True`` default; anything else (missing, non-bool) falls through to
    ``True``.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("name_collision_scan")
            if isinstance(raw, bool):
                return raw
    return True


def resolve_name_collision_automerge_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the name-collision auto-merge opt-in (issue athenaeum#1170). DEFAULT OFF.

    Gates only the UNAMBIGUOUS-collision auto-merge branch of
    :func:`athenaeum.name_collisions.resolve_name_collisions` — with this
    off (the default), every collision the nightly scan finds still writes
    a proposal block to ``_pending_merges.md`` (see
    :func:`resolve_name_collision_scan_enabled`), it just never
    self-approves.

    This default is a deliberate, reasoned choice, not an oversight: issue
    athenaeum#1170 was split on 2026-08-31 from the one-time destructive
    repair sweep over collisions ALREADY PRESENT in the operator's live
    corpus, which is issue athenaeum#1246 — ``~operator``-gated and blocked
    by this issue. Shipping auto-merge ON by default here would make the
    very next nightly run perform exactly that unattended sweep, defeating
    the split athenaeum#1170 -> athenaeum#1246 was meant to create. So the
    auto-merge path is fully built and fully tested (see
    :mod:`athenaeum.name_collisions` and its test suite) and ships OFF: an
    operator who explicitly sets ``librarian.name_collision_automerge:
    true`` gets auto-merge of the unambiguous subset only (an ambiguous
    collision always queues for human review regardless of this flag), and
    every auto-merge is reversible via ``git revert``/``git show`` by
    construction (the same ``fold-into-existing`` write path issue athenaeum#947
    already made recoverable).

    Precedence: yaml ``librarian.name_collision_automerge`` (a plain
    ``bool``) overrides the ``False`` default; anything else (missing,
    non-bool) falls through to ``False``.
    """
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("name_collision_automerge")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_verdict_epoch_batch_interval_days(config: dict[str, Any] | None) -> int:
    """Resolve the comparator-epoch batching interval in days (issue athenaeum#712).

    "Epoch bumps are batched (default monthly) and the batching interval is
    a documented config key" — this is that key. Precedence:
    ``ATHENAEUM_VERDICT_EPOCH_BATCH_INTERVAL_DAYS`` env > yaml
    ``librarian.verdict_epoch_batch_interval_days`` > ``30``. See
    :func:`_resolve_positive_int_knob` for the coercion contract (``bool`` /
    non-int / ``<= 0`` values fall through to the default).
    """
    return _resolve_positive_int_knob(
        config,
        "verdict_epoch_batch_interval_days",
        "ATHENAEUM_VERDICT_EPOCH_BATCH_INTERVAL_DAYS",
        30,
    )


def resolve_off_corpus_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the off-corpus indexable-store master switch (issue athenaeum#984). DEFAULT OFF.

    Gates the ENTIRE off-corpus subsystem (:mod:`athenaeum.off_corpus`): with
    this off, :func:`athenaeum.librarian.reindex` never touches a second
    index, ``recall`` never federates a second result set, and
    :func:`athenaeum.verdicts.record_pair_decision` keeps its pre-athenaeum#984
    behavior of refusing (not writing) an erasure-class pair — byte-identical
    to before this issue existed. Mirrors :func:`resolve_verdict_ledger_enabled`'s
    shape exactly: env ``ATHENAEUM_OFF_CORPUS_ENABLED`` (``1``/``true``/``yes``/``on``,
    case-insensitive) > yaml ``off_corpus.enabled`` > default ``False``. No seed
    in ``_DEFAULTS`` (issue athenaeum#231's precedent). Non-bool yaml values and
    unrecognized env strings fall through to off.
    """
    env = os.environ.get("ATHENAEUM_OFF_CORPUS_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("off_corpus")
        if isinstance(cfg, dict):
            raw = cfg.get("enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_off_corpus_adapter_name(config: dict[str, Any] | None) -> str | None:
    """Resolve the ``storage.adapters`` entry name that backs the off-corpus
    purgeable surface (issue athenaeum#984). yaml-only, no env var — a physical
    surface name is not the shape of knob this repo's env-var convention
    covers (mirrors ``storage.mapping``/``storage.adapters`` themselves,
    which are also yaml-only). Returns ``None`` when unset; a ``None`` name
    with :func:`resolve_off_corpus_enabled` true is a configuration error
    :mod:`athenaeum.off_corpus` raises loudly (D6: fail closed, loudly) —
    this resolver itself stays defensive/non-raising like every other
    resolver in this module.
    """
    if isinstance(config, dict):
        cfg = config.get("off_corpus")
        if isinstance(cfg, dict):
            raw = cfg.get("adapter")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


# ---------------------------------------------------------------------------
# Erasure retention packs (issue athenaeum#985, AC9)
# ---------------------------------------------------------------------------


def resolve_retention_pack_selection(config: dict[str, Any] | None) -> str:
    """Resolve which retention pack is ACTIVE (issue athenaeum#985, AC9).

    Precedence: env ``ATHENAEUM_RETENTION_PACK`` > yaml ``erasure.retention_pack``
    > default ``"us-default"``. This is the SELECTION axis only — it names
    which pack (of :func:`athenaeum.erasure.available_retention_packs`'s
    result) governs; the pack's own rule table is a separate axis
    (:func:`resolve_retention_pack_overrides`), mirroring how
    :func:`resolve_sensitivity_routing` keeps "is a class routed" separate
    from :func:`resolve_sensitivity_classes`' "what does the class contain."
    An empty/whitespace-only override at either tier is treated as unset.
    """
    env = os.environ.get("ATHENAEUM_RETENTION_PACK")
    if env and env.strip():
        return env.strip()
    if isinstance(config, dict):
        erasure_cfg = config.get("erasure")
        if isinstance(erasure_cfg, dict):
            raw = erasure_cfg.get("retention_pack")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "us-default"


def resolve_retention_pack_overrides(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Resolve ``erasure.retention_packs.<name>`` operator-authored pack overrides/additions.

    Returns the RAW (still-unvalidated) per-pack mapping dicts keyed by pack
    name — :func:`athenaeum.erasure.available_retention_packs` validates each
    and builds the :class:`~athenaeum.erasure.RetentionPack` objects, exactly
    mirroring :func:`resolve_sensitivity_classes`'s split with
    :func:`athenaeum.sensitivity.available_classes`. Returns an EMPTY dict
    when unset — the two packaged packs (``us-default``, ``eu-gdpr``) are
    still resolved regardless, from ``src/athenaeum/retention_packs/*.yaml``,
    not from this function — so this resolver is NOT seeded in
    ``_DEFAULTS`` (issue athenaeum#231's rule: seeding here would make the
    packaged-file default unreachable). Non-string keys and non-mapping
    values are dropped defensively; a malformed entry surfaces loudly later,
    at pack-build time, with the pack name in the message.
    """
    if not isinstance(config, dict):
        return {}
    erasure_cfg = config.get("erasure") or {}
    if not isinstance(erasure_cfg, dict):
        return {}
    raw = erasure_cfg.get("retention_packs")
    if not isinstance(raw, dict):
        return {}
    packs: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(definition, dict):
            continue
        packs[name.strip()] = definition
    return packs


# ---------------------------------------------------------------------------
# Five-verdict comparator, phase 2 (issue athenaeum#715) -- auto-supersession,
# its rate limits, and the two instruments. Every knob here is inert unless
# :func:`resolve_comparator_enabled` is on: the comparator subsystem is the
# only caller, and it is itself gated. See ``docs/configuration.md``.
# ---------------------------------------------------------------------------


def resolve_auto_supersession_enabled(config: dict[str, Any] | None) -> bool:
    """Resolve the auto-supersession opt-in (issue athenaeum#715). DEFAULT OFF.

    Auto-supersession RETIRES a claim -- the one genuinely destructive effect
    in the comparator's verdict set -- so it ships behind its own switch
    *inside* the already-off :func:`resolve_comparator_enabled` gate rather
    than riding on it. An operator who wants the comparator's verdicts
    without any automatic retirement turns this off and every contradiction
    routes to the decision queue instead.

    Env ``ATHENAEUM_AUTO_SUPERSESSION_ENABLED``
    (``1``/``true``/``yes``/``on``, case-insensitive) > yaml
    ``librarian.auto_supersession_enabled`` > default ``False``. No seed in
    ``_DEFAULTS`` (issue athenaeum#231).
    """
    env = os.environ.get("ATHENAEUM_AUTO_SUPERSESSION_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("auto_supersession_enabled")
            if isinstance(raw, bool):
                return raw
    return False


def resolve_standing_state_claim_kinds(config: dict[str, Any] | None) -> frozenset[str]:
    """Resolve which :data:`athenaeum.models.CLAIM_KINDS` count as STANDING STATE.

    Issue athenaeum#715's first auto-supersession precondition is "it is a
    standing-state fact" -- a claim about a state that holds until something
    changes it, so that a later claim about the same coordinates genuinely
    REPLACES it. The default set is ``fact``, ``decision``, ``policy``.

    The three excluded kinds are excluded on purpose, and an operator
    widening this set should know why:

    - ``observation`` is a point-in-time event. A later observation does not
      retire an earlier one; both happened.
    - ``opinion`` is evaluative -- two asserters may hold different, both-valid
      opinions. athenaeum#327 already routes an opinion pair to ``attribute_both``
      rather than a precedence winner; auto-retiring one would contradict that.
    - ``definition`` is timeless.

    An UNCLASSIFIED claim (``claim_kind`` absent -- the fail-open ``""`` of
    :func:`athenaeum.models.parse_claim_kind`) is never standing-state here.
    That is deliberate fail-CLOSED behaviour for a destructive action: the
    rest of the codebase fails open on an unclassified claim because the
    consequence is only a missed optimisation, whereas here the consequence
    is retiring a claim nobody classified.

    Env ``ATHENAEUM_STANDING_STATE_CLAIM_KINDS`` (comma-separated) > yaml
    ``librarian.standing_state_claim_kinds`` (a list) > the default set.
    Values outside :data:`athenaeum.models.CLAIM_KINDS` are dropped; an empty
    result falls through to the default rather than disabling every
    precondition silently.
    """
    from athenaeum.models import CLAIM_KINDS

    default = frozenset({"fact", "decision", "policy"})
    raw_values: list[str] = []
    env = os.environ.get("ATHENAEUM_STANDING_STATE_CLAIM_KINDS")
    if env is not None:
        raw_values = [part.strip() for part in env.split(",")]
    elif isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("standing_state_claim_kinds")
            if isinstance(raw, (list, tuple)):
                raw_values = [str(part).strip() for part in raw if isinstance(part, str)]
    resolved = frozenset(value for value in raw_values if value in CLAIM_KINDS)
    return resolved or default


def resolve_supersession_self_revision_window_days(config: dict[str, Any] | None) -> int:
    """Resolve the PER-CLAIM self-revision rate-limit window, in days (athenaeum#715).

    "The third auto-supersession of the same claim by the same asserter
    within 90 days queue-flags instead of auto-applying" -- this is the 90.
    Per-claim limits catch OSCILLATION (one asserter flip-flopping a single
    fact); the per-asserter limit
    (:func:`resolve_supersession_asserter_weekly_max`) catches diffuse drift.

    Env ``ATHENAEUM_SUPERSESSION_SELF_REVISION_WINDOW_DAYS`` > yaml
    ``librarian.supersession_self_revision_window_days`` > ``90``. See
    :func:`_resolve_positive_int_knob` for the coercion contract.
    """
    return _resolve_positive_int_knob(
        config,
        "supersession_self_revision_window_days",
        "ATHENAEUM_SUPERSESSION_SELF_REVISION_WINDOW_DAYS",
        90,
    )


def resolve_supersession_claim_window_max(config: dict[str, Any] | None) -> int:
    """Resolve the PER-CLAIM self-revision cap inside the window (athenaeum#715).

    The ORDINAL of the auto-supersession that must queue-flag rather than
    auto-apply: at the default ``3``, the first two same-asserter
    self-revisions of one claim inside
    :func:`resolve_supersession_self_revision_window_days` auto-apply and the
    third does not. Counting is over the audit trail
    (:mod:`athenaeum.supersession`'s ledger), not over frontmatter.

    Env ``ATHENAEUM_SUPERSESSION_CLAIM_WINDOW_MAX`` > yaml
    ``librarian.supersession_claim_window_max`` > ``3``.
    """
    return _resolve_positive_int_knob(
        config,
        "supersession_claim_window_max",
        "ATHENAEUM_SUPERSESSION_CLAIM_WINDOW_MAX",
        3,
    )


def resolve_supersession_asserter_weekly_max(config: dict[str, Any] | None) -> int:
    """Resolve the PER-ASSERTER weekly self-revision cap (issue athenaeum#715).

    "An asserter whose same-asserter auto-supersessions of single-source
    facts exceed 10/week corpus-wide has condition (a) suspended pending
    review" -- this is the 10. Unlike the per-claim limit, exceeding this
    suspends condition (a) for that asserter ENTIRELY (every claim), because
    the failure it catches is one sloppy or compromised writer drifting the
    corpus a little in many places rather than oscillating in one.

    Env ``ATHENAEUM_SUPERSESSION_ASSERTER_WEEKLY_MAX`` > yaml
    ``librarian.supersession_asserter_weekly_max`` > ``10``.
    """
    return _resolve_positive_int_knob(
        config,
        "supersession_asserter_weekly_max",
        "ATHENAEUM_SUPERSESSION_ASSERTER_WEEKLY_MAX",
        10,
    )


def resolve_compatible_recheck_days(config: dict[str, Any] | None) -> int:
    """Resolve the ``compatible`` TTL re-check age, in days (issue athenaeum#715).

    A ``compatible`` content relation is the one verdict that says "these two
    coexist" WITHOUT a coordinate separating them, so it is the one most
    likely to be falsified by later writes: the subject drifts and the two
    pages start answering the same question. athenaeum#715 asks for a TTL
    re-check "for high-write subjects -- default: re-compare after 6 months
    or 20 content-adjacent writes"; this is the 6 months, as ``183`` days.
    Either trigger firing is enough (see
    :func:`resolve_compatible_recheck_writes`).

    Env ``ATHENAEUM_COMPATIBLE_RECHECK_DAYS`` > yaml
    ``librarian.compatible_recheck_days`` > ``183``.
    """
    return _resolve_positive_int_knob(
        config,
        "compatible_recheck_days",
        "ATHENAEUM_COMPATIBLE_RECHECK_DAYS",
        183,
    )


def resolve_compatible_recheck_writes(config: dict[str, Any] | None) -> int:
    """Resolve the ``compatible`` TTL re-check write count (issue athenaeum#715).

    The "or 20 content-adjacent writes" half of the TTL above: once either
    side of a ``compatible`` pair has accumulated this many content-changing
    writes since the verdict was recorded, the pair is re-compared even if
    :func:`resolve_compatible_recheck_days` has not elapsed.

    Env ``ATHENAEUM_COMPATIBLE_RECHECK_WRITES`` > yaml
    ``librarian.compatible_recheck_writes`` > ``20``.
    """
    return _resolve_positive_int_knob(
        config,
        "compatible_recheck_writes",
        "ATHENAEUM_COMPATIBLE_RECHECK_WRITES",
        20,
    )


def resolve_sibling_widening_budget(config: dict[str, Any] | None) -> int:
    """Resolve the per-run budget for sibling-scope widening probes (athenaeum#715).

    The sibling-widening instrument deliberately spends Gate-2 calls on pairs
    Gate 1 ALREADY settled as DISTINCT, to catch convergent local practice
    that would otherwise stay permanently fragmented across sibling scopes.
    That is unbounded LLM cost by construction, so athenaeum#715 requires it be
    "bounded by a documented budget" -- this is that budget, counted in
    ``content_relation`` calls per run. Set it to ``0``... you cannot: a
    ``<= 0`` value falls through to the default per
    :func:`_resolve_positive_int_knob`. Disable the instrument by leaving
    :func:`resolve_comparator_enabled` off, or set the budget to ``1``.

    Env ``ATHENAEUM_SIBLING_WIDENING_BUDGET`` > yaml
    ``librarian.sibling_widening_budget`` > ``25``.
    """
    return _resolve_positive_int_knob(
        config,
        "sibling_widening_budget",
        "ATHENAEUM_SIBLING_WIDENING_BUDGET",
        25,
    )


def resolve_sibling_widening_min_similarity(config: dict[str, Any] | None) -> float:
    """Resolve the "top band" similarity floor for sibling widening (athenaeum#715).

    Only TOP-BAND-similarity, scope-separated DISTINCTs get the extra
    memoized ``content_relation`` call. Similarity's only job here is
    PROPOSING which pairs to spend the budget on -- exactly as it proposes
    merge candidates elsewhere -- and it never reaches a verdict, so this is
    a candidate-generation knob and NOT one of the confidence thresholds
    athenaeum#715 bans (those attach a scalar to a VERDICT; see
    ``docs/configuration.md``).

    Env ``ATHENAEUM_SIBLING_WIDENING_MIN_SIMILARITY`` > yaml
    ``librarian.sibling_widening_min_similarity`` > ``0.85``. A parsed env
    value is authoritative over yaml (issue athenaeum#524 M1); a ``bool`` /
    non-numeric / out-of-``(0, 1]`` yaml value falls through to the default.
    """
    value = _env_number("ATHENAEUM_SIBLING_WIDENING_MIN_SIMILARITY", float)
    if value is not None and 0.0 < value <= 1.0:
        return value
    if isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("sibling_widening_min_similarity")
            if raw is not None and not isinstance(raw, bool):
                try:
                    parsed = float(raw)
                except (TypeError, ValueError):
                    return 0.85
                if 0.0 < parsed <= 1.0:
                    return parsed
    return 0.85


def resolve_sibling_widening_classes(config: dict[str, Any] | None) -> frozenset[str]:
    """Resolve the "guideline-like" memory classes for sibling widening (athenaeum#715).

    Scope-separated DISTINCTs are only probed for convergence in classes
    where two sibling scopes independently arriving at the same rule is a
    real, recurring pattern worth unifying -- ``guideline``, ``procedure``,
    ``axiom``. A scope-separated pair of ``entity`` pages is two different
    entities and probing it is pure cost.

    Env ``ATHENAEUM_SIBLING_WIDENING_CLASSES`` (comma-separated) > yaml
    ``librarian.sibling_widening_classes`` (a list) > the default set.
    Values outside :data:`athenaeum.memory_class.MEMORY_CLASSES` are dropped;
    an empty result falls through to the default.
    """
    from athenaeum.memory_class import MEMORY_CLASSES

    default = frozenset({"guideline", "procedure", "axiom"})
    raw_values: list[str] = []
    env = os.environ.get("ATHENAEUM_SIBLING_WIDENING_CLASSES")
    if env is not None:
        raw_values = [part.strip() for part in env.split(",")]
    elif isinstance(config, dict):
        cfg = config.get("librarian")
        if isinstance(cfg, dict):
            raw = cfg.get("sibling_widening_classes")
            if isinstance(raw, (list, tuple)):
                raw_values = [str(part).strip() for part in raw if isinstance(part, str)]
    resolved = frozenset(value for value in raw_values if value in MEMORY_CLASSES)
    return resolved or default


def resolve_authority_grant_implications(
    config: dict[str, Any] | None,
) -> dict[str, frozenset[str]]:
    """Resolve the grant-implication map used to order asserter authority (athenaeum#715).

    Authority in athenaeum#715 is a PARTIAL ORDER over the grants an asserter
    declares, never a chain of ranks. This map is what makes the order
    non-trivial: it declares which grants IMPLY which others, so that an
    asserter holding ``admin`` compares as strictly greater than one holding
    only ``reader`` without either having to enumerate the other's grants.

    .. code-block:: yaml

        librarian:
          authority_grant_implications:
            admin: [editor]
            editor: [reader]

    Yaml only -- an implication graph is not an emergency override and has no
    sane env-var encoding. Default ``{}``: with no declared implications the
    order degenerates to plain set inclusion over declared grants, which is
    still a correct partial order (just a flatter one). Non-string keys,
    non-list values, and non-string members are dropped defensively;
    :func:`athenaeum.asserter_authority.grant_closure` is cycle-safe, so a
    malformed cyclic map cannot hang a run.
    """
    if not isinstance(config, dict):
        return {}
    cfg = config.get("librarian")
    if not isinstance(cfg, dict):
        return {}
    raw = cfg.get("authority_grant_implications")
    if not isinstance(raw, dict):
        return {}
    implications: dict[str, frozenset[str]] = {}
    for grant, implied in raw.items():
        if not isinstance(grant, str) or not grant.strip():
            continue
        if not isinstance(implied, (list, tuple)):
            continue
        members = frozenset(
            member.strip() for member in implied if isinstance(member, str) and member.strip()
        )
        if members:
            implications[grant.strip()] = members
    return implications


def resolve_person_registry_root(knowledge_root: Path, config: dict[str, Any] | None) -> Path:
    """Resolve the on-disk root :class:`athenaeum.person_registry.PersonRegistry`
    scans for ``type: person`` pages (issue athenaeum#1183).

    Default: ``<knowledge_root>/wiki`` — the SAME directory
    :class:`athenaeum.models.EntityIndex` already scans. This is deliberate
    backward compatibility: athenaeum#1183 demotes ``type: person`` pages out of
    the general entity-index NAME keys (see
    :data:`athenaeum.models.DEMOTED_NAME_MATCH_TYPES`), but does not itself
    move a single file — the one-time physical relocation of person pages in
    a live corpus is issue athenaeum#1247 (blocked by athenaeum#1183). Until that
    relocation runs, every person page still lives under ``wiki/``, so this
    resolver has to keep pointing there for :class:`~athenaeum.person_registry.PersonRegistry`
    to find anything on an unmigrated corpus. Once athenaeum#1247 physically moves
    person pages elsewhere, set ``person_registry.root`` (relative paths
    resolve against *knowledge_root*; an absolute path is used as-is) to
    repoint this WITHOUT a code change.

    No env override: unlike a run-level budget knob, this is a structural
    corpus-layout fact an operator sets once (if ever), not a per-invocation
    dial — mirrors :func:`resolve_authority_grant_implications`'s yaml-only
    posture for the same reason.
    """
    if isinstance(config, dict):
        cfg = config.get("person_registry")
        if isinstance(cfg, dict):
            raw = cfg.get("root")
            if isinstance(raw, str) and raw.strip():
                candidate = Path(raw).expanduser()
                return candidate if candidate.is_absolute() else knowledge_root / candidate
    return knowledge_root / "wiki"
