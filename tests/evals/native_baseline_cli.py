# SPDX-License-Identifier: Apache-2.0
"""Native-memory baseline across models (issue athenaeum#3061, Stage 1).

Two arms through a real ``claude -p``, each in a fresh empty working directory:

* ``N`` native only: the probe corpus rendered as Claude Code auto memory (a
  ``MEMORY.md`` index of one line per page plus one topic file per page) under
  ``$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/memory/``, keyed on that session's
  working directory. No athenaeum MCP server and no athenaeum hook.
* ``P`` athenaeum push plus pull: ``push_breadcrumb_pull`` with
  ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1``.

Per arm and model it records the correct rate, the reach-the-source rate (a
topic-file ``Read`` for N, a ``recall`` call for P) and cost per correct, split
into task-shaped probes and ``unprompted_push`` probes.

Needs a logged-in ``claude`` under an isolated ``CLAUDE_CONFIG_DIR``. Costs real
money; ``--max-spend`` stops new runs once the measured total passes it.

Extractor: the breadcrumb hook's topic extractor runs in regex-only mode (no
model key is available to the scratch hook home, and the ``claude-cli`` provider
cannot meet the hook's 3 second budget). The hook's "running without a cached
key" degradation notice is stripped from the push so it does not leak into the
prompt, as it would not on an install with a key.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tests.evals.corpus import _normalize_marker_for_match, answer_bearing_uids
from tests.evals.host_config_cli import (
    _assistant_tool_uses,
    _prepare_corpus,
    plugin_server_entries,
)
from tests.evals.north_star_report import grade_correctness
from tests.evals.rollout import (
    RECALL_TOOL_NAME,
    REFERENCE_TAG_INSTRUCTION,
    Arm,
    HostConfig,
    RolloutRecord,
    _find_probe,
    _native_index_description,
    build_push_breadcrumb_context,
    parse_stream,
    read_loaded_memory_files,
    run_push_breadcrumb_pull,
)

ARMS = ("N", "P", "PE", "PH", "PEH", "PI", "PW", "T")
#: Arms that run through ``push_breadcrumb_pull`` (athenaeum push plus pull).
PUSH_ARMS = ("P", "PE", "PH", "PEH", "PI", "PW")
#: Arms that run as native auto memory with no athenaeum MCP server or hook.
NATIVE_ARMS = ("N", "T")
ARM_LABELS = {
    "N": "native only",
    "P": "breadcrumb (today)",
    "PE": "breadcrumb + top-hit excerpt",
    "PH": "breadcrumb + tool-name hint",
    "PEH": "breadcrumb + excerpt + hint",
    "PI": "breadcrumb + CLAUDE.md recall line",
    "PW": "breadcrumb + coding plugin",
    "T": "takeover (athenaeum-rendered auto memory)",
}
EXCERPT_CHARS = 600
HINT_TEXT = (
    "To read any of these in full, call the MCP tool `mcp__athenaeum__recall` with a "
    "short query (it may be deferred: load it with ToolSearch first)."
)
INSTRUCTION_TEXT = (
    "Memory: before you answer or change anything, call the `mcp__athenaeum__recall` "
    "tool with the key terms of the task, and read what it returns first."
)
MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "sonnet",
    "opus": "opus",
}
#: Claude Code replaces every non-alphanumeric character of the real path.
_PROJECT_DIR_UNSAFE = re.compile(r"[^a-zA-Z0-9]")
UNPROMPTED = "unprompted_push"
P_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def encode_project_dir(cwd: Path) -> str:
    """The ``projects/`` directory name Claude Code derives from *cwd*'s real path."""
    return _PROJECT_DIR_UNSAFE.sub("-", str(cwd.resolve()))


def native_memory_dir(config_dir: Path, cwd: Path) -> Path:
    """Where this session's auto memory lives under the scratch config."""
    return config_dir / "projects" / encode_project_dir(cwd) / "memory"


@dataclass(frozen=True)
class MemoryNote:
    """One note of an auto-memory directory: a topic file plus its index line."""

    filename: str
    name: str
    description: str
    text: str


