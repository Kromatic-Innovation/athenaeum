# SPDX-License-Identifier: Apache-2.0
"""Minimal synthetic source → raw-intake adapter (illustrative).

This is a *generic, synthetic* worked example of the adapter contract
documented in ``docs/adapter-contract.md``. It writes a single raw-intake
file that the librarian will pick up on the next ``athenaeum run`` and
compile into the wiki. It contains **no** Kromatic-specific integration
details, credentials, or PII — real production adapters (LinkedIn, Gmail,
contact-sync, …) live in their own private host repositories by design;
athenaeum's OSS contract stops at the on-disk raw-intake shape (see
``docs/provenance-shape.md`` §6).

The example targets **Lane A** — the general entity-schema intake lane:

    <knowledge-root>/raw/<source>/<timestamp>-<uuid8>.md

Run it against a throwaway knowledge root::

    python -m athenaeum init --path /tmp/kb          # scaffold raw/ + wiki/
    python examples/adapters/minimal_adapter.py /tmp/kb
    python -m athenaeum run --path /tmp/kb           # compile raw → wiki

The two public helpers used below — ``render_frontmatter`` and
``generate_uid`` — are part of athenaeum's stable ``__all__`` surface, so
an adapter built on them does not reach into internal modules.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from athenaeum import generate_uid, render_frontmatter

# The intake filename convention the librarian recognises:
#   <YYYYMMDDTHHMMSSZ>-<8 hex chars>.md
# (mirrors ``athenaeum.librarian.RAW_FILE_RE``). Files that do not match are
# still discovered, but this canonical name is what tooling expects.
_TIMESTAMP_FMT = "%Y%m%dT%H%M%SZ"


def _sanitize_source(source: str) -> str:
    """Reduce a source label to a filesystem-safe subdirectory name.

    Mirrors the MCP ``remember`` tool: keep alphanumerics plus ``-`` and
    ``_``. A source that sanitizes to the empty string is rejected — the
    ``raw/<source>/`` directory must have a real name so the compiled wiki
    can trace a claim back to its origin adapter.
    """
    safe = "".join(c for c in source if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError(
            f"source {source!r} contains no alphanumeric characters; "
            "pick a stable, human-readable adapter name (e.g. 'notes')"
        )
    return safe


def write_raw_intake(
    knowledge_root: Path,
    source: str,
    body: str,
    *,
    source_type: str = "external",
    source_ref: str,
    name: str | None = None,
    description: str | None = None,
) -> Path:
    """Write one raw-intake file under ``raw/<source>/`` and return its path.

    Contract highlights (full detail in ``docs/adapter-contract.md``):

    * **Location** — ``<knowledge-root>/raw/<source>/<timestamp>-<uuid8>.md``.
      ``source`` names the adapter and becomes the intake subdirectory.
    * **Write-once** — the ``<timestamp>-<uuid8>`` filename is unique per
      call, so an adapter never rewrites an existing intake file. Re-running
      an adapter appends new files; near-duplicates are collapsed at compile
      time by the librarian's clustering/dedupe, not by the adapter.
    * **Provenance** — every write declares where the fact came from via a
      ``source: <type>:<ref>`` scalar. Never cite the raw filename itself as
      the source (see ``policies/auto-memory-citation.md``); cite the
      ultimate origin (an external URL, an API, a document, a person).
    * **Append-only & path-safe** — writes stay strictly inside ``raw/`` and
      never touch ``wiki/`` (the librarian is the *only* writer to the wiki).
    * **Atomic** — the file is written to a same-directory temp file and
      ``os.replace``d into place so a crash mid-write never leaves the
      librarian a half-written frontmatter block to parse.
    """
    raw_root = (knowledge_root / "raw").resolve()
    target_dir = (raw_root / _sanitize_source(source)).resolve()

    # Path-safety: the write must land inside raw/, never wiki/ or a sibling
    # reached via traversal. is_relative_to (not str.startswith) so a
    # "raw-sibling" directory can't masquerade as a descendant of "raw".
    if not (target_dir == raw_root or target_dir.is_relative_to(raw_root)):
        raise ValueError("refusing to write outside the raw/ intake tree")

    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime(_TIMESTAMP_FMT)
    filename = f"{timestamp}-{generate_uid()}.md"
    filepath = target_dir / filename

    # Open-schema frontmatter (see athenaeum.models.parse_frontmatter). Only
    # `source` is contract-critical here; `name`/`description` help the
    # compiler and are optional.
    meta: dict[str, object] = {}
    if name is not None:
        meta["name"] = name
    if description is not None:
        meta["description"] = description
    meta["source"] = f"{source_type}:{source_ref}"

    content = render_frontmatter(meta) + "\n" + body.rstrip() + "\n"

    # Atomic same-dir temp + os.replace (POSIX + Windows atomic rename).
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, suffix=".md.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, filepath)
    except BaseException:
        # Leave the original tree untouched on any failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return filepath


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "knowledge_root",
        type=Path,
        help="Path to an initialised knowledge root (see `athenaeum init`).",
    )
    parser.add_argument(
        "--source",
        default="notes",
        help="Adapter name; becomes the raw/<source>/ subdirectory.",
    )
    args = parser.parse_args(argv)

    # A synthetic, generic fact — no real people, no PII.
    written = write_raw_intake(
        args.knowledge_root,
        source=args.source,
        name="acme-widget-launch",
        description="Synthetic example fact written by the minimal adapter.",
        source_type="external",
        source_ref="https://example.com/press/widget-launch",
        body=(
            "# Acme Widget launch\n\n"
            "Acme announced the Widget on 2026-01-15. This is a synthetic "
            "example fact used to demonstrate the raw-intake adapter contract; "
            "it contains no real or private information."
        ),
    )
    print(f"wrote raw intake file: {written}")
    print("compile it with:  athenaeum run --path", args.knowledge_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
