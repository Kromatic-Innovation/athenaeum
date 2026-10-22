# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2078 -- allow tier-3 merges into oversized pages, then split;
recover parked observations whose text was dropped.

Covers AC1 (gate flags instead of refusing), AC2 (append-mode full-echo),
AC3 (split pass), AC4 (observation round-trip), AC5 (recover-parked) and AC6
(moot closure). No LLM, no network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.answers import parse_pending_questions
from athenaeum.models import EntityAction, EntityIndex, EscalationItem, RawFile, parse_frontmatter
from athenaeum.tiers import (
    _MAX_EXISTING_BODY_CHARS,
    DEFAULT_PAGE_SIZE_THRESHOLD_CHARS,
    check_page_size_gate,
    close_moot_notices,
    reresolve_open_questions,
    tier3_derive_actions,
    tier3_merge_append_params,
    tier3_merge_full,
    tier4_escalate,
)


def _action(name: str = "Acme Corp") -> EntityAction:
    return EntityAction(
        kind="update",
        name=name,
        entity_type="",
        tags=[],
        access="",
        existing_uid="a1b2c3d4",
        observations="Acme opened a Lisbon office.",
    )


def _resp(text: str, stop: str = "end_turn") -> MagicMock:
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    r.stop_reason = stop
    return r


def _client(text: str, stop: str = "end_turn") -> MagicMock:
    c = MagicMock()
    c.messages.create.return_value = _resp(text, stop)
    return c


# --------------------------------------------------------------------------
# AC1
# --------------------------------------------------------------------------


