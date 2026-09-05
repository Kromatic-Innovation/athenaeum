# SPDX-License-Identifier: Apache-2.0
"""``athenaeum enumerate`` — the generalized ENUMERATION primitive (issue athenaeum#965).

Shell-accessible CLI surface over :func:`athenaeum.enumeration.enumerate_entities`
— the MCP tool of the same name (registered in :mod:`athenaeum.mcp_server`)
wraps the identical function, so the two entry points cannot drift. See that
module's docstring for the full contract (backend, PII gating, audience
scoping, pagination).

Factoring rule (L5 presentation, per ``_cmd_query.py``'s own docstring): a
self-contained CLI subcommand lives in its own ``_cmd_<name>.py`` and
registers via ``add_<name>_subparser`` — this is that module for
``enumerate``, kept separate from ``_cmd_query.py``'s ``recall``/``people``
group because it is a distinct primitive (no query text, no ranking), not a
sibling shape of the same read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, resolve_cache_dir
from athenaeum.enumeration import DEFAULT_LIMIT, PREDICATE_KINDS, FieldPredicate


def _parse_where(raw: str) -> FieldPredicate:
    """Parse one ``--where`` value: ``FIELD[,FIELD2,...]:KIND:VALUE``.

    ``FIELD`` may be a comma-separated ORDERED fallback list (OR across
    fields — the ``--company``-style shape generalized, issue athenaeum#965).
    ``KIND`` is one of ``eq``, ``ne``, ``substring``, ``regex`` — ``ne`` is
    ``eq`` negated (``FieldPredicate.negate=True``), the ergonomic shape AC
    amendment 1's own ``do_not_email != true`` example uses; it is not a
    fourth independent match kind. ``VALUE`` is everything after the second
    ``:`` verbatim (so a regex value may itself contain ``:``).
    """
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"--where must be FIELD[,FIELD2,...]:KIND:VALUE, got {raw!r}"
        )
    fields_str, kind_token, value = parts
    fields = tuple(f.strip() for f in fields_str.split(",") if f.strip())
    if not fields:
        raise argparse.ArgumentTypeError(f"--where has no field name(s): {raw!r}")
    kind = kind_token.strip().lower()
    negate = False
    if kind == "ne":
        kind = "eq"
        negate = True
    if kind not in PREDICATE_KINDS:
        raise argparse.ArgumentTypeError(
            "--where KIND must be one of eq, ne, substring, regex "
            f"(got {kind_token!r})"
        )
    try:
        return FieldPredicate(fields=fields, kind=kind, value=value, negate=negate)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_enumerate_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``enumerate``."""
    parser = subparsers.add_parser(
        "enumerate",
        help="Enumerate every entity of a declared type matching field "
        "predicates — no query text (issue athenaeum#965). The generalized "
        "form of the former `athenaeum people` (removed, athenaeum#1079) — "
        "see docs/design/recall-architecture.md's capability-parity table.",
    )
    parser.add_argument(
        "--type",
        dest="entity_type",
        required=True,
        help="Declared entity type (a page's `type:`). Call `athenaeum query "
        "entity-schema`-equivalent (the MCP `entity_schema` tool) to discover "
        "this deployment's classes. An unrecognized value does not error — "
        "the response's `known_classes` names what this deployment DOES have.",
    )
    parser.add_argument(
        "--where",
        dest="where",
        action="append",
        type=_parse_where,
        default=[],
        metavar="FIELD[,FIELD2,...]:KIND:VALUE",
        help="Field predicate, AND-combined, repeatable. KIND is one of eq, "
        "ne, substring, regex (eq/substring/regex all compare "
        "case-insensitively). FIELD may be a comma-separated ORDERED "
        "fallback list, OR-combined (e.g. "
        "current_company,linkedin_company_at_connect:substring:Acme).",
    )
    parser.add_argument(
        "--sort",
        dest="sort_key",
        default="name",
        help="Frontmatter field to sort by (default: name).",
    )
    parser.add_argument(
        "--ascending",
        action="store_true",
        help="Sort ascending instead of the default descending.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max rows to return (default: {DEFAULT_LIMIT}; 0 = unlimited).",
    )
    parser.add_argument(
        "--cursor",
        default=None,
        help="Opaque continuation token from a prior call's `next_cursor`.",
    )
    parser.add_argument(
        "--field",
        dest="field",
        action="append",
        default=[],
        metavar="NAME",
        help="Additional declared field to include per hit (repeatable), "
        "beyond the always-present uid/type/name.",
    )
    parser.add_argument(
        "--with-pii",
        dest="with_pii",
        action="store_true",
        help="Required to predicate or select `google_contact_*` fields "
        "(issue athenaeum#965 AC amendment 1). Same flag contract "
        "`recall --with-pii` already uses. NOT required for `do_not_email` "
        "(ungated by athenaeum#1122).",
    )
    parser.add_argument(
        "--audience",
        type=str,
        default=None,
        help="Run under a restricted read scope (issue athenaeum#538), "
        "matching `recall --audience`. Unset = owner = full access.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    parser.set_defaults(func=cmd_enumerate)


def cmd_enumerate(args: argparse.Namespace) -> int:
    """Print one JSON document: ``{hits, next_cursor, known_classes}``."""
    from athenaeum.config import (
        load_config,
        resolve_audience,
        resolve_extra_intake_roots,
    )
    from athenaeum.enumeration import enumerate_entities

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    cache_dir = resolve_cache_dir(args.cache_dir).resolve()
    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)
    caller_audience = resolve_audience(cfg, getattr(args, "audience", None))

    try:
        result = enumerate_entities(
            wiki_root,
            cache_dir,
            entity_type=args.entity_type,
            predicates=args.where,
            sort_key=args.sort_key,
            descending=not args.ascending,
            limit=args.limit,
            cursor=args.cursor,
            fields=args.field,
            with_pii=args.with_pii,
            caller_audience=caller_audience,
            extra_roots=extra_roots,
            config=cfg,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "hits": list(result.hits),
                "next_cursor": result.next_cursor,
                "known_classes": list(result.known_classes),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0
