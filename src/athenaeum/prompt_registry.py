# SPDX-License-Identifier: Apache-2.0
"""Prompt registry — a single index over athenaeum's inline LLM prompt constants.

The prompts themselves still live next to their parsers in their home modules
(``tiers.py``, ``resolutions.py``, …); this module only *imports* them. It is an
index, not a home — the load-bearing contract each prompt shares with its
adjacent parser (e.g. ``MERGE_SYSTEM``'s anchored-ops JSON shape parsed by
``parse_merge_ops_response``) is left exactly where it is. See issue athenaeum#561.

Two gaps this closes, without moving or changing any prompt text:

1. **Review discipline.** A prompt edit buried in a triple-quoted literal in a
   100 KB module does not read as a prompt change in review. The parametrized
   golden test (``tests/test_prompt_goldens.py``) pins every prompt's bytes to a
   snapshot under ``tests/data/prompts/``; an intentional edit shows up as a
   reviewable golden diff, and an accidental one fails the test loudly.
2. **Discoverability.** Finding any prompt is one grep in this file; editing it
   is one hop to the named constant in its home module.

``docs/prompts.md`` is generated from this registry and verified byte-current by
the same test, so it cannot rot. Regenerate goldens + docs after any intentional
prompt-text change::

    python -m athenaeum.prompt_registry --write

**Contract:** :data:`PROMPTS` maps a stable registry name to the LIVE prompt
constant (re-imported from its home module, never copied) so a golden test
can pin exact bytes and ``docs/prompts.md`` can render an always-current
inventory. This module never edits or generates prompt text.

**Factoring rule:** this module owns the INDEX (name -> module/constant/
knob/max_tokens) plus the golden-snapshot and docs rendering that read from
it. The prompt TEXT itself stays owned by its home module, next to the parser
that consumes it — moving prompt text into this module would break the
load-bearing adjacency between a prompt and its parser (e.g.
``MERGE_SYSTEM``'s anchored-ops JSON shape and ``parse_merge_ops_response``).

**Layering:** L3 service, but an unusual one — it ``importlib.import_module``s
EVERY L4 module that owns a registered prompt (:mod:`athenaeum.tiers`,
:mod:`athenaeum.contradictions`, :mod:`athenaeum.resolutions`,
:mod:`athenaeum.claim_kind`, :mod:`athenaeum.query_topics`,
:mod:`athenaeum.reasoning_tiers`) at IMPORT TIME (module-scope
:data:`PROMPTS` dict comprehension) to resolve the live constants. This is the
one deliberate exception to "L3 does not import L4" in this file's
assignment: the registry's whole point is indexing prompts that live in L4
modules, so nothing here is imported BACK by those modules — the dependency
runs one way only (registry -> prompt owner), never creating a cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptMeta:
    """Where a registered prompt lives and how it is invoked."""

    module: str  # dotted module path, e.g. "athenaeum.tiers"
    constant: str  # constant name in that module, e.g. "CLASSIFY_SYSTEM"
    knob: str  # model knob string passed to config.resolve_model(...)
    max_tokens: int  # output-token budget at the call site


# Single source of truth: (registry name, home-module constant, model knob,
# max_tokens). The module is derived from the name's first segment, so the name,
# the constant, and the call-site knobs/budgets are recorded exactly once. Knob
# and max_tokens were read from each prompt's live call site (see docs/prompts.md).
_META_ROWS: list[tuple[str, str, str, int]] = [
    ("tiers.classify_system", "CLASSIFY_SYSTEM", "classify", 4096),
    ("tiers.classify_user_template", "CLASSIFY_USER_TEMPLATE", "classify", 4096),
    # write-knob budgets re-baselined by issue athenaeum#578 (Sonnet-5-bound + adaptive
    # thinking headroom): create/merge_patch 2048 -> 6144, merge_full 8192 -> 12288.
    ("tiers.create_system", "CREATE_SYSTEM", "write", 6144),
    ("tiers.create_template", "CREATE_TEMPLATE", "write", 6144),
    ("tiers.merge_system", "MERGE_SYSTEM", "write", 6144),
    ("tiers.merge_system_full", "MERGE_SYSTEM_FULL", "write", 12288),
    ("tiers.merge_template", "MERGE_TEMPLATE", "write", 6144),
    ("tiers.merge_template_full", "MERGE_TEMPLATE_FULL", "write", 12288),
    ("contradictions.detect_system", "_DETECT_SYSTEM", "classify", 1024),
    # resolve-knob budgets re-baselined by issue athenaeum#578 (Opus-5-bound adaptive
    # thinking headroom): resolve 1024 -> 8192, freetext_edit 4096 -> 8192.
    ("resolutions.resolve_system", "_RESOLVE_SYSTEM", "resolve", 8192),
    ("resolutions.freetext_edit_system", "_FREETEXT_EDIT_SYSTEM", "resolve", 8192),
    ("claim_kind.claim_kind_system", "CLAIM_KIND_SYSTEM", "classify", 64),
    ("query_topics.system_prompt", "_SYSTEM_PROMPT", "topic", 256),
    ("query_topics.user_template", "_USER_TEMPLATE", "topic", 256),
    ("reasoning_tiers.t1_system_prompt", "T1_SYSTEM_PROMPT", "reasoning_t1", 256),
    ("reasoning_tiers.t2_system_prompt", "T2_SYSTEM_PROMPT", "reasoning_t2", 4096),
]

PROMPT_META: dict[str, PromptMeta] = {
    name: PromptMeta("athenaeum." + name.split(".", 1)[0], constant, knob, max_tokens)
    for name, constant, knob, max_tokens in _META_ROWS
}

#: The distinct model knobs, derived from ``_META_ROWS`` (issue athenaeum#781) rather
#: than duplicated here — this IS the single source of truth the spend-ledger
#: per-knob attribution (:mod:`athenaeum.spend`, ``athenaeum spend --by-knob``)
#: and its tests key off of, so the knob set cannot drift from the prompts
#: that actually define it. Sorted for stable iteration/display order.
KNOBS: tuple[str, ...] = tuple(sorted({knob for _, _, knob, _ in _META_ROWS}))


def _resolve(meta: PromptMeta) -> str:
    """Import the home module and return the live constant — the text is not copied."""
    module = importlib.import_module(meta.module)
    value = getattr(module, meta.constant)
    if not isinstance(value, str):
        raise TypeError(f"{meta.module}.{meta.constant} is not a str prompt")
    return value


# Stable name -> the live prompt constant, imported from its home module. This is
# the registry: 16 prompts, each text still owned (and parsed) by its module.
PROMPTS: dict[str, str] = {name: _resolve(meta) for name, meta in PROMPT_META.items()}


def prompt_manifest() -> dict[str, str]:
    """Map each registered prompt name to the sha256 hexdigest of its bytes."""
    return {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in PROMPTS.items()
    }


def prompt_manifest_hash(length: int = 8) -> str:
    """A single short aggregate digest over the whole prompt manifest.

    Hashes the canonical ``name=sha256`` lines (name-sorted, so it is stable
    across dict ordering) into one short hex token. Its purpose (issue athenaeum#567) is
    to let a librarian run record *which prompt bytes it used* in ONE
    run-summary key instead of sixteen; it changes iff any registered prompt's
    bytes change.
    """
    manifest = prompt_manifest()
    canonical = "\n".join(f"{name}={manifest[name]}" for name in sorted(manifest))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:length]


# --------------------------------------------------------------------------- #
# Golden-snapshot rendering (shared by the test and the --write regenerator).
# --------------------------------------------------------------------------- #

_SENTINEL = "# ===8<=== prompt bytes below this line are asserted verbatim ===8<==="


def render_golden(name: str) -> str:
    """Full golden-file content for *name*: footgun header + sentinel + exact bytes.

    The header (Amendment A3) states the file is an ASSERTION, not the source, and
    names the module + constant to edit instead — so the review discipline the
    goldens create does not become a trap for the manual-editing workflow.
    """
    meta = PROMPT_META[name]
    header = (
        "# GOLDEN SNAPSHOT — ASSERTION, NOT SOURCE.\n"
        "# Editing this file changes what the test EXPECTS, not what the model "
        "receives.\n"
        f"# To change this prompt, edit `{meta.module}.{meta.constant}`, then run: "
        "python -m athenaeum.prompt_registry --write\n"
        f"{_SENTINEL}\n"
    )
    return header + PROMPTS[name]


def parse_golden(content: str) -> str:
    """Recover the asserted prompt bytes from a golden file (everything after the sentinel)."""
    marker = _SENTINEL + "\n"
    idx = content.find(marker)
    if idx == -1:
        raise ValueError(
            "golden file missing the sentinel line; regenerate with "
            "`python -m athenaeum.prompt_registry --write`"
        )
    return content[idx + len(marker):]


# --------------------------------------------------------------------------- #
# Source-location + docs rendering.
# --------------------------------------------------------------------------- #


def _source_file(meta: PromptMeta) -> Path:
    module = importlib.import_module(meta.module)
    if not module.__file__:  # pragma: no cover - namespace package guard
        raise LookupError(f"{meta.module} has no __file__")
    return Path(module.__file__)


def _source_line(meta: PromptMeta) -> int:
    """1-based line of the constant's module-level assignment (computed, so it can't rot)."""
    text = _source_file(meta).read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(meta.constant)}\s*[:=]", re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        raise LookupError(f"could not locate {meta.constant} in {meta.module}")
    return text.count("\n", 0, match.start()) + 1


def _display_path(meta: PromptMeta) -> str:
    """Canonical repo-relative path, derived from the module name (install-location independent)."""
    return "src/" + meta.module.replace(".", "/") + ".py"


def _fence_for(text: str) -> str:
    """A backtick fence at least one longer than any backtick run inside *text*."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_docs() -> str:
    """Render the full contents of the generated ``docs/prompts.md``."""
    manifest = prompt_manifest()
    lines: list[str] = [
        "<!-- GENERATED FILE — do not edit by hand.",
        "     Regenerate with: python -m athenaeum.prompt_registry --write",
        "     Source of truth: src/athenaeum/prompt_registry.py plus each prompt's",
        "     home-module constant. This file is verified byte-current by",
        "     tests/test_prompt_goldens.py, so a stale copy fails CI. -->",
        "",
        "# LLM prompt inventory",
        "",
        f"Athenaeum sends {len(PROMPTS)} distinct prompt constants to the model. Each "
        "stays an inline",
        "constant in its home module (next to the parser it feeds); "
        "`athenaeum.prompt_registry`",
        "indexes them and this file is generated from that index.",
        "",
    ]
    for name in PROMPTS:
        meta = PROMPT_META[name]
        loc = f"{_display_path(meta)}:{_source_line(meta)}"
        fence = _fence_for(PROMPTS[name])
        lines += [
            f"## `{name}`",
            "",
            f"- **Constant:** `{meta.module}.{meta.constant}`",
            f"- **Source:** `{loc}`",
            f"- **Model knob:** `{meta.knob}` &middot; **max_tokens:** `{meta.max_tokens}`",
            f"- **sha256:** `{manifest[name]}`",
            "",
            f"{fence}text",
            PROMPTS[name].rstrip("\n"),
            fence,
            "",
        ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# --write regenerator.
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    for base in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (base, *base.parents):
            if (parent / "pyproject.toml").is_file() and (parent / "src" / "athenaeum").is_dir():
                return parent
    raise RuntimeError("could not locate repo root (pyproject.toml + src/athenaeum)")


def write_goldens_and_docs(root: Path | None = None) -> None:
    """Regenerate every golden under tests/data/prompts/ and docs/prompts.md."""
    root = root or _repo_root()
    golden_dir = root / "tests" / "data" / "prompts"
    golden_dir.mkdir(parents=True, exist_ok=True)
    valid = {f"{name}.txt" for name in PROMPTS}
    for existing in golden_dir.glob("*.txt"):
        if existing.name not in valid:  # drop orphan goldens for renamed/removed prompts
            existing.unlink()
    for name in PROMPTS:
        (golden_dir / f"{name}.txt").write_text(render_golden(name), encoding="utf-8")
    (root / "docs" / "prompts.md").write_text(render_docs(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate prompt goldens + docs/prompts.md, or print the manifest."
    )
    parser.add_argument(
        "--write", action="store_true", help="write goldens + docs/prompts.md to disk"
    )
    args = parser.parse_args(argv)
    if args.write:
        write_goldens_and_docs()
        print(f"wrote {len(PROMPTS)} goldens + docs/prompts.md")
    else:
        for name, digest in prompt_manifest().items():
            print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
