# SPDX-License-Identifier: Apache-2.0
"""Integrate mode for the pages repository (athenaeum#2935).

Every repository is built under ``tmp_path`` by the two-clone harness; the remote
is a local bare directory and no model is called. All people and organisations are
invented.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import librarian, sync_integrate
from athenaeum.models import render_frontmatter
from athenaeum.storage import StorageConfigError, available_adapters
from athenaeum.store import PushFailureReason, PushResult
from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    parse_store_declaration,
)
from athenaeum.sync_integrate import (
    PageRoot,
    SyncSettings,
    integrate_remote,
    push_after_compile,
    resolve_sync_mode,
    sync_before_compile,
    validate_page_text,
)
from tests.fixtures.two_clone_harness import Install, TwoCloneHarness, git, make_two_clone_harness


def declaration_text(mode: str = "integrate") -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "store_id": "st_q7m2xk4vwz5d",
            "roles": {"staff": {"rights": ["read"], "members": ["*"]}},
            "sync": {"mode": mode},
        }
    )


def declared(mode: str = "integrate") -> StoreDeclaration:
    result = parse_store_declaration(declaration_text(mode))
    assert isinstance(result, StoreDeclaration)
    return result


def page(uid: str, name: str, body: str = "", **extra: Any) -> str:
    meta: dict[str, Any] = {"uid": uid, "type": "person", "name": name, "access": "internal"}
    meta.update(extra)
    return render_frontmatter(meta) + (
        body or f"# {name}\n\n## Summary\n\n{name} works at Acme Corp.\n"
    )


BASE = page("person-jordan-reyes", "Jordan Reyes", role="Engineer")
SETTINGS = SyncSettings(
    page_roots=(PageRoot("wiki"),),
    sleep=lambda _s: None,
)


@pytest.fixture
def harness(tmp_path: Path) -> TwoCloneHarness:
    return make_two_clone_harness(
        tmp_path,
        seed={
            "wiki/jordan.md": BASE,
            "wiki/_store/access.yaml": declaration_text(),
        },
    )


def tree(inst: Install) -> str:
    return git(inst.root, "rev-parse", "HEAD^{tree}")


def parents(inst: Install, rev: str = "HEAD") -> list[str]:
    return git(inst.root, "rev-list", "--parents", "-n1", rev).split()[1:]


def compile_page(inst: Install, uid: str, name: str) -> None:
    """Stand in for the compile step: a librarian run commits a new page."""
    inst.commit(f"wiki/{uid}.md", page(uid, name), f"compile {uid}")


# -- section 3.3: which mode applies -----------------------------------------


def test_no_declaration_is_fast_forward() -> None:
    d = resolve_sync_mode(None)
    assert (d.mode, d.integrate, d.local_ignored) == ("fast_forward", False, False)


def test_declared_integrate_is_acted_on() -> None:
    d = resolve_sync_mode(declared("integrate"))
    assert d.integrate and not d.local_ignored


def test_local_fast_forward_narrows_a_declared_integrate() -> None:
    d = resolve_sync_mode(declared("integrate"), "fast_forward")
    assert d.mode == "fast_forward" and not d.local_ignored and d.reason == "narrowed-locally"


def test_local_integrate_on_a_store_that_does_not_declare_it_is_ignored_and_reported() -> None:
    for decl in (None, declared("fast_forward")):
        d = resolve_sync_mode(decl, "integrate")
        assert d.mode == "fast_forward" and d.local_ignored and d.local == "integrate"


def test_an_unreadable_declaration_never_integrates() -> None:
    bad = parse_store_declaration("sync: [not, a, mapping]")
    assert isinstance(bad, StoreDeclarationInvalid)
    d = resolve_sync_mode(bad, "integrate")
    assert d.mode == "fast_forward" and d.local_ignored and d.reason == "declaration-invalid"


def test_adapter_sync_mode_is_validated_and_defaults_to_none() -> None:
    def adapters(**extra: Any) -> dict[str, Any]:
        entry = {"backing_store": "markdown", "surface_root": "pages", **extra}
        return available_adapters({"storage": {"adapters": {"pages": entry}}})

    assert adapters()["pages"].sync_mode is None
    assert adapters(sync_mode="fast_forward")["pages"].sync_mode == "fast_forward"
    with pytest.raises(StorageConfigError, match="sync_mode"):
        adapters(sync_mode="rebase")


# -- AC1: two diverged installs converge by one two-parent merge -------------


def test_two_diverged_installs_converge_on_one_tree(harness: TwoCloneHarness) -> None:
    first, second = harness.first, harness.second
    # Each install fetches (nothing new), compiles a page of its own, and pushes.
    for inst, uid, name in (
        (first, "person-priya-raman", "Priya Raman"),
        (second, "person-sam-ortiz", "Sam Ortiz"),
    ):
        report = sync_before_compile(inst.root, "main", SETTINGS)
        assert report.integrate is not None and report.integrate.status == "up-to-date"
        compile_page(inst, uid, name)
    first_sha, second_sha = first.head(), second.head()

    assert push_after_compile(first.root, "main", SETTINGS).ok

    # The second install is now behind and diverged: it integrates, then pushes.
    report = sync_before_compile(second.root, "main", SETTINGS)
    assert report.integrate is not None and report.integrate.status == "merged"
    merge = second.head()
    assert parents(second) == [second_sha, first_sha]  # one two-parent merge, never a rebase
    assert push_after_compile(second.root, "main", SETTINGS).ok
    assert harness.remote_tip() == merge

    # The first install fast-forwards onto the merge and both end on the same tree.
    report = sync_before_compile(first.root, "main", SETTINGS)
    assert report.integrate is not None and report.integrate.status == "fast-forward"
    assert first.head() == second.head() == merge
    assert tree(first) == tree(second)
    for uid in ("person-priya-raman", "person-sam-ortiz"):
        assert (first.root / "wiki" / f"{uid}.md").exists()


def test_both_installs_edit_one_page_and_converge_without_a_conflict(
    harness: TwoCloneHarness,
) -> None:
    first, second = harness.first, harness.second
    first.commit("wiki/jordan.md", BASE.replace("role: Engineer", "role: Engineer\nstage: lead"))
    second.commit("wiki/jordan.md", BASE.replace("Acme Corp.", "Acme Corp, in Lisbon."))
    assert push_after_compile(first.root, "main", SETTINGS).ok
    assert sync_before_compile(second.root, "main", SETTINGS).integrate.status == "merged"
    assert push_after_compile(second.root, "main", SETTINGS).ok
    sync_before_compile(first.root, "main", SETTINGS)
    merged = (second.root / "wiki" / "jordan.md").read_text()
    assert "stage: lead" in merged and "in Lisbon" in merged and "<<<<<<<" not in merged
    assert tree(first) == tree(second)


def test_a_contested_unit_keeps_the_remote_version_and_is_held(harness: TwoCloneHarness) -> None:
    first, second = harness.first, harness.second
    first.commit("wiki/jordan.md", BASE.replace("works at Acme Corp.", "works at Globex."))
    second.commit("wiki/jordan.md", BASE.replace("works at Acme Corp.", "works at Initech."))
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert result.status == "merged"
    text = (second.root / "wiki" / "jordan.md").read_text()
    assert "Globex" in text and "Initech" not in text  # the remote version stays
    assert [(h.kind, h.reasons, h.uid) for h in result.held] == [
        ("page", ("contested",), "person-jordan-reyes")
    ]
    # the local version is not lost: it is in history, named by blob id
    assert git(second.root, "cat-file", "-t", result.held[0].local) == "blob"


# -- AC2: a rejected push integrates and pushes again ------------------------


def test_a_rejected_push_fetches_integrates_and_pushes_again(
    harness: TwoCloneHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = harness.first, harness.second
    compile_page(second, "person-sam-ortiz", "Sam Ortiz")
    real_push = sync_integrate.push_branch
    calls: list[int] = []

    def racing_push(repo: Path, branch: str, remote: str = "origin") -> PushResult:
        calls.append(1)
        if len(calls) == 1:  # another writer wins the race between fetch and push
            compile_page(first, "person-priya-raman", "Priya Raman")
            assert real_push(first.root, "main").ok
        return real_push(repo, branch, remote)

    monkeypatch.setattr(sync_integrate, "push_branch", racing_push)
    outcome = push_after_compile(second.root, "main", SETTINGS)
    assert outcome.ok and outcome.attempts == 2
    assert harness.remote_tip() == second.head()
    assert len(parents(second)) == 2  # integrated by a merge, not rebased
    assert (second.root / "wiki" / "person-priya-raman.md").exists()


def test_after_three_rejections_the_commits_stay_local_and_push_reports_contended(
    harness: TwoCloneHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = harness.second
    compile_page(second, "person-sam-ortiz", "Sam Ortiz")
    local_before = second.head()
    pushes: list[int] = []
    waits: list[float] = []

    def always_rejected(repo: Path, branch: str, remote: str = "origin") -> PushResult:
        pushes.append(1)
        return PushResult(False, PushFailureReason.REJECTED_REMOTE_MOVED, "fetch first")

    monkeypatch.setattr(sync_integrate, "push_branch", always_rejected)
    settings = SyncSettings(sleep=waits.append)
    report = sync_integrate.SyncReport()
    outcome = push_after_compile(second.root, "main", settings, report)

    assert (outcome.ok, outcome.attempts, outcome.reason) == (False, 3, "contended")
    assert len(pushes) == 3
    assert len(waits) == 2 and all(0 < w < 5 for w in waits)  # a short wait between attempts
    assert second.head() == local_before  # commits stay local
    assert report.steps[-1] == sync_integrate.SyncStep("push", "failed", "contended")


def test_a_push_failure_other_than_a_rejection_is_not_retried(
    harness: TwoCloneHarness, tmp_path: Path
) -> None:
    second = harness.second
    compile_page(second, "person-sam-ortiz", "Sam Ortiz")
    git(second.root, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    outcome = push_after_compile(second.root, "main", SETTINGS)
    assert (outcome.ok, outcome.attempts, outcome.reason) == (False, 1, "other")


# -- AC3: validation before push; hold by default ----------------------------


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("# no frontmatter\n", "frontmatter-unparseable"),
        (render_frontmatter({"type": "person", "name": "A"}) + "x\n", "missing-uid"),
        (render_frontmatter({"uid": "u", "name": "A"}) + "x\n", "missing-type"),
        (render_frontmatter({"uid": "u", "type": "person"}) + "x\n", "missing-name"),
        (page("u", "A", "<<<<<<< ours\nx\n>>>>>>> theirs\n"), "conflict-marker"),
        (page("u", "A", "Title\n=======\nA setext heading is fine.\n"), None),
        (page("u", "A"), None),
    ],
)
def test_validate_page_text(text: str, why: str | None) -> None:
    assert validate_page_text(text) == why


def test_a_merged_page_with_a_conflict_marker_is_held_and_keeps_the_remote_version(
    harness: TwoCloneHarness,
) -> None:
    first, second = harness.first, harness.second
    first.commit("wiki/jordan.md", BASE.replace("role: Engineer", "role: Engineer\nstage: lead"))
    second.commit("wiki/jordan.md", BASE + "\n## Notes\n\n<<<<<<< ours\nx\n>>>>>>> theirs\n")
    remote_text = (first.root / "wiki" / "jordan.md").read_text()
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert result.status == "merged"
    assert [(h.kind, h.reasons) for h in result.held] == [("page", ("conflict-marker",))]
    assert (second.root / "wiki" / "jordan.md").read_text() == remote_text


def test_a_link_from_a_shared_page_to_a_private_one_is_held(tmp_path: Path) -> None:
    config = {
        "storage": {
            "adapters": {
                "shared": {"backing_store": "markdown", "surface_root": "pub", "visibility": 1}
            }
        }
    }
    shared_base = page("person-shared", "Shared Person")
    h = make_two_clone_harness(
        tmp_path, seed={"pub/shared.md": shared_base, "wiki/private.md": page("p-1", "Private")}
    )
    settings = SyncSettings(
        page_roots=(PageRoot("wiki", "wiki"), PageRoot("pub", "shared")),
        config=config,
        sleep=lambda _s: None,
    )
    h.first.commit(
        "pub/shared.md",
        shared_base.replace("# Shared Person", "# Shared Person\n\nSee [[private]]."),
    )
    h.second.commit("pub/shared.md", shared_base.replace("Acme Corp.", "Acme Corp, Lisbon."))
    assert push_after_compile(h.first.root, "main", settings).ok
    remote_text = (h.first.root / "pub" / "shared.md").read_text()
    result = integrate_remote(h.second.root, "main", settings)
    assert [(x.kind, x.reasons) for x in result.held] == [("page", ("store-boundary-link",))]
    assert (h.second.root / "pub" / "shared.md").read_text() == remote_text


def test_a_store_file_changed_on_both_sides_keeps_the_remote_version(
    harness: TwoCloneHarness,
) -> None:
    first, second = harness.first, harness.second
    first.commit("wiki/_store/access.yaml", declaration_text("integrate") + "# first\n")
    second.commit("wiki/_store/access.yaml", declaration_text("integrate") + "# second\n")
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert [h.kind for h in result.held] == ["store-file"]
    assert (second.root / "wiki" / "_store" / "access.yaml").read_text().endswith("# first\n")


def test_an_unregistered_path_changed_on_both_sides_keeps_the_remote_version(
    tmp_path: Path,
) -> None:
    h = make_two_clone_harness(tmp_path, seed={"wiki/jordan.md": BASE, "extras/notes.txt": "n\n"})
    h.first.commit("extras/notes.txt", "from first\n")
    h.second.commit("extras/notes.txt", "from second\n")
    assert push_after_compile(h.first.root, "main", SETTINGS).ok
    result = integrate_remote(h.second.root, "main", SETTINGS)
    assert [(x.kind, x.reasons) for x in result.held] == [
        ("unregistered-path", ("no-rule-for-path",))
    ]
    assert (h.second.root / "extras" / "notes.txt").read_text() == "from first\n"


def test_a_uid_already_in_the_ancestor_on_two_pages_still_blocks_and_nothing_is_pushed(
    harness: TwoCloneHarness,
) -> None:
    first, second = harness.first, harness.second
    # The uid is in the common ancestor (one page); one install renames it while the other
    # edits it, so two pages carry it. Section 7.4 cannot re-mint that: it stays blocked.
    first.commit("wiki/jordan.md", BASE.replace("Acme Corp.", "Acme Corp, Lisbon."))
    git(second.root, "mv", "wiki/jordan.md", "wiki/jordan-renamed.md")
    git(second.root, "commit", "-q", "-m", "rename")
    assert push_after_compile(first.root, "main", SETTINGS).ok
    before = second.head()
    result = integrate_remote(second.root, "main", SETTINGS)
    assert (result.status, result.reason) == ("blocked", "uid-collision")
    assert second.head() == before  # the local branch did not move
    outcome = push_after_compile(second.root, "main", SETTINGS)
    assert not outcome.ok and outcome.reason == "integrate-blocked"
    assert harness.remote_tip() != second.head()


# -- AC4: the merge commit record --------------------------------------------


def test_merge_commit_carries_sync_marker_and_one_line_per_page_changed_on_both_sides(
    tmp_path: Path,
) -> None:
    other = page("person-priya-raman", "Priya Raman")
    h = make_two_clone_harness(
        tmp_path,
        seed={"wiki/jordan.md": BASE, "wiki/priya.md": other, "wiki/solo.md": page("s-1", "Solo")},
    )
    secret = "Jordan's salary is 12345"
    h.first.commit("wiki/jordan.md", BASE.replace("role: Engineer", "role: Engineer\nstage: lead"))
    h.first.commit("wiki/priya.md", other.replace("Acme Corp.", f"Acme Corp. {secret}"))
    h.first.commit("wiki/solo.md", page("s-1", "Solo", "# Solo\n\nonly first changed this\n"))
    h.second.commit("wiki/jordan.md", BASE.replace("Acme Corp.", "Acme Corp, Lisbon."))
    h.second.commit("wiki/priya.md", other.replace("Acme Corp.", "Acme Corp, Porto."))
    assert push_after_compile(h.first.root, "main", SETTINGS).ok
    result = integrate_remote(h.second.root, "main", SETTINGS)
    message = git(h.second.root, "log", "-1", "--format=%B")

    assert "Athenaeum-Sync: 1" in message.splitlines()
    lines = [ln for ln in message.splitlines() if ln.startswith("Athenaeum-Page:")]
    assert sorted(ln.split()[1] for ln in lines) == ["person-jordan-reyes", "person-priya-raman"]
    assert set(result.pages_changed_both) == {"person-jordan-reyes", "person-priya-raman"}
    assert "s-1" not in message  # changed on one side only: no line
    shape = re.compile(
        r"^Athenaeum-Page: \S+ resolver=(rule|held)( reason=\S+)? "
        r"base=[0-9a-f]{40} local=[0-9a-f]{40} remote=[0-9a-f]{40} result=[0-9a-f]{40}$"
    )
    assert all(shape.match(ln) for ln in lines), lines
    blob_ids = {
        tok.split("=")[1]
        for ln in lines
        for tok in ln.split()
        if "=" in tok and "blob" not in tok and re.fullmatch(r"[0-9a-f]{40}", tok.split("=")[1])
    }
    assert blob_ids and all(git(h.second.root, "cat-file", "-t", b) == "blob" for b in blob_ids)
    # ids only: no page text, no page name, no value from any page
    for forbidden in (secret, "Acme", "Lisbon", "Porto", "Jordan Reyes", "Priya Raman", "Engineer"):
        assert forbidden not in message


# -- AC5 (counter-example) and wiring through the librarian run --------------


def _wired(
    tmp_path: Path, decl: str | None, local: str | None
) -> tuple[TwoCloneHarness, Path, dict]:
    seed = {"jordan.md": BASE}
    if decl is not None:
        seed["_store/access.yaml"] = decl
    (tmp_path / "h").mkdir()
    h = make_two_clone_harness(tmp_path / "h", seed=seed)
    adapter: dict[str, Any] = {"backing_store": "markdown", "surface_root": str(h.second.root)}
    if local is not None:
        adapter["sync_mode"] = local
    kr = tmp_path / "kr"
    kr.mkdir()
    return h, kr, {"storage": {"adapters": {"pages": adapter}}}


def _run_second(kr: Path, cfg: dict) -> librarian.SurfaceSyncState:
    state = librarian.SurfaceSyncState()
    librarian._maybe_pull_before_run(
        kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
    )
    librarian._maybe_push_after_run(
        kr, config=cfg, push_after_run=True, dry_run=False, head_at_start=None, surface_sync=state
    )
    return state


def _diverge(h: TwoCloneHarness) -> str:
    h.first.commit("priya.md", page("person-priya-raman", "Priya Raman"))
    assert h.first.push().ok
    h.second.commit("sam.md", page("person-sam-ortiz", "Sam Ortiz"))
    return h.second.head()


def test_declared_integrate_run_merges_pushes_and_reports_one_row_per_step(
    tmp_path: Path,
) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), None)
    mine = _diverge(h)
    state = _run_second(kr, cfg)
    assert len(parents(h.second)) == 2 and parents(h.second)[0] == mine
    assert h.remote_tip() == h.second.head()
    rows = {(r["surface"], r["op"]): r["outcome"] for r in state.results}
    assert rows[("pages", "integrate")] == "ok" and rows[("pages", "push")] == "ok"
    assert {"snapshot", "fetch"} <= {op for (_s, op) in rows}
    assert state.as_profile_fields()["reason"] == "completed"


def test_store_without_declaration_never_merges_and_a_local_integrate_is_ignored_and_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h, kr, cfg = _wired(tmp_path, None, "integrate")
    mine = _diverge(h)
    with caplog.at_level(logging.WARNING):
        state = _run_second(kr, cfg)
    assert h.second.head() == mine  # no merge commit: behaves as fast-forward today
    assert h.remote_tip() != mine
    assert any(
        r["surface"] == "pages"
        and r["op"] == "sync-mode"
        and r["reason"] == "local-setting-ignored"
        for r in state.results
    )
    assert "local sync_mode 'integrate'" in caplog.text and "is ignored" in caplog.text


def test_declared_integrate_narrowed_locally_to_fast_forward_never_merges(
    tmp_path: Path,
) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), "fast_forward")
    mine = _diverge(h)
    state = _run_second(kr, cfg)
    assert h.second.head() == mine and h.remote_tip() != mine
    assert not any(r["op"] == "integrate" for r in state.results)


def test_a_contended_push_is_reported_and_fires_the_push_failed_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), None)
    _diverge(h)
    monkeypatch.setattr(
        sync_integrate,
        "push_branch",
        lambda *a, **k: PushResult(False, PushFailureReason.REJECTED_REMOTE_MOVED, "x"),
    )
    monkeypatch.setattr(sync_integrate.time, "sleep", lambda _s: None)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    with caplog.at_level(logging.WARNING):
        state = _run_second(kr, cfg)
    fields = state.as_profile_fields()
    assert fields["reason"] == "failed" and "pages:push=contended" in str(fields["failed_why"])
    assert "athenaeum-push-failed" in caplog.text


def test_integrate_uses_the_configured_push_remote(tmp_path: Path) -> None:
    # Review on PR athenaeum#2983: integrate mode honours ``librarian.push_remote``
    # exactly as the fast-forward sync does, instead of assuming ``origin``.
    h, kr, cfg = _wired(tmp_path, declaration_text(), None)
    git(h.second.root, "remote", "rename", "origin", "upstream")
    cfg["librarian"] = {"push_remote": "upstream"}
    plans = [p for p in sync_integrate.plan_repo_syncs(kr, cfg) if p.decision.integrate]
    assert [p.settings.remote for p in plans] == ["upstream"]
    mine = _diverge(h)
    state = _run_second(kr, cfg)
    assert len(parents(h.second)) == 2 and parents(h.second)[0] == mine
    assert h.remote_tip() == h.second.head()
    rows = {(r["surface"], r["op"]): r["outcome"] for r in state.results}
    assert rows[("pages", "push")] == "ok"


def test_a_surface_repository_keeps_its_own_branch_not_librarian_push_branch(
    tmp_path: Path,
) -> None:
    h, kr, cfg = _wired(tmp_path, declaration_text(), None)
    cfg["librarian"] = {"push_branch": "elsewhere"}
    plans = [p for p in sync_integrate.plan_repo_syncs(kr, cfg) if p.decision.integrate]
    assert [p.branch for p in plans] == [h.second.branch]


def test_sync_records_fetch_and_push_evidence(tmp_path: Path) -> None:
    """athenaeum#2991: the fetch and push outcome land in the machine-local evidence file."""
    import subprocess

    from athenaeum import store_access_evidence as evidence

    repo = tmp_path / "solo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    nope = str(tmp_path / "nope.git")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", nope], check=True)
    from athenaeum.sync_integrate import push_after_compile, sync_before_compile

    sync_before_compile(repo, "main")
    push_after_compile(repo, "main")
    entry = evidence.load_evidence()[evidence.location_key(repo)]
    assert entry["fetch"]["ok"] is False and entry["push"]["ok"] is False
    assert entry["remote"] == "origin"


