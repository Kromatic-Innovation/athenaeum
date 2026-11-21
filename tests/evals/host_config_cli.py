# SPDX-License-Identifier: Apache-2.0
"""Host-config measurement of the ``push_breadcrumb_pull`` pull rate (issue
athenaeum#3057).

Runs ONE north-star arm, ``push_breadcrumb_pull``, through a real ``claude -p``
under three host configurations and reports, per configuration, the ``recall``
call rate, the ``ToolSearch`` calls made before the first ``recall``, the
correct-answer rate and the cost per correct answer:

* ``A`` clean: athenaeum only, tools loaded upfront (the grid as run today).
* ``B`` deferred: athenaeum plus invented stub MCP servers, so the tools are
  deferred behind ``ToolSearch``.
* ``C`` deferred plus plugin: as ``B`` with a coding plugin loaded
  (``--plugin-path`` names an installed plugin directory; it is copied to a
  scratch directory first, so the original is never written to).

Needs a logged-in ``claude`` under an isolated ``CLAUDE_CONFIG_DIR`` (see
``tests/evals/README.md``, "Running the cli-mode spot-check"). Costs real
money: ``--max-spend`` refuses to continue once the measured total passes it.

Each run is checked against the configuration it is meant to represent: in
``B``/``C`` the ``init`` event must list ``ToolSearch`` and must NOT list
``mcp__athenaeum__recall`` upfront, and in ``A`` it must list ``recall``. A
run that fails the check is recorded as a wiring failure and excluded, so a
``B`` that silently equals ``A`` cannot produce a row.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from athenaeum.search import fts5_index_available, get_backend
from tests.evals.corpus import build_corpus
from tests.evals.north_star_report import grade_correctness
from tests.evals.rollout import (
    RECALL_TOOL_NAME,
    HostConfig,
    RolloutRecord,
    _find_probe,
    run_push_breadcrumb_pull,
)

CONFIG_NAMES = ("A", "B", "C")
DEFAULT_SCALE = "core"
DEFAULT_MAX_SPEND_USD = 25.0
#: Invented stub servers for ``B``/``C``: (name prefix, tool count).
STUB_SERVERS: tuple[tuple[str, int], ...] = (("harbor", 12), ("lantern", 12), ("quarry", 12))
TOOL_SEARCH_NAME = "ToolSearch"
#: Plugin env keys never forwarded to the spawn (analytics identifiers).
_DROPPED_PLUGIN_ENV_PREFIXES = ("WOZCODE_POSTHOG",)
PLUGIN_SERVER_NAME = "plugin_woz_code"


@dataclass(frozen=True)
class RunMetrics:
    """What one ``claude -p`` run is reduced to."""

    config: str
    probe_id: str
    recall_calls: int
    tool_search_before_first_recall: int | None
    total_tool_search: int
    correct: bool | None
    cost_usd: float | None
    wiring_ok: bool
    wiring_note: str
    harness_failure: str | None
    #: Tool calls other than ``recall`` and ``ToolSearch``, how many of them
    #: were a host plugin's tools, and how many were file-writing tools.
    other_tool_calls: int = 0
    plugin_tool_calls: int = 0
    write_tool_calls: int = 0


def stub_server_entries(python: str, stub_script: Path) -> dict[str, Any]:
    """The ``--mcp-config`` entries for :data:`STUB_SERVERS`."""
    return {
        prefix: {
            "type": "stdio",
            "command": python,
            "args": [str(stub_script), prefix, str(count)],
            "env": {},
        }
        for prefix, count in STUB_SERVERS
    }


def plugin_server_entries(plugin_copy: Path) -> dict[str, Any]:
    """The plugin's own MCP servers, rewritten for ``--strict-mcp-config``.

    Strict mode ignores servers a plugin declares, so the plugin's ``.mcp.json``
    is read from the scratch copy, ``${CLAUDE_PLUGIN_ROOT}`` is resolved, and
    each server is added to the generated config under the name Claude Code
    would namespace it by (``plugin_<plugin>_<server>``), keeping tool names
    identical to a real install. ``alwaysLoad`` is kept, so the plugin's tools stay
    upfront as in a real install. Analytics env keys are dropped.
    """
    raw = json.loads((plugin_copy / ".mcp.json").read_text(encoding="utf-8"))
    entries: dict[str, Any] = {}
    for name, spec in (raw.get("mcpServers") or {}).items():
        resolved = json.loads(json.dumps(spec).replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_copy)))
        resolved["env"] = {
            k: v
            for k, v in (resolved.get("env") or {}).items()
            if not k.startswith(_DROPPED_PLUGIN_ENV_PREFIXES)
        }
        resolved.setdefault("type", "stdio")
        entries[f"plugin_woz_{name}"] = resolved
    return entries


def build_host_config(
    name: str, *, python: str, stub_script: Path, plugin_copy: Path | None
) -> HostConfig:
    """The :class:`HostConfig` for configuration *name* (``A``, ``B`` or ``C``)."""
    if name == "A":
        return HostConfig(name="A", tool_search=False)
    stubs = stub_server_entries(python, stub_script)
    if name == "B":
        return HostConfig(name="B", extra_servers=stubs, tool_search=True)
    if name == "C":
        if plugin_copy is None:
            raise ValueError("configuration C needs --plugin-path")
        plugin_servers = plugin_server_entries(plugin_copy)
        return HostConfig(
            name="C",
            extra_servers={**stubs, **plugin_servers},
            tool_search=True,
            plugin_dir=plugin_copy,
            allowed_tools=tuple(f"mcp__{server}" for server in plugin_servers),
        )
    raise ValueError(f"unknown configuration {name!r}; expected one of {CONFIG_NAMES}")


def _assistant_tool_uses(transcript: Iterable[Mapping[str, Any]]) -> list[str]:
    """Tool names in the order the model called them."""
    names: list[str] = []
    for event in transcript:
        if not isinstance(event, Mapping) or event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                names.append(str(block.get("name") or ""))
    return names


def count_tool_search_before_recall(tool_names: Sequence[str]) -> tuple[int | None, int, int]:
    """``(ToolSearch calls before the first recall or None, total ToolSearch, recall calls)``."""
    before: int | None = None
    searches = 0
    recalls = 0
    for name in tool_names:
        if name == RECALL_TOOL_NAME:
            recalls += 1
            if before is None:
                before = searches
        elif name == TOOL_SEARCH_NAME:
            searches += 1
    return before, searches, recalls


def _tool_references(transcript: Iterable[Mapping[str, Any]]) -> list[str]:
    """Tool names ``ToolSearch`` loaded, from ``tool_reference`` result blocks."""
    refs: list[str] = []
    for event in transcript:
        if not isinstance(event, Mapping) or event.get("type") != "user":
            continue
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            inner = block.get("content") if isinstance(block, Mapping) else None
            for item in inner if isinstance(inner, list) else []:
                if isinstance(item, Mapping) and item.get("type") == "tool_reference":
                    refs.append(str(item.get("tool_name") or ""))
    return refs


def check_wiring(config: str, transcript: Iterable[Mapping[str, Any]]) -> tuple[bool, str]:
    """Does the stream show the deferral state *config* is meant to have?

    The ``init`` event's ``tools`` list names deferred tools too, so it cannot
    tell upfront from deferred (found in athenaeum#3057's first probe: ``recall``
    was listed while a ``ToolSearch`` call was still needed to load it). The
    evidence used instead is behavioural: a ``tool_reference`` result for
    ``recall`` from ``ToolSearch`` means it was deferred; ``ToolSearch`` absent
    from ``init`` (``ENABLE_TOOL_SEARCH=false``) with no such reference means it
    was upfront. A deferred config whose run never called ``recall`` shows only
    that ``ToolSearch`` is offered, which is necessary but not proof.
    """
    events = list(transcript)
    init_tools: list[str] | None = None
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_tools = [str(t) for t in event.get("tools") or []]
            break
    if init_tools is None:
        return False, "no init event in the stream"
    has_tool_search = TOOL_SEARCH_NAME in init_tools
    refs = _tool_references(events)
    recall_loaded_by_search = RECALL_TOOL_NAME in refs
    recalled = RECALL_TOOL_NAME in _assistant_tool_uses(events)
    if config == "A":
        if has_tool_search or refs:
            return False, "A: tools were deferred (ToolSearch offered or used)"
        return True, "A: recall upfront (no ToolSearch)"
    if not has_tool_search:
        return False, f"{config}: ToolSearch is not offered"
    if recalled and not recall_loaded_by_search:
        return False, f"{config}: recall called without being loaded by ToolSearch"
    if recalled:
        return True, f"{config}: recall deferred and loaded via ToolSearch"
    return True, f"{config}: ToolSearch offered; recall not called, deferral unproven"


def result_cost_usd(transcript: Iterable[Mapping[str, Any]]) -> float | None:
    """``total_cost_usd`` from the stream's ``result`` event, if present."""
    for event in transcript:
        if isinstance(event, Mapping) and event.get("type") == "result":
            value = event.get("total_cost_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


_WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")


def count_other_tools(tool_names: Sequence[str]) -> tuple[int, int, int]:
    """``(other, plugin, write)`` call counts, ``recall`` and ``ToolSearch`` excluded."""
    other = [n for n in tool_names if n not in (RECALL_TOOL_NAME, TOOL_SEARCH_NAME)]
    plugin = sum(1 for n in other if n.startswith("mcp__plugin_"))
    write = sum(1 for n in other if n in _WRITE_TOOLS or n.endswith(("__Edit", "__Write")))
    return len(other), plugin, write


def reduce_record(config: str, record: RolloutRecord, correct: bool | None) -> RunMetrics:
    """Reduce one rollout record to :class:`RunMetrics`."""
    tool_names = _assistant_tool_uses(record.transcript)
    before, searches, recalls = count_tool_search_before_recall(tool_names)
    ok, note = check_wiring(config, record.transcript)
    other, plugin, write = count_other_tools(tool_names)
    return RunMetrics(
        config=config,
        probe_id=record.probe_id,
        recall_calls=recalls,
        tool_search_before_first_recall=before,
        total_tool_search=searches,
        correct=correct,
        cost_usd=result_cost_usd(record.transcript),
        wiring_ok=ok,
        wiring_note=note,
        harness_failure=record.harness_failure,
        other_tool_calls=other,
        plugin_tool_calls=plugin,
        write_tool_calls=write,
    )


@dataclass(frozen=True)
class ConfigRow:
    """One configuration's aggregate row."""

    config: str
    n_runs: int
    n_excluded: int
    recall_rate: float | None
    mean_tool_search_before_first_recall: float | None
    correct_rate: float | None
    cost_usd: float
    cost_per_correct: float | None


def aggregate(config: str, runs: Sequence[RunMetrics]) -> ConfigRow:
    """Aggregate *runs* of one configuration. Wiring and harness failures are excluded."""
    kept = [r for r in runs if r.wiring_ok and r.harness_failure is None]
    n = len(kept)
    pulled = [r for r in kept if r.recall_calls > 0]
    graded = [r for r in kept if r.correct is not None]
    correct_n = sum(1 for r in graded if r.correct)
    cost = sum(r.cost_usd or 0.0 for r in kept)
    before = [
        r.tool_search_before_first_recall
        for r in pulled
        if r.tool_search_before_first_recall is not None
    ]
    return ConfigRow(
        config=config,
        n_runs=n,
        n_excluded=len(runs) - n,
        recall_rate=(len(pulled) / n) if n else None,
        mean_tool_search_before_first_recall=(sum(before) / len(before)) if before else None,
        correct_rate=(correct_n / len(graded)) if graded else None,
        cost_usd=cost,
        cost_per_correct=(cost / correct_n) if correct_n else None,
    )


def _fmt(value: float | None, spec: str) -> str:
    if value is None:
        return "n/a"
    return "$" + format(value, spec[1:]) if spec.startswith("$") else format(value, spec)


def render_table(rows: Sequence[ConfigRow]) -> str:
    """The three-row markdown table the report carries."""
    lines = [
        "| config | runs (excluded) | recall rate | ToolSearch before first recall "
        "| correct rate | cost | cost per correct |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {r.config} | {r.n_runs} ({r.n_excluded}) | {_fmt(r.recall_rate, '.3f')} | "
            f"{_fmt(r.mean_tool_search_before_first_recall, '.2f')} | "
            f"{_fmt(r.correct_rate, '.3f')} | ${r.cost_usd:.2f} | "
            f"{_fmt(r.cost_per_correct, '$.3f')} |"
        )
    return "\n".join(lines)


def _prepare_corpus(scale: str, root: Path, search_backend: str) -> tuple[Any, Path, Path, Path]:
    corpus = build_corpus(scale)
    wiki_root = corpus.materialize(root)
    cache_dir = root / "cache"
    hook_home = root / "hook_home"
    get_backend(search_backend).build_index(wiki_root, cache_dir)
    if search_backend == "vector":
        get_backend("fts5").build_index(wiki_root, cache_dir)
        if not fts5_index_available(cache_dir):
            raise RuntimeError("vector run built no FTS5 index for hybrid recall")
    return corpus, wiki_root, cache_dir, hook_home


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--configs", default="A,B,C", help="comma-separated subset of A,B,C")
    p.add_argument(
        "--probes", default=None, help="comma-separated probe ids (default: every core probe)"
    )
    p.add_argument("--corpus-scale", default=DEFAULT_SCALE)
    p.add_argument("--replicates", type=int, default=1, help="runs per (config, probe)")
    p.add_argument("--search-backend", default="vector")
    p.add_argument("--model", default=None)
    p.add_argument("--claude-binary", default="claude")
    p.add_argument(
        "--plugin-path", type=Path, default=None, help="an installed plugin directory (config C)"
    )
    p.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND_USD)
    p.add_argument("--out", type=Path, default=None, help="JSONL path for per-run metrics")
    p.add_argument("--timeout", type=float, default=240.0)
    p.add_argument("--workers", type=int, default=4, help="concurrent claude -p runs")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    for c in configs:
        if c not in CONFIG_NAMES:
            print(f"unknown configuration {c!r}", file=sys.stderr)
            return 2
    if "C" in configs and args.plugin_path is None:
        print("configuration C needs --plugin-path", file=sys.stderr)
        return 2
    stub_script = Path(__file__).with_name("stub_mcp_server.py")
    model_kwargs = {"model": args.model} if args.model else {}
    all_runs: dict[str, list[RunMetrics]] = {c: [] for c in configs}
    spent = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plugin_copy: Path | None = None
        if args.plugin_path is not None:
            plugin_copy = root / "plugin"
            shutil.copytree(args.plugin_path, plugin_copy, symlinks=True)
        corpus, _wiki, cache_dir, hook_home = _prepare_corpus(
            args.corpus_scale, root / "corpus", args.search_backend
        )
        probe_ids = (
            [p.strip() for p in args.probes.split(",") if p.strip()]
            if args.probes
            else [p.id for p in corpus.probes]
        )
        hosts = {
            c: build_host_config(
                c, python=sys.executable, stub_script=stub_script, plugin_copy=plugin_copy
            )
            for c in configs
        }
        lock = threading.Lock()
        stopped = threading.Event()

        def one_run(config: str, probe_id: str) -> None:
            nonlocal spent
            with lock:
                if spent > args.max_spend:
                    stopped.set()
            if stopped.is_set():
                return
            probe = _find_probe(corpus, probe_id)
            try:
                record = run_push_breadcrumb_pull(
                    probe,
                    root / "corpus",
                    hook_home,
                    cache_dir,
                    args.corpus_scale,
                    claude_binary=args.claude_binary,
                    timeout=args.timeout,
                    host_config=hosts[config],
                    **model_kwargs,
                )
                metrics = reduce_record(config, record, grade_correctness(record, probe, corpus))
            except subprocess.TimeoutExpired:
                # A timed-out run is excluded, not fatal: its cost is unknown.
                metrics = RunMetrics(
                    config, probe_id, 0, None, 0, None, None, True, "", f"timeout {args.timeout}s"
                )
            with lock:
                spent += metrics.cost_usd or 0.0
                all_runs[config].append(metrics)
                if args.out is not None:
                    with args.out.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(asdict(metrics)) + "\n")

        # Resume: rows already in --out count as done (per config and probe).
        done: dict[tuple[str, str], int] = {}
        if args.out is not None and args.out.exists():
            for line in args.out.read_text(encoding="utf-8").splitlines():
                row = RunMetrics(**json.loads(line))
                all_runs.setdefault(row.config, []).append(row)
                spent += row.cost_usd or 0.0
                done[(row.config, row.probe_id)] = done.get((row.config, row.probe_id), 0) + 1

        # Interleaved (probe, replicate, config) so a --max-spend stop leaves the
        # configurations with near-equal run counts.
        tasks = [
            (c, probe_id) for probe_id in probe_ids for _ in range(args.replicates) for c in configs
        ]
        pending: list[tuple[str, str]] = []
        for task in tasks:
            if done.get(task, 0) > 0:
                done[task] -= 1
            else:
                pending.append(task)
        tasks = pending
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(lambda t: one_run(*t), tasks))
        if stopped.is_set():
            print(f"--max-spend ${args.max_spend:.2f} reached; stopped early", file=sys.stderr)
    rows = [aggregate(c, all_runs[c]) for c in configs]
    print(render_table(rows))
    print(f"\nmeasured spend: ${spent:.2f}")
    bad = [r for runs in all_runs.values() for r in runs if not r.wiring_ok]
    if bad:
        print(
            f"{len(bad)} run(s) failed the wiring check, e.g. {bad[0].wiring_note}", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
