#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Snapshot the public surface of a checkout (issue athenaeum#1335).

Produces the baseline `tests/fixtures/public_surface_baseline.json` that
`tests/test_public_surface_guard.py` compares the working tree against.

The baseline must describe **the last PUBLISHED release**, not the last tag and
not `develop` — the guard's question is "what can a consumer who installed from
PyPI already import", and only a published version answers it. Regenerate it
when a release is actually published, not when one is prepared.

Run it against a historical tree by pointing `--source-root` at a checkout of
that tag; the script puts that tree on `sys.path` FIRST so the runtime
dimensions (CLI subcommands, `__all__`) describe it rather than the working
copy. That is how the committed v0.19.0 baseline was produced — nothing in it is
hand-listed:

    git worktree add --detach /tmp/ath-v0190 v0.19.0
    python scripts/snapshot_public_surface.py \\
        --source-root /tmp/ath-v0190/src --version 0.19.0 \\
        --output tests/fixtures/public_surface_baseline.json
    git worktree remove /tmp/ath-v0190

With no arguments it snapshots the working tree and prints to stdout, which is
the quickest way to see what the guard currently sees.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _working_tree_version() -> str:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="the 'src' directory to snapshot (default: this repo's own src/)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="version label to record (default: the working tree's pyproject version)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="file to write (default: stdout)",
    )
    args = parser.parse_args(argv)

    source_root = args.source_root or (_REPO_ROOT / "src")
    # Prepend, not append: an editable install of the working tree is already on
    # sys.path, and appending would silently snapshot THIS tree while labelling
    # it with the historical version — the exact wrong-baseline failure this
    # guard exists to prevent, reintroduced one level up.
    sys.path.insert(0, str(source_root.resolve()))

    sys.path.insert(0, str(_REPO_ROOT))
    from tests.public_surface import extract_surface

    surface = extract_surface(source_root)
    payload = {
        "_comment": (
            "Public surface of the last PUBLISHED release. Regenerate with "
            "scripts/snapshot_public_surface.py when a release is published "
            "(issue athenaeum#1335). Do not hand-edit: a name omitted here is a "
            "name the guard can never notice the removal of."
        ),
        "version": args.version or _working_tree_version(),
        "source_root": str(source_root),
        "generated": datetime.now(UTC).isoformat(),
        **surface,
    }
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        counts = ", ".join(
            f"{k}={len(v)}"
            for k, v in surface.items()  # type: ignore[arg-type]
        )
        print(f"wrote {args.output} ({payload['version']}: {counts})", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
