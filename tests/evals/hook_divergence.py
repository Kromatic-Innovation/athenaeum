# SPDX-License-Identifier: Apache-2.0
"""Zero-spend divergence classifier for the two ``UserPromptSubmit`` hooks.

Runs the packaged adapter (:mod:`athenaeum.claude_code_adapter`, resolved as
the installed ``athenaeum-claude-hook`` console script) and the retired shell
hook (``examples/claude-code/user-prompt-recall.sh``) over every
``build_corpus("core")`` probe against ONE shared materialized corpus and ONE
shared hook index, and buckets each probe by *what* differs between the two
``additionalContext`` strings.

Written for issue athenaeum#1912, whose spec needed the 29/47 "bullet list
differs" and 18/47 "notice text differs" counts carried from the 2026-09-29
measurement lane re-derived on a shared index rather than inherited. It makes
**no model call of any kind** — both hooks are pinned to their regex term
extractor, and the only subprocesses are the hooks themselves.

Three properties the earlier ad-hoc diagnostics did not have, and which the
counts are worthless without:

* **One shared hook index.** The 2026-09-29 numbers compared an adapter run
  against the paid harness's own index with a shell run against a separately
  built one, so two chromadb builds' worth of nondeterminism rode inside every
  count. ``--mode adapter-vs-adapter`` measures exactly that noise floor by
  running the SAME hook against two separately built indexes over the same
  corpus; any bucket count from ``--mode adapter-vs-shell`` below that floor
  is noise, not a mechanism.
* **A fresh session id for every (probe, side) call.** Sharing an index never
  implies sharing a seen-file: both hooks exclude pages they have already
  pushed *in the same session*, so a reused session id silently starves later
  probes on one side only.
* **Normalization that removes exactly two known shell-hook defects and
  nothing else** — see :func:`normalize`. A wider normalizer would launder
  real divergence into the "identical" bucket, which is the failure mode this
  module exists to avoid.

Run it (from the repository root)::

    python -m tests.evals.hook_divergence --mode adapter-vs-shell
    python -m tests.evals.hook_divergence --mode adapter-vs-adapter
    python -m tests.evals.hook_divergence --mode adapter-vs-shell --backend fts5
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from tests.evals.corpus import build_corpus
from tests.evals.rollout import (
    SESSION_START_HOOK,
    SHELL_USER_PROMPT_HOOK,
    _resolve_adapter_console_script,
    build_breadcrumb_hook_env,
)

#: Bucket names, in the order the issue's acceptance criterion lists them.
BUCKETS = (
    "byte-identical-after-normalization",
    "bullet-list-differs",
    "notice-text-only-differs",
    "other",
)

#: The rendered-bullet prefix both hooks emit, single-sourced here so the
#: splitter and the normalizer cannot disagree about what a bullet is.
_BULLET_PREFIX = "  - "

#: The shell hook's spurious empty record renders as exactly this line
#: (``_BULLET_PREFIX`` with no content) — defect 2 in the module docstring of
#: ``tests/evals/test_adapter_overflow_breadcrumb_1905.py``.
_EMPTY_BULLET = _BULLET_PREFIX.rstrip()

_PREAMBLE_PREFIX = "[Knowledge context]"


def normalize(additional_context: str) -> str:
    """Remove the two shell-hook defects that are not divergences, and nothing
    else.

    Both are named in the module docstring of
    ``tests/evals/test_adapter_overflow_breadcrumb_1905.py``:

    1. *The shell hook's trailing newline.* Its ``$MATCHES`` accumulator ends
       every bullet with a ``\\n``, so its ``additionalContext`` always ends
       with one; the adapter renders ``preamble + "\\n" + text`` with no
       trailing separator.
    2. *The shell hook's spurious empty bullet before the notice.* Splitting
       the ``__ATHENAEUM_OVERFLOW__`` sentinel off ``$RESULTS`` leaves a
       trailing newline behind, so the render loop emits a bare ``  - `` line
       ahead of the notice.

    Deliberately NOT normalized: whitespace inside a bullet, bullet ordering,
    notice wording, the preamble. Each of those is a real difference this
    classifier exists to count.
    """
    text = additional_context[:-1] if additional_context.endswith("\n") else additional_context
    return "\n".join(line for line in text.split("\n") if line.rstrip() != _EMPTY_BULLET)


def split_render(normalized: str) -> tuple[list[str], str]:
    """``(bullets, notice)`` for an already-:func:`normalize`-d string.

    The notice is identified structurally rather than by its wording: it is
    the only non-empty, non-preamble line without the bullet prefix, which
    ``athenaeum.recall_overflow.render_overflow_line`` guarantees by
    construction.
    """
    bullets: list[str] = []
    notice = ""
    for line in normalized.split("\n"):
        if not line or line.startswith(_PREAMBLE_PREFIX):
            continue
        if line.startswith(_BULLET_PREFIX):
            bullets.append(line)
            continue
        notice = line
    return bullets, notice


def classify(left: str, right: str) -> str:
    """Bucket one probe's pair of ``additionalContext`` strings.

    *left* and *right* are the RAW hook outputs; normalization happens here so
    no caller can forget it.
    """
    nl, nr = normalize(left), normalize(right)
    if nl == nr:
        return "byte-identical-after-normalization"
    lb, ln = split_render(nl)
    rb, rn = split_render(nr)
    if lb != rb:
        return "bullet-list-differs"
    if ln != rn:
        return "notice-text-only-differs"
    return "other"


@dataclass(frozen=True)
class ProbeResult:
    """One probe's outcome: its bucket plus both raw strings, so a caller can
    inspect *why* a probe landed where it did without re-running the hooks."""

    probe_id: str
    bucket: str
    left: str
    right: str


class ClassifierUnavailable(RuntimeError):
    """A prerequisite (``bash``, ``jq``, an FTS5-capable ``sqlite3``, the
    console script, or — when the vector backend is requested — ``chromadb``
    and a built vector index) is missing. Raised rather than silently
    degrading to a comparison that would measure something other than what
    the caller asked for."""


def missing_prerequisites(*, backend: str) -> list[str]:
    """Names of every prerequisite that is absent, or ``[]``.

    Separated from the run itself so a pytest caller can turn the list into a
    ``skip`` reason and a CLI caller can print it, without either duplicating
    the probe logic.
    """
    missing: list[str] = []
    for tool in ("bash", "jq"):
        if shutil.which(tool) is None:
            missing.append(tool)
    if shutil.which("sqlite3") is None:
        missing.append("sqlite3")
    else:
        probe = subprocess.run(
            ["sqlite3", ":memory:", "CREATE VIRTUAL TABLE t USING fts5(a);"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode != 0:
            missing.append("sqlite3-with-fts5")
    script = _resolve_adapter_console_script()
    if not script.is_absolute() or not script.exists():
        missing.append("athenaeum-claude-hook console script")
    if backend == "vector":
        try:
            import chromadb  # noqa: F401
        except Exception:  # noqa: BLE001 — any import failure means "unavailable"
            missing.append("chromadb")
    return missing


def _hook_env(knowledge_root: Path, hook_home: Path, *, backend: str) -> dict[str, str]:
    env = build_breadcrumb_hook_env(knowledge_root, hook_home)
    # Both hooks read SEARCH_BACKEND; pinning it here is what makes the
    # ``--backend fts5`` leg a genuine "vector index absent" measurement
    # rather than a vector run with the index merely unread.
    env["SEARCH_BACKEND"] = backend
    # Byte-comparing two renders means byte-comparing two `sort` outputs. The
    # shell hook's `sort -u` is locale-sensitive; the core's `sorted()` is
    # codepoint order unconditionally. Pin the locale so the comparison is
    # about the hooks, not about whichever collation the ambient environment
    # happened to carry.
    env["LC_ALL"] = "C"
    return env


def build_index(knowledge_root: Path, hook_home: Path, *, backend: str) -> None:
    """Run ``session-start-recall.sh`` once to build the hook index under
    *hook_home*.

    Called directly rather than through ``tests.evals.rollout.build_hook_index``
    on purpose: that helper memoizes on ``knowledge_root`` alone, which would
    silently hand ``--mode adapter-vs-adapter`` the SAME index twice and
    report a noise floor of zero by construction.
    """
    env = _hook_env(knowledge_root, hook_home, backend=backend)
    result = subprocess.run(
        ["bash", str(SESSION_START_HOOK)],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise ClassifierUnavailable(f"session-start-recall.sh failed: {result.stderr[-2000:]}")
    if backend == "vector" and not (hook_home / ".cache" / "athenaeum" / "wiki-vectors").is_dir():
        raise ClassifierUnavailable("vector index was requested but session start built none")


def _query(
    argv: list[str],
    knowledge_root: Path,
    hook_home: Path,
    query: str,
    *,
    backend: str,
    timeout: float = 180.0,
) -> str:
    result = subprocess.run(
        argv,
        input=json.dumps({"prompt": query, "session_id": f"divergence-{uuid.uuid4().hex}"}),
        env=_hook_env(knowledge_root, hook_home, backend=backend),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ClassifierUnavailable(f"hook {argv[-1]} failed: {result.stderr[-2000:]}")
    if not result.stdout.strip():
        return ""
    payload = json.loads(result.stdout)
    return str(payload.get("hookSpecificOutput", {}).get("additionalContext", ""))


def adapter_argv() -> list[str]:
    return [str(_resolve_adapter_console_script())]


def shell_argv() -> list[str]:
    return ["bash", str(SHELL_USER_PROMPT_HOOK)]


def run(
    *,
    mode: str = "adapter-vs-shell",
    backend: str = "vector",
    scale: str = "core",
    workdir: Path | None = None,
) -> list[ProbeResult]:
    """Classify every probe at *scale*.

    ``adapter-vs-shell`` shares ONE hook index between the two sides (the
    measurement the issue's first acceptance criterion asks for).
    ``adapter-vs-adapter`` runs the adapter against TWO separately built
    indexes over the same materialized corpus — the index-build noise floor
    every other count has to clear.
    """
    missing = missing_prerequisites(backend=backend)
    if missing:
        raise ClassifierUnavailable("missing prerequisites: " + ", ".join(missing))

    owned_tmp: tempfile.TemporaryDirectory[str] | None = None
    if workdir is None:
        owned_tmp = tempfile.TemporaryDirectory(prefix="hook-divergence-")
        workdir = Path(owned_tmp.name)
    try:
        knowledge_root = workdir / "knowledge"
        corpus = build_corpus(scale)
        corpus.materialize(knowledge_root)

        if mode == "adapter-vs-shell":
            home = workdir / "shared-home"
            build_index(knowledge_root, home, backend=backend)
            left_home, right_home = home, home
            left_argv, right_argv = adapter_argv(), shell_argv()
        elif mode == "adapter-vs-adapter":
            left_home, right_home = workdir / "home-a", workdir / "home-b"
            build_index(knowledge_root, left_home, backend=backend)
            build_index(knowledge_root, right_home, backend=backend)
            left_argv = right_argv = adapter_argv()
        else:  # pragma: no cover — argparse constrains the CLI
            raise ValueError(f"unknown mode: {mode!r}")

        results: list[ProbeResult] = []
        for probe in corpus.probes:
            left = _query(left_argv, knowledge_root, left_home, probe.query, backend=backend)
            right = _query(right_argv, knowledge_root, right_home, probe.query, backend=backend)
            results.append(ProbeResult(probe.id, classify(left, right), left, right))
        return results
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()


def tally(results: list[ProbeResult]) -> dict[str, int]:
    counts = {bucket: 0 for bucket in BUCKETS}
    for result in results:
        counts[result.bucket] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode",
        choices=("adapter-vs-shell", "adapter-vs-adapter"),
        default="adapter-vs-shell",
    )
    parser.add_argument("--backend", choices=("vector", "fts5"), default="vector")
    parser.add_argument("--scale", default="core")
    parser.add_argument("--json", action="store_true", help="emit the per-probe buckets as JSON")
    args = parser.parse_args(argv)

    try:
        results = run(mode=args.mode, backend=args.backend, scale=args.scale)
    except ClassifierUnavailable as exc:
        print(f"unavailable: {exc}", file=sys.stderr)
        return 2

    counts = tally(results)
    if args.json:
        payload = {"counts": counts, "probes": {r.probe_id: r.bucket for r in results}}
        print(json.dumps(payload, indent=2))
    else:
        print(f"mode={args.mode} backend={args.backend} scale={args.scale} probes={len(results)}")
        for bucket in BUCKETS:
            print(f"  {counts[bucket]:3d}  {bucket}")
        for bucket in BUCKETS[1:]:
            named = [r.probe_id for r in results if r.bucket == bucket]
            if named:
                print(f"\n{bucket}:\n  " + "\n  ".join(named))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
