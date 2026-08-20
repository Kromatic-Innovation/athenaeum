# SPDX-License-Identifier: Apache-2.0
"""``athenaeum {recall,people,person,query-topics,stopwords,test-mcp}`` — read-only query tools.

Six subcommands grouped here because each is a shell-accessible, read-only
query or diagnostic over the compiled wiki / search stack, used by validation
harnesses, hooks, and operator debugging — none mutates the knowledge base.

Factoring rule (L5 presentation): a self-contained CLI subcommand lives in
its own ``_cmd_<name>.py`` (or a small same-domain group module like this
one) and registers via ``add_<name>_subparser`` — this is where a NEW
subcommand goes, not inline in ``cli.py``'s ``main()``. This module may
import library modules (L4/L3) but ``cli.py`` only imports the
``add_*_subparser`` entry points, kept lazy/local to keep top-level import
cost down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from athenaeum._cli_shared import _iso_date
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT, resolve_cache_dir

if TYPE_CHECKING:
    # Annotation-only (issue athenaeum#886): every `pii` import in this module stays
    # local to its function, per the module docstring's factoring rule, so this
    # never lands on `cli.py`'s top-level import path.
    from athenaeum.pii import ExcludedRecordIndex


def _numeric_frontmatter_value(value: object) -> int | float | str:
    """Narrow an open-schema frontmatter value to something ``float``/``int`` accept.

    :func:`athenaeum.models.parse_frontmatter` intentionally returns
    ``dict[str, object]`` (YAML frontmatter is open-schema — see that
    function's docstring), so a numeric-looking field like ``warm_score`` is
    typed ``object`` at the point of use here. Callers already wrap the
    ``float()``/``int()`` call in ``except (TypeError, ValueError)`` to
    tolerate a malformed or missing value at runtime; this just gives mypy a
    type ``float``/``int`` actually accept, falling back to ``str(value)`` for
    anything else so the existing except-clause is what catches a genuinely
    unparseable value (e.g. a non-numeric string), not a new failure mode here.
    """
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def add_query_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register ``recall``, ``people``, ``query-topics``, ``stopwords``, ``test-mcp``."""
    # Local, not module-level: keeps this module's import cost off `cli.py`'s
    # top-level path (see the module docstring's factoring rule). Needed here
    # because `--usage-class` pins its `choices` to the canonical tuple rather
    # than transcribing the class names, which would drift.
    from athenaeum.pii import USAGE_CLASSES

    # test-mcp command — smoke-test the MCP memory setup without a session
    test_mcp_parser = subparsers.add_parser(
        "test-mcp",
        help="Smoke-test MCP remember/recall against a synthetic knowledge dir",
    )
    test_mcp_parser.add_argument(
        "--keep",
        action="store_true",
        help="Don't delete the temp knowledge dir on exit (for debugging)",
    )
    test_mcp_parser.set_defaults(func=cmd_test_mcp)

    # people command — frontmatter-only filter over type:person wikis
    people_parser = subparsers.add_parser(
        "people",
        help="List type:person wikis filtered by frontmatter (company / tag / tier / score). "
        "No LLM, no embeddings — deterministic over the wiki tree.",
    )
    people_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    people_parser.add_argument(
        "--company",
        action="append",
        default=[],
        help=(
            "Match current_company OR linkedin_company_at_connect "
            "(case-insensitive substring). Repeat to AND."
        ),
    )
    people_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Require this exact tag (repeat to AND).",
    )
    people_parser.add_argument(
        "--tier",
        default="",
        help="Shorthand for --tag tier:<value> (warm-a / warm-b / warm-c / extended / active).",
    )
    people_parser.add_argument(
        "--title-regex",
        action="append",
        default=[],
        help=(
            "Match current_title OR linkedin_position_at_connect against this "
            "regex (case-insensitive). Repeat to AND multiple patterns."
        ),
    )
    people_parser.add_argument(
        "--company-regex",
        action="append",
        default=[],
        help=(
            "Match current_company OR linkedin_company_at_connect against this "
            "regex (case-insensitive). Repeat to AND multiple patterns."
        ),
    )
    people_parser.add_argument(
        "--top-touch",
        type=int,
        default=0,
        help="Sort by recent-touch signal (meeting+sent counts) and return top N. "
        "Default sort is by warm_score desc.",
    )
    people_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to print (default: 50; 0 = unlimited)",
    )
    people_parser.add_argument(
        "--format",
        choices=["table", "tsv"],
        default="table",
        help="Output shape (default: table).",
    )
    people_parser.set_defaults(func=cmd_people)

    # person command — one-call person read by uid (issue athenaeum#864). Distinct
    # from `people` above: `people` filters/lists many type:person wikis by
    # frontmatter; `person` reads exactly ONE person's page (+ optional
    # contact data) by uid. DEPRECATED as of athenaeum#887 in favour of the
    # class-generic `entity` command below and `recall --with-pii`; retained,
    # unchanged, until athenaeum#888 removes it.
    person_parser = subparsers.add_parser(
        "person",
        help="DEPRECATED (athenaeum#887; use `entity` or `recall --with-pii`). "
        "One-call read of a SINGLE person's page by uid, with an explicit "
        "--include-contact flag (default off). Not `people` (which filters/lists "
        "many person wikis by frontmatter) — this reads exactly one person.",
    )
    person_parser.add_argument(
        "--uid",
        required=True,
        help="The person's durable uid.",
    )
    person_parser.add_argument(
        "--include-contact",
        action="store_true",
        help="Include the actual contact values (default: off — withheld "
        "fields carry a redaction marker instead).",
    )
    person_parser.add_argument(
        "--usage-class",
        action="append",
        choices=list(USAGE_CLASSES),
        default=[],
        metavar="CLASS",
        help="Return only contact values of this usage class (repeatable; "
        f"one of {', '.join(USAGE_CLASSES)}). Default: every value, each "
        "carrying its class. `--usage-class observed` is the "
        "outreach-eligible set — address-book population and outreach "
        "eligibility are different permissions (issue athenaeum#866).",
    )
    person_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    person_parser.set_defaults(func=cmd_person)

    # entity command (issue athenaeum#886) — the generic form of `person`, reading
    # ANY entity class's excluded record. `person` delegates to the same body.
    entity_parser = subparsers.add_parser(
        "entity",
        help="One-call read of a SINGLE entity's page by uid, for any entity "
        "class, with an explicit --include-excluded flag (default off). The "
        "generic form of `person`; prints the same JSON object shape.",
    )
    entity_parser.add_argument(
        "--uid",
        required=True,
        help="The entity's durable uid.",
    )
    entity_parser.add_argument(
        "--class",
        dest="entity_class",
        default="person",
        help="The page's `type:` (person, vendor, …). Selects which excluded "
        "SURFACE is read — a `person` page's record lives on the `pii` "
        "surface — while the page itself is resolved by uid whatever its "
        "type. Default: person.",
    )
    entity_parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Include the actual excluded values (default: off — withheld "
        "fields carry a redaction marker instead).",
    )
    entity_parser.add_argument(
        "--usage-class",
        action="append",
        choices=list(USAGE_CLASSES),
        default=[],
        metavar="CLASS",
        help="Return only values of this usage class (repeatable; one of "
        f"{', '.join(USAGE_CLASSES)}). Default: every value, each carrying "
        "its class.",
    )
    entity_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    entity_parser.set_defaults(func=cmd_entity)

    # query-topics command — LLM-based topic extraction for hook query rewriting
    query_topics_parser = subparsers.add_parser(
        "query-topics",
        help="Extract substantive search topics from a prompt (Haiku). "
        "Used by the UserPromptSubmit hook to rewrite queries before "
        "FTS5/vector search. Prints one topic per line to stdout; "
        "empty output means fall back to the caller's built-in extractor.",
    )
    query_topics_parser.add_argument(
        "prompt",
        type=str,
        help="The user's raw message.",
    )
    query_topics_parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for the LLM before giving up (default: 3.0)",
    )
    query_topics_parser.add_argument(
        "--knowledge-root",
        "--path",
        type=Path,
        default=None,
        help="Knowledge directory whose athenaeum.yaml supplies "
        "models.topic (default: ~/knowledge). "
        "--path is an alias, matching init/status/serve.",
    )
    query_topics_parser.set_defaults(func=cmd_query_topics)

    # stopwords command — print the canonical stopword list for shell hooks
    stopwords_parser = subparsers.add_parser(
        "stopwords",
        help="Print the stopword list (one word per line). "
        "Used by the example UserPromptSubmit hook's regex fallback "
        "to stay in sync with the FTS5 query filter.",
    )
    stopwords_parser.set_defaults(func=cmd_stopwords)

    # recall command — shell-accessible recall for validation harnesses
    # and operator debugging. Wraps the MCP `recall` tool so scripts and
    # `gh_wait_status.sh`-style tooling can exercise the same search path
    # without spinning up a Claude Code session.
    recall_parser = subparsers.add_parser(
        "recall",
        help="Search the wiki from the shell (one tab-separated hit per line)",
    )
    recall_parser.add_argument(
        "query",
        type=str,
        help="Search query string",
    )
    recall_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Maximum results to return (default: 5)",
    )
    recall_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT,
        help="Knowledge directory (default: ~/knowledge)",
    )
    recall_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory (default: ~/.cache/athenaeum)",
    )
    recall_parser.add_argument(
        "--backend",
        choices=["keyword", "fts5", "vector"],
        default=None,
        help="Override configured backend (default: read from athenaeum.yaml)",
    )
    recall_parser.add_argument(
        "--audience",
        type=str,
        default=None,
        help="Issue athenaeum#312: run recall under a restricted read scope. "
        "Comma-separated role/group ids; only pages tagged for one of these "
        "roles (or 'access: open') are returned. Unset = owner = full access. "
        "Exercises the identical filter path as `serve --audience`.",
    )
    recall_parser.add_argument(
        "--with-pii",
        dest="with_pii",
        action="store_true",
        help="Issue athenaeum#885/#886: also resolve each matching entity's EXCLUDED "
        "fields — contact data for a person, and whatever else the operator "
        "routes off-corpus for any other class. Appended to each hit line as "
        "tab-separated `field=value` pairs; a withheld field appears as "
        "`field=[redacted:N]` so withheld never looks like absent. Default off, "
        "and free when off: no excluded surface is scanned at all. The join "
        "runs strictly AFTER the audience and recallable filters, so it can "
        "never widen what this command returns.",
    )
    recall_parser.add_argument(
        "--usage-class",
        action="append",
        choices=list(USAGE_CLASSES),
        default=[],
        metavar="CLASS",
        help="With --with-pii, return only excluded values of this usage class "
        f"(repeatable; one of {', '.join(USAGE_CLASSES)}). Matches "
        "`query person --usage-class`. Default: every value.",
    )
    recall_parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Issue athenaeum#308: temporal 'as-of' view. Return the wiki as it stood on "
        "this date — pages outside their [valid_from, valid_until] window then "
        "are excluded; a fact valid then but expired now is included. Builds a "
        "throwaway as-of index in a scratch cache dir (indexed backends) or "
        "filters at query time (keyword); the live index is untouched. Unset = "
        "today.",
    )
    recall_parser.add_argument(
        "--type",
        dest="type_filter",
        action="append",
        default=[],
        metavar="CLASS",
        help="Issue athenaeum#964: narrow to one or more entity classes (a page's "
        "`type:`), repeatable (OR semantics). Opaque, NOT validated against "
        "wiki/_schema/types.md — see the MCP `entity_schema` tool for the "
        "declared/observed registry. Unset = every class (default, unchanged "
        "behavior). An unrecognized value prints the deployment's known "
        "classes instead of a bare 'no results'.",
    )
    recall_parser.set_defaults(func=cmd_recall)


