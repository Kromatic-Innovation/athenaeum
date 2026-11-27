# SPDX-License-Identifier: Apache-2.0
"""Screen raw intake first in a run, and hold unscreened raw files out of commits (athenaeum#3178).

Other programs write raw intake directly. Before this module the first thing a
compile run did with such a file was commit it (the pre-processing snapshot and
the snapshot that precedes a sync stage the whole knowledge directory), while
value routing ran later and changed only the in-memory copy: a routed value
stayed in the raw file, was committed and could be pushed.

Two mechanisms, one module:

* :func:`screen_raw_intake` is the screening phase. It runs at the very start of
  a run, before any commit. For every raw file not yet screened it applies the
  same screen ``remember`` applies at write time
  (:func:`athenaeum.sensitivity_routing.screen_intake_content`: free-text value
  routing, discard, declared-field rules). The vault record is written first,
  then the raw file is replaced atomically with the screened text, then the file
  is marked screened. A file that cannot be screened without being processed (no
  declared page type on an installation that routes or discards a whole page
  type, or a declared type that is itself routed or discarded whole) is not
  rewritten; it stays unscreened until it has been processed.
* :func:`make_raw_hold` is the hold. With ``librarian.hold_unscreened_raw`` on
  (the default) and at least one routing or discard rule configured, it names the
  raw files a whole-directory commit must leave out: those neither screened nor
  processed. :func:`athenaeum.storage.open_store` registers it with
  :mod:`athenaeum.store`, whose whole-directory commits honour it. With no rule,
  or the setting off, nothing is registered and every commit is exactly what it
  was.

Screened state is a per-installation ledger of ``relative path -> sha256 of the
screened bytes`` in the cache directory, never a second copy of a file and never
a value. A file whose bytes no longer match its entry (another program wrote it
again) is unscreened again. Losing the ledger loses nothing: screening a screened
file changes nothing, so the next run re-marks it.

Raw intake addressed to a shared store (``raw/_stores/``), answer files
(``raw/answers/``) and sources named in ``librarian.non_intake_sources`` are not
screened and not held: the first two are records other installs wait for, the
last is a tool's operational artifacts, not intake.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.config` (L2),
:mod:`athenaeum.storage` and :mod:`athenaeum.store` (L1) and, by name at call
time, :mod:`athenaeum.sensitivity_routing`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    StorageConfigError,
    resolve_cache_dir,
    resolve_hold_unscreened_raw,
    resolve_non_intake_sources,
    resolve_raw_file_max_bytes,
    resolve_sensitivity_routing,
    resolve_storage_history,
    resolve_storage_mapping,
)
from athenaeum.models import parse_frontmatter
from athenaeum.storage import (
    STORE_RAW_DIRNAME,
    available_adapters,
    configured_routes,
)
from athenaeum.store import RawHold

log = logging.getLogger(__name__)

__all__ = [
    "HoldMessage",
    "committed_raw_with_values",
    "emit_hold_messages",
    "install_raw_hold",
    "RawRewrite",
    "RawScreenReport",
    "hold_active",
    "hold_messages",
    "hold_requested",
    "make_raw_hold",
    "mark_processed",
    "raw_screen_ledger_path",
    "register_raw_root",
    "rules_configured",
    "screen_raw_intake",
    "whole_page_types",
]

RAW_DIRNAME = "raw"
ANSWERS_SOURCE = "answers"
LEDGER_DIRNAME = "raw-screened"
STATE_SCREENED = "screened"
STATE_PROCESSED = "processed"

NOTICE_TEXT = (
    "Raw notes are not snapshotted before they are processed. Routed or discarded "
    "values are screened out of history and kept only in their vault, or nowhere "
    "for a discard."
)
HOLD_OFF_WARNING_TEXT = (
    "librarian.hold_unscreened_raw is off and raw intake is kept in a git working "
    "tree: routed or discarded values can leak into that history."
)
REACH_TEXT = (
    "Anything else that commits or copies the knowledge directory can capture a raw "
    "file before it is screened, and a value already committed stays in history."
)


# ---------------------------------------------------------------------------
# What is configured
# ---------------------------------------------------------------------------


def rules_configured(config: dict[str, Any] | None) -> bool:
    """True when at least one routing or discard rule is configured.

    A rule is ``sensitivity.routing.enabled`` (value routing) or any
    ``storage.routes`` entry (a field, page-type or class route, or a discard). A
    configuration that cannot be read counts as configured: the hold then errs
    toward keeping values out of commits.
    """
    try:
        if resolve_sensitivity_routing(config).get("enabled", False):
            return True
    except ValueError:
        return True
    try:
        return bool(configured_routes(config))
    except StorageConfigError:
        return True


def hold_requested(config: dict[str, Any] | None) -> bool:
    """``librarian.hold_unscreened_raw`` (default on); an unreadable value reads as on."""
    try:
        return resolve_hold_unscreened_raw(config)
    except StorageConfigError:
        return True


def hold_active(config: dict[str, Any] | None) -> bool:
    """The hold is on AND at least one rule exists; with no rule nothing is held."""
    return hold_requested(config) and rules_configured(config)


def whole_page_types(config: dict[str, Any] | None) -> frozenset[str]:
    """Page types a rule routes or discards whole (so a raw of that type cannot be screened).

    A ``storage.routes`` rule naming a page type with no ``fields``, and a
    ``storage.mapping`` entry whose location is outside the index.
    """
    types: set[str] = set()
    try:
        for route in configured_routes(config):
            if route.page_type is not None and route.fields is None:
                types.add(route.page_type)
    except StorageConfigError:
        pass
    try:
        adapters = available_adapters(config)
        for page_type, adapter_name in resolve_storage_mapping(config).items():
            adapter = adapters.get(adapter_name)
            if adapter is not None and not adapter.corpus_policy.in_corpus:
                types.add(page_type)
    except StorageConfigError:
        pass
    return frozenset(types)


def _needs_processing(meta: dict[str, Any], whole: frozenset[str]) -> bool:
    """A raw that cannot be screened without being processed.

    On an installation that routes or discards a whole page type, a file with no
    declared page type (its type is only known once it is classified) and a file
    whose declared type is one of those page types.
    """
    if not whole:
        return False
    declared = str(meta.get("type") or "").strip()
    return not declared or declared in whole


# ---------------------------------------------------------------------------
# The screened-state ledger
# ---------------------------------------------------------------------------


def raw_screen_ledger_path(knowledge_root: Path, cache_dir: Path | None = None) -> Path:
    """The ledger file for *knowledge_root* (one per installation, in the cache dir)."""
    base = resolve_cache_dir(cache_dir)
    key = hashlib.sha256(str(Path(knowledge_root).resolve()).encode("utf-8")).hexdigest()[:16]
    return base / LEDGER_DIRNAME / f"{key}.json"


def _load_ledger(path: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        rel: entry
        for rel, entry in files.items()
        if isinstance(entry, dict) and isinstance(entry.get("sha"), str)
    }


def _save_ledger(path: Path, ledger: dict[str, dict[str, str]]) -> None:
    atomic_write_text(
        path, json.dumps({"version": 1, "files": ledger}, sort_keys=True, indent=0) + "\n"
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _out_of_scope_sources(config: dict[str, Any] | None) -> frozenset[str]:
    return frozenset({ANSWERS_SOURCE, STORE_RAW_DIRNAME, *resolve_non_intake_sources(config)})


#: Raw roots other than ``<knowledge root>/raw`` that a run was pointed at with
#: ``--raw-root`` and that sit inside the knowledge root, by resolved knowledge
#: root. Set by :func:`register_raw_root`, read by every scope check.
_EXTRA_RAW_ROOTS: dict[Path, set[str]] = {}


def register_raw_root(knowledge_root: Path, raw_root: Path | None) -> None:
    """Record a run's own raw root so it is screened and held like ``raw/``.

    Ignored when it is the default ``raw/`` or lies outside the knowledge root
    (nothing there is in the repository a commit would capture).
    """
    if raw_root is None:
        return
    root = Path(knowledge_root).resolve()
    try:
        rel = Path(raw_root).resolve().relative_to(root).as_posix()
    except ValueError:
        return
    if rel and rel != RAW_DIRNAME:
        _EXTRA_RAW_ROOTS.setdefault(root, set()).add(rel)


def _prefixes(knowledge_root: Path) -> list[str]:
    """Repository-relative raw roots: ``raw`` and any registered extra one."""
    return [RAW_DIRNAME, *sorted(_EXTRA_RAW_ROOTS.get(Path(knowledge_root).resolve(), ()))]


def _in_scope(rel: str, excluded: frozenset[str], prefixes: list[str]) -> bool:
    for prefix in prefixes:
        if not rel.startswith(prefix + "/"):
            continue
        rest = rel[len(prefix) + 1 :].split("/")
        # A file directly in the raw root has no source directory; it is still intake.
        return len(rest) == 1 or rest[0] not in excluded
    return False


def _ref_for(rel: str, prefixes: list[str]) -> str:
    """``RawFile.ref`` for a raw path: ``<source>/<file name>``."""
    for prefix in prefixes:
        if rel.startswith(prefix + "/"):
            rest = rel[len(prefix) + 1 :].split("/")
            return f"{rest[0]}/{rest[-1]}" if len(rest) >= 2 else rest[-1]
    return rel.rsplit("/", 1)[-1]


def _iter_raw_files(
    knowledge_root: Path, excluded: frozenset[str], prefixes: list[str]
) -> list[Path]:
    found: list[Path] = []
    for prefix in prefixes:
        raw_root = knowledge_root / prefix
        if not raw_root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(raw_root, followlinks=False):
            here = Path(dirpath)
            if here == raw_root:
                dirnames[:] = sorted(d for d in dirnames if d not in excluded)
            else:
                dirnames.sort()
            for name in sorted(filenames):
                path = here / name
                if path.is_symlink():
                    continue  # git tracks the link, not what it points at
                found.append(path)
    return found


# ---------------------------------------------------------------------------
# The screening phase
# ---------------------------------------------------------------------------


@dataclass
class RawRewrite:
    """One raw file the screen replaced: what keyed records need to follow it."""

    rel: str
    ref: str
    old_text: str
    new_text: str
    old_fingerprint: str
    new_fingerprint: str


@dataclass
class RawScreenReport:
    """The outcome of one screening phase. Paths only, never a value."""

    active: bool = False
    hold_on: bool = False
    scanned: int = 0
    already: int = 0
    screened: int = 0
    rewritten: list[RawRewrite] = field(default_factory=list)
    needs_processing: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def unscreened(self) -> list[str]:
        """Every raw file still neither screened nor processed, sorted."""
        return sorted({*self.needs_processing, *self.failed})

    def profile_fields(self) -> dict[str, object]:
        """The run-summary tokens: counts, and with the hold off, each file named."""
        fields: dict[str, object] = {
            "reason": "completed",
            "scanned": self.scanned,
            "screened": self.screened,
            "rewritten": len(self.rewritten),
        }
        if self.hold_on:
            fields["held"] = len(self.unscreened)
            fields["failed"] = len(self.failed)
        else:
            fields["unscreened_staged"] = ",".join(self.unscreened) if self.unscreened else "none"
        return fields


def _fingerprint(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def screen_raw_intake(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    *,
    cache_dir: Path | None = None,
    raw_root: Path | None = None,
) -> RawScreenReport:
    """Screen every raw file not yet screened; return what happened.

    Inactive (nothing read, nothing written) unless a routing or discard rule is
    configured. Per file: vault write first (inside the screen), then the atomic
    replace, then the ledger mark. A failure of any step leaves the raw file as
    it was, unscreened, and names it in the report.
    """
    report = RawScreenReport()
    register_raw_root(knowledge_root, raw_root)
    if not rules_configured(config):
        return report
    report.active = True
    report.hold_on = hold_requested(config)
    knowledge_root = Path(knowledge_root)
    ledger_path = raw_screen_ledger_path(knowledge_root, cache_dir)
    ledger = _load_ledger(ledger_path)
    whole = whole_page_types(config)
    excluded = _out_of_scope_sources(config)
    max_bytes = resolve_raw_file_max_bytes(config)
    screen_intake_content = importlib.import_module(
        "athenaeum.sensitivity_routing"
    ).screen_intake_content

    present: set[str] = set()
    prefixes = _prefixes(knowledge_root)
    for path in _iter_raw_files(knowledge_root, excluded, prefixes):
        rel = path.relative_to(knowledge_root).as_posix()
        present.add(rel)
        report.scanned += 1
        try:
            data = path.read_bytes()
        except OSError:
            report.failed.append(rel)
            log.warning("raw screening: %s could not be read; it stays unscreened", rel)
            continue
        digest = _sha(data)
        entry = ledger.get(rel)
        if entry is not None and entry.get("sha") == digest:
            report.already += 1
            continue
        if max_bytes and len(data) > max_bytes:
            report.failed.append(rel)
            log.warning("raw screening: %s is over the per-file limit; it stays unscreened", rel)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            report.failed.append(rel)
            log.warning("raw screening: %s is not UTF-8 text; it stays unscreened", rel)
            continue
        meta, _body = parse_frontmatter(text)
        if _needs_processing(meta, whole):
            report.needs_processing.append(rel)
            continue
        try:
            screened_text = screen_intake_content(
                text, raw_ref=f"raw:{rel}", config=config, knowledge_root=knowledge_root
            )
            if screened_text != text:
                old_fp = _fingerprint(path)
                atomic_write_text(path, screened_text)
                report.rewritten.append(
                    RawRewrite(
                        rel=rel,
                        ref=_ref_for(rel, prefixes),
                        old_text=text,
                        new_text=screened_text,
                        old_fingerprint=old_fp,
                        new_fingerprint=_fingerprint(path),
                    )
                )
                digest = _sha(screened_text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - any failure leaves the file unscreened
            report.failed.append(rel)
            log.warning(
                "raw screening: %s was not screened (%s); it is unscreened and unchanged",
                rel,
                type(exc).__name__,
            )
            continue
        ledger[rel] = {"sha": digest, "state": STATE_SCREENED}
        report.screened += 1

    for rel in [r for r in ledger if r not in present]:
        del ledger[rel]
    try:
        _save_ledger(ledger_path, ledger)
    except OSError:
        log.warning("raw screening: the screened-state ledger could not be written")
    return report


def mark_processed(
    knowledge_root: Path,
    config: dict[str, Any] | None,
    path: Path,
    *,
    cache_dir: Path | None = None,
) -> bool:
    """Record that a raw file that could not be screened has now been processed.

    Only a file that needed processing to be screened is marked; one whose
    screening failed stays held until a later run screens it (processing it does
    not remove a value from the file). Returns whether the file was marked.
    """
    if not hold_active(config):
        return False
    knowledge_root = Path(knowledge_root)
    try:
        rel = Path(path).resolve().relative_to(knowledge_root.resolve()).as_posix()
        data = Path(path).read_bytes()
        text = data.decode("utf-8")
    except (OSError, ValueError):
        return False
    if not _in_scope(rel, _out_of_scope_sources(config), _prefixes(knowledge_root)):
        return False
    meta, _body = parse_frontmatter(text)
    if not _needs_processing(meta, whole_page_types(config)):
        return False
    ledger_path = raw_screen_ledger_path(knowledge_root, cache_dir)
    ledger = _load_ledger(ledger_path)
    ledger[rel] = {"sha": _sha(data), "state": STATE_PROCESSED}
    try:
        _save_ledger(ledger_path, ledger)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------


def make_raw_hold(knowledge_root: Path, config: dict[str, Any] | None) -> RawHold:
    """The hold :mod:`athenaeum.store` consults before a whole-directory commit.

    Given the repository root and the changed paths under ``raw/``, returns those
    that are in scope and whose current bytes are neither marked screened nor
    marked processed. Reads only the files git reports as changed.
    """
    knowledge_root = Path(knowledge_root)
    excluded = _out_of_scope_sources(config)

    def hold(repo: Path, rel_paths: list[str]) -> list[str]:
        ledger = _load_ledger(raw_screen_ledger_path(knowledge_root))
        held: list[str] = []
        for rel in rel_paths:
            if not _in_scope(rel, excluded, _prefixes(knowledge_root)):
                continue
            path = Path(repo) / rel
            if path.is_symlink() or not path.is_file():
                continue
            try:
                digest = _sha(path.read_bytes())
            except OSError:
                held.append(rel)  # cannot prove it screened
                continue
            entry = ledger.get(rel)
            if entry is None or entry.get("sha") != digest:
                held.append(rel)
        return held

    return hold


# ---------------------------------------------------------------------------
# The notice and the warnings (one function, two readers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldMessage:
    """One message: ``kind`` is ``notice`` or ``warning``."""

    kind: str
    text: str


def raw_in_git_tree(knowledge_root: Path) -> bool:
    """True when raw intake sits inside a git working tree (any ancestor holds ``.git``)."""
    here = (Path(knowledge_root) / RAW_DIRNAME).resolve()
    return any((p / ".git").exists() for p in (here, *here.parents))


def knowledge_dir_is_git_repo(knowledge_root: Path) -> bool:
    return (Path(knowledge_root) / ".git").exists()


def hold_messages(config: dict[str, Any] | None, knowledge_root: Path) -> list[HoldMessage]:
    """The hold notice and warnings for an installation, in print order.

    The one source both the rule-load path (a run) and ``athenaeum storage
    status`` print from. Empty when no routing or discard rule is configured.

    * Hold on and a rule configured: the notice.
    * Hold off and a rule configured, raw intake kept in a git working tree: the
      hold-off warning.
    * Either way, raw intake inside a git working tree or a knowledge directory
      that is not a git repository: the reach warning.
    """
    if not rules_configured(config):
        return []
    messages: list[HoldMessage] = []
    in_tree = raw_in_git_tree(knowledge_root)
    if hold_requested(config):
        messages.append(HoldMessage("notice", NOTICE_TEXT))
    elif in_tree:
        messages.append(HoldMessage("warning", HOLD_OFF_WARNING_TEXT))
    if in_tree or not knowledge_dir_is_git_repo(knowledge_root):
        messages.append(HoldMessage("warning", REACH_TEXT))
    return messages


def committed_raw_with_values(
    config: dict[str, Any] | None, knowledge_root: Path
) -> list[tuple[str, list[str]]]:
    """Raw files whose committed text holds a routed or discarded value: ``(path, classes)``.

    Reads each tracked raw file as committed at ``HEAD`` and names the classes a
    screen would act on in it (:func:`athenaeum.sensitivity_routing.routed_classes_in`),
    never a value. Only the latest committed version of a path is read: an
    earlier version that a later commit replaced is not listed. Empty when no
    rule is configured or the directory is not a git repository.
    """
    if not rules_configured(config):
        return []
    from athenaeum.store import committed_text, committed_tracked_paths

    routed_classes_in = importlib.import_module("athenaeum.sensitivity_routing").routed_classes_in
    knowledge_root = Path(knowledge_root)
    excluded = _out_of_scope_sources(config)
    found: list[tuple[str, list[str]]] = []
    prefixes = _prefixes(knowledge_root)
    tracked = [r for prefix in prefixes for r in committed_tracked_paths(knowledge_root, prefix)]
    for rel in sorted(tracked):
        if not _in_scope(rel, excluded, prefixes):
            continue
        text = committed_text(knowledge_root, rel)
        if not text:
            continue
        classes = routed_classes_in(text, config=config)
        if classes:
            found.append((rel, classes))
    return found


_EMITTED: set[tuple[str, str]] = set()


def emit_hold_messages(config: dict[str, Any] | None, knowledge_root: Path) -> list[HoldMessage]:
    """Log :func:`hold_messages` once per process and knowledge root; return them."""
    messages = hold_messages(config, knowledge_root)
    for message in messages:
        key = (str(Path(knowledge_root).resolve()), message.text)
        if key in _EMITTED:
            continue
        _EMITTED.add(key)
        log.warning("%s", message.text)
    return messages


def install_raw_hold(config: dict[str, Any] | None, knowledge_root: Path) -> None:
    """Register the hold with the store when it is active, clear it when it is not.

    A ``config`` of ``None`` leaves whatever is registered alone: a caller with no
    configuration to hand must not undo a hold the run installed.
    """
    from athenaeum.store import set_raw_hold

    if config is None:
        return
    hold = make_raw_hold(knowledge_root, config) if hold_active(config) else None
    set_raw_hold(knowledge_root, hold)


def history_is_none(config: dict[str, Any] | None) -> bool:
    try:
        return resolve_storage_history(config) == "none"
    except StorageConfigError:
        return False
