# SPDX-License-Identifier: Apache-2.0
"""The filing checks after a merge is published (athenaeum#2928, slice S17).

Built on the two-clone harness: the remote is a local bare directory and no model is
called. All people, organisations and addresses are invented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import librarian, sync_filing_checks, sync_integrate
from athenaeum.models import render_frontmatter
from athenaeum.store import PushResult
from athenaeum.store_decisions import load_queue, queue_path
from athenaeum.sync_filing_checks import (
    CHECK_HELD_ELSEWHERE,
    CHECK_NAMES_OTHER,
    CHECK_PERSON_ON_ORGANISATION,
    FilingResult,
    run_after_push,
)
from athenaeum.sync_integrate import (
    PageRoot,
    PushOutcome,
    SyncReport,
    SyncSettings,
    integrate_remote,
    push_after_compile,
)
from tests.fixtures.two_clone_harness import TwoCloneHarness, git, make_two_clone_harness

STORE = "st_q7m2xk4vwz5d"
ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "me"}
ROOTS = (PageRoot("wiki"),)
SETTINGS = SyncSettings(page_roots=ROOTS, sleep=lambda _s: None)


def declaration_text() -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "store_id": STORE,
            "roles": {"owner": {"rights": ["read", "compile"], "members": [ME]}},
            "sync": {"mode": "integrate"},
        }
    )


def page(uid: str, name: str, type_: str = "person", sections: str = "", **extra: Any) -> str:
    meta: dict[str, Any] = {"uid": uid, "type": type_, "name": name, "access": "internal"}
    meta.update(extra)
    return render_frontmatter(meta) + f"# {name}\n\n## Summary\n\nA short summary.\n\n{sections}"


def contact(line: str) -> str:
    return f"## Contact\n\n{line}\n"


JORDAN = page("person-jordan-reyes", "Jordan Reyes")
PRIYA = page("person-priya-raman", "Priya Raman", sections=contact("Email: pr@example.org"))
ACME = page("company-acme", "Acme Corp", "company")


@pytest.fixture
def h(tmp_path: Path) -> TwoCloneHarness:
    return make_two_clone_harness(
        tmp_path,
        seed={
            "wiki/jordan.md": JORDAN,
            "wiki/priya.md": PRIYA,
            "wiki/acme.md": ACME,
            "wiki/_store/access.yaml": declaration_text(),
        },
    )


def publish(h: TwoCloneHarness, mine: dict[str, str], theirs: dict[str, str]) -> PushOutcome:
    """The other install pushes *theirs*; this one commits *mine*, integrates and pushes."""
    for path, text in theirs.items():
        h.first.commit(path, text)
    assert h.first.push().ok
    for path, text in mine.items():
        h.second.commit(path, text)
    assert integrate_remote(h.second.root, "main", SETTINGS).ok
    outcome = push_after_compile(h.second.root, "main", SETTINGS)
    assert outcome.ok
    return outcome


def run(h: TwoCloneHarness, outcome: PushOutcome, tmp_path: Path) -> tuple[FilingResult, Path]:
    kr = tmp_path / "kr"
    kr.mkdir(exist_ok=True)
    report = SyncReport()
    result = run_after_push(
        h.second.root,
        outcome,
        ROOTS,
        knowledge_root=kr,
        store_id=STORE,
        report=report,
        mounts={STORE: h.second.root / "wiki"},
        asserter=ME,
    )
    assert result is not None
    return result, kr


NEUTRAL = {"wiki/jordan.md": JORDAN.replace("A short summary.", "Edited elsewhere.")}


# -- the pre-push remote tip is recorded ----------------------------------------------


def test_push_outcome_records_the_remote_tip_before_and_the_commit_published(
    h: TwoCloneHarness,
) -> None:
    outcome = publish(
        h, {"wiki/jordan.md": JORDAN + "x\n"}, NEUTRAL | {"wiki/acme.md": ACME + "y\n"}
    )
    assert outcome.remote_before == h.first.head()
    assert outcome.pushed == h.second.head() == h.remote_tip()


def test_after_a_rejected_push_the_tip_is_the_one_the_second_integrate_fetched(
    h: TwoCloneHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.second.commit("wiki/jordan.md", JORDAN + "x\n")
    real_push = sync_integrate.push_branch
    raced: list[str] = []

    def racing_push(repo: Path, branch: str, remote: str = "origin") -> PushResult:
        if not raced:  # another writer wins between the fetch and the push
            raced.append(h.first.commit("wiki/acme.md", ACME + "y\n"))
            assert real_push(h.first.root, "main").ok
        return real_push(repo, branch, remote)

    monkeypatch.setattr(sync_integrate, "push_branch", racing_push)
    outcome = push_after_compile(h.second.root, "main", SETTINGS)
    assert outcome.ok and outcome.attempts == 2
    assert outcome.remote_before == raced[0]


# -- AC1: only the changed units, each check ------------------------------------------


def test_a_contact_value_already_on_another_page_opens_a_decision_naming_page_and_unit(
    h: TwoCloneHarness, tmp_path: Path
) -> None:
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org")
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    result, kr = run(h, outcome, tmp_path)

    assert [(f.check, f.uid, f.unit) for f in result.findings] == [
        (CHECK_HELD_ELSEWHERE, "person-jordan-reyes", "section:contact")
    ]
    (item,) = load_queue(kr, STORE)
    assert item["kind"] == "filing-check" and item["disposition"] == "pending"
    assert [p["uid"] for p in item["pages"]] == ["person-jordan-reyes", "person-priya-raman"]
    assert item["detail"]["unit"] == "section:contact"
    assert item["detail"]["page_path"] == "wiki/jordan.md"
    assert "pr@example.org" not in queue_path(kr, STORE).read_text()  # no value in the queue


def test_a_local_part_naming_a_different_person_opens_a_decision(
    h: TwoCloneHarness, tmp_path: Path
) -> None:
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes",
            "Jordan Reyes",
            sections=contact("Email: priya.raman@example.net"),
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    result, kr = run(h, outcome, tmp_path)
    assert [(f.check, f.unit) for f in result.findings] == [(CHECK_NAMES_OTHER, "section:contact")]
    assert [p["uid"] for p in load_queue(kr, STORE)[0]["pages"]] == [
        "person-jordan-reyes",
        "person-priya-raman",
    ]


def test_an_address_naming_the_page_own_person_passes(h: TwoCloneHarness, tmp_path: Path) -> None:
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes",
            "Jordan Reyes",
            sections=contact("Email: jordan.reyes@example.net"),
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    result, kr = run(h, outcome, tmp_path)
    assert result.units_checked == 1 and result.findings == []
    assert load_queue(kr, STORE) == []


def test_a_person_shaped_address_on_an_organisation_page_opens_a_decision(
    h: TwoCloneHarness, tmp_path: Path
) -> None:
    mine = {
        "wiki/acme.md": page(
            "company-acme", "Acme Corp", "company", contact("Email: sam.ortiz@acme.example")
        )
    }
    outcome = publish(h, mine, NEUTRAL)
    result, _kr = run(h, outcome, tmp_path)
    assert [(f.check, f.uid, f.unit) for f in result.findings] == [
        (CHECK_PERSON_ON_ORGANISATION, "company-acme", "section:contact")
    ]


def test_a_role_address_on_an_organisation_page_passes(h: TwoCloneHarness, tmp_path: Path) -> None:
    mine = {
        "wiki/acme.md": page(
            "company-acme", "Acme Corp", "company", contact("Email: info@acme.example")
        )
    }
    outcome = publish(h, mine, NEUTRAL)
    result, _kr = run(h, outcome, tmp_path)
    assert result.units_checked == 1 and result.findings == []


@pytest.mark.parametrize("local", ["customer.service", "help-desk", "sales_team"])
def test_a_compound_role_address_on_an_organisation_page_passes(
    h: TwoCloneHarness, tmp_path: Path, local: str
) -> None:
    text = contact(f"Email: {local}@acme.example")
    mine = {"wiki/acme.md": page("company-acme", "Acme Corp", "company", text)}
    outcome = publish(h, mine, NEUTRAL)
    result, _kr = run(h, outcome, tmp_path)
    assert result.units_checked == 1 and result.findings == []


def test_only_units_that_differ_from_the_remote_value_are_checked(
    h: TwoCloneHarness, tmp_path: Path
) -> None:
    # The remote already holds a failing unit; this push changes only the Summary.
    bad = page("person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org"))
    h.first.commit("wiki/jordan.md", bad)
    assert h.first.push().ok
    assert integrate_remote(h.second.root, "main", SETTINGS).status == "fast-forward"
    h.first.commit("wiki/acme.md", ACME + "y\n")
    assert h.first.push().ok
    h.second.commit("wiki/jordan.md", bad.replace("A short summary.", "Other summary."))
    assert integrate_remote(h.second.root, "main", SETTINGS).ok
    outcome = push_after_compile(h.second.root, "main", SETTINGS)
    assert outcome.ok
    result, kr = run(h, outcome, tmp_path)
    assert result.units_checked == 1 and result.findings == []
    assert load_queue(kr, STORE) == []


# -- AC5: counter-example ---------------------------------------------------------------


def test_a_merge_that_publishes_no_changed_unit_runs_no_check(
    h: TwoCloneHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both writers file the very same failing text; the merge publishes nothing new.
    same = page("person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org"))
    outcome = publish(h, {"wiki/jordan.md": same}, {"wiki/jordan.md": same})
    assert (
        len(git(h.second.root, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    )  # a merge
    calls: list[str] = []
    monkeypatch.setattr(
        sync_filing_checks, "_check_unit", lambda *a, **k: calls.append("check") or iter(())
    )
    result, kr = run(h, outcome, tmp_path)
    assert result.merge_published is True
    assert result.units_checked == 0 and result.findings == [] and calls == []
    assert load_queue(kr, STORE) == []


def test_a_push_without_a_merge_runs_no_check(h: TwoCloneHarness, tmp_path: Path) -> None:
    bad = page("person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org"))
    h.second.commit("wiki/jordan.md", bad)
    outcome = push_after_compile(h.second.root, "main", SETTINGS)
    assert outcome.ok
    result, kr = run(h, outcome, tmp_path)
    assert result.merge_published is False and result.units_checked == 0
    assert load_queue(kr, STORE) == []


# -- AC2: the push never waits, nothing is held, nothing is reverted ---------------------


def test_the_checks_run_after_the_push_and_leave_the_published_page_as_it_is(
    h: TwoCloneHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = page("person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org"))
    seen: list[str] = []
    real = sync_filing_checks.run_filing_checks

    def spy(repo: Path, before: str, pushed: str, roots: Any) -> FilingResult:
        seen.append(h.remote_tip())  # the remote already holds the pushed commit
        return real(repo, before, pushed, roots)

    monkeypatch.setattr(sync_filing_checks, "run_filing_checks", spy)
    outcome = publish(h, {"wiki/jordan.md": bad}, {"wiki/acme.md": ACME + "y\n"})
    tip_before = h.remote_tip()
    result, _kr = run(h, outcome, tmp_path)
    assert result.findings and seen == [outcome.pushed]
    assert h.remote_tip() == tip_before == h.second.head()  # no revert, no follow-up commit
    assert (h.second.root / "wiki" / "jordan.md").read_text() == bad  # the page is unchanged
    assert git(h.second.root, "status", "--porcelain") == ""


def test_a_crashing_check_is_reported_and_the_push_outcome_stands(
    h: TwoCloneHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = publish(h, {"wiki/jordan.md": JORDAN + "x\n"}, {"wiki/acme.md": ACME + "y\n"})

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("check exploded")

    monkeypatch.setattr(sync_filing_checks, "run_filing_checks", boom)
    report = SyncReport()
    assert (
        run_after_push(
            h.second.root, outcome, ROOTS, knowledge_root=tmp_path, store_id=STORE, report=report
        )
        is None
    )
    assert report.steps[-1] == sync_integrate.SyncStep("filing-checks", "failed", "check-crashed")
    assert outcome.ok and h.remote_tip() == outcome.pushed


def test_no_check_runs_after_a_failed_push(h: TwoCloneHarness, tmp_path: Path) -> None:
    failed = PushOutcome(False, 3, "contended")
    report = SyncReport()
    assert (
        run_after_push(
            h.second.root, failed, ROOTS, knowledge_root=tmp_path, store_id=STORE, report=report
        )
        is None
    )
    assert report.steps == []


# -- AC3: an identical open item is not repeated ----------------------------------------


def test_an_open_item_for_the_same_page_and_unit_is_not_filed_twice(
    h: TwoCloneHarness, tmp_path: Path
) -> None:
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org")
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    _result, kr = run(h, outcome, tmp_path)
    again, _ = run(h, outcome, tmp_path)
    assert again.skipped_existing == 1 and again.filed == 0
    assert len(load_queue(kr, STORE)) == 1


def test_an_install_without_compile_files_nothing_and_still_reports_the_findings(
    h: TwoCloneHarness, tmp_path: Path
) -> None:
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org")
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    result = run_after_push(
        h.second.root,
        outcome,
        ROOTS,
        knowledge_root=tmp_path,
        store_id=STORE,
        mounts={STORE: h.second.root / "wiki"},
        asserter={"iss": ISS, "sub": "stranger"},
    )
    assert result is not None and len(result.findings) == 1 and result.refused == 1
    assert result.filed == 0 and load_queue(tmp_path, STORE) == []


# -- AC4: pages are read at the pushed revision through the seam; no index --------------


def test_pages_are_read_at_the_pushed_revision_and_no_index_is_consulted(
    h: TwoCloneHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum import models, pii_attribution

    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("an index was consulted")

    monkeypatch.setattr(pii_attribution, "build_attribution_context", forbidden)
    monkeypatch.setattr(models.EntityIndex, "__init__", forbidden)
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org")
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    # The working tree no longer matches the pushed commit; only history is read.
    for name in ("jordan", "priya", "acme"):
        (h.second.root / "wiki" / f"{name}.md").write_text("scribble\n")
    result, _kr = run(h, outcome, tmp_path)
    assert [f.check for f in result.findings] == [CHECK_HELD_ELSEWHERE]


def test_the_module_names_no_index_or_context_builder() -> None:
    source = Path(sync_filing_checks.__file__).read_text()
    for forbidden in ("EntityIndex", "build_attribution_context", "AttributionContext"):
        assert forbidden not in source.split('"""', 2)[2]


