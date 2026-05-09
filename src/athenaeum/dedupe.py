# SPDX-License-Identifier: Apache-2.0
"""Person-wiki dedupe — find HIGH-confidence duplicate pairs and merge them.

Ported from the cwc-side scripts ``find_duplicate_persons.py`` and
``merge_duplicate_persons.py``. Behavior preserved for the HIGH-confidence
tier (apollo_id / linkedin_url / exact normalized name); MEDIUM-tier
fuzzy-name matching is intentionally dropped from the public API surface
because it requires human triage and the CSV worksheet round-trip — that
workflow stays in the cwc scripts.

Per-claim provenance preservation (issue #90):

- Scalar fields: canonical wins; canonical's ``field_sources.<field>``
  is preserved verbatim.
- List fields (emails, tags, aliases, …): the union of values is taken
  with canonical-first ordering. When the absorbed wiki carried a
  ``field_sources.<list_field>`` for a list whose values came from it,
  that attribution survives in the merged ``field_sources`` map: the
  canonical's entry wins on the key (matches the canonical-first list
  ordering) but if canonical had no entry, the absorbed entry is kept.
- Wiki-level ``source``: canonical wins. The absorbed wiki's
  ``source`` is recorded in a merge audit trail under
  ``merged_from_sources: {<absorbed_uid>: <absorbed_source>}`` so
  attribution is recoverable without resurrecting the absorbed file.

Idempotence: ``merge_duplicate_persons(..., apply=True)`` is a no-op on
already-merged pairs (absorbed file missing → ``already_merged`` count).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

from athenaeum.models import parse_frontmatter, render_frontmatter

# Honorifics + suffixes to drop before name matching.
_HONORIFICS = {
    "dr",
    "dr.",
    "mr",
    "mr.",
    "mrs",
    "mrs.",
    "ms",
    "ms.",
    "prof",
    "prof.",
    "professor",
    "sir",
    "hon",
    "hon.",
}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "md", "esq"}

# Frontmatter keys merged with MAX semantics.
_MAX_KEYS_NUMERIC = {"warm_score", "meeting_count_24mo", "sent_count_24mo"}
_MAX_KEYS_DATE = {"last_touch", "updated", "apollo_enriched_on"}

# Apollo / LinkedIn / Google namespaces — coalesce (canonical wins).
_APOLLO_KEYS = {
    "apollo_id",
    "apollo_headline",
    "apollo_location",
    "apollo_employment_history",
    "current_title",
    "current_company",
}
_LINKEDIN_KEYS = {
    "linkedin_url",
    "linkedin_position_at_connect",
    "linkedin_company_at_connect",
    "linkedin_connected_on",
}
_GOOGLE_KEYS = {
    "google_contact",
    "google_contact_kromatic",
    "google_contact_tristankromer",
}
_SOCIAL_KEYS = {
    "twitter_url",
    "github_url",
}

# Fields where "list union with canonical-first order" is the merge rule.
_LIST_UNION_KEYS = {"emails", "tags", "aliases"}


# --- Public types ---


@dataclass
class DuplicatePair:
    """A HIGH-confidence duplicate-person pair.

    Attributes:
        canonical_uid: uid of the wiki that wins the merge.
        absorbed_uid: uid of the wiki to be absorbed (deleted on apply).
        match_signal: one of ``"apollo_id"``, ``"linkedin_url"``,
            ``"name_exact"``.
        confidence: always ``"HIGH"`` for pairs returned by
            :func:`find_duplicate_persons` — kept on the dataclass for
            JSONL/YAML round-trip compatibility with the cwc worksheet.
        canonical_path: absolute path to the canonical wiki file.
        absorbed_path: absolute path to the absorbed wiki file.
    """

    canonical_uid: str
    absorbed_uid: str
    match_signal: str
    confidence: str = "HIGH"
    canonical_path: str = ""
    absorbed_path: str = ""


@dataclass
class MergeReport:
    """Outcome of a merge run."""

    merged: int = 0
    already_merged: int = 0
    missing_canonical: int = 0
    missing_absorbed: int = 0
    skipped_parse: int = 0
    references_rewritten: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


# --- Name / URL normalization ---


def _normalize_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b(dr|mr|mrs|ms|prof|hon|sir)\.(\S)", r"\1. \2", s)
    s = re.sub(r"[^\w\s.\-']", " ", s, flags=re.UNICODE)
    tokens = [
        t.strip(".")
        for t in s.split()
        if t.strip(".")
        and t not in _HONORIFICS
        and t.strip(".") not in _HONORIFICS
        and t not in _SUFFIXES
        and t.strip(".") not in _SUFFIXES
    ]
    return " ".join(tokens)


def _canonical_linkedin(url: str) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = u.rstrip("/")
    u = u.split("?")[0]
    return u


# --- Wiki loading ---


def _load_persons(wiki_root: Path) -> list[dict[str, Any]]:
    persons: list[dict[str, Any]] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if not meta or meta.get("type") != "person":
            continue
        name = str(meta.get("name") or "")
        uid = str(meta.get("uid") or "")
        if not name or not uid:
            continue
        emails = meta.get("emails") or []
        if not isinstance(emails, list):
            emails = []
        persons.append(
            {
                "path": path,
                "uid": uid,
                "name": name,
                "normalized": _normalize_name(name),
                "emails": [str(e).lower() for e in emails],
                "linkedin": _canonical_linkedin(str(meta.get("linkedin_url") or "")),
                "apollo_id": str(meta.get("apollo_id") or ""),
                "warm_score": meta.get("warm_score"),
                "updated": str(meta.get("updated") or ""),
            }
        )
    return persons


# --- Discovery ---


def _pair_key(uid_a: str, uid_b: str) -> tuple[str, str]:
    return (uid_a, uid_b) if uid_a < uid_b else (uid_b, uid_a)


def _pick_canonical(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apollo-enriched > higher warm_score > more emails > recently updated."""

    def score(p: dict[str, Any]) -> tuple[int, float, int, str]:
        try:
            ws = float(p["warm_score"] or 0)
        except (ValueError, TypeError):
            ws = 0.0
        return (1 if p["apollo_id"] else 0, ws, len(p["emails"]), p["updated"])

    return (a, b) if score(a) >= score(b) else (b, a)


