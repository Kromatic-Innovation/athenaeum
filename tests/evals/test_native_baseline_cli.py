# SPDX-License-Identifier: Apache-2.0
"""Offline pins for the native-baseline harness (issue athenaeum#3061)."""

from __future__ import annotations

from pathlib import Path

from tests.evals import native_baseline_cli as nb


def test_project_dir_encoding_replaces_every_non_alphanumeric(tmp_path: Path) -> None:
    cwd = tmp_path / "a.b_c" / "cwd"
    cwd.mkdir(parents=True)
    enc = nb.encode_project_dir(cwd)
    assert "/" not in enc and "." not in enc and "_" not in enc
    assert enc.endswith("a-b-c-cwd")
    assert (
        nb.native_memory_dir(tmp_path / "cfg", cwd)
        == tmp_path / "cfg" / "projects" / enc / "memory"
    )


def test_render_auto_memory_writes_index_line_and_topic_file(tmp_path: Path) -> None:
    notes = [
        nb.MemoryNote("a.md", "Alpha", "first line", "# Alpha\nfirst line\nmore"),
        nb.MemoryNote("b.md", "Beta", "other", "# Beta\nother"),
    ]
    nb.render_auto_memory(notes, tmp_path / "memory")
    index = (tmp_path / "memory" / "MEMORY.md").read_text().splitlines()
    assert index == ["- Alpha — first line", "- Beta — other"]
    assert (tmp_path / "memory" / "a.md").read_text().endswith("more")


def test_topic_reads_excludes_the_index_and_outside_files(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()

    def read(path: Path) -> dict:
        return {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(path)}}]
            },
        }

    events = [read(mem / "MEMORY.md"), read(mem / "a.md"), read(tmp_path / "other.md")]
    assert nb.topic_reads(events, mem) == [str((mem / "a.md").resolve())]


def test_strip_degradation_notice_keeps_the_breadcrumb() -> None:
    text = "[athenaeum] degraded\n[Knowledge context] pages:\n  - A"
    assert nb.strip_degradation_notice(text) == "[Knowledge context] pages:\n  - A"


def test_aggregate_splits_cost_per_correct_and_skips_failures() -> None:
    def row(correct: bool, reached: bool, cost: float, failure=None) -> nb.RunRow:
        return nb.RunRow(
            "N", "haiku", "p", "single_hop", correct, reached, cost, failure, None, None, correct
        )

    cell = nb.aggregate(
        "N", "haiku", [row(True, True, 1.0), row(False, False, 1.0), row(True, True, 9.0, "x")]
    )
    assert (cell.n, cell.correct_rate, cell.reach_rate, cell.cost_per_correct) == (2, 0.5, 0.5, 2.0)


def _corpus_stub() -> object:
    from types import SimpleNamespace as NS

    pages = [
        NS(
            uid="a",
            name="Alpha",
            body="x" * 900,
            related=[NS(uid="b")],
            filename="a.md",
            to_markdown=lambda: "A",
        ),
        NS(
            uid="b",
            name="Beta",
            body="second",
            related=[],
            filename="b.md",
            to_markdown=lambda: "B",
        ),
    ]
    probes = [NS(id=f"t{i}", probe_class="single_hop") for i in range(20)] + [
        NS(id="u1", probe_class="unprompted_push"),
        NS(id="u2", probe_class="unprompted_push"),
    ]
    return NS(pages=pages, probes=probes)


def test_shape_push_adds_excerpt_and_hint_only_where_the_arm_says() -> None:
    corpus = _corpus_stub()
    push = "[Knowledge context] pages:\n  - Alpha — d\n  - Beta — e"
    assert nb.shape_push("P", push, corpus) == push
    pe = nb.shape_push("PE", push, corpus)
    assert (
        'excerpt of "Alpha"' in pe
        and "x" * nb.EXCERPT_CHARS in pe
        and "x" * (nb.EXCERPT_CHARS + 1) not in pe
    )
    assert nb.HINT_TEXT not in pe
    ph = nb.shape_push("PH", push, corpus)
    assert nb.HINT_TEXT in ph and "excerpt" not in ph
    peh = nb.shape_push("PEH", push, corpus)
    assert nb.HINT_TEXT in peh and "excerpt of" in peh
    assert nb.shape_push("PEH", "", corpus) == ""


