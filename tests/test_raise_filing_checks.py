# SPDX-License-Identifier: Apache-2.0
"""Raise S21: the filing checks after a raise is accepted (athenaeum#2927).

No model is called. All people, organisations and addresses are invented.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import raise_acceptance as ra
from athenaeum import raise_filing_checks as rfc
from athenaeum import raise_supersede as rs
from athenaeum.filing_checks import (
    CHECK_HELD_ELSEWHERE,
    CHECK_NAMES_OTHER,
    CHECK_PERSON_ON_ORGANISATION,
    DECISION_KIND_FILING_CHECK,
)
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.raise_acceptance import (
    DECISION_KIND_HELD,
    DECISION_KIND_RAISE,
    accept_raise,
    submit_raise,
)
from athenaeum.raise_acceptance_ledger import (
    DISPOSITION_ACCEPTED,
    disposition_of,
    is_rejected,
    iter_rows,
)
from athenaeum.raise_messages import KIND_RAISE, KIND_SUPERSEDE, RaiseMessage, render_message
from athenaeum.raise_projection import Projection, derived_uid, payload_hash, render
from athenaeum.raise_supersede import apply_supersede
from athenaeum.store_decisions import load_queue, resolve_decision

ISS = "https://git.example.net"
PROPOSER = {"iss": ISS, "sub": "1203"}
COMPILER = {"iss": ISS, "sub": "compiler"}
STRANGER = {"iss": ISS, "sub": "9999"}
ORIGIN = "st_f2n6rb5hjc3t"
TARGET = "st_q7m2xk4vwz5d"
RID = "rs_k3v7t2hq4m4xw7pd5n6b"
UID = derived_uid(TARGET, RID)
PAGE = {
    "type": "person",
    "name": "Mara Ellison",
    "description": "Operations lead at a supplier.",
    "aliases": [],
    "tags": ["contact"],
}
CLEAN = "## Summary\n\nRuns operations.\n\n## Contact\n\nEmail: mara@example.com\n"
#: The address Priya's page of the target already holds.
HELD = "## Summary\n\nRuns operations.\n\n## Contact\n\nEmail: pr@example.org\n"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.net",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.net",
}


def _mid(n: int) -> str:
    return "rm_" + "abcdefghij"[n] * 20


def _page(uid: str, name: str, type_: str = "person", body: str = "") -> str:
    meta = {"uid": uid, "type": type_, "name": name}
    return render_frontmatter(meta) + f"# {name}\n\n{body}"


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.kr = tmp / "knowledge"
        self.root = tmp / "knowledge" / "target"
        self.cache = tmp / "cache"
        (self.root / "_store").mkdir(parents=True)
        self.cache.mkdir()
        decl = {
            "version": 2,
            "store_id": TARGET,
            "roles": {
                "writers": {"rights": ["read", "propose"], "members": [PROPOSER]},
                "owners": {"rights": ["read", "compile", "answer"], "members": [COMPILER]},
            },
        }
        (self.root / "_store" / "access.yaml").write_text(yaml.safe_dump(decl), encoding="utf-8")
        (self.root / "priya.md").write_text(
            _page("person-priya-raman", "Priya Raman", body="Email: pr@example.org\n")
        )
        (self.root / "jordan.md").write_text(_page("person-jordan-reyes", "Jordan Reyes"))
        self.git("init", "-q")
        self.commit_all("seed")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True
        ).stdout

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def msg(self, kind: str = KIND_RAISE, *, body: str = CLEAN, **over: Any) -> dict[str, Any]:
        page = dict(over.pop("page", PAGE))
        fields: dict[str, Any] = dict(
            kind=kind,
            message_id=_mid(0),
            origin=ORIGIN,
            target=TARGET,
            proposer=PROPOSER,
            sent_at="2026-11-18T09:30:00Z",
            raise_id=RID,
            seq=1,
            via="proposer",
            page=page,
            body=body,
            content_hash=payload_hash(render(Projection(meta=dict(page), body=body))),
        )
        fields.update(over)
        return json.loads(render_message(RaiseMessage(**fields)))

    def submit(self, **kw: Any) -> ra.Outcome:
        return submit_raise(
            self.kr, self.msg(**kw), target_store_id=TARGET, target_root=self.root,
            cache_dir=self.cache, location="install_raw",
        )

    def accept(self, decision_id: str, **kw: Any) -> ra.Outcome:
        return accept_raise(
            self.kr, target_store_id=TARGET, target_root=self.root, cache_dir=self.cache,
            decision_id=decision_id, **kw,
        )

    def raise_and_accept(self, **kw: Any) -> ra.Outcome:
        accept_kw = {k: kw.pop(k) for k in ("mounts", "asserter") if k in kw}
        out = self.submit(**kw)
        assert out.status == ra.ASKED, out
        assert out.decision is not None
        return self.accept(out.decision["id"], **accept_kw)

    def supersede(self, body: str, seq: int = 2, mid: int = 1, **kw: Any) -> ra.Outcome:
        record = self.msg(KIND_SUPERSEDE, body=body, seq=seq, message_id=_mid(mid))
        return apply_supersede(
            self.kr, record, target_store_id=TARGET, target_root=self.root,
            cache_dir=self.cache, location="install_raw", **kw,
        )

    def copy_path(self) -> Path:
        hits = list(self.root.glob(f"{UID}*.md"))
        assert len(hits) == 1
        return hits[0]

    def pending(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            r for r in load_queue(self.kr, TARGET)
            if r["disposition"] == "pending" and (kind is None or r["kind"] == kind)
        ]

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# -- AC1: after a copy is created, the checks run on it against the target ------


def test_ac1_a_created_copy_is_checked_against_the_targets_pages(env: Env) -> None:
    out = env.raise_and_accept(body=HELD)
    assert out.status == ra.ACCEPTED
    assert out.filing is not None and out.filing.copy == rfc.COPY_CREATED
    assert [f.check for f in out.filing.findings] == [CHECK_HELD_ELSEWHERE]
    assert out.filing.filed == 1
    [item] = env.pending(DECISION_KIND_FILING_CHECK)
    # The item names the copy, and the other page the check named.
    assert [p["uid"] for p in item["pages"]] == [UID, "person-priya-raman"]
    assert item["detail"]["page_uid"] == UID
    assert item["detail"]["raise_id"] == RID and item["detail"]["copy"] == "created"
    assert item["detail"]["unit"] == "section:contact"
    assert "raised copy" in item["question"]
    assert "pr@example.org" not in json.dumps(item)  # never the contact value


def test_ac1_a_clean_copy_runs_the_checks_and_opens_nothing(env: Env) -> None:
    out = env.raise_and_accept(body=CLEAN)
    assert out.filing is not None and not out.filing.crashed
    assert out.filing.units_checked > 0 and out.filing.findings == []
    assert env.pending(DECISION_KIND_FILING_CHECK) == []


def test_ac1_the_other_two_checks_run_on_a_copy(env: Env) -> None:
    org = {**PAGE, "type": "company", "name": "Harbour Freight Co"}
    out = env.raise_and_accept(
        page=org, body="## Contact\n\nEmail: jordan.reyes@example.com\n"
    )
    assert out.filing is not None
    checks = {f.check for f in out.filing.findings}
    assert checks == {CHECK_NAMES_OTHER, CHECK_PERSON_ON_ORGANISATION}
    assert len(env.pending(DECISION_KIND_FILING_CHECK)) == 2


def test_ac1_pages_are_read_at_the_published_commit_not_the_working_tree(env: Env) -> None:
    # A page written but never committed is not one of the store's published pages,
    # and a committed page scribbled over in the working tree is read as committed.
    (env.root / "draft.md").write_text(
        _page("person-draft", "Dana Hollis", body="Email: mara@example.com\n")
    )
    (env.root / "priya.md").write_text("scribble\n")
    out = env.raise_and_accept(body=HELD + "Also: mara@example.com\n")
    assert out.filing is not None
    [finding] = out.filing.findings
    assert finding.check == CHECK_HELD_ELSEWHERE
    assert [uid for uid, _t, _p in finding.others] == ["person-priya-raman"]


def test_ac1_store_files_are_not_pages(env: Env) -> None:
    (env.root / "_store" / "note.md").write_text(
        _page("store-note", "Store note", body="Email: mara@example.com\n")
    )
    env.commit_all("store note")
    out = env.raise_and_accept(body=CLEAN)
    assert out.filing is not None and out.filing.findings == []


def test_ac1_a_target_with_no_git_is_read_as_it_stands(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "priya.md").write_text(
        _page("person-priya-raman", "Priya Raman", body="Email: pr@example.org\n")
    )
    copy = root / f"{UID}-mara-ellison.md"
    copy.write_text(_page(UID, "Mara Ellison", body=HELD))
    result = rfc.run_filing_checks(root, copy, None)
    assert result.copy == rfc.COPY_CREATED
    assert [f.check for f in result.findings] == [CHECK_HELD_ELSEWHERE]


def test_ac1_a_copy_left_by_a_crashed_acceptance_is_checked_whole(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted = env.submit(body=HELD)
    assert submitted.decision is not None
    real = ra.record_acceptance

    def crash(*a: Any, **k: Any) -> None:
        raise RuntimeError("crashed after the commit, before the ledger row")

    monkeypatch.setattr(ra, "record_acceptance", crash)
    with pytest.raises(RuntimeError):
        env.accept(submitted.decision["id"])
    monkeypatch.setattr(ra, "record_acceptance", real)
    # Something else commits before the acceptance is retried.
    (env.root / "jordan.md").write_text(_page("person-jordan-reyes", "Jordan Reyes", body="x\n"))
    env.commit_all("unrelated")
    out = env.accept(submitted.decision["id"])
    assert out.status == ra.ACCEPTED and out.commit is None  # nothing new written
    assert out.filing is not None and out.filing.copy == rfc.COPY_CREATED
    assert [f.check for f in out.filing.findings] == [CHECK_HELD_ELSEWHERE]


def test_ac1_a_copy_the_commit_does_not_hold_is_read_as_it_stands(env: Env) -> None:
    copy = env.root / f"{UID}-mara-ellison.md"
    copy.write_text(_page(UID, "Mara Ellison", body=HELD))  # never committed
    result = rfc.run_filing_checks(env.root, copy, env.head(), created=True)
    assert [f.check for f in result.findings] == [CHECK_HELD_ELSEWHERE]


# -- AC1: after a copy is replaced, the checks run on what changed --------------


def test_ac1_a_replaced_copy_is_checked(env: Env) -> None:
    env.raise_and_accept(body=CLEAN)
    out = env.supersede(HELD)
    assert out.status == rs.APPLIED, out
    assert out.filing is not None and out.filing.copy == rfc.COPY_REPLACED
    assert [f.check for f in out.filing.findings] == [CHECK_HELD_ELSEWHERE]
    [item] = env.pending(DECISION_KIND_FILING_CHECK)
    assert item["detail"]["copy"] == "replaced" and item["detail"]["page_uid"] == UID


def test_ac1_a_replaced_copy_is_checked_only_on_the_units_that_changed(env: Env) -> None:
    first = env.raise_and_accept(body=HELD)
    assert first.filing is not None and first.filing.filed == 1
    [item] = env.pending(DECISION_KIND_FILING_CHECK)
    resolve_decision(env.kr, store_id=TARGET, decision_id=item["id"], disposition="dismissed")
    # The supersede changes the summary only: the settled finding is not opened again.
    out = env.supersede(HELD.replace("Runs operations.", "Runs operations and logistics."))
    assert out.status == rs.APPLIED
    assert out.filing is not None and out.filing.units_checked >= 1
    assert out.filing.findings == []
    assert env.pending(DECISION_KIND_FILING_CHECK) == []


def test_ac1_an_edited_copy_merged_with_a_supersede_is_checked(env: Env) -> None:
    env.raise_and_accept(body=CLEAN)
    path = env.copy_path()
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    path.write_text(
        render_frontmatter(dict(meta)) + body.replace("Runs operations.", "Runs ops."),
        encoding="utf-8",
    )
    env.commit_all("edit here")
    out = env.supersede(CLEAN.replace("mara@example.com", "pr@example.org"))
    assert out.status == rs.APPLIED, out
    assert "Runs ops." in env.copy_path().read_text(encoding="utf-8")  # merged
    assert out.filing is not None
    assert [f.check for f in out.filing.findings] == [CHECK_HELD_ELSEWHERE]


def test_ac1_an_unapplied_supersede_runs_no_check(env: Env) -> None:
    env.raise_and_accept(body=CLEAN)
    assert env.supersede(CLEAN.replace("Runs operations.", "Runs more.")).status == rs.APPLIED
    out = env.supersede(HELD, seq=2, mid=2)  # stale: not above the applied seq
    assert out.status == rs.IGNORED
    assert out.filing is None
    assert env.pending(DECISION_KIND_FILING_CHECK) == []


# -- AC2: accepted and published before the check, and the route stays ----------


def test_ac2_the_copy_is_committed_and_accepted_before_the_checks_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = ra.run_after_publish

    def spy(knowledge_root: Path, **kw: Any) -> rfc.RaiseFilingResult:
        rel = kw["copy_path"].relative_to(env.root).as_posix()
        seen["committed"] = rel in env.git("ls-tree", "-r", "--name-only", "HEAD")
        seen["commit_is_head"] = kw["commit"] == env.head()
        row = disposition_of(env.cache, _mid(0))
        seen["ledger"] = row.disposition if row else None
        seen["raise_pending"] = env.pending(DECISION_KIND_RAISE)
        return real(knowledge_root, **kw)

    monkeypatch.setattr(ra, "run_after_publish", spy)
    out = env.raise_and_accept(body=HELD)
    assert seen == {
        "committed": True,
        "commit_is_head": True,
        "ledger": DISPOSITION_ACCEPTED,
        "raise_pending": [],
    }
    assert out.status == ra.ACCEPTED and out.filing is not None and out.filing.filed == 1


def test_ac2_a_failing_check_leaves_the_outcome_as_a_clean_one(
    env: Env, tmp_path: Path
) -> None:
    other = Env(tmp_path / "other")
    clean, failing = other.raise_and_accept(body=CLEAN), env.raise_and_accept(body=HELD)
    assert (failing.status, failing.reason) == (clean.status, clean.reason) == (ra.ACCEPTED, None)
    assert [r.disposition for r in iter_rows(env.cache)] == [
        r.disposition for r in iter_rows(other.cache)
    ]


def test_ac2_a_crash_in_the_checks_never_undoes_the_acceptance(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("checker broke")

    monkeypatch.setattr(rfc, "run_filing_checks", boom)
    out = env.raise_and_accept(body=HELD)
    assert out.status == ra.ACCEPTED
    assert out.filing is not None and out.filing.crashed
    assert env.copy_path().exists()
    assert disposition_of(env.cache, _mid(0)).disposition == DISPOSITION_ACCEPTED  # type: ignore[union-attr]
    # A crash on a supersede is the same: the replacement stands.
    monkeypatch.setattr(rfc, "run_filing_checks", boom)
    rep = env.supersede(HELD.replace("Runs operations.", "Runs more."))
    assert rep.status == rs.APPLIED and rep.filing is not None and rep.filing.crashed


# -- AC3: each failure opens an item; the copy stays as published ----------------


def test_ac3_the_copy_stays_as_published_after_a_failure(env: Env) -> None:
    out = env.raise_and_accept(body=HELD)
    assert out.filing is not None and out.filing.filed == 1
    _meta, body = parse_frontmatter(env.copy_path().read_text(encoding="utf-8"))
    assert body == HELD  # the payload's body, unchanged
    assert env.head() == out.commit  # no commit after the acceptance
    assert env.git("status", "--porcelain") == ""


def test_ac3_a_replaced_copy_stays_as_published_after_a_failure(env: Env) -> None:
    env.raise_and_accept(body=CLEAN)
    out = env.supersede(HELD)
    assert out.filing is not None and out.filing.filed == 1
    _meta, body = parse_frontmatter(env.copy_path().read_text(encoding="utf-8"))
    assert body == HELD and env.head() == out.commit


def test_ac3_one_item_per_failure_and_none_repeated(env: Env) -> None:
    org = {**PAGE, "type": "company", "name": "Harbour Freight Co"}
    out = env.raise_and_accept(
        page=org,
        body=(
            "## Contact\n\nEmail: jordan.reyes@example.com\n\n"
            "## Billing\n\nEmail: pr@example.org\n"
        ),
    )
    assert out.filing is not None
    assert len(out.filing.findings) == 3 and out.filing.filed == 3
    again = rfc.run_after_publish(
        env.kr, target_store_id=TARGET, target_root=env.root, copy_path=env.copy_path(),
        commit=None, raise_id=RID,
    )
    assert again.filed == 0 and again.skipped_existing == 3
    assert len(env.pending(DECISION_KIND_FILING_CHECK)) == 3


def test_ac3_an_install_without_compile_files_nothing_and_still_accepts(env: Env) -> None:
    out = env.raise_and_accept(body=HELD, mounts={TARGET: env.root}, asserter=STRANGER)
    assert out.status == ra.ACCEPTED
    assert out.filing is not None and out.filing.refused == 1 and out.filing.filed == 0


def test_ac3_the_compiling_install_files_with_its_rights(env: Env) -> None:
    out = env.raise_and_accept(body=HELD, mounts={TARGET: env.root}, asserter=COMPILER)
    assert out.filing is not None and out.filing.filed == 1


# -- AC4: counter-example -- a failing payload is neither rejected nor asked -----


def test_ac4_a_failing_payload_is_not_rejected_and_is_not_an_ask(env: Env) -> None:
    submitted = env.submit(body=HELD)
    # Before acceptance the filing checks do not run: the raise is asked about
    # exactly as a clean one is, never held or rejected for what a check would find.
    assert submitted.status == ra.ASKED and submitted.reason is None
    assert env.pending(DECISION_KIND_HELD) == []
    assert env.pending(DECISION_KIND_FILING_CHECK) == []
    assert submitted.decision is not None
    out = env.accept(submitted.decision["id"])
    assert out.status == ra.ACCEPTED and out.reason is None
    assert not is_rejected(env.cache, RID)
    # The failure is a filing-check item, never a raise decision or a hold.
    assert env.pending(DECISION_KIND_RAISE) == []
    assert env.pending(DECISION_KIND_HELD) == []
    assert [r["kind"] for r in env.pending()] == [DECISION_KIND_FILING_CHECK]
    assert all(r.reason is None for r in iter_rows(env.cache) if r.disposition != "held")


def test_ac4_a_failing_supersede_is_applied_not_held(env: Env) -> None:
    env.raise_and_accept(body=CLEAN)
    out = env.supersede(HELD)
    assert out.status == rs.APPLIED and out.reason is None
    assert env.pending(DECISION_KIND_HELD) == []
    assert [r["kind"] for r in env.pending()] == [DECISION_KIND_FILING_CHECK]


# -- no index ---------------------------------------------------------------------


def test_the_module_names_no_index_or_context_builder() -> None:
    for module in (rfc,):
        source = Path(module.__file__).read_text()  # type: ignore[arg-type]
        for forbidden in ("EntityIndex", "build_attribution_context", "AttributionContext"):
            assert forbidden not in source.split('"""', 2)[2]