def find_duplicate_persons(wiki_root: Path) -> list[DuplicatePair]:
    """Surface HIGH-confidence duplicate-person pairs.

    HIGH-confidence signals (preserved from the cwc-side script):

    - shared ``apollo_id``
    - shared canonicalized ``linkedin_url``
    - exact-match normalized ``name`` (group size 2 or 3 — name buckets
      with 4+ wikis are dropped here as common-name false-positives,
      matching the script's MEDIUM downgrade)

    The MEDIUM tier (fuzzy-name token Jaccard ≥ 0.8) is intentionally
    not surfaced through this API — that path requires human triage via
    the CSV worksheet workflow which lives in the cwc scripts.
    """
    persons = _load_persons(wiki_root)
    seen: dict[tuple[str, str], DuplicatePair] = {}

    def record(a: dict[str, Any], b: dict[str, Any], signal: str) -> None:
        if a["uid"] == b["uid"]:
            return
        key = _pair_key(a["uid"], b["uid"])
        if key in seen:
            return  # first signal wins (apollo > linkedin > name_exact)
        canonical, absorbed = _pick_canonical(a, b)
        seen[key] = DuplicatePair(
            canonical_uid=canonical["uid"],
            absorbed_uid=absorbed["uid"],
            match_signal=signal,
            confidence="HIGH",
            canonical_path=str(canonical["path"]),
            absorbed_path=str(absorbed["path"]),
        )

    by_apollo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in persons:
        if p["apollo_id"]:
            by_apollo[p["apollo_id"]].append(p)
    for group in by_apollo.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                record(group[i], group[j], "apollo_id")

    by_linkedin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in persons:
        if p["linkedin"]:
            by_linkedin[p["linkedin"]].append(p)
    for group in by_linkedin.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                record(group[i], group[j], "linkedin_url")

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in persons:
        if p["normalized"]:
            by_name[p["normalized"]].append(p)
    for group in by_name.values():
        # 2-3 wikis with the same normalized name = HIGH; 4+ = ambiguous
        # common name (the cwc script flags those MEDIUM and routes to
        # the CSV; here we drop them since this API is HIGH-only).
        if len(group) < 2 or len(group) > 3:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                record(group[i], group[j], "name_exact")

    return list(seen.values())


# --- Merge primitives (provenance-aware) ---


def _union_list(a: list | None, b: list | None) -> list:
    out: list = []
    seen_keys: set = set()
    for item in (a or []) + (b or []):
        key = repr(item) if isinstance(item, (dict, list)) else item
        if key not in seen_keys:
            seen_keys.add(key)
            out.append(item)
    return out


