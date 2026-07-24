# SPDX-License-Identifier: Apache-2.0
"""``athenaeum authority {lint,convert}`` — authority manifest CLI (issue #426).

Mirrors ``athenaeum merges`` / ``athenaeum questions`` in shape: a thin CLI
dispatcher over library functions in :mod:`athenaeum.authority`, no logic of
its own.

Two subcommands:

- ``lint``     scan ``wiki/*.md`` for pages that duplicate a manifest-listed
                 authoritative source. READ-ONLY — never mutates a page,
                 regardless of flags (there is no ``--apply`` on ``lint``).
- ``convert``  convert ONE duplicating page (by path) into a one-line
                 pointer stub. Default is dry-run (prints the would-be
                 result); ``--apply`` writes it. Never runs over the whole
                 corpus — issue #426 is explicit that converting the live
                 corpus is a separate operator task (#437); this CLI only
                 ever touches the single ``--page`` path given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.authority import (
    AuthorityManifestError,
    convert_page_to_pointer_stub,
    find_duplicates_in_wiki,
    load_authority_manifest,
)
from athenaeum.config import load_config, resolve_authority_manifest_path


def _resolve_knowledge_root(args: argparse.Namespace) -> Path:
    return (getattr(args, "path", None) or Path("~/knowledge")).expanduser().resolve()


def _load_manifest_or_exit(knowledge_root: Path):
    config = load_config(knowledge_root)
    manifest_path = resolve_authority_manifest_path(knowledge_root, config)
    try:
        return load_authority_manifest(manifest_path)
    except AuthorityManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_authority(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum authority {lint,convert}``."""
    sub = getattr(args, "authority_target", None)
    if sub == "lint":
        return _cmd_authority_lint(args)
    if sub == "convert":
        return _cmd_authority_convert(args)
    print("usage: athenaeum authority {lint,convert} [...]", file=sys.stderr)
    return 2


def _cmd_authority_lint(args: argparse.Namespace) -> int:
    """READ-ONLY: list wiki pages that duplicate a manifest-listed source.

    Never mutates ``wiki/`` — there is no write path in this branch at all
    (no lock is taken, no file is opened for writing). Exit codes mirror the
    repo convention for a "found something to review" dry-run report:
    0 - no duplicates found; 1 - a manifest error occurred; a non-zero exit
    is NOT used to signal "duplicates found" here (unlike ``auto-memory
    prune``'s dry-run) because ``lint`` is a routine read-only report, not a
    pre-apply confirmation gate.
    """
    knowledge_root = _resolve_knowledge_root(args)
    wiki_root = knowledge_root / "wiki"
    manifest = _load_manifest_or_exit(knowledge_root)
    if manifest is None:
        return 1
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    matches = find_duplicates_in_wiki(wiki_root, manifest)

    if args.json:
        payload = [
            {
                "page": str(m.page_path),
                "matched_topic": m.matched_topic,
                "source_slug": m.source.slug,
                "source_location": m.source.location,
            }
            for m in matches
        ]
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0

    if not matches:
        print("0 duplicates")
        return 0

    print(f"{len(matches)} duplicate(s):")
    for m in matches:
        print(
            f"  {m.page_path.name}: topic {m.matched_topic!r} owned by "
            f"{m.source.slug} ({m.source.location})"
        )
    return 0


def _cmd_authority_convert(args: argparse.Namespace) -> int:
    """Convert ONE page into a pointer stub for the given source slug.

    Default is dry-run (prints the converted text without writing);
    ``--apply`` writes it. Scoped to exactly the ``--page`` given — this
    command never walks the corpus (running the converter against the whole
    live corpus is operator task #437, out of scope here).
    """
    knowledge_root = _resolve_knowledge_root(args)
    manifest = _load_manifest_or_exit(knowledge_root)
    if manifest is None:
        return 1

    page_path: Path = args.page
    if not page_path.is_file():
        print(f"error: page not found: {page_path}", file=sys.stderr)
        return 1

    source = next((s for s in manifest.sources if s.slug == args.source_slug), None)
    if source is None:
        print(
            f"error: no authority-manifest source with slug {args.source_slug!r}",
            file=sys.stderr,
        )
        return 1

    new_text = convert_page_to_pointer_stub(page_path, source, title=args.title)

    if not args.apply:
        print(f"[DRY RUN] would convert {page_path} to:", file=sys.stderr)
        sys.stdout.write(new_text)
        return 0

    page_path.write_text(new_text, encoding="utf-8")
    print(f"converted {page_path} -> pointer stub ({source.slug})")
    return 0


def add_authority_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum authority`` and its two modes on ``subparsers``."""
    a_parser = subparsers.add_parser(
        "authority",
        help="Authority manifest: detect + convert memories that duplicate a "
        "live source (skill file, code path, config) into pointer stubs "
        "(issue #426).",
    )
    a_sub = a_parser.add_subparsers(dest="authority_target")

    lint_p = a_sub.add_parser(
        "lint",
        help="List wiki pages that duplicate a manifest-listed authoritative "
        "source. READ-ONLY — never mutates wiki/.",
    )
    lint_p.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge)",
    )
    lint_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )

    convert_p = a_sub.add_parser(
        "convert",
        help="Convert ONE page into a one-line pointer stub for a given "
        "manifest source. Default is dry-run; --apply writes the file. "
        "Scoped to a single --page; never walks the corpus (see #437).",
    )
    convert_p.add_argument(
        "--path",
        type=Path,
        default=Path("~/knowledge"),
        help="Knowledge directory (default: ~/knowledge), used to resolve "
        "the authority manifest.",
    )
    convert_p.add_argument(
        "--page",
        type=Path,
        required=True,
        help="Path to the wiki page to convert.",
    )
    convert_p.add_argument(
        "--source-slug",
        dest="source_slug",
        required=True,
        help="The manifest source slug this page duplicates.",
    )
    convert_p.add_argument(
        "--title",
        default=None,
        help="Override the stub's title (default: the page's frontmatter "
        "name).",
    )
    convert_p.add_argument(
        "--apply",
        action="store_true",
        help="Write the converted stub to --page. Without this flag, the "
        "command is a dry-run that prints the result to stdout.",
    )
