# SPDX-License-Identifier: Apache-2.0
"""One-line ``description:`` frontmatter for wiki pages (issue athenaeum#1324).

The per-turn recall hook injects up to three wiki page NAMES into every
prompt. A bare name does not tell the model whether the page is relevant, so
it has to ``recall`` each candidate to find out — the exact round-trip the
injection exists to avoid. The FTS5 index (:mod:`athenaeum.search`) already
carries a ``description`` column read from page frontmatter; what was
missing is the pages carrying one. Measured 2026-09-02 on the live corpus:
43 of 24,614 indexed pages had a non-empty ``description``.

This module owns the field end to end:

* :func:`split_description_line` — the Tier-3 create prompt now asks the
  writer for a leading ``Description: <one sentence>`` line ahead of the page
  body, in the SAME call that writes the body (no extra request per page).
  This parses it off; :func:`athenaeum.tiers.tier3_entity_from_text` is the
  caller.
* :func:`derive_description_from_body` — a deterministic, zero-LLM fallback:
  the page's opening paragraph, sentence-truncated. Used when the writer
  omits the line, and as the ``--mechanical`` backfill mode.
* :func:`build_description_report` / :func:`apply_description_backfill` —
  the resumable backfill over an existing tree, mirroring
  :mod:`athenaeum.memory_class_backfill` (athenaeum#996): dry-run by default,
  never overwrites a non-empty value, never fabricates frontmatter, textual
  single-line insertion so a second run is a byte-level no-op, batched LLM
  calls routed through the ``classify`` knob (Haiku-class by default) with
  ceiling enforcement and one spend-ledger row per batch.

**Shape invariant.** A description is ONE line, at most
:data:`DESCRIPTION_MAX_CHARS` characters, with no newlines, double quotes,
or backslashes — :func:`normalize_description` enforces it everywhere a value
enters. Two consumers depend on it: the hook's shell scanner reads the
frontmatter line-by-line, and the backfill writes the value as a YAML
double-quoted scalar without a YAML emitter.

Layering: L2 (domain logic over page text and the wiki tree). Imports
:mod:`athenaeum.models` for :func:`parse_frontmatter`; takes function-level
imports of the provider seam and the spend ledger, the convention
:mod:`athenaeum.memory_class_backfill` set. Imported by :mod:`athenaeum.tiers`
(L4) and ``_cmd_description`` (L5). Holds no argparse and prints nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter

log = logging.getLogger(__name__)

#: Hard cap on a description's length. The hook injects up to three of these
#: per prompt, so the field is a summary line, not an abstract.
DESCRIPTION_MAX_CHARS = 200

#: Pages per backfill LLM call. Batching amortizes the system prompt; the
#: per-page excerpt (below) keeps a batch of 20 well inside a small model's
#: comfortable input.
DEFAULT_BATCH_SIZE = 20

#: Body characters handed to the describer per page. The opening of a page
#: is what a one-line summary is made from; whole pages would multiply cost
#: for no better sentence.
_BODY_EXCERPT_CHARS = 1200

_DESCRIBE_MAX_TOKENS = 4096

#: Same shape as ``models._FM_RE``, re-declared for the same reason
#: :mod:`athenaeum.memory_class_backfill` re-declares it: this module needs
#: the match SPANS to insert one line inside the block without re-rendering
#: it, and must tell "no delimiter" apart from "delimiter, unparseable YAML".
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)

#: The leading line the Tier-3 create prompt asks for. Tolerates markdown
#: bold around the label and either case, since writers vary.
_DESCRIPTION_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?description(?:\*\*)?\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE
)

_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^[^\]]+\]:")
_FOOTNOTE_REF_RE = re.compile(r"\[\^[^\]]+\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_EMPHASIS_RE = re.compile(r"[*_`]+")
#: Contact data must never land in an indexed summary field, whatever the
#: source page carries. Cheap scrub for the mechanical path; the LLM path is
#: instructed the same way and scrubbed too.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{7,}\d(?![\w-])")

_DESCRIBE_SYSTEM = """\
You write one-line summaries of knowledge-base pages for a search index.

For each page, write ONE sentence (at most 200 characters) saying what the
entity is and why it matters — the sentence a reader needs to decide whether
the page is relevant to their question without opening it.