# -- the erasure mark (sync S16, design note section 5.1) ---------------------


def _delete(inst: Install, path: str, message: str) -> None:
    git(inst.root, "rm", "-q", "--", path)
    git(inst.root, "commit", "-q", "-m", message)


def _erase(inst: Install, path: str, uid: str) -> None:
    from athenaeum.erasure import erase_pages_and_commit

    assert erase_pages_and_commit(inst.root, [(inst.root / path, uid)])


EDITED = BASE.replace("Acme Corp.", "Acme Corp, in Lisbon.")


def test_erasure_on_the_remote_side_wins_over_a_local_edit(harness: TwoCloneHarness) -> None:
    first, second = harness.first, harness.second
    _erase(first, "wiki/jordan.md", "person-jordan-reyes")
    second.commit("wiki/jordan.md", EDITED)
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert result.status == "merged"
    assert not (second.root / "wiki" / "jordan.md").exists()
    assert result.erased == ("person-jordan-reyes",)
    assert result.held == ()


def test_erasure_on_the_local_side_wins_over_a_remote_edit(harness: TwoCloneHarness) -> None:
    first, second = harness.first, harness.second
    first.commit("wiki/jordan.md", EDITED)
    _erase(second, "wiki/jordan.md", "person-jordan-reyes")
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert result.status == "merged"
    assert not (second.root / "wiki" / "jordan.md").exists()
    assert result.erased == ("person-jordan-reyes",)