def cmd_recall(args: argparse.Namespace) -> int:
    """Shell-accessible recall — prints one tab-separated hit per line.

    Output format per line: ``<score>\\t<filename>\\t<preview>``, where
    ``<preview>`` is the first 80 chars of the wiki page body (post
    frontmatter), newlines collapsed to spaces. Used by validation
    harnesses and operator debugging scripts that can't rely on an MCP
    session. Reads ``search_backend`` + extra intake roots from
    ``athenaeum.yaml`` the same way ``serve`` and ``rebuild-index`` do,
    so results match what the MCP ``recall`` tool would return.

    **``--with-pii`` (issues athenaeum#885/#886) re-derives the layer ordering
    HERE, deliberately.** This function is a SECOND implementation of the same
    read: it constructs its own backend and its own ``is_page_authorized`` /
    ``storage_policy_configured`` / ``is_recallable`` drops rather than calling
    ``mcp_server.recall_search``. So the flag is not plumbing over athenaeum#885's
    change — the guarantee that the excluded join happens strictly AFTER every
    filter has to hold in this function's own control flow, and is asserted by
    this command's own layer-ordering tests rather than by athenaeum#885's
    (which do not cover this code path at all). The join is placed after the
    audience drop, the athenaeum#308 temporal drop and the athenaeum#532
    ``recallable`` drop — immediately before the line is printed, which is the
    only position from which it cannot widen the output.

    **A handle-shaped query (issue athenaeum#907) prints one JSON document
    instead of tab-separated lines** — the same
    ``athenaeum.identity_resolution.resolve_handle_query`` call the MCP
    ``recall`` tool makes, so the two entry points never drift. Detection is a
    complete no-op for an ordinary query: the tab-separated output below is
    unaffected by this branch existing.
    """
    from athenaeum.config import (
        load_config,
        resolve_audience,
        resolve_extra_intake_roots,
    )
    from athenaeum.models import (
        is_inactive_memory,
        is_page_authorized,
        parse_frontmatter,
    )
    from athenaeum.search import get_backend
    from athenaeum.storage import is_recallable, storage_policy_configured

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"

    if not wiki_root.exists():
        print(f"Wiki directory not found: {wiki_root}", file=sys.stderr)
        return 1

    cfg = load_config(knowledge_root)
    backend_name = args.backend or cfg.get("search_backend", "fts5")
    cache_dir = resolve_cache_dir(args.cache_dir).resolve()
    extra_roots = resolve_extra_intake_roots(knowledge_root, cfg)

    # Issue athenaeum#312: resolve the read-scope pin (CLI > env > yaml). None = owner.
    caller_audience = resolve_audience(cfg, getattr(args, "audience", None))
    # Issue athenaeum#532: enforce the storage-adapter ``recallable`` policy at render
    # only when a non-default storage policy is configured (strict no-op else).
    enforce_recallable = storage_policy_configured(cfg)

    # Issue athenaeum#907: a handle-shaped query answers by exact reverse
    # lookup, printed as one JSON document, rather than being sent through the
    # backend below. `resolve_handle_query` is a complete no-op — no lookup at
    # all — for a non-handle-shaped query, so the fallthrough below is
    # byte-identical to before this branch existed. Both `recall` entry
    # points (this one and the MCP tool) call the SAME resolver.
    from athenaeum import identity_resolution

    handle_resolution = identity_resolution.resolve_handle_query(
        knowledge_root,
        wiki_root,
        args.query,
        caller_audience=caller_audience,
        config=cfg,
        with_pii=getattr(args, "with_pii", False),
        usage_classes=getattr(args, "usage_class", None) or None,
    )
    if handle_resolution is not None:
        print(json.dumps(handle_resolution.to_dict(), indent=2, sort_keys=True))
        return 0

    try:
        backend = get_backend(backend_name)
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Issue athenaeum#308: an as-of view queries the wiki as it stood on a past date.
    # Indexed backends (fts5/vector) filter at BUILD time, so we build a
    # THROWAWAY as-of index in a scratch cache dir and query that — the live
    # index at ``cache_dir`` is never touched. The keyword backend scans on
    # query and honors ``as_of`` directly; a scratch build is a cheap no-op for
    # it. ``as_of`` is passed to ``query`` too so keyword filters at query time.
    as_of = getattr(args, "as_of", None)
    query_cache = cache_dir
    if as_of is not None:
        query_cache = cache_dir / "_asof" / as_of.isoformat()
        query_cache.mkdir(parents=True, exist_ok=True)
        try:
            backend.build_index(
                wiki_root,
                query_cache,
                extra_roots=extra_roots,
                as_of=as_of,
                config=cfg,
            )
        except ImportError as exc:
            print(f"As-of index build failed: {exc}", file=sys.stderr)
            return 1

    type_filter = getattr(args, "type_filter", None) or None

    try:
        hits = backend.query(
            args.query,
            query_cache,
            n=args.top_k,
            wiki_root=wiki_root,
            caller_audience=caller_audience,
            as_of=as_of,
            type_filter=type_filter,
        )
    except NotImplementedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Issue athenaeum#964: same "never a silent no-results" rule as the MCP `recall`
    # tool — an unrecognized `--type` value prints the deployment's actual
    # entity classes rather than leaving an empty hit list unexplained.
    if type_filter:
        from athenaeum.entity_schema import resolve_entity_classes
        from athenaeum.search import normalize_type_filter

        normalized_types = normalize_type_filter(type_filter) or ()
        known_names = {
            c.name for c in resolve_entity_classes(wiki_root, caller_audience=caller_audience)
        }
        unrecognized = [t for t in normalized_types if t not in known_names]
        if unrecognized:
            classes_str = ", ".join(sorted(known_names)) if known_names else "(none)"
            print(
                f"Note: --type value(s) {', '.join(unrecognized)} are not a "
                f"recognized entity class on this deployment. Known classes: "
                f"{classes_str}.",
                file=sys.stderr,
            )

    from athenaeum.mcp_server import _resolve_hit_path

    # Issues athenaeum#885/#886: ONE index per surface class for the whole
    # command, shared across every hit — never one scan per hit. Each loads
    # lazily, so a class no hit resolves to costs nothing and a run without
    # --with-pii never touches this at all.
    excluded_indexes: dict[str, ExcludedRecordIndex] = {}
    for filename, _name, score in hits:
        page_path, _display = _resolve_hit_path(filename, wiki_root, extra_roots)
        preview = ""
        fm: dict[str, object] = {}
        readable = False
        if page_path is not None and page_path.is_file():
            try:
                text = page_path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                preview = " ".join(body.split())[:80]
                readable = True
            except (OSError, UnicodeDecodeError):
                pass
        # Layer C fail-closed re-check against fresh on-disk frontmatter.
        if caller_audience is not None and (
            not readable or not is_page_authorized(fm, caller_audience)
        ):
            continue
        # Issue athenaeum#308: temporal backstop — drop any hit outside its validity
        # window relative to ``as_of`` (default today), so the CLI output stays
        # consistent with the requested view regardless of backend build state.
        if readable and is_inactive_memory(fm, as_of):
            continue
        # Issue athenaeum#532 (H4): honor the storage-adapter ``recallable`` corpus
        # policy, matching the MCP ``recall`` tool. Only enforced when the
        # config defines a non-default storage policy — a strict no-op
        # otherwise. Fail-closed: an unreadable hit cannot have its class
        # verified, so it is withheld.
        if enforce_recallable and (
            not readable or not is_recallable(str(fm.get("type") or ""), cfg)
        ):
            continue
        # Issues athenaeum#885/#886: the excluded-field join, LAST — after the
        # audience drop, the as-of temporal drop and the `recallable` drop
        # above. This position is the guarantee: a hit any filter removed has
        # already `continue`d, so it never reaches an excluded-surface lookup
        # and the flag can never be used to probe for a record behind a page
        # this caller may not read. Skipped entirely (zero scans) when unset.
        excluded_suffix = ""
        if getattr(args, "with_pii", False):
            excluded_suffix = _excluded_suffix_for_hit(
                page_path,
                fm,
                knowledge_root=knowledge_root,
                config=cfg,
                indexes=excluded_indexes,
                usage_classes=getattr(args, "usage_class", None) or None,
            )
        print(f"{score:.2f}\t{filename}\t{preview}{excluded_suffix}")

    return 0


