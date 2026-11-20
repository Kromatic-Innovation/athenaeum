# SPDX-License-Identifier: Apache-2.0
"""Relevance policy slice 2 (athenaeum#2644): the deterministic pre-tier gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.drops import iter_drops
from athenaeum.relevance_policy import (
    POLICY_FILENAME,
    RelevancePolicyError,
    filter_relevance_never,
    gate_raw_content,
    load_relevance_policy,
)

POLICY = (
    "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
    "| never | | personal | |\n| always | | personal/travel | |\n"
)
PERSONAL = (
    "---\ntype: person\nuid: aa11bb22\nname: Ada Example\ntags: [personal]\n---\n"
    "Likes oat milk.\n"
)
TRAVEL = (
    "---\ntype: person\nuid: cc33dd44\nname: Ben Example\ntags: [personal/travel]\n---\n"
    "Prefers aisle seats.\n"
)
WORK = (
    "---\ntype: person\nuid: ee55ff66\nname: Cy Example\ntags: [active]\n---\n"
    "Runs the platform team.\n"
)


def _seed(tmp_path: Path, policy: str | None = POLICY) -> Path:
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n| company |\n"
    )
    (wiki / "_schema" / "tags.md").write_text(
        "# Tags\n\n| Tag |\n|-----|\n| active |\n| personal |\n| personal/travel |\n"
    )
    (wiki / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    if policy is not None:
        (wiki / "_schema" / POLICY_FILENAME).write_text(policy)
    (root / "raw" / "sessions").mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q", "-b", "test-branch"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test Runner"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    return root


def _pages(root: Path) -> list[Path]:
    return sorted(p for p in (root / "wiki").glob("*.md") if not p.name.startswith("_"))


def _policy(tmp_path: Path):
    return load_relevance_policy(_seed(tmp_path) / "wiki")


# --- the pure decision --------------------------------------------------------


def test_never_covers_declared_tag_and_names_the_rule(tmp_path):
    gate = gate_raw_content(_policy(tmp_path), PERSONAL)
    assert gate.fully_dropped
    assert gate.reason.startswith("relevance-policy: never tag=personal (rule 1")


def test_exception_rule_and_unmatched_are_kept(tmp_path):
    pol = _policy(tmp_path)
    for text in (TRAVEL, WORK):
        gate = gate_raw_content(pol, text)
        assert not gate.drops and gate.kept_content == text


def test_type_and_subject_keys(tmp_path):
    root = _seed(
        tmp_path / "x",
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n"
        "| never | company | | |\n| never | | | ee55ff66 |\n",
    )
    pol = load_relevance_policy(root / "wiki")
    assert gate_raw_content(pol, "---\ntype: company\nname: Acme\n---\nx\n").fully_dropped
    assert gate_raw_content(pol, WORK).fully_dropped
    assert not gate_raw_content(pol, PERSONAL).drops


def test_no_policy_is_a_noop(tmp_path):
    assert gate_raw_content(None, PERSONAL).drops == ()
    assert not load_relevance_policy(_seed(tmp_path / "y", None) / "wiki").rules
    empty = load_relevance_policy(_seed(tmp_path / "z", None) / "wiki")
    assert gate_raw_content(empty, PERSONAL).kept_content == PERSONAL


def test_mixed_markdown_raw_is_handled_per_item(tmp_path):
    pol = _policy(tmp_path)
    for order in ((PERSONAL, WORK, TRAVEL), (WORK, PERSONAL, TRAVEL)):
        gate = gate_raw_content(pol, "".join(order))
        assert [d.item_index for d in gate.drops] == [order.index(PERSONAL)]
        assert not gate.fully_dropped
        assert "oat milk" not in gate.kept_content
        assert "platform team" in gate.kept_content and "aisle seats" in gate.kept_content
        # the remainder is itself a well-formed raw
        again = gate_raw_content(pol, gate.kept_content)
        assert not again.drops


def test_horizontal_rule_is_not_an_item_boundary(tmp_path):
    text = WORK + "\nIntro\n\n---\n\nmore prose\n\n---\n\nend\n"
    gate = gate_raw_content(_policy(tmp_path), text)
    assert not gate.drops and gate.n_items == 1


def test_mixed_jsonl_raw_is_handled_per_item(tmp_path):
    pol = _policy(tmp_path)
    lines = [
        '{"type": "person", "tags": ["personal"], "note": "a"}\n',
        '{"type": "person", "tags": ["active"], "note": "b"}\n',
        "not json\n",
    ]
    gate = gate_raw_content(pol, "".join(lines), jsonl=True)
    assert len(gate.drops) == 1 and not gate.fully_dropped
    assert gate.kept_content == lines[1] + lines[2]


# --- auto-memory ------------------------------------------------------------------


def test_filter_relevance_never_partitions_and_never_deletes(tmp_path):
    pol = _policy(tmp_path)
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text(PERSONAL)
    b.write_text(WORK)
    ams = [MagicMock(path=a), MagicMock(path=b), MagicMock(path=tmp_path / "gone.md")]
    kept, refused = filter_relevance_never(ams, pol)
    assert [x.path for x in kept] == [b, tmp_path / "gone.md"]
    assert [x.path for x, _ in refused] == [a]
    assert a.exists()
    assert filter_relevance_never(ams, None) == (ams, [])


# --- process_one / run() on both transports, and their parity ------------------


def _run(root: Path, monkeypatch, *, batch: bool):
    import anthropic as anthropic_mod

    from athenaeum.librarian import run

    client = MagicMock()
    client.messages.create.side_effect = AssertionError("LLM tier reached")
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(root / "cache"))
    return run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        batch_mode=batch,
    ), client


@pytest.mark.parametrize("batch", [False, True], ids=["sync", "batch"])
def test_whole_raw_never_is_dropped_recorded_and_deleted(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    raw = root / "raw" / "sessions" / "20260101T120000Z-aabbccd0.md"
    raw.write_text(PERSONAL)
    rc, client = _run(root, monkeypatch, batch=batch)
    assert rc == 0
    assert not raw.exists()  # deleted on processing, exactly as today
    assert _pages(root) == []  # passthrough did NOT compile it
    client.messages.create.assert_not_called()
    rows = iter_drops(root / "cache")
    assert len(rows) == 1
    assert rows[0].reason.startswith("relevance-policy: never tag=personal")


@pytest.mark.parametrize("batch", [False, True], ids=["sync", "batch"])
def test_mixed_raw_compiles_only_uncovered_items(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path)
    raw = root / "raw" / "sessions" / "20260101T120000Z-aabbccd0.md"
    raw.write_text(PERSONAL + WORK)
    # The kept head is now WORK's own frontmatter document -> Tier-0 passthrough.
    rc, _client = _run(root, monkeypatch, batch=batch)
    assert rc == 0
    assert not raw.exists()
    pages = _pages(root)
    assert len(pages) == 1
    body = pages[0].read_text()
    assert "platform team" in body and "oat milk" not in body
    rows = iter_drops(root / "cache")
    assert len(rows) == 1 and rows[0].reason.startswith("relevance-policy: never")


def test_batch_parity_with_librarian(tmp_path, monkeypatch):
    """Same inputs, both transports: identical page set and identical drop reasons."""
    outcomes = []
    for batch in (False, True):
        root = _seed(tmp_path / ("b" if batch else "s"))
        sess = root / "raw" / "sessions"
        (sess / "20260101T120000Z-aaaaaaa1.md").write_text(PERSONAL)
        (sess / "20260101T120000Z-aaaaaaa2.md").write_text(TRAVEL)
        (sess / "20260101T120000Z-aaaaaaa3.md").write_text(PERSONAL + WORK)
        rc, _ = _run(root, monkeypatch, batch=batch)
        assert rc == 0
        outcomes.append(
            (
                sorted(p.name for p in _pages(root)),
                sorted(r.reason for r in iter_drops(root / "cache")),
                sorted(p.name for p in sess.glob("*.md")),
            )
        )
    assert outcomes[0] == outcomes[1]
    assert len(outcomes[0][0]) == 2 and len(outcomes[0][1]) == 2


@pytest.mark.parametrize("batch", [False, True], ids=["sync", "batch"])
def test_no_policy_file_behaviour_unchanged(tmp_path, monkeypatch, batch):
    root = _seed(tmp_path, None)
    raw = root / "raw" / "sessions" / "20260101T120000Z-aabbccd0.md"
    raw.write_text(PERSONAL)
    rc, _ = _run(root, monkeypatch, batch=batch)
    assert rc == 0
    assert len(_pages(root)) == 1  # compiled as before
    assert iter_drops(root / "cache") == []


def test_invalid_policy_fails_closed_and_keeps_raw(tmp_path, monkeypatch):
    root = _seed(
        tmp_path,
        "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| never | | nosuchtag | |\n",
    )
    raw = root / "raw" / "sessions" / "20260101T120000Z-aabbccd0.md"
    raw.write_text(PERSONAL)
    with pytest.raises(RelevancePolicyError):
        _run(root, monkeypatch, batch=False)
    assert raw.exists()


def test_auto_memory_phase_drops_covered_file_but_never_deletes_it(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from athenaeum.librarian import RunContext, TokenUsage, _run_auto_memory_phase

    root = _seed(tmp_path)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(root / "cache"))
    auto = root / "raw" / "auto-memory" / "_unscoped"
    auto.mkdir(parents=True)
    covered = auto / "user_oat_milk.md"
    covered.write_text(
        "---\nname: Oat milk\ntype: user\ntags: [personal]\noriginSessionId: s1\n---\n"
        "Likes oat milk.\n"
    )
    (root / "athenaeum.yaml").write_text("recall:\n  extra_intake_roots:\n    - raw/auto-memory\n")
    ctx = RunContext(
        raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root,
        dry_run=False, max_files=None, max_api_calls=None, max_runtime=None,
        cluster_only=False, merge_only=False, strict_budget=False, batch_mode=None,
        retire=None, push_after_run=None, pull_before_run=None, projects_root=None,
        install_signal_handlers=False, changed_paths=None, full_compile=False,
        now=datetime.now(timezone.utc), heartbeat=None, out_run_stats=None,
    )
    ctx.config = {"recall": {"extra_intake_roots": ["raw/auto-memory"]}}
    ctx.usage = TokenUsage()
    ctx.run_deadline = None

    _run_auto_memory_phase(ctx)

    assert covered.exists()
    rows = iter_drops(root / "cache")
    assert len(rows) == 1 and rows[0].reason.startswith("relevance-policy: never tag=personal")
    assert rows[0].raw_ref == "auto-memory/_unscoped/user_oat_milk.md"


# --- resume path: a classify batch collected after a policy change -------------------

_NEVER_ARCHIVED = (
    "| Verdict | Type | Tag | Subject |\n|---|---|---|---|\n| never | | archived | |\n"
)
_KEPT_ITEM = (
    "---\ntype: concept\ntags: [active]\n---\nStandalone fact about WidgetKept gadget.\n"
)
_COVERED_ITEM = (
    "---\ntype: concept\ntags: [archived]\n---\nSecretCoveredMarker about WidgetDropped.\n"
)


def _collect_after_policy_change(tmp_path, monkeypatch, content):
    from tests import test_batch_mode as tb

    tb._patch_uids(monkeypatch)
    scripted = tb._scripted_responder

    def no_observations(params):
        # Empty observations make the write tier fall back to the raw's own
        # text, so a covered item left in the raw WOULD reach the write prompt.
        out = scripted(params)
        if params["model"] == tb.DEFAULT_CLASSIFY_MODEL and out != "[]":
            # A policy keyed on a tag asks classify for a category (athenaeum#2647);
            # a confident "none applies" keeps this test about the item gate.
            items = json.loads(out.replace("Facts about WidgetKept.", ""))
            return json.dumps([{**i, "category": None, "confidence": "high"} for i in items])
        return out

    monkeypatch.setattr(tb, "_scripted_responder", no_observations)
    root, cache_dir, client, usage, raw_files = tb._spill_a_classify_batch(
        tmp_path, "rp", [content]
    )
    # The policy arrives AFTER the batch was submitted.
    (root / "wiki" / "_schema").mkdir(exist_ok=True)
    (root / "wiki" / "_schema" / POLICY_FILENAME).write_text(_NEVER_ARCHIVED)
    write_prompts: list[str] = []

    def responder(params):
        msg = params["messages"][0]["content"]
        if "## Entity to create" in msg or "## Existing page content" in msg:
            write_prompts.append(msg)
        return no_observations(params)

    write_client = tb._FakeClient(responder, allow_sync=True)
    out = tb._collect(root, cache_dir, client, usage, write_client=write_client)
    return root, raw_files, out, write_prompts


def test_collected_classify_handle_wholly_covered_is_discarded(tmp_path, monkeypatch):
    root, raw_files, out, write_prompts = _collect_after_policy_change(
        tmp_path, monkeypatch, _COVERED_ITEM
    )
    assert out.reconciliation.get("relevance-policy") == 1
    assert out.created == 0 and write_prompts == []
    assert raw_files[0].path.exists()  # left on disk for the next run's gate
    assert not list((root / "wiki").glob("*widget*"))


def test_collected_classify_handle_mixed_raw_never_reaches_write_tier(tmp_path, monkeypatch):
    root, raw_files, out, write_prompts = _collect_after_policy_change(
        tmp_path, monkeypatch, _KEPT_ITEM + _COVERED_ITEM
    )
    assert not out.reconciliation.get("relevance-policy")
    assert out.created == 1
    assert write_prompts, "the uncovered item should have been written"
    assert any("WidgetKept" in p for p in write_prompts)
    assert all("SecretCoveredMarker" not in p for p in write_prompts)
    for page in (root / "wiki").glob("*.md"):
        assert "SecretCoveredMarker" not in page.read_text()


ROUTE_POLICY = (
    "| Verdict | Type | Tag | Subject | Store |\n|---|---|---|---|---|\n"
    "| never | | personal | | |\n| route | | personal | | st_h4r5n2kd6wq3 |\n"
)


@pytest.mark.parametrize("batch", [False, True], ids=["sync", "batch"])
def test_a_route_row_after_a_never_row_does_not_let_the_note_through(
    tmp_path, monkeypatch, batch
):
    """athenaeum#2796: ``never`` beats ``route``; capture is unchanged."""
    root = _seed(tmp_path, ROUTE_POLICY)
    raw = root / "raw" / "sessions" / "20260101T120000Z-aabbccd0.md"
    raw.write_text(PERSONAL)
    rc, client = _run(root, monkeypatch, batch=batch)
    assert rc == 0
    assert not raw.exists()
    assert _pages(root) == []
    client.messages.create.assert_not_called()
    rows = iter_drops(root / "cache")
    assert len(rows) == 1
    assert rows[0].reason.startswith("relevance-policy: never tag=personal")