class TestAC1GateFlagsInsteadOfRefusing:
    def test_over_threshold_with_meta_flags_and_allows(self) -> None:
        meta: dict = {"uid": "a1b2c3d4"}
        body = "x" * (DEFAULT_PAGE_SIZE_THRESHOLD_CHARS + 1)
        result = check_page_size_gate(_action(), body, "s/x.md", None, existing_meta=meta)
        assert result is None
        assert meta["needs_split"] is True

    def test_under_threshold_does_not_flag(self) -> None:
        meta: dict = {}
        assert check_page_size_gate(_action(), "short", "s/x.md", None, existing_meta=meta) is None
        assert "needs_split" not in meta

    def test_real_dispatch_merges_into_oversized_page_and_persists_flag(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        big = "Fintech startup, Series B. " * 500
        (wiki / "a1b2c3d4-acme-corp.md").write_text(
            f"---\nuid: a1b2c3d4\ntype: company\nname: Acme Corp\n---\n\n{big}\n"
        )
        raw = RawFile(
            path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
            source="sessions",
            timestamp="20240407T120000Z",
            uuid8="aabb0011",
            _content="Acme opened a Lisbon office.",
        )
        client = _client(
            json.dumps({"ops": [{"op": "append_section", "text": "Lisbon office opened."}]})
        )
        _new, pending, uids, escalations = tier3_derive_actions(
            raw, [_action()], EntityIndex(wiki), wiki, client
        )
        assert escalations == []
        assert uids == ["a1b2c3d4"]
        client.messages.create.assert_called_once()
        written = pending[0][1]
        assert "Lisbon office opened." in written
        meta, body = parse_frontmatter(written)
        assert meta["needs_split"] is True
        assert big.strip() in body  # nothing dropped


# --------------------------------------------------------------------------
# AC2
# --------------------------------------------------------------------------


def _big_body() -> str:
    head = "# Acme\n\nUNIQUE-HEAD-MARKER first claim.[^1]\n\n"
    filler = "Filler sentence about Acme. " * 1200
    tail = "\n\nTail claim.[^2]\n\n[^1]: src/a.md\n[^2]: src/b.md\n"
    body = head + filler + tail
    assert len(body) > _MAX_EXISTING_BODY_CHARS
    return body


class TestAC2AppendMode:
    def test_params_send_bounded_tail_not_whole_body(self) -> None:
        body = _big_body()
        params = tier3_merge_append_params(_action(), body, "s/x.md")
        user = params["messages"][0]["content"]
        assert "UNIQUE-HEAD-MARKER" not in user
        assert "Tail claim." in user
        assert "ONLY the new markdown to append" in params["system"]

    def test_full_echo_on_oversized_body_appends_and_never_replaces(self) -> None:
        body = _big_body()
        client = _client("ADDS_NEW_CLAIM: true\n- Opened a Lisbon office.[^1]\n\n[^1]: s/x.md\n")
        merged, esc = tier3_merge_full(_action(), body, "s/x.md", client)
        assert esc is None
        assert merged is not None
        assert merged.startswith(body.rstrip("\n"))  # existing content intact
        assert "UNIQUE-HEAD-MARKER" in merged
        assert "Lisbon office" in merged
        # the model's [^1] collided with the page's [^1]; it was renumbered
        assert merged.count("[^1]:") == 1
        assert "[^3]" in merged
        assert client.messages.create.call_count == 1

    def test_model_echoing_a_rewritten_page_cannot_shrink_it(self) -> None:
        body = _big_body()
        client = _client("ADDS_NEW_CLAIM: true\nOnly a short rewrite.")
        merged, _ = tier3_merge_full(_action(), body, "s/x.md", client)
        assert merged is not None and merged.startswith(body.rstrip("\n"))
        assert len(merged) > len(body)

    def test_no_new_claim_records_only_a_citation(self) -> None:
        body = _big_body()
        client = _client("ADDS_NEW_CLAIM: false")
        merged, esc = tier3_merge_full(_action(), body, "s/x.md", client)
        assert esc is None and merged is not None
        assert merged.startswith(body.rstrip("\n"))

    def test_no_merge_and_escalate_and_truncation(self) -> None:
        body = _big_body()
        merged, esc = tier3_merge_full(_action(), body, "s/x.md", _client("NO_MERGE: dup"))
        assert merged is None and esc is None
        merged, esc = tier3_merge_full(
            _action(), body, "s/x.md", _client("ADDS_NEW_CLAIM: true\nESCALATE: clash")
        )
        assert merged is None and isinstance(esc, EscalationItem)
        merged, esc = tier3_merge_full(
            _action(), body, "s/x.md", _client("ADDS_NEW_CLAIM: true\n- partial", "max_tokens")
        )
        assert merged is None and isinstance(esc, EscalationItem)
        merged, esc = tier3_merge_full(_action(), body, "s/x.md", _client("   "))
        assert merged is None and isinstance(esc, EscalationItem)


# --------------------------------------------------------------------------
# AC4
# --------------------------------------------------------------------------

OBSERVATION = (
    "First paragraph of the parked note.\n\n"
    "## a heading-looking line\n"
    "---\n"
    "**Conflict type**: not really\n"
    "- [x] a fake checkbox\n"
    "```python\nprint('fenced')\n```\n"
    '{"json": "line"}'
)


def _notice(entity: str = "Big Page", ref: str = "drive/abc-note.md") -> EscalationItem:
    return EscalationItem(
        raw_ref=ref,
        entity_name=entity,
        conflict_type="oversize_page",
        description=(
            "Page 'Big Page' is over the threshold. The new observation from "
            f"{ref} follows so it is not lost:\n\n{OBSERVATION}"
        ),
    )


class TestAC4ObservationRoundTrip:
    def test_notice_block_round_trips_its_observation(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir()
        tier4_escalate([_notice()], pending)
        # a second, unrelated block after it must still parse independently
        tier4_escalate([_notice("Other Page", "drive/other.md")], pending)
        pqs = parse_pending_questions(pending)
        assert len(pqs) == 2
        pq = next(p for p in pqs if p.entity == "Big Page")
        assert pq.observation == OBSERVATION
        assert pq.recovered_observation == OBSERVATION
        assert pq.answer_lines == []  # no phantom answer
        assert pq.answered is False
        assert pq.description.endswith("follows so it is not lost:")
        assert "First paragraph" not in pq.description

    def test_observation_containing_a_longer_backtick_run(self, tmp_path: Path) -> None:
        obs = "before\n````\ninner\n````\nafter"
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir()
        item = _notice()
        item.description = f"Parked, follows so it is not lost:\n\n{obs}"
        tier4_escalate([item], pending)
        (pq,) = parse_pending_questions(pending)
        assert pq.observation == obs

    def test_legacy_block_tail_is_recovered_as_observation(self, tmp_path: Path) -> None:
        pending = tmp_path / "wiki" / "_pending_questions.md"
        pending.parent.mkdir()
        pending.write_text(
            '## [2026-08-26] Entity: "Box" (from contact-sync/x.jsonl)\n'
            "- [ ] Page is big, observation follows so it is not lost:\n\n"
            "**Conflict type**: oversize_page\n"
            "**Description**: Page is big, observation follows so it is not lost:\n\n"
            "legacy tail text\n"
        )
        (pq,) = parse_pending_questions(pending)
        assert pq.observation == ""
        assert pq.recovered_observation == "legacy tail text"


# --------------------------------------------------------------------------
# AC6
# --------------------------------------------------------------------------


def _legacy_block(ref: str, *, tail: str = "", ctype: str = "oversize_page") -> str:
    return (
        f'## [2026-08-26] Entity: "Box" (from {ref})\n'
        "- [ ] Page is big, the observation follows so it is not lost:\n\n"
        f"**Conflict type**: {ctype}\n"
        "**Description**: Page is big, the observation follows so it is not lost:\n"
        + (f"\n{tail}\n" if tail else "")
    )


class TestAC6MootClosure:
    def _setup(self, tmp_path: Path, *blocks: str) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (tmp_path / "raw" / "drive").mkdir(parents=True)
        pending = wiki / "_pending_questions.md"
        pending.write_text("# Pending Questions\n\n" + "\n\n---\n\n".join(blocks))
        return pending

    def test_closes_only_empty_and_sourceless(self, tmp_path: Path) -> None:
        live = tmp_path / "raw" / "drive" / "live.md"
        pending = self._setup(
            tmp_path,
            _legacy_block("drive/gone.md"),  # moot
            _legacy_block("drive/live.md"),  # source still on disk
            _legacy_block("drive/gone2.md", tail="has the observation"),  # carries text
            _legacy_block("drive/gone3.md", ctype="principled"),  # genuine question, not notice
        )
        live.write_text("x")
        assert close_moot_notices(pending) == 1
        remaining = {p.source for p in parse_pending_questions(pending)}
        assert remaining == {"drive/live.md", "drive/gone2.md", "drive/gone3.md"}
        archive = (pending.parent / "_pending_questions_archive.md").read_text()
        assert "drive/gone.md" in archive and "closed as moot" in archive

    def test_full_echo_refusal_recognised_by_text(self, tmp_path: Path) -> None:
        block = (
            '## [2026-08-26] Entity: "Box" (from drive/gone.md)\n'
            "- [ ] Tier 3 full-echo merge refused: body is 25000 chars\n\n"
            "**Conflict type**: principled\n"
            "**Description**: Tier 3 full-echo merge refused: body is 25000 chars\n"
        )
        pending = self._setup(tmp_path, block)
        assert close_moot_notices(pending) == 1

    def test_runs_without_a_resolver_client(self, tmp_path: Path) -> None:
        pending = self._setup(tmp_path, _legacy_block("drive/gone.md"))
        assert reresolve_open_questions(pending, client=None) == 1
        assert parse_pending_questions(pending) == []


# --------------------------------------------------------------------------
# AC3 split pass
# --------------------------------------------------------------------------


def _page(wiki: Path, uid: str, name: str, *, sections: int, flagged: bool = False) -> Path:
    body = "Intro.\n\n" + "\n\n".join(
        f"## Section {i}\n\nDetail {i}. " + "Filler prose. " * 300 for i in range(sections)
    )
    flag = "needs_split: true\n" if flagged else ""
    path = wiki / f"{uid}-{name}.md"
    path.write_text(f"---\nuid: {uid}\ntype: project\nname: {name}\n{flag}---\n\n{body}\n")
    return path


class TestAC3SplitPass:
    def test_uid_approved_split_and_flag_cleared(self, tmp_path: Path) -> None:
        from athenaeum.oversize_split import split_pages

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        path = _page(wiki, "aaaa1111", "big", sections=3, flagged=True)
        (out,) = split_pages(wiki, uids={"aaaa1111"}, auto=False, dry_run=False)
        assert out.status == "split"
        meta, body = parse_frontmatter(path.read_text())
        assert "needs_split" not in meta
        assert len(list(wiki.glob("*.md"))) == 4  # hub + 3 children
        assert "Detail 1." not in body  # moved to a child

    def test_unapproved_flagged_page_is_only_reported(self, tmp_path: Path) -> None:
        from athenaeum.oversize_split import split_pages

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "aaaa1111", "big", sections=3, flagged=True)
        (out,) = split_pages(wiki, uids=set(), auto=True, dry_run=False)
        assert out.status == "not_approved"
        assert len(list(wiki.glob("*.md"))) == 1

    def test_auto_splits_only_over_hard_ceiling(self, tmp_path: Path) -> None:
        from athenaeum.oversize_split import split_pages

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "aaaa1111", "big", sections=3, flagged=True)
        cfg = {"librarian": {"oversize_hard_ceiling_chars": 5000}}
        (out,) = split_pages(wiki, uids=set(), auto=True, config=cfg, dry_run=False)
        assert out.status == "split"

    def test_dry_run_writes_nothing_and_headingless_is_reported(self, tmp_path: Path) -> None:
        from athenaeum.oversize_split import split_pages

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        a = _page(wiki, "aaaa1111", "big", sections=3, flagged=True)
        before = a.read_text()
        (out,) = split_pages(wiki, uids={"aaaa1111"}, auto=False, dry_run=True)
        assert out.status == "would_split" and a.read_text() == before
        flat = wiki / "bbbb2222-flat.md"
        flat.write_text(
            "---\nuid: bbbb2222\nname: flat\nneeds_split: true\n---\n\n" + "x " * 6000
        )
        results = split_pages(wiki, uids={"bbbb2222"}, auto=False, dry_run=False)
        outs = {o.uid: o.status for o in results}
        assert outs["bbbb2222"] == "no_heading"

    def test_cli_dry_run_prints_counts_only(self, tmp_path: Path, capsys) -> None:
        from athenaeum.cli import main

        root = tmp_path / "k"
        (root / "wiki").mkdir(parents=True)
        _page(root / "wiki", "aaaa1111", "secretname", sections=3, flagged=True)
        assert main(["split-oversize", "--path", str(root)]) == 0
        out = capsys.readouterr().out
        assert "candidates:" in out and "secretname" not in out