def _coalesce(a: Any, b: Any) -> Any:
    return a if a else b


def _max_numeric(a: Any, b: Any) -> Any:
    def f(x: Any) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return float("-inf")

    if a is None:
        return b
    if b is None:
        return a
    return a if f(a) >= f(b) else b


def _max_date(a: Any, b: Any) -> Any:
    if not a:
        return b
    if not b:
        return a
    return a if str(a) >= str(b) else b


def _value_to_source_map(
    side_fs_entry: Any,
    side_field_value: Any,
) -> dict[str, Any]:
    """Project one side's ``field_sources.<k>`` entry to ``{repr(value): source}``.

    - Per-value list shape → walk records, key by ``repr(record["value"])``.
    - Legacy str/dict shape → applies to every value in that side's
      underlying field list (``side_field_value`` if list, else single
      value); each gets the same source.
    - Anything else → empty map.
    """
    out: dict[str, Any] = {}
    if isinstance(side_fs_entry, list):
        for entry in side_fs_entry:
            if isinstance(entry, dict) and "value" in entry and "source" in entry:
                out[repr(entry["value"])] = entry["source"]
        return out
    if side_fs_entry is None:
        return out
    # Legacy: one source for the whole field. Broadcast across the side's
    # actual list values (if any) so per-value union can absorb them.
    if isinstance(side_field_value, list):
        for v in side_field_value:
            out[repr(v)] = side_fs_entry
    elif side_field_value is not None:
        out[repr(side_field_value)] = side_fs_entry
    return out


def _build_per_value_field_sources(
    merged_list: list,
    canonical_fs_entry: Any,
    canonical_field_value: Any,
    absorbed_fs_entry: Any,
    absorbed_field_value: Any,
) -> list[dict[str, Any]]:
    """Build the per-value list shape for one list-typed field.

    Canonical-wins-per-value: for each value in the merged list (post-
    union), use canonical's source if known, else absorbed's. Values
    with no attribution on either side are omitted (no dangling stale
    entries — the field's value is still present, just unattributed).
    """
    canon_map = _value_to_source_map(canonical_fs_entry, canonical_field_value)
    absorb_map = _value_to_source_map(absorbed_fs_entry, absorbed_field_value)
    out: list[dict[str, Any]] = []
    for v in merged_list:
        key = repr(v)
        if key in canon_map:
            out.append({"value": v, "source": canon_map[key]})
        elif key in absorb_map:
            out.append({"value": v, "source": absorb_map[key]})
    return out