def _excluded_suffix_for_hit(
    page_path: Path | None,
    fm: dict[str, object],
    *,
    knowledge_root: Path,
    config: dict[str, object] | None,
    indexes: dict[str, "ExcludedRecordIndex"],
    usage_classes: list[str] | None,
) -> str:
    """One hit's excluded fields as tab-separated ``field=value`` pairs, or ``""``.

    The CLI rendering of the same join the MCP ``recall`` tool performs
    (issues athenaeum#885/#886), reusing athenaeum#883's public, ``EntityIndex``-free
    assembly seam — this caller already holds the hit's fresh frontmatter, so
    rebuilding an ``EntityIndex`` here would be a defect, not an optimization
    detail.

    Returns the empty string — never an error — whenever there is nothing to
    say: no readable page, no ``uid``, a page class whose mapped surface class
    is not actually excluded, or no matching record. A WITHHELD field renders
    as ``field=[redacted:N]`` rather than vanishing, so withheld and absent
    stay distinguishable on the shell surface too.
    """
    from athenaeum import pii
    from athenaeum.storage import is_excluded

    if page_path is None:
        return ""
    uid = str(fm.get("uid") or "").strip()
    if not uid:
        return ""

    surface_class = pii.surface_class_for_page_class(str(fm.get("type") or ""), config)
    # The gate, before any join: a class whose mapped surface is NOT excluded
    # resolves to the default wiki adapter, and joining there would scan the
    # wiki root as if it were the excluded store.
    if not surface_class or not is_excluded(surface_class, config):
        return ""

    index = indexes.get(surface_class)
    if index is None:
        index = pii.ExcludedRecordIndex(
            pii.excluded_surface_root(surface_class, knowledge_root, config)
        )
        indexes[surface_class] = index

    record_path = index.by_uid(uid)
    if record_path is None:
        return ""

    fields, redactions, _ = pii.assemble_excluded_read(
        page_path,
        fm,
        pii.read_bounce_record(record_path),
        surface_class=surface_class,
        config=config,
        include_excluded=True,
        usage_classes=usage_classes,
    )
    parts = [f"{name}={','.join(values)}" for name, values in fields.items()]
    parts += [f"{m.field}=[redacted:{m.value_count}]" for m in redactions]
    return "".join(f"\t{part}" for part in parts)