def notes_from_corpus(corpus: Any, *, with_links: bool = False) -> list[MemoryNote]:
    """Notes for the eval corpus. A compiled-wiki source (the Stage 2 takeover
    arm, athenaeum#3062) only has to provide another function with this shape.

    With *with_links* each index line also names the page's outgoing ``related``
    pages ("see also"), the graph edge athenaeum knows and plain auto memory
    does not put in an index line.
    """
    names = {pg.uid: pg.name for pg in corpus.pages}
    notes = []
    for pg in corpus.pages:
        description = _native_index_description(pg.body)
        if with_links and pg.related:
            linked = "; ".join(names[e.uid] for e in pg.related if e.uid in names)
            if linked:
                description = f"{description} (see also: {linked})"
        notes.append(MemoryNote(pg.filename, pg.name, description, pg.to_markdown()))
    return notes


def render_auto_memory(notes: Sequence[MemoryNote], memory_dir: Path) -> None:
    """Write *notes* as Claude Code auto memory: one ``MEMORY.md`` line per note
    (``- name — description``) and one topic file per note."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for note in notes:
        (memory_dir / note.filename).write_text(note.text, encoding="utf-8")
        lines.append(f"- {note.name} — {note.description}")
    (memory_dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def strip_degradation_notice(text: str) -> str:
    """Drop the hook's ``[athenaeum] ...`` degradation notice lines from a push."""
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("[athenaeum]"))


@dataclass(frozen=True)
class RunRow:
    arm: str
    model: str
    probe_id: str
    probe_class: str
    correct: bool | None
    reached: bool
    cost_usd: float | None
    harness_failure: str | None
    memory_loaded: bool | None = None
    resolved_model: str | None = None
    #: Answer carries the planted fact, ignoring whether the page was opened.
    content_correct: bool | None = None
    #: Which topic extractor the push hook ran (``model`` or ``regex``); None for native.
    extractor: str | None = None