def _merge_field_sources(
    canonical_meta: dict[str, Any],
    absorbed_meta: dict[str, Any],
    merged_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the merged ``field_sources`` map.

    Rules (per #90 + #102):

    - For SCALAR fields: canonical's ``field_sources.<key>`` wins;
      absorbed-only entries carry forward.
    - For LIST fields: emit the per-value list-of-records shape (issue
      #102 design lock §2). Canonical wins per value; absorbed-only
      values bring their own attribution. Legacy single-source entries
      on either side broadcast across that side's underlying values
      before merging.
    - Keys whose merged value is absent from the merged frontmatter are
      pruned (don't keep dangling attributions).
    """
    cfs = canonical_meta.get("field_sources") or {}
    afs = absorbed_meta.get("field_sources") or {}
    if not isinstance(cfs, dict):
        cfs = {}
    if not isinstance(afs, dict):
        afs = {}
    if not cfs and not afs:
        return None
    merged: dict[str, Any] = {}
    all_keys: set[str] = set(cfs) | set(afs)
    for k in all_keys:
        if k not in merged_meta:
            continue  # prune dangling
        merged_val = merged_meta[k]
        if isinstance(merged_val, list):
            # Per-value shape for list fields (#102).
            per_value = _build_per_value_field_sources(
                merged_val,
                cfs.get(k),
                canonical_meta.get(k),
                afs.get(k),
                absorbed_meta.get(k),
            )
            if per_value:
                merged[k] = per_value
        else:
            # Scalar field: canonical wins; absorbed-only carries forward.
            if k in cfs:
                merged[k] = cfs[k]
            elif k in afs:
                merged[k] = afs[k]
    return merged or None


def _merge_meta(
    canonical: dict[str, Any], absorbed: dict[str, Any], absorbed_uid: str
) -> dict[str, Any]:
    out = dict(canonical)

    # List unions
    for k in _LIST_UNION_KEYS:
        merged = _union_list(canonical.get(k), absorbed.get(k))
        if merged:
            out[k] = merged
    if absorbed.get("name") and absorbed["name"] != canonical.get("name"):
        out["aliases"] = _union_list(out.get("aliases"), [absorbed["name"]])

    # Google fields
    for k in _GOOGLE_KEYS:
        merged_v = _coalesce(canonical.get(k), absorbed.get(k))
        if merged_v:
            out[k] = merged_v
    if (
        canonical.get("google_contact")
        and absorbed.get("google_contact")
        and canonical["google_contact"] != absorbed["google_contact"]
    ):
        for kspec in ("google_contact_kromatic", "google_contact_tristankromer"):
            if absorbed.get(kspec) and not canonical.get(kspec):
                out[kspec] = absorbed[kspec]

    # Apollo + LinkedIn + social URL coalesce
    for k in _APOLLO_KEYS | _LINKEDIN_KEYS | _SOCIAL_KEYS:
        merged_v = _coalesce(canonical.get(k), absorbed.get(k))
        if merged_v is not None:
            out[k] = merged_v

    # MAX-merge numeric/date fields
    for k in _MAX_KEYS_NUMERIC:
        merged_v = _max_numeric(canonical.get(k), absorbed.get(k))
        if merged_v is not None and merged_v != float("-inf"):
            out[k] = merged_v
    for k in _MAX_KEYS_DATE:
        merged_v = _max_date(canonical.get(k), absorbed.get(k))
        if merged_v:
            out[k] = merged_v

    # Stamp updated + audit trail
    out["updated"] = date.today().isoformat()
    merged_from = list(out.get("merged_from") or [])
    if absorbed_uid not in merged_from:
        merged_from.append(absorbed_uid)
    out["merged_from"] = merged_from

    # Wiki-level source: canonical wins; absorbed source archived.
    absorbed_source = absorbed.get("source")
    if absorbed_source is not None:
        mfs = dict(out.get("merged_from_sources") or {})
        mfs[absorbed_uid] = absorbed_source
        out["merged_from_sources"] = mfs

    # Per-claim field_sources merge
    new_fs = _merge_field_sources(canonical, absorbed, out)
    if new_fs is not None:
        out["field_sources"] = new_fs
    elif "field_sources" in out:
        del out["field_sources"]

    return out


def _merge_body(canonical_body: str, absorbed_body: str, absorbed_uid: str) -> str:
    absorbed_stripped = (absorbed_body or "").strip()
    if not absorbed_stripped:
        return canonical_body
    canonical_stripped = (canonical_body or "").strip()
    if not canonical_stripped:
        return absorbed_body
    if absorbed_stripped in canonical_stripped:
        return canonical_body
    return (
        canonical_body.rstrip()
        + "\n\n"
        + f"## Merged from {absorbed_uid}\n\n"
        + absorbed_stripped
        + "\n"
    )


def _perform_merge(canonical_path: Path, absorbed_path: Path, dry_run: bool) -> str:
    canonical_text = canonical_path.read_text(encoding="utf-8")
    absorbed_text = absorbed_path.read_text(encoding="utf-8")
    cmeta, cbody = parse_frontmatter(canonical_text)
    ameta, abody = parse_frontmatter(absorbed_text)
    if not cmeta or not ameta:
        return f"SKIP_PARSE:{canonical_path.name}|{absorbed_path.name}"
    canonical_uid = str(cmeta.get("uid") or "")
    absorbed_uid = str(ameta.get("uid") or "")
    if not canonical_uid or not absorbed_uid:
        return f"SKIP_NO_UID:{absorbed_path.name}"

    new_meta = _merge_meta(cmeta, ameta, absorbed_uid)
    new_body = _merge_body(cbody, abody, absorbed_uid)
    new_text = render_frontmatter(new_meta) + "\n" + new_body

    if dry_run:
        return f"WOULD_MERGE:{absorbed_uid}->{canonical_uid}"

    canonical_path.write_text(new_text, encoding="utf-8")
    absorbed_path.unlink()
    return f"MERGED:{absorbed_uid}->{canonical_uid}"


def rewrite_references(
    absorbed_uid: str,
    canonical_uid: str,
    canonical_path: Path,
    wiki_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Repoint cross-references to ``absorbed_uid`` at ``canonical_uid``.

    Walks every ``*.md`` in ``wiki_dir`` and substitutes any occurrence of
    ``absorbed_uid`` with ``canonical_uid`` (flat string replace — catches
    ``[[uid]]`` body links, frontmatter list entries, and prose mentions
    alike). Idempotent: when no sibling still mentions ``absorbed_uid``,
    returns 0.

    Skips:

    - The canonical wiki itself (preserves the ``merged_from`` audit
      trail and any ``## Merged from <absorbed_uid>`` body header).
    - Files whose name starts with ``_`` (archives, indexes).

    Ports the cwc-side ``merge_duplicate_persons.py::rewrite_references``
    behavior (issue #103). Returns the count of files rewritten; with
    ``dry_run=True``, returns the count that *would* be rewritten.
    """
    n = 0
    try:
        canonical_resolved = canonical_path.resolve()
    except OSError:
        canonical_resolved = canonical_path
    for path in wiki_dir.glob("*.md"):
        if path.name.startswith("_"):
            continue
        try:
            if path.resolve() == canonical_resolved:
                continue
        except OSError:
            if path == canonical_path:
                continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if absorbed_uid not in text:
            continue
        new_text = text.replace(absorbed_uid, canonical_uid)
        if new_text != text:
            if not dry_run:
                path.write_text(new_text, encoding="utf-8")
            n += 1
    return n


def merge_duplicate_persons(
    pairs: Iterable[DuplicatePair],
    apply: bool = False,
    wiki_root: Path | None = None,
) -> MergeReport:
    """Merge a list of duplicate pairs.

    Idempotent: pairs whose absorbed file is already gone are counted
    under ``already_merged`` and skipped. Defaults to dry-run; pass
    ``apply=True`` to write changes.

    ``wiki_root`` is used only to resolve relative paths recorded on
    :class:`DuplicatePair` — absolute paths bypass it.
    """
    report = MergeReport(dry_run=not apply)
    for pair in pairs:
        cpath = Path(pair.canonical_path)
        apath = Path(pair.absorbed_path)
        if not cpath.is_absolute() and wiki_root is not None:
            cpath = wiki_root / cpath
        if not apath.is_absolute() and wiki_root is not None:
            apath = wiki_root / apath
        if not cpath.exists():
            report.missing_canonical += 1
            continue
        if not apath.exists():
            report.already_merged += 1
            continue
        try:
            outcome = _perform_merge(cpath, apath, dry_run=not apply)
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{pair.absorbed_uid}: {exc}")
            continue
        if outcome.startswith("SKIP_PARSE"):
            report.skipped_parse += 1
        elif outcome.startswith("MERGED") or outcome.startswith("WOULD_MERGE"):
            report.merged += 1
            # Sweep cross-uid references so siblings repoint at canonical.
            # Use the directory the canonical lives in as the wiki dir
            # (matches the existing absolute-path-first contract on
            # DuplicatePair).
            sweep_dir = cpath.parent if cpath.parent.exists() else wiki_root
            if sweep_dir is not None:
                try:
                    report.references_rewritten += rewrite_references(
                        absorbed_uid=pair.absorbed_uid,
                        canonical_uid=pair.canonical_uid,
                        canonical_path=cpath,
                        wiki_dir=sweep_dir,
                        dry_run=not apply,
                    )
                except OSError as exc:
                    report.errors.append(
                        f"rewrite_references {pair.absorbed_uid}: {exc}"
                    )
    return report


# --- Report I/O for CLI plumbing ---


def pairs_to_yaml(pairs: list[DuplicatePair]) -> str:
    """Serialize pairs as a YAML document (list of dicts)."""
    return yaml.safe_dump(
        [asdict(p) for p in pairs],
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def pairs_from_yaml(text: str) -> list[DuplicatePair]:
    """Parse pairs back from YAML produced by :func:`pairs_to_yaml`."""
    raw = yaml.safe_load(text) or []
    if not isinstance(raw, list):
        raise ValueError("dedupe report must be a YAML list")
    out: list[DuplicatePair] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"dedupe entry must be a dict, got {type(item).__name__}")
        out.append(
            DuplicatePair(
                canonical_uid=str(item["canonical_uid"]),
                absorbed_uid=str(item["absorbed_uid"]),
                match_signal=str(item["match_signal"]),
                confidence=str(item.get("confidence", "HIGH")),
                canonical_path=str(item.get("canonical_path", "")),
                absorbed_path=str(item.get("absorbed_path", "")),
            )
        )
    return out


__all__ = [
    "DuplicatePair",
    "MergeReport",
    "find_duplicate_persons",
    "merge_duplicate_persons",
    "pairs_to_yaml",
    "pairs_from_yaml",
    "rewrite_references",
]