def cmd_people(args: argparse.Namespace) -> int:
    """List type:person wikis filtered by frontmatter — frontmatter-only, no LLM.

    Filters AND together. Companies match current_company OR
    linkedin_company_at_connect (case-insensitive substring). Tags must
    match exactly. Tier is shorthand for ``tag tier:<value>``. Default
    sort is by ``warm_score`` desc; ``--top-touch N`` switches to a
    recent-touch composite score and returns the top N.
    """
    import re

    from athenaeum.models import parse_frontmatter

    knowledge_root = args.path.expanduser().resolve()
    wiki_root = knowledge_root / "wiki"
    if not wiki_root.is_dir():
        print(f"Wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    needle_companies = [c.lower() for c in args.company if c]
    required_tags = list(args.tag)
    if args.tier:
        required_tags.append(f"tier:{args.tier}")

    title_regexes = [
        re.compile(p, re.IGNORECASE) for p in (args.title_regex or []) if p
    ]
    company_regexes = [
        re.compile(p, re.IGNORECASE) for p in (args.company_regex or []) if p
    ]

    rows: list[dict] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or meta.get("type") != "person":
            continue

        tags_raw = meta.get("tags") or []
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        if required_tags and not all(t in tags for t in required_tags):
            continue

        company_fields = [
            str(meta.get("current_company") or ""),
            str(meta.get("linkedin_company_at_connect") or ""),
        ]
        if needle_companies:
            blob = " ".join(company_fields).lower()
            if not all(needle in blob for needle in needle_companies):
                continue
        if company_regexes:
            company_blob = " ".join(company_fields)
            if not all(rx.search(company_blob) for rx in company_regexes):
                continue

        title_fields = [
            str(meta.get("current_title") or ""),
            str(meta.get("linkedin_position_at_connect") or ""),
        ]
        if title_regexes:
            title_blob = " ".join(title_fields)
            if not all(rx.search(title_blob) for rx in title_regexes):
                continue

        try:
            warm_score = float(_numeric_frontmatter_value(meta.get("warm_score") or 0))
        except (TypeError, ValueError):
            warm_score = 0.0
        try:
            meeting_count = int(
                _numeric_frontmatter_value(meta.get("meeting_count_24mo") or 0)
            )
        except (TypeError, ValueError):
            meeting_count = 0
        try:
            sent_count = int(
                _numeric_frontmatter_value(meta.get("sent_count_24mo") or 0)
            )
        except (TypeError, ValueError):
            sent_count = 0

        title = (
            meta.get("current_title") or meta.get("linkedin_position_at_connect") or ""
        )
        company = (
            meta.get("current_company") or meta.get("linkedin_company_at_connect") or ""
        )
        rows.append(
            {
                "name": str(meta.get("name") or ""),
                "current_title": str(title),
                "current_company": str(company),
                "warm_score": warm_score,
                "meeting_count_24mo": meeting_count,
                "sent_count_24mo": sent_count,
                "touch_score": meeting_count * 3 + sent_count,
                "last_touch": str(meta.get("last_touch") or ""),
                "uid": str(meta.get("uid") or ""),
                "path": path.name,
            }
        )

    if args.top_touch:
        rows.sort(key=lambda r: -r["touch_score"])
        rows = rows[: args.top_touch]
    else:
        rows.sort(key=lambda r: -r["warm_score"])
        if args.limit > 0:
            rows = rows[: args.limit]

    if args.format == "tsv":
        for r in rows:
            print(
                "\t".join(
                    str(r[k])
                    for k in (
                        "name",
                        "current_title",
                        "current_company",
                        "warm_score",
                        "meeting_count_24mo",
                        "sent_count_24mo",
                        "last_touch",
                        "uid",
                        "path",
                    )
                )
            )
        return 0

    if not rows:
        print("(no matches)")
        return 0

    name_w = max(len(r["name"]) for r in rows)
    title_w = max(len(r["current_title"][:40]) for r in rows) or 1
    company_w = max(len(r["current_company"][:30]) for r in rows) or 1
    print(
        f"{'name':{name_w}}  {'title':{title_w}}  "
        f"{'company':{company_w}}  score   touch  last_touch"
    )
    for r in rows:
        print(
            f"{r['name']:{name_w}}  "
            f"{r['current_title'][:40]:{title_w}}  "
            f"{r['current_company'][:30]:{company_w}}  "
            f"{r['warm_score']:>6.1f}  "
            f"{r['touch_score']:>5}  "
            f"{r['last_touch']}"
        )
    print(f"\n{len(rows)} match(es)")
    return 0


def cmd_person(args: argparse.Namespace) -> int:
    """One-call person read by uid — the person-shaped read surface (issue athenaeum#864).

    DEPRECATED (issue athenaeum#887) — use ``athenaeum query entity``, or
    ``athenaeum recall --with-pii``; both answer for any entity class. This
    command keeps working identically and is not removed here (removal is
    athenaeum#888). It prints a one-line migration notice to **stderr**, never
    to stdout: stdout is a JSON object a script parses, and a notice there
    would be a breaking output change dressed up as a deprecation.

    Prints a single JSON object to stdout: ``pii.EntityRead.to_dict()``.
    With ``--include-contact`` unset (default), withheld contact fields carry
    a redaction marker (field name + that a value exists) instead of the
    value; a person with no contact record at all prints the page with no
    redaction markers — not an error. Loads ``athenaeum.yaml`` the same way
    the sibling commands do, so the contact surface resolves per the
    operator's ``storage.mapping``.

    Every returned contact value carries its usage classification (issue
    athenaeum#866) under ``classifications``, co-indexed with ``contact``.
    ``--usage-class`` (repeatable) returns only values of the named classes —
    ``--usage-class observed`` is the outreach-eligible set.

    An unknown uid prints an error to stderr and returns exit code 1.

    Since issue athenaeum#886 this DELEGATES to :func:`cmd_entity` with the class
    fixed to ``person``. Its flags, stdout and exit codes are unchanged —
    byte-identical stdout is asserted by test, including a
    ``--usage-class``-filtered case.
    """
    from athenaeum.mcp_server import READ_PERSON_SURFACE_DEPRECATION

    print(
        f"[deprecated] {READ_PERSON_SURFACE_DEPRECATION} "
        "(shell: `athenaeum query entity --uid ... --class person "
        "[--include-excluded]`, or `athenaeum recall --with-pii`)",
        file=sys.stderr,
    )
    return _read_entity_to_stdout(
        args.path,
        args.uid,
        page_class="person",
        include_excluded=args.include_contact,
        usage_classes=args.usage_class or None,
        not_found_label="person",
    )


def cmd_entity(args: argparse.Namespace) -> int:
    """One-call entity read by uid, for ANY entity class (issue athenaeum#886).

    The generic form of :func:`cmd_person`, printing the SAME JSON object shape
    (``pii.EntityRead.to_dict()``) to stdout. With ``--include-excluded`` unset
    (the default), withheld fields carry a redaction marker instead of the
    value; an entity with no excluded record at all prints the page with no
    markers — not an error.

    ``--class`` names the page's ``type:`` and selects which excluded SURFACE
    is read (a ``person`` page's record lives on the ``pii`` surface); the page
    itself is resolved by uid whatever its type. It defaults to ``person`` so
    the command is useful without knowing the mapping.

    An unknown uid prints an error to stderr and returns exit code 1.
    """
    return _read_entity_to_stdout(
        args.path,
        args.uid,
        page_class=args.entity_class,
        include_excluded=args.include_excluded,
        usage_classes=args.usage_class or None,
        not_found_label="entity",
    )


def _read_entity_to_stdout(
    path: Path,
    uid: str,
    *,
    page_class: str,
    include_excluded: bool,
    usage_classes: list[str] | None,
    not_found_label: str,
) -> int:
    """Shared body of ``query person`` and ``query entity`` (issue athenaeum#886).

    Extracted so the two commands cannot drift in what they print: the person
    command is the generic one with the class fixed, and the only difference
    between them is the noun in the not-found message — presentation, never
    behaviour.
    """
    from athenaeum.config import load_config
    from athenaeum.pii import read_entity, surface_class_for_page_class

    knowledge_root = path.expanduser().resolve()
    config = load_config(knowledge_root)

    result = read_entity(
        knowledge_root,
        config,
        uid,
        surface_class=surface_class_for_page_class(page_class, config),
        include_excluded=include_excluded,
        usage_classes=usage_classes,
    )
    if result is None:
        print(f"Error: no {not_found_label} found for uid={uid!r}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_stopwords(_args: argparse.Namespace) -> int:
    """Print the canonical stopword list, one word per line, sorted."""
    from athenaeum.search import STOPWORDS

    for word in STOPWORDS:
        print(word)
    return 0


def cmd_query_topics(args: argparse.Namespace) -> int:
    """Print extracted topics, one per line. Empty output = fall back."""
    from athenaeum.config import load_config
    from athenaeum.query_topics import extract_topics

    # Issue athenaeum#232: load the operator's yaml so ``models.topic`` reaches the
    # call. --knowledge-root covers non-default roots; when omitted,
    # load_config falls back to ~/knowledge.
    knowledge_root = (
        args.knowledge_root.expanduser().resolve()
        if args.knowledge_root is not None
        else None
    )
    config = load_config(knowledge_root)
    for topic in extract_topics(args.prompt, timeout=args.timeout, config=config):
        print(topic)
    return 0


def cmd_test_mcp(args: argparse.Namespace) -> int:
    """Smoke-test the MCP remember/recall round-trip without a live session.

    MCP tools are only callable from within a running Claude Code session
    (the tool list is established at session start). This command exercises
    the underlying functions directly against a synthetic knowledge dir so
    users can verify their athenaeum install works before relying on it.

    Steps:
      1. remember_write  — appends a test observation to raw/
      2. recall_search   — keyword search against a seeded wiki page
      3. create_server   — verifies FastMCP is importable and the server
                           factory returns a configured instance
    """
    import shutil
    import tempfile

    from athenaeum.mcp_server import recall_search, remember_write

    tmp_root = Path(tempfile.mkdtemp(prefix="athenaeum-test-mcp-"))
    raw_root = tmp_root / "raw"
    wiki_root = tmp_root / "wiki"
    raw_root.mkdir()
    wiki_root.mkdir()

    (wiki_root / "test-page.md").write_text(
        "---\n"
        "name: Athenaeum Test Page\n"
        "tags: [smoke-test]\n"
        "description: Seeded page used by `athenaeum test-mcp` to exercise recall.\n"
        "---\n\n"
        "This page contains the keyword ATHENAEUMSMOKETEST for recall verification.\n"
    )

    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    def _record(name: str, ok: bool, detail: str = "") -> None:
        if ok:
            passed.append(name)
            print(f"  PASS  {name}")
        else:
            failed.append((name, detail))
            print(f"  FAIL  {name}: {detail}", file=sys.stderr)

    print(f"Testing athenaeum MCP setup (temp dir: {tmp_root})")

    try:
        result = remember_write(
            raw_root,
            "Smoke test observation from `athenaeum test-mcp`.",
            source="test-mcp",
            # Declare per-claim provenance so the smoke test itself doesn't
            # trip the issue-athenaeum#90 "no `sources` supplied" warning.
            sources="cli:athenaeum-test-mcp",
        )
        written = list((raw_root / "test-mcp").glob("*.md"))
        ok = result.startswith("Saved to ") and len(written) == 1
        _record("remember_write", ok, f"unexpected result: {result!r}")

        result = recall_search(wiki_root, "ATHENAEUMSMOKETEST", top_k=3)
        ok = "Athenaeum Test Page" in result
        _record("recall_search (keyword)", ok, f"no match in: {result[:200]!r}")

        try:
            from athenaeum.mcp_server import create_server

            server = create_server(raw_root=raw_root, wiki_root=wiki_root)
            ok = server is not None and hasattr(server, "run")
            _record("create_server (FastMCP)", ok, "factory returned unusable object")
        except ImportError as exc:
            _record(
                "create_server (FastMCP)",
                False,
                f"FastMCP not installed: {exc}. Install with: pip install athenaeum[mcp]",
            )
    finally:
        if args.keep:
            print(f"\nTemp dir preserved at: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1