Rules:
- Plain text only: no markdown, no quotes, no line breaks.
- Prefer the concrete over the generic ("Dutch fintech running a 2026 pilot
with Kromatic" beats "A company").
- Never include email addresses, phone numbers, or street addresses.
- Use only what the page says. Do not guess.

Return ONLY a JSON array, one object per input page, each
{"i": <the page's integer index>, "description": "<the sentence>"}.
Omit a page entirely if its content is too thin to summarize.\
"""

_DESCRIBE_USER_TEMPLATE = """\
Summarize each page below. Return the JSON array described in the system \
prompt and nothing else.

{pages}\
"""


# --------------------------------------------------------------------------- #
# Value shape
# --------------------------------------------------------------------------- #


def normalize_description(value: object) -> str | None:
    """Coerce *value* to the one-line shape, or ``None`` if nothing usable.

    Collapses whitespace (including newlines) to single spaces, drops
    double quotes and backslashes (so the value can be written as a YAML
    double-quoted scalar without an emitter, and read back by a line-based
    scanner), scrubs contact data, and truncates on a word boundary to
    :data:`DESCRIPTION_MAX_CHARS`.
    """
    if not isinstance(value, str):
        return None
    text = value.replace('"', "'").replace("\\", "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")
    text = " ".join(text.split())
    text = _EMAIL_RE.sub("", text)
    text = _PHONE_RE.sub("", text)
    text = " ".join(text.split()).strip(" -:;,'")
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX_CHARS:
        cut = text[: DESCRIPTION_MAX_CHARS - 1]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        text = cut.rstrip(" ,;:-") + "…"
    return text


def split_description_line(text: str) -> tuple[str | None, str]:
    """Split a leading ``Description: …`` line off a Tier-3 create response.

    Returns ``(description, remainder)``. Only the FIRST non-blank line is
    considered, so a ``Description:`` that appears inside the body is left
    alone. The remainder has the line (and any blank lines after it)
    removed; when no such line leads the text, the description is ``None``
    and the remainder is *text* unchanged.
    """
    lines = text.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return None, text
    match = _DESCRIPTION_LINE_RE.match(lines[index])
    if match is None:
        return None, text
    description = normalize_description(match.group("value"))
    rest = index + 1
    while rest < len(lines) and not lines[rest].strip():
        rest += 1
    return description, "\n".join(lines[rest:])


def derive_description_from_body(body: str) -> str | None:
    """Deterministic fallback: the opening paragraph, sentence-truncated.

    Skips headings, footnote definitions, list markers and blank lines to
    find the first prose paragraph; strips markdown decoration and footnote
    references; keeps whole sentences while they fit the cap. Returns
    ``None`` when the body has no prose paragraph at all (a page that is
    only a heading and a table, say) — the caller must not synthesize one.
    """
    paragraph: list[str] = []
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            if paragraph:
                break
            continue
        if _HEADING_RE.match(line) or _FOOTNOTE_DEF_RE.match(line):
            if paragraph:
                break
            continue
        if line.startswith(("|", "```", "<", "---")):
            if paragraph:
                break
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"^\[\s*[xX ]?\]\s*", "", line)
        paragraph.append(line)
    if not paragraph:
        return None
    text = " ".join(paragraph)
    text = _FOOTNOTE_REF_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub("", text)
    text = " ".join(text.split())
    if not text:
        return None
    # Keep whole sentences while they fit; normalize_description handles the
    # word-boundary cut if even the first sentence is too long.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = ""
    for sentence in sentences:
        candidate = f"{kept} {sentence}".strip()
        if kept and len(candidate) > DESCRIPTION_MAX_CHARS:
            break
        kept = candidate
    return normalize_description(kept)


# --------------------------------------------------------------------------- #
# Backfill over an existing tree
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageOutcome:
    """What the backfill decided for one file, and why.

    ``description`` is the value that WOULD be (or was) written; ``None``
    for every skip. ``reason`` is a closed vocabulary the report groups by:
    ``mechanical`` / ``llm`` (assignments) and ``already-described`` /
    ``no-frontmatter`` / ``empty-frontmatter`` / ``unparseable-frontmatter`` /
    ``retired`` / ``undecided`` (skips).
    """

    path: Path
    description: str | None
    reason: str

    @property
    def assigned(self) -> bool:
        return self.description is not None


@dataclass
class DescriptionReport:
    """Counts + per-page outcomes for one backfill pass.

    Holds every outcome, not just totals: a pass over ~25k pages is only
    reviewable if an operator can see WHICH pages a count refers to, and
    ``--dry-run`` is the review surface.
    """

    scanned: int = 0
    outcomes: list[PageOutcome] = field(default_factory=list)
    llm_calls: int = 0
    llm_available: bool = True

    def record(self, outcome: PageOutcome) -> None:
        self.outcomes.append(outcome)

    @property
    def assignments(self) -> list[PageOutcome]:
        return [o for o in self.outcomes if o.assigned]

    def counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "assigned": len(self.assignments),
            "counts_by_reason": self.counts_by_reason(),
            "llm_calls": self.llm_calls,
            "llm_available": self.llm_available,
        }


def discover_wiki_pages(wiki_root: Path) -> list[Path]:
    """Every ``.md`` page under *wiki_root*, sorted, infra ledgers excluded."""
    return sorted(p for p in wiki_root.rglob("*.md") if p.is_file() and not p.name.startswith("_"))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        log.warning("description backfill: unreadable page %s: %s", path, exc)
        return None


def _triage_page(
    path: Path, text: str, *, include_retired: bool
) -> tuple[PageOutcome | None, dict[str, Any] | None, str]:
    """Decide whether *path* needs a description at all.

    Returns ``(outcome, meta, body)``. A non-``None`` outcome is a skip and
    the final word for this page; ``None`` means "candidate" and the caller
    decides the value.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return PageOutcome(path, None, "no-frontmatter"), None, ""

    meta, body = parse_frontmatter(text)
    if not meta:
        reason = "empty-frontmatter" if not match.group(1).strip() else "unparseable-frontmatter"
        return PageOutcome(path, None, reason), None, ""

    existing = meta.get("description")
    if isinstance(existing, str) and existing.strip():
        return PageOutcome(path, None, "already-described"), None, ""

    if not include_retired and bool(meta.get("retired")):
        return PageOutcome(path, None, "retired"), None, ""

    return None, meta, body.strip()


def _render_batch(items: list[tuple[int, Path, dict[str, Any], str]]) -> str:
    blocks = []
    for index, path, meta, body in items:
        name = meta.get("name") or path.stem
        page_type = meta.get("type") or "(none)"
        excerpt = body[:_BODY_EXCERPT_CHARS] or "(empty)"
        blocks.append(f"[{index}] name: {name}\n    type: {page_type}\n    body: {excerpt}")
    return "\n\n".join(blocks)


def describe_pages(
    candidates: list[tuple[Path, dict[str, Any], str]],
    *,
    client: Any,
    config: dict[str, Any] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    wiki_root: Path | None = None,
) -> tuple[dict[Path, str], int]:
    """Describe *candidates* in batched LLM calls. Returns ``(decisions, calls)``.

    Pages the model omits or answers unusably are absent from ``decisions``;
    the caller reports them ``undecided``. A batch left unprocessed because
    the spend ceiling tripped lands in the same bucket. Routed through the
    ``classify`` knob — a Haiku-class summarization sibling of the Tier-2
    classifier — so an operator can pin this pass to a backend without a new
    config key (the precedent :mod:`athenaeum.memory_class_backfill` set).
    One spend-ledger row per batch; the ceiling is checked BEFORE each batch
    against this call's cumulative usage.
    """
    from athenaeum import push_metrics, spend
    from athenaeum.config import DEFAULT_CLASSIFY_MODEL, resolve_model
    from athenaeum.json_utils import extract_json_array
    from athenaeum.models import TokenUsage
    from athenaeum.provider import (
        resolve_max_tokens,
        resolve_provider,
        resolve_thinking,
        response_text,
    )

    decisions: dict[Path, str] = {}
    calls = 0
    if not candidates:
        return decisions, calls

    model = resolve_model("classify", "ATHENAEUM_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL, config)
    max_tokens = resolve_max_tokens(
        "classify", "ATHENAEUM_CLASSIFY_MAX_TOKENS", _DESCRIBE_MAX_TOKENS, config
    )
    thinking = resolve_thinking("classify", "ATHENAEUM_CLASSIFY_THINKING", "disabled", config)
    provider = resolve_provider(config, knob="classify")
    session_id = push_metrics.resolve_session_id() or None
    run_usage = TokenUsage()

    step = max(1, batch_size)
    for start in range(0, len(candidates), step):
        chunk = candidates[start : start + step]
        items = [(offset, path, meta, body) for offset, (path, meta, body) in enumerate(chunk)]

        _ceiling = spend.ceiling_tripped(run_usage, provider=provider, config=config)
        if _ceiling is not None:
            log.warning(
                "description backfill: spend ceiling reached (%s) — stopping early "
                "at offset %d; %d page(s) left undecided",
                _ceiling,
                start,
                len(candidates) - start,
            )
            break

        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                thinking=thinking,
                system=_DESCRIBE_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": _DESCRIBE_USER_TEMPLATE.format(pages=_render_batch(items)),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 — one batch must not kill the pass
            log.warning(
                "description backfill: batch at offset %d failed (%s): %s",
                start,
                exc.__class__.__name__,
                exc,
            )
            continue
        calls += 1

        _usage = getattr(response, "usage", None)
        if _usage is not None:
            _counts = (
                int(getattr(_usage, "input_tokens", 0) or 0),
                int(getattr(_usage, "output_tokens", 0) or 0),
                int(getattr(_usage, "cache_creation_input_tokens", 0) or 0),
                int(getattr(_usage, "cache_read_input_tokens", 0) or 0),
            )
            run_usage.add(*_counts, model=model, knob="classify")
            batch_usage = TokenUsage()
            batch_usage.add(*_counts, model=model, knob="classify")
            spend.record_spend(
                batch_usage,
                run_type=spend.RUN_TYPE_DESCRIPTION_BACKFILL,
                provider=provider,
                session_id=session_id,
                config=config,
                wiki_root=wiki_root,
            )

        parsed = extract_json_array(response_text(response)) or []
        for record in parsed:
            if not isinstance(record, dict):
                continue
            raw_index = record.get("i")
            if not isinstance(raw_index, int) or not 0 <= raw_index < len(chunk):
                continue
            value = normalize_description(record.get("description"))
            if value is None:
                continue
            decisions[chunk[raw_index][0]] = value

    return decisions, calls


def build_description_report(
    wiki_root: Path,
    *,
    mechanical: bool = False,
    client: Any = None,
    config: dict[str, Any] | None = None,
    include_retired: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
) -> DescriptionReport:
    """Scan *wiki_root* and decide a description for every eligible page.

    *mechanical* derives every value with :func:`derive_description_from_body`
    and makes zero LLM calls. Otherwise candidates go to :func:`describe_pages`
    through *client*. *limit* caps the number of candidates decided in this
    pass (pages beyond it are reported ``undecided``), which together with
    the already-described skip makes repeated runs a resumable drain.
    Nothing is written here; :func:`apply_description_backfill` is the only
    writer, so ``--dry-run`` is this function called alone.
    """
    report = DescriptionReport()
    candidates: list[tuple[Path, dict[str, Any], str]] = []
    overflow: list[Path] = []

    for path in discover_wiki_pages(wiki_root):
        text = _read(path)
        if text is None:
            continue
        report.scanned += 1
        outcome, meta, body = _triage_page(path, text, include_retired=include_retired)
        if outcome is not None:
            report.record(outcome)
            continue
        assert meta is not None
        if limit is not None and len(candidates) >= limit:
            overflow.append(path)
            continue
        candidates.append((path, meta, body))

    for path in overflow:
        report.record(PageOutcome(path, None, "undecided"))

    if not candidates:
        return report

    if mechanical:
        for path, _meta, body in candidates:
            value = derive_description_from_body(body)
            report.record(PageOutcome(path, value, "mechanical" if value else "undecided"))
        return report

    if client is None:
        report.llm_available = False
        for path, _meta, _body in candidates:
            report.record(PageOutcome(path, None, "undecided"))
        return report

    decisions, calls = describe_pages(
        candidates, client=client, config=config, batch_size=batch_size, wiki_root=wiki_root
    )
    report.llm_calls = calls
    for path, _meta, _body in candidates:
        value = decisions.get(path)
        report.record(PageOutcome(path, value, "llm" if value else "undecided"))
    return report


def insert_description(text: str, description: str) -> str | None:
    """Return *text* with a ``description:`` line appended to its frontmatter.

    ``None`` when *text* has no frontmatter block — the caller skips, never
    synthesizes one. Textual insertion touching no other byte, so a second
    run is a no-op at the byte level. The value is normalized first and
    written as a YAML double-quoted scalar; normalization guarantees it
    contains no double quote, backslash, or newline, so no emitter is needed
    and the index's line scanner reads it back verbatim.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    value = normalize_description(description)
    if value is None:
        return None
    end = match.end(1)
    newline = "\r\n" if "\r\n" in text[: match.end()] else "\n"
    return f'{text[:end]}{newline}description: "{value}"{text[end:]}'


def apply_description_backfill(report: DescriptionReport) -> int:
    """Write every assignment in *report*. Returns the number of files changed.

    Re-checks each file's frontmatter at write time rather than trusting the
    scan: a page that gained a description between scan and write must not
    be overwritten.
    """
    from athenaeum.atomic_io import atomic_write_text

    changed = 0
    for outcome in report.assignments:
        text = _read(outcome.path)
        if text is None:
            continue
        meta, _body = parse_frontmatter(text)
        existing = meta.get("description")
        if isinstance(existing, str) and existing.strip():
            continue
        updated = insert_description(text, outcome.description or "")
        if updated is None or updated == text:
            continue
        atomic_write_text(outcome.path, updated)
        changed += 1
    return changed


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DESCRIPTION_MAX_CHARS",
    "DescriptionReport",
    "PageOutcome",
    "apply_description_backfill",
    "build_description_report",
    "derive_description_from_body",
    "describe_pages",
    "discover_wiki_pages",
    "insert_description",
    "normalize_description",
    "split_description_line",
]