def test_a_retire_without_the_mark_and_an_edit_is_contested_not_erased(
    harness: TwoCloneHarness,
) -> None:
    first, second = harness.first, harness.second
    _delete(first, "wiki/jordan.md", "retire jordan")
    second.commit("wiki/jordan.md", EDITED)
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert result.status == "merged"
    assert result.erased == ()
    assert "Lisbon" in (second.root / "wiki" / "jordan.md").read_text()  # the edit survives
    assert [(h.kind, h.reasons) for h in result.held] == [("page", ("contested",))]


def test_a_mark_for_a_different_uid_does_not_erase_the_page(harness: TwoCloneHarness) -> None:
    first, second = harness.first, harness.second
    git(first.root, "rm", "-q", "--", "wiki/jordan.md")
    git(first.root, "commit", "-q", "-m", "x\n\nAthenaeum-Erasure: person-someone-else")
    second.commit("wiki/jordan.md", EDITED)
    assert push_after_compile(first.root, "main", SETTINGS).ok
    result = integrate_remote(second.root, "main", SETTINGS)
    assert result.erased == () and result.held


def test_the_mark_is_found_anywhere_in_the_side_history_since_the_ancestor(
    harness: TwoCloneHarness,
) -> None:
    first, second = harness.first, harness.second
    _erase(first, "wiki/jordan.md", "person-jordan-reyes")
    first.commit("wiki/other.md", page("person-priya-raman", "Priya Raman"))  # a later commit
    second.commit("wiki/jordan.md", EDITED)
    assert push_after_compile(first.root, "main", SETTINGS).ok
    assert integrate_remote(second.root, "main", SETTINGS).erased == ("person-jordan-reyes",)


def test_the_merge_commit_records_the_erasure_with_the_uid_only(harness: TwoCloneHarness) -> None:
    first, second = harness.first, harness.second
    _erase(first, "wiki/jordan.md", "person-jordan-reyes")
    second.commit("wiki/jordan.md", EDITED)
    assert push_after_compile(first.root, "main", SETTINGS).ok
    integrate_remote(second.root, "main", SETTINGS)
    message = git(second.root, "log", "-1", "--format=%B")
    assert "Athenaeum-Erasure: person-jordan-reyes" in message.splitlines()
    assert not [ln for ln in message.splitlines() if ln.startswith("Athenaeum-Page:")]
    for forbidden in ("Jordan Reyes", "jordan.md", "Lisbon", "Acme", "Engineer"):
        assert forbidden not in message


def test_the_erasure_deletion_commit_carries_the_uid_and_nothing_else(
    harness: TwoCloneHarness,
) -> None:
    _erase(harness.first, "wiki/jordan.md", "person-jordan-reyes")
    message = git(harness.first.root, "log", "-1", "--format=%B")
    assert message.splitlines() == [
        "athenaeum: erasure",
        "",
        "Athenaeum-Erasure: person-jordan-reyes",
    ]