def test_a_page_in_another_directory_is_not_read(h: TwoCloneHarness, tmp_path: Path) -> None:
    h.first.commit(
        "other/elsewhere.md", page("person-x", "Xia Lin", sections=contact("Email: pr@example.org"))
    )
    mine = {
        "wiki/jordan.md": page(
            "person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org")
        )
    }
    outcome = publish(h, mine, {"wiki/acme.md": ACME + "y\n"})
    result, _ = run(h, outcome, tmp_path)
    assert [p[0] for f in result.findings for p in f.others] == ["person-priya-raman"]


# -- the librarian run -----------------------------------------------------------------


def test_a_declared_integrate_run_files_the_decision_and_reports_a_filing_checks_row(
    tmp_path: Path,
) -> None:
    (tmp_path / "h").mkdir()
    seed = {
        "jordan.md": JORDAN,
        "priya.md": PRIYA,
        "_store/access.yaml": declaration_text(),
    }
    hh = make_two_clone_harness(tmp_path / "h", seed=seed)
    kr = tmp_path / "kr"
    kr.mkdir()
    cfg = {
        "owner": {"asserter": ME},
        "storage": {
            "adapters": {
                "pages": {"backing_store": "markdown", "surface_root": str(hh.second.root)}
            }
        },
    }
    hh.first.commit("acme.md", ACME)
    assert hh.first.push().ok
    hh.second.commit(
        "jordan.md",
        page("person-jordan-reyes", "Jordan Reyes", sections=contact("Email: pr@example.org")),
    )
    state = librarian.SurfaceSyncState()
    librarian._maybe_pull_before_run(
        kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
    )
    librarian._maybe_push_after_run(
        kr, config=cfg, push_after_run=True, dry_run=False, head_at_start=None, surface_sync=state
    )
    rows = {(r["surface"], r["op"]): r["outcome"] for r in state.results}
    assert rows[("pages", "push")] == "ok" and rows[("pages", "filing-checks")] == "ok"
    (item,) = load_queue(kr, STORE)
    assert item["detail"]["page_uid"] == "person-jordan-reyes"
    assert state.as_profile_fields()["reason"] == "completed"
    assert hh.remote_tip() == hh.second.head()


