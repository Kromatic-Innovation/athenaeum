# SPDX-License-Identifier: Apache-2.0
"""``athenaeum dimensions show|compare`` — issue athenaeum#714.

The registry's consumer for THIS PR (per the issue's own Wiring AC: "at
minimum, coordinate stamping at write time and a CLI surface that shows a
claim's coordinates and compares two claims' coordinates axis-by-axis"). The
five-verdict comparator that would consume the full algebra automatically is
explicitly a separate, future child of epic athenaeum#709 — this command is the
manual, axis-by-axis surface in the meantime.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` and registers via ``add_<name>_subparser`` — see
``cli.py``'s module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, load_config, resolve_dimensions


def _read_meta(path: Path) -> dict[str, object] | None:
    from athenaeum.models import parse_frontmatter

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return None
    meta, _ = parse_frontmatter(text)
    if not isinstance(meta, dict):
        print(f"error: {path} has no parseable frontmatter", file=sys.stderr)
        return None
    return meta


def cmd_dimensions(args: argparse.Namespace) -> int:
    """Dispatch ``athenaeum dimensions show|compare``."""
    target = getattr(args, "dimensions_target", None)
    if target not in ("show", "compare"):
        print("usage: athenaeum dimensions show|compare [...]", file=sys.stderr)
        return 2

    from athenaeum.dimensions import coordinate_value
    from athenaeum.pii import json_date_default

    knowledge_root = (args.path or DEFAULT_KNOWLEDGE_ROOT).expanduser().resolve()
    config = load_config(knowledge_root)
    registry = resolve_dimensions(config)

    if target == "show":
        meta = _read_meta(Path(args.file))
        if meta is None:
            return 1
        coords = {d.name: coordinate_value(d, meta) for d in registry}
        if args.json:
            # Issue athenaeum#1110: `coordinate_value` returns the RAW (unparsed)
            # frontmatter value (dimensions.py's own docstring) for kernel
            # dimensions like recorded-time/observed-time/valid-time, which can
            # be a bare YAML date/datetime — same defect class as `entity`'s
            # raw-frontmatter passthrough, same fix.
            sys.stdout.write(json.dumps(coords, default=json_date_default) + "\n")
            return 0
        print(f"coordinates for {args.file}:")
        for name, value in coords.items():
            print(f"  {name}: {value!r}")
        return 0

    # compare
    from athenaeum.dimensions import compare_dimension

    meta_a = _read_meta(Path(args.file_a))
    meta_b = _read_meta(Path(args.file_b))
    if meta_a is None or meta_b is None:
        return 1
    results = {d.name: compare_dimension(d, meta_a, meta_b) for d in registry}
    if args.json:
        sys.stdout.write(json.dumps(results) + "\n")
        return 0
    print(f"axis-by-axis comparison: {args.file_a}  vs  {args.file_b}")
    for name, relation in results.items():
        print(f"  {name}: {relation}")
    return 0


def add_dimensions_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``athenaeum dimensions`` and its ``show``/``compare`` modes."""
    parser = subparsers.add_parser(
        "dimensions",
        help=(
            "Dimension registry: show a claim's coordinates or compare two "
            "claims' coordinates axis-by-axis (issue athenaeum#714)."
        ),
    )
    parser.set_defaults(func=cmd_dimensions)
    sub = parser.add_subparsers(dest="dimensions_target")

    show = sub.add_parser(
        "show", help="Show one wiki page's coordinates across every registered dimension."
    )
    show.set_defaults(func=cmd_dimensions)
    show.add_argument("file", help="Path to a wiki page (.md).")
    show.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory, for loading athenaeum.yaml (default: ~/knowledge).",
    )
    show.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    compare = sub.add_parser(
        "compare",
        help="Compare two wiki pages' coordinates axis-by-axis, one relation per dimension.",
    )
    compare.set_defaults(func=cmd_dimensions)
    compare.add_argument("file_a", help="Path to the first wiki page (.md).")
    compare.add_argument("file_b", help="Path to the second wiki page (.md).")
    compare.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory, for loading athenaeum.yaml (default: ~/knowledge).",
    )
    compare.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