def _init_event(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "init":
            return e
    return {}


def _result_cost(events: Sequence[Mapping[str, Any]]) -> float | None:
    for e in events:
        if e.get("type") == "result" and isinstance(e.get("total_cost_usd"), (int, float)):
            return float(e["total_cost_usd"])
    return None


def topic_reads(events: Sequence[Mapping[str, Any]], memory_dir: Path) -> list[str]:
    """Topic files (not ``MEMORY.md``) the model opened with the ``Read`` tool."""
    out: list[str] = []
    prefix = str(memory_dir.resolve())
    for e in events:
        if e.get("type") != "assistant":
            continue
        for block in (e.get("message") or {}).get("content") or []:
            if not (isinstance(block, Mapping) and block.get("type") == "tool_use"):
                continue
            if block.get("name") != "Read":
                continue
            path = str((block.get("input") or {}).get("file_path") or "")
            real = str(Path(path).resolve()) if path else ""
            if real.startswith(prefix) and Path(real).name != "MEMORY.md":
                out.append(real)
    return out


def content_correct(record: RolloutRecord, probe: Any, corpus: Any) -> bool | None:
    """Like ``grade_correctness`` minus the delivered-page gate.

    An answer-bearing page counts when one of its planted markers is in the
    answer, whether or not the page was opened. For N this credits a fact the
    one-line ``MEMORY.md`` entry already carries. Abstention probes use the
    normal grader, which has no delivery gate.
    """
    if probe.probe_class == "abstention":
        return grade_correctness(record, probe, corpus)
    if not probe.answer_tokens:
        return None
    pages = {pg.uid: pg for pg in corpus.pages}
    required = answer_bearing_uids(probe, pages)
    if not required:
        return None
    answer = _normalize_marker_for_match(record.answer)
    for uid in required:
        markers = [m for u, m in probe.answer_markers if u == uid]
        if not markers or not any(_normalize_marker_for_match(m) in answer for m in markers):
            return False
    return True


def run_native(
    probe: Any,
    corpus: Any,
    corpus_scale: str,
    *,
    config_dir: Path,
    model: str,
    claude_binary: str,
    timeout: float,
    with_links: bool = False,
) -> tuple[RolloutRecord, bool, Path]:
    """One N session. Returns ``(record, memory_loaded, memory_dir)``."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "cwd"
        cwd.mkdir()
        memory_dir = native_memory_dir(config_dir, cwd)
        project_dir = memory_dir.parent
        try:
            render_auto_memory(notes_from_corpus(corpus, with_links=with_links), memory_dir)
            empty_mcp = Path(tmp) / "mcp.json"
            empty_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            argv = [
                claude_binary,
                "-p",
                "--mcp-config",
                str(empty_mcp),
                "--strict-mcp-config",
                "--append-system-prompt",
                REFERENCE_TAG_INSTRUCTION,
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                model,
            ]
            proc = subprocess.run(
                argv,
                input=probe.query,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=cwd,
            )
            parsed = parse_stream((proc.stdout or "").splitlines(), tool_names=None)
            init = _init_event(parsed.transcript)
            auto = ((init.get("memory_paths") or {}).get("auto")) or ""
            memory_loaded = bool(auto) and Path(auto).resolve() == memory_dir.resolve()
            loaded: dict[str, str] = {p: "" for p in topic_reads(parsed.transcript, memory_dir)}
            session_id = init.get("session_id")
            if session_id:
                for path, content in read_loaded_memory_files(config_dir, str(session_id)).items():
                    if not path.endswith("MEMORY.md"):
                        loaded.setdefault(path, content)
            record = RolloutRecord(
                arm=Arm.NATIVE_INDEX,
                probe_id=probe.id,
                probe_class=probe.probe_class,
                corpus_scale=corpus_scale,
                answer=parsed.answer,
                turn_tokens=parsed.turn_tokens,
                tool_calls=parsed.tool_calls,
                recall_called=False,
                injected_context_tokens=None,
                turn_count=parsed.turn_count,
                transcript=[{"native_memory": {"loaded_memory_files": loaded}}, *parsed.transcript],
                mode="cli",
                config_isolated=True,
            )
            return record, memory_loaded, memory_dir
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)


def reduce_native(record: RolloutRecord, memory_dir: Path) -> bool:
    """Did the model open a topic file?"""
    return bool(topic_reads(record.transcript, memory_dir))


@dataclass(frozen=True)
class Cell:
    arm: str
    model: str
    n: int
    correct_rate: float | None
    reach_rate: float | None
    content_rate: float | None
    cost: float
    cost_per_correct: float | None


def _content_rate(kept: Sequence[RunRow]) -> float | None:
    graded = [r for r in kept if r.content_correct is not None]
    return (sum(1 for r in graded if r.content_correct) / len(graded)) if graded else None


def aggregate(arm: str, model: str, rows: Sequence[RunRow]) -> Cell:
    kept = [r for r in rows if r.harness_failure is None]
    graded = [r for r in kept if r.correct is not None]
    correct = sum(1 for r in graded if r.correct)
    cost = sum(r.cost_usd or 0.0 for r in kept)
    return Cell(
        arm=arm,
        model=model,
        n=len(kept),
        correct_rate=(correct / len(graded)) if graded else None,
        reach_rate=(sum(1 for r in kept if r.reached) / len(kept)) if kept else None,
        content_rate=_content_rate(kept),
        cost=cost,
        cost_per_correct=(cost / correct) if correct else None,
    )


def _f(v: float | None, spec: str) -> str:
    return "n/a" if v is None else format(v, spec)


def render_table(rows: Sequence[RunRow], models: Sequence[str]) -> str:
    """Markdown table: arm x model, split task-shaped / unprompted_push."""
    lines = [
        "| subset | model | arm | runs | correct | content-correct | reach | cost "
        "| cost per correct |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, keep in (
        ("task-shaped", lambda r: r.probe_class != UNPROMPTED),
        ("unprompted_push", lambda r: r.probe_class == UNPROMPTED),
    ):
        for model in models:
            for arm in ARMS:
                sel = [r for r in rows if r.model == model and r.arm == arm and keep(r)]
                if not sel:
                    continue
                c = aggregate(arm, model, sel)
                cpc = "n/a" if c.cost_per_correct is None else f"${c.cost_per_correct:.3f}"
                lines.append(
                    f"| {label} | {model} | {arm} | {c.n} | {_f(c.correct_rate, '.3f')} | "
                    f"{_f(c.content_rate, '.3f')} | {_f(c.reach_rate, '.3f')} | "
                    f"${c.cost:.2f} | {cpc} |"
                )
    return "\n".join(lines)


def stage2_probe_ids(corpus: Any, *, task_sample: int = 12, seed: int = 7) -> list[str]:
    """Every ``unprompted_push`` probe plus a fixed, seeded sample of task-shaped ones."""
    unprompted = [p.id for p in corpus.probes if p.probe_class == UNPROMPTED]
    task = sorted(p.id for p in corpus.probes if p.probe_class != UNPROMPTED)
    return sorted(unprompted) + sorted(random.Random(seed).sample(task, task_sample))


def _first_bullet_page(push: str, corpus: Any) -> Any:
    """The corpus page named by the first ``  - name`` bullet of *push*, or None."""
    names = {pg.name: pg for pg in corpus.pages}
    for line in push.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            return names.get(stripped.lstrip("-").strip().split("—")[0].strip())
    return None


def shape_push(arm: str, push: str, corpus: Any) -> str:
    """The push text for *arm*: the breadcrumb, optionally with the top hit's
    excerpt and a tool-name hint appended."""
    if not push:
        return push
    extra: list[str] = []
    if arm in ("PE", "PEH"):
        page = _first_bullet_page(push, corpus)
        if page is not None:
            extra.append(f'Top match, excerpt of "{page.name}":\n{page.body[:EXCERPT_CHARS]}')
    if arm in ("PH", "PEH"):
        extra.append(HINT_TEXT)
    return "\n".join([push, *extra]) if extra else push


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--models", default="haiku", help="comma list of haiku,sonnet,opus")
    p.add_argument("--arms", default="N,P", help=f"comma list of {','.join(ARMS)}")
    p.add_argument("--probes", default=None)
    p.add_argument("--probe-set", choices=("core", "stage2"), default="core")
    p.add_argument("--plugin-path", type=Path, default=None, help="installed plugin dir (arm PW)")
    p.add_argument("--corpus-scale", default="core")
    p.add_argument("--replicates", type=int, default=1)
    p.add_argument("--search-backend", default="fts5")
    p.add_argument("--claude-binary", default="claude")
    p.add_argument("--max-spend", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out", type=Path, default=None)
    return p


def _spend_rows(hook_home: Path) -> int:
    """Count of topic-extraction model calls the hook has logged so far."""
    ledger = hook_home / ".cache" / "athenaeum" / "spend.jsonl"
    try:
        return sum(
            1 for ln in ledger.read_text(encoding="utf-8").splitlines() if "query-topics" in ln
        )
    except OSError:
        return 0


#: Haiku 4.5 list price per million tokens (input, output), for the extractor's
#: API-billed spend. The hook's model calls bill the API key, not the plan.
EXTRACTOR_PRICE_PER_MTOK = (1.0, 5.0)


def extractor_spend_note(hook_home: Path) -> str:
    """One line on the hook extractor's API calls, from the hook's own ledger."""
    ledger = hook_home / ".cache" / "athenaeum" / "spend.jsonl"
    calls = tokens_in = tokens_out = 0
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("run_type") != "query-topics":
            continue
        calls += 1
        for usage in (row.get("tokens_by_model") or {}).values():
            tokens_in += int(usage.get("input", 0) or 0)
            tokens_out += int(usage.get("output", 0) or 0)
    price_in, price_out = EXTRACTOR_PRICE_PER_MTOK
    cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    return (
        f"extractor (API-billed): {calls} model calls, {tokens_in} input / "
        f"{tokens_out} output tokens, about ${cost:.4f}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    import os

    args = build_arg_parser().parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    if any(m not in MODELS for m in models) or any(a not in ARMS for a in arms):
        print(f"models must be in {sorted(MODELS)}, arms in {ARMS}", file=sys.stderr)
        return 2
    if "PW" in arms and args.plugin_path is None:
        print("arm PW needs --plugin-path", file=sys.stderr)
        return 2
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "")
    if not config_dir.is_dir():
        print("CLAUDE_CONFIG_DIR must name an isolated, logged-in config", file=sys.stderr)
        return 2
    rows: list[RunRow] = []
    spent = 0.0
    lock = threading.Lock()
    hook_lock = threading.Lock()
    stopped = threading.Event()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        corpus, _wiki, cache_dir, hook_home = _prepare_corpus(
            args.corpus_scale, root / "corpus", args.search_backend
        )
        if args.probes:
            probe_ids = [p.strip() for p in args.probes.split(",") if p.strip()]
        elif args.probe_set == "stage2":
            probe_ids = stage2_probe_ids(corpus)
        else:
            probe_ids = [p.id for p in corpus.probes]
        plugin_copy: Path | None = None
        if args.plugin_path is not None and "PW" in arms:
            plugin_copy = root / "plugin"
            shutil.copytree(args.plugin_path, plugin_copy, symlinks=True)
        hosts: dict[str, HostConfig] = {}
        for arm in arms:
            if arm not in PUSH_ARMS:
                continue
            if arm == "PW":
                assert plugin_copy is not None
                servers = plugin_server_entries(plugin_copy)
                hosts[arm] = HostConfig(
                    name=arm,
                    extra_servers=servers,
                    plugin_dir=plugin_copy,
                    allowed_tools=tuple(f"mcp__{name}" for name in servers),
                    env=P_ENV,
                )
            elif arm == "PI":
                hosts[arm] = HostConfig(name=arm, claude_md=INSTRUCTION_TEXT, env=P_ENV)
            else:
                hosts[arm] = HostConfig(name=arm, env=P_ENV)
        done: dict[tuple[str, str, str], int] = {}
        if args.out is not None and args.out.exists():
            for line in args.out.read_text(encoding="utf-8").splitlines():
                row = RunRow(**json.loads(line))
                rows.append(row)
                spent += row.cost_usd or 0.0
                k = (row.arm, row.model, row.probe_id)
                done[k] = done.get(k, 0) + 1

        def one(arm: str, model_key: str, probe_id: str) -> None:
            nonlocal spent
            with lock:
                if spent > args.max_spend:
                    stopped.set()
            if stopped.is_set():
                return
            probe = _find_probe(corpus, probe_id)
            model = MODELS[model_key]
            memory_loaded: bool | None = None
            extractor: list[str] = []

            def push_context(knowledge_root: Path, hook_home_: Path, query: str, **kw: Any) -> str:
                # Serialised so the before/after count of logged model calls
                # belongs to this run: it is how the extractor is confirmed.
                with hook_lock:
                    before = _spend_rows(hook_home_)
                    text = build_push_breadcrumb_context(knowledge_root, hook_home_, query, **kw)
                    extractor.append("model" if _spend_rows(hook_home_) > before else "regex")
                return shape_push(arm, strip_degradation_notice(text), corpus)

            try:
                if arm in NATIVE_ARMS:
                    record, memory_loaded, mdir = run_native(
                        probe,
                        corpus,
                        args.corpus_scale,
                        config_dir=config_dir,
                        model=model,
                        claude_binary=args.claude_binary,
                        timeout=args.timeout,
                        with_links=(arm == "T"),
                    )
                    reached = reduce_native(record, mdir)
                    failure = None if memory_loaded else "MEMORY.md was not loaded"
                else:
                    record = run_push_breadcrumb_pull(
                        probe,
                        root / "corpus",
                        hook_home,
                        cache_dir,
                        args.corpus_scale,
                        claude_binary=args.claude_binary,
                        model=model,
                        timeout=args.timeout,
                        context_fn=push_context,
                        host_config=hosts[arm],
                    )
                    reached = RECALL_TOOL_NAME in _assistant_tool_uses(record.transcript)
                    failure = record.harness_failure
                correct = grade_correctness(record, probe, corpus)
                content = content_correct(record, probe, corpus)
                cost = _result_cost(record.transcript)
                resolved = _init_event(record.transcript).get("model")
                row = RunRow(
                    arm,
                    model_key,
                    probe_id,
                    probe.probe_class,
                    correct,
                    reached,
                    cost,
                    failure,
                    memory_loaded,
                    str(resolved) if resolved else None,
                    content,
                    extractor[-1] if extractor else None,
                )
            except subprocess.TimeoutExpired:
                row = RunRow(
                    arm,
                    model_key,
                    probe_id,
                    probe.probe_class,
                    None,
                    False,
                    None,
                    f"timeout {args.timeout}s",
                    memory_loaded,
                    None,
                    None,
                    extractor[-1] if extractor else None,
                )
            with lock:
                spent += row.cost_usd or 0.0
                rows.append(row)
                if args.out is not None:
                    with args.out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(asdict(row)) + "\n")

        tasks = [
            (a, m, pid)
            for pid in probe_ids
            for _ in range(args.replicates)
            for m in models
            for a in arms
        ]
        # Shuffled (fixed seed) so a --max-spend stop drops an unbiased sample, not
        # the last probe classes.
        random.Random(0).shuffle(tasks)
        pending = []
        for t in tasks:
            if done.get(t, 0) > 0:
                done[t] -= 1
            else:
                pending.append(t)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(lambda t: one(*t), pending))
        extractor_note = extractor_spend_note(hook_home)
    print(render_table(rows, models))
    print(f"\nmeasured spend (claude -p runs): ${spent:.2f}")
    print(extractor_note)
    if stopped.is_set():
        print(f"--max-spend ${args.max_spend:.2f} reached; stopped early", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