def test_a_crash_in_the_checks_does_not_fail_the_librarian_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "h").mkdir()
    hh = make_two_clone_harness(
        tmp_path / "h", seed={"jordan.md": JORDAN, "_store/access.yaml": declaration_text()}
    )
    kr = tmp_path / "kr"
    kr.mkdir()
    cfg = {
        "owner": {"asserter": ME},
        "storage": {
            "adapters": {
                "pages": {"backing_store": "markdown", "surface_root": str(hh.second.root)}
            }
        },
    }
    hh.first.commit("acme.md", ACME)
    assert hh.first.push().ok
    hh.second.commit("jordan.md", JORDAN + "x\n")

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("exploded")

    monkeypatch.setattr(sync_filing_checks, "run_filing_checks", boom)
    state = librarian.SurfaceSyncState()
    librarian._maybe_pull_before_run(
        kr, config=cfg, pull_before_run=True, dry_run=False, surface_sync=state
    )
    librarian._maybe_push_after_run(
        kr, config=cfg, push_after_run=True, dry_run=False, head_at_start=None, surface_sync=state
    )
    rows = {(r["surface"], r["op"]): r["outcome"] for r in state.results}
    assert rows[("pages", "push")] == "ok"
    assert rows[("pages", "filing-checks")] == "failed"
    assert hh.remote_tip() == hh.second.head()