def test_stage2_probe_ids_are_every_unprompted_plus_a_fixed_task_sample() -> None:
    corpus = _corpus_stub()
    ids = nb.stage2_probe_ids(corpus, task_sample=5)
    assert ids == nb.stage2_probe_ids(corpus, task_sample=5)
    assert {"u1", "u2"} <= set(ids) and len(ids) == 7


def test_takeover_notes_add_see_also_links_only_when_asked() -> None:
    corpus = _corpus_stub()
    assert "see also" not in nb.notes_from_corpus(corpus)[0].description
    assert nb.notes_from_corpus(corpus, with_links=True)[0].description.endswith("(see also: Beta)")


def test_hook_key_file_is_read_at_spawn_time_into_the_hook_env_only(monkeypatch, tmp_path) -> None:
    from tests.evals import rollout

    key_file = tmp_path / "config.env"
    key_file.write_text("OTHER=1\nexport ANTHROPIC_API_KEY='k-123'\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(rollout.HOOK_KEY_FILE_ENV_VAR, str(key_file))
    env = rollout.build_breadcrumb_hook_env(tmp_path / "k", tmp_path / "h")
    assert env["ANTHROPIC_API_KEY"] == "k-123"
    import os

    assert "ANTHROPIC_API_KEY" not in os.environ
    monkeypatch.delenv(rollout.HOOK_KEY_FILE_ENV_VAR)
    assert (
        rollout.build_breadcrumb_hook_env(tmp_path / "k", tmp_path / "h")["ANTHROPIC_API_KEY"] == ""
    )


def test_claude_md_is_written_into_the_scratch_cwd(monkeypatch, tmp_path) -> None:
    import subprocess
    from types import SimpleNamespace

    from tests.evals import rollout

    seen: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        seen["md"] = (Path(kwargs["cwd"]) / "CLAUDE.md").read_text()
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(rollout.shutil, "which", lambda _b: "/bin/claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    probe = SimpleNamespace(id="p", probe_class="single_hop", query="q", expected_uids=())
    rollout.run_push_breadcrumb_pull(
        probe,
        tmp_path,
        tmp_path,
        tmp_path,
        "core",
        context_fn=lambda *a, **k: "",
        host_config=rollout.HostConfig(name="PI", claude_md="call recall"),
    )
    assert seen["md"] == "call recall"


def test_index_load_facts_reads_claude_codes_own_warning_marker() -> None:
    full = "- A — a\n- B — b\n"
    assert nb.index_load_facts(full) == (2, False)
    cut = "- A — a\n\n> WARNING: MEMORY.md is 300 lines (limit: 200). Only part of it was loaded.\n"
    assert nb.index_load_facts(cut) == (1, True)


def test_long_index_is_shuffled_but_a_short_one_keeps_corpus_order() -> None:
    from types import SimpleNamespace as NS

    def corpus(n: int) -> object:
        pages = [
            NS(
                uid=f"u{i}",
                name=f"P{i}",
                body=f"b{i}",
                related=[],
                filename=f"u{i}.md",
                to_markdown=lambda: "x",
            )
            for i in range(n)
        ]
        return NS(pages=pages)

    short = [n.name for n in nb.notes_from_corpus(corpus(50))]
    assert short == [f"P{i}" for i in range(50)]
    long = [n.name for n in nb.notes_from_corpus(corpus(300))]
    assert sorted(long) == sorted(f"P{i}" for i in range(300)) and long != sorted(long)
    assert long == [n.name for n in nb.notes_from_corpus(corpus(300))]


def test_neighbour_push_lists_related_pages_and_counts_as_delivered_bullets() -> None:
    from types import SimpleNamespace as NS

    decision = NS(
        uid="d", name="Decision X", body="# Decision X\n\nNeeds updates fast.", related=[]
    )
    system = NS(uid="s", name="System", body="body", related=[NS(uid="d")])
    corpus = NS(pages=[system, decision])
    push = "[Knowledge context] pages:\n  - System — body"
    out = nb.shape_push("PL", push, corpus)
    assert 'Linked from the top match "System"' in out
    assert "  - Decision X — Needs updates fast." in out
    assert "excerpt" not in out
    both = nb.shape_push("PLEI", push, corpus)
    assert "excerpt of" in both and "Decision X" in both
    assert nb.shape_push("PEI", push, corpus).count("Linked from") == 0
    assert nb.shape_push("PL", "[k]\n  - Unknown — x", corpus) == "[k]\n  - Unknown — x"


def test_memory_dir_touched_sees_bash_searches_not_just_reads(tmp_path) -> None:
    mem = tmp_path / "proj" / "memory"
    mem.mkdir(parents=True)

    def use(name: str, **inp) -> dict:
        return {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
        }

    assert nb.memory_dir_touched([use("Bash", command=f'grep -ril relay "{mem}"')], mem)
    assert nb.memory_dir_touched([use("Read", file_path=str(mem / "a.md"))], mem)
    assert not nb.memory_dir_touched([use("Bash", command="ls -la && grep -r relay .")], mem)


def test_every_hit_neighbour_push_pools_links_and_skips_named_pages() -> None:
    from types import SimpleNamespace as NS

    d1 = NS(uid="d1", name="Decision One", body="# D1\n\nfirst fact", related=[])
    d2 = NS(uid="d2", name="Decision Two", body="# D2\n\nsecond fact", related=[])
    a = NS(uid="a", name="Alpha", body="x", related=[NS(uid="d1"), NS(uid="b")])
    b = NS(uid="b", name="Beta", body="y", related=[NS(uid="d2"), NS(uid="d1")])
    corpus = NS(pages=[a, b, d1, d2])
    push = "[k] pages:\n  - Beta — y\n  - Alpha — x"
    out = nb.shape_push("PLA", push, corpus)
    assert out.count("Decision One") == 1 and "Decision Two" in out
    assert "  - Alpha —" not in out.split("Linked from the matches above:")[1]
    assert nb.shape_push("PLAEI", push, corpus).count("excerpt of") == 1
    assert nb.shape_push("PLA", "[k]\n  - Nobody — x", corpus) == "[k]\n  - Nobody — x"


def test_ship_arms_registered_and_instruction_is_the_shipped_section() -> None:
    """athenaeum#3073: the ship arms run the real hook unshaped, with the shipped
    ``CLAUDE.md.example`` recall instruction."""
    from tests.evals import native_baseline_cli as nb

    assert set(nb.SHIP_ARMS) <= set(nb.PUSH_ARMS) <= set(nb.ARMS)
    text = nb.shipped_instruction_text()
    assert text.startswith("### Per-turn push")
    assert "call `recall` yourself" in text
    assert "Auto-remember" not in text
    assert nb.shape_push("SON", "push", object()) == "push"


def test_run_shape_reads_unmatched_line_and_calls_after_it() -> None:
    from tests.evals.native_baseline_cli import UNMATCHED_LINE_MARKER, run_shape

    def use(name: str) -> dict:
        return {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
        }

    text = [{"type": "text", "text": UNMATCHED_LINE_MARKER}]
    result = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": text}]},
    }
    assert run_shape([use("recall"), result, use("recall"), use("Read")]) == (
        ["recall", "recall", "Read"],
        True,
        2,
    )
    assert run_shape([use("recall")]) == (["recall"], False, 0)
    assert run_shape([{"type": "user", "message": "plain string"}]) == ([], False, 0)