# --------------------------------------------------------------------------
# AC5
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestAC5RecoverParked:
    def _repo(self, tmp_path: Path) -> tuple[Path, Path, list[dict]]:
        root = tmp_path / "k"
        (root / "raw" / "drive").mkdir(parents=True)
        (root / "wiki").mkdir()
        subprocess.run(["git", "init", "-q", "-b", "develop", str(root)], check=True)
        f = root / "raw" / "drive" / "note.md"
        f.write_text("the parked observation")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "add")
        commit = _git(root, "rev-parse", "HEAD")
        f.unlink()
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "consume")
        pending = root / "wiki" / "_pending_questions.md"
        pending.write_text(
            "# Pending Questions\n\n"
            + _legacy_block("drive/note.md")
            + "\n\n---\n\n"
            + _legacy_block("drive/refetch.md")
        )
        ids = {p.source: p.id for p in parse_pending_questions(pending)}
        manifest = [
            {
                "question_id": ids["drive/note.md"],
                "conflict_type": "oversize_page",
                "entity": "Box",
                "raw_path": "raw/drive/note.md",
                "last_commit": commit,
            },
            {
                "question_id": ids["drive/refetch.md"],
                "conflict_type": "oversize_page",
                "entity": "Box",
                "raw_path": "raw/drive/refetch.md",
                "last_commit": None,
            },
        ]
        mpath = tmp_path / "manifest.json"
        mpath.write_text(json.dumps(manifest))
        return root, mpath, manifest

    def test_restore_then_resolve(self, tmp_path: Path, capsys) -> None:
        from athenaeum.cli import main

        root, mpath, _ = self._repo(tmp_path)
        base = ["--manifest", str(mpath), "--path", str(root)]

        assert main(["recover-parked", "restore", *base]) == 0  # dry run
        assert not (root / "raw" / "drive" / "note.md").exists()

        assert main(["recover-parked", "restore", *base, "--apply"]) == 0
        out = capsys.readouterr().out
        assert "restored:" in out and "needs_refetch:" in out and "Box" not in out
        restored = root / "raw" / "drive" / "note.md"
        assert restored.read_text() == "the parked observation"

        # not yet ingested -> nothing closes
        assert main(["recover-parked", "resolve", *base, "--apply"]) == 0
        assert len(parse_pending_questions(root / "wiki" / "_pending_questions.md")) == 2

        restored.unlink()  # the librarian consumed it
        assert main(["recover-parked", "resolve", *base, "--apply"]) == 0
        left = parse_pending_questions(root / "wiki" / "_pending_questions.md")
        assert [p.source for p in left] == ["drive/refetch.md"]  # untracked: left open
        archive = (root / "wiki" / "_pending_questions_archive.md").read_text()
        assert "re-ingested" in archive

    def test_restore_is_idempotent(self, tmp_path: Path, capsys) -> None:
        from athenaeum.cli import main

        root, mpath, _ = self._repo(tmp_path)
        base = ["--manifest", str(mpath), "--path", str(root), "--apply"]
        assert main(["recover-parked", "restore", *base]) == 0
        assert main(["recover-parked", "restore", *base]) == 0
        out = capsys.readouterr().out.split("=== ")[-1]
        assert "restored:             0" in out and "already_present:      1" in out

    def test_manifest_path_traversal_refused(self, tmp_path: Path, capsys) -> None:
        from athenaeum.cli import main

        root, mpath, manifest = self._repo(tmp_path)
        manifest[0]["raw_path"] = "raw/../../etc/passwd"
        mpath.write_text(json.dumps(manifest))
        rc = main(
            ["recover-parked", "restore", "--manifest", str(mpath), "--path", str(root), "--apply"]
        )
        assert rc == 1
        assert "raw/" in capsys.readouterr().err
