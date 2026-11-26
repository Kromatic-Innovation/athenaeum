# SPDX-License-Identifier: Apache-2.0
"""One retire command for any store, scoped to what the caller can access (issue athenaeum#3151).

Acceptance (issue athenaeum#3151):
  - one command retires a wiki page, a contact record and an off-corpus
    record by uid, each with a dry-run report and an ``--apply`` that
    snapshots, removes, reindexes and records the reason;
  - a caller without read on a store gets "not found"; with read but not
    write, a named refusal; an invalid declaration fails closed (read-only);
    one refusal refuses the whole run;
  - retiring a person page retires its contact record; retiring the only
    contact records of a live page needs ``--force-retire``;
  - ``--pageless`` lists the page-less records, retires them on ``--apply``,
    and ``contact-surface-lint`` then reports ``pageless_records: 0``;
  - no output, log line or commit message carries an unmasked address or
    phone number.

Every test runs on a fixture git repo under ``tmp_path``; every address and
number is invented.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest
import yaml

from athenaeum import cli, store_rights
from athenaeum.retire_targets import (
    ACCESS_HIDDEN,
    ACCESS_OK,
    ACCESS_REFUSED,
    OFF_CORPUS_RETIREMENT_LEDGER,
    STORE_CONTACT,
    STORE_OFF_CORPUS,
    STORE_PAGE,
    plan_pageless_retirement,
    plan_retirement,
    split_token,
    target_access,
)

ISS = "https://id.example.net"
EMAIL_LIVE = "dana.quill@example.org"
EMAIL_ORPHAN = "orphan.only@example.org"
EMAIL_SHARED = "shared.box@example.org"
PHONE = "+1 555 010 4477"
MASKED_ORPHAN = "o***@example.org"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _page(uid: str, name: str, ptype: str = "person") -> str:
    return f"---\nuid: {uid}\nname: {name}\ntype: {ptype}\n---\n\n{name} body.\n"


def _record(uid: str, *emails: str, phone: str | None = None) -> str:
    lines = ["---", f"uid: {uid}", "pii: true", "emails:"]
    lines += [f"  - '{e}'" for e in emails] or ["  []"]
    if phone:
        lines += ["phones:", f"  - '{phone}'"]
    return "\n".join(lines + ["---", "Synthetic.", ""])


def _decl(rights: list[str]) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "store_id": "st_q7m2xk4vwz5d",
            "roles": {"r": {"rights": rights, "members": [{"iss": ISS, "sub": "1"}]}},
        }
    )


@pytest.fixture(autouse=True)
def _fresh_rights():
    store_rights.reset_run_rights()
    yield
    store_rights.reset_run_rights()


@pytest.fixture
def kr(tmp_path: Path) -> Path:
    """A knowledge root with two person pages, an orphan record and an off-corpus record.

    - ``aaaa1111`` Alpha: live page, contact record (live email + phone).
    - ``bbbb2222`` Beta: live page, contact record (the shared email).
    - ``cccc3333``: page-less contact record (one unique email, one email
      also on Beta's live record).
    - ``dddd4444``: off-corpus record.
    """
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "aaaa1111-alpha.md").write_text(_page("aaaa1111", "Alpha"), encoding="utf-8")
    (wiki / "bbbb2222-beta.md").write_text(_page("bbbb2222", "Beta"), encoding="utf-8")
    contacts = root / "excluded"
    contacts.mkdir()
    (contacts / "aaaa1111.md").write_text(
        _record("aaaa1111", EMAIL_LIVE, phone=PHONE), encoding="utf-8"
    )
    (contacts / "bbbb2222.md").write_text(_record("bbbb2222", EMAIL_SHARED), encoding="utf-8")
    (contacts / "cccc3333.md").write_text(
        _record("cccc3333", EMAIL_ORPHAN, EMAIL_SHARED), encoding="utf-8"
    )
    off = tmp_path / "off-corpus-store"
    off.mkdir()
    (off / "dddd4444-claim.md").write_text(
        _page("dddd4444", "Off Corpus Claim", ptype="erasure-claim"), encoding="utf-8"
    )
    config = {
        "owner": {"asserter": {"iss": ISS, "sub": "1"}},
        "off_corpus": {"enabled": True, "adapter": "oc"},
        "storage": {
            "mapping": {"pii": "excluded", "erasure-claim": "oc"},
            "adapters": {
                "oc": {
                    "backing_store": "markdown",
                    "surface_root": str(off),
                    "corpus_policy": {
                        "embedded": False,
                        "recallable": True,
                        "merge_eligible": False,
                    },
                }
            },
        },
    }
    (root / "athenaeum.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Retire Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


def _config(kr: Path) -> dict:
    from athenaeum.config import load_config

    return load_config(kr)


def _retire(kr: Path, tmp_path: Path, *extra: str) -> int:
    return cli.main(
        [
            "retire",
            "--path",
            str(kr),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--backend",
            "fts5",
            "--reason",
            "stale test data",
            *extra,
        ]
    )


def _declare(surface_root: Path, text: str) -> None:
    (surface_root / "_store").mkdir(parents=True, exist_ok=True)
    (surface_root / "_store" / "access.yaml").write_text(text, encoding="utf-8")


class TestTokens:
    def test_prefix_pins_store(self) -> None:
        assert split_token("contact:ab12") == ("contact", "ab12")
        assert split_token("off-corpus:ab12") == ("off-corpus", "ab12")
        assert split_token("page:ab12") == ("page", "ab12")

    def test_unknown_prefix_is_part_of_the_uid(self) -> None:
        assert split_token("other:ab12") == (None, "other:ab12")
        assert split_token("ab12") == (None, "ab12")


class TestResolution:
    def test_each_store_resolves(self, kr: Path) -> None:
        plan = plan_retirement(kr, ["aaaa1111", "cccc3333", "dddd4444"], _config(kr))
        assert not plan.blocked, (plan.unresolved, plan.refused, plan.errors)
        stores = {t.uid: t.store for t in plan.targets}
        assert stores == {
            "aaaa1111": STORE_PAGE,
            "cccc3333": STORE_CONTACT,
            "dddd4444": STORE_OFF_CORPUS,
        }
        page = plan.of_store(STORE_PAGE)[0]
        assert page.cascade_records == 1

    def test_unknown_uid_blocks(self, kr: Path) -> None:
        plan = plan_retirement(kr, ["aaaa1111", "zzzz9999"], _config(kr))
        assert plan.blocked
        assert [e.uid for e in plan.unresolved] == ["zzzz9999"]

    def test_uid_on_page_and_off_corpus_is_ambiguous(self, kr: Path, tmp_path: Path) -> None:
        (tmp_path / "off-corpus-store" / "dup.md").write_text(
            _page("aaaa1111", "Dup", ptype="erasure-claim"), encoding="utf-8"
        )
        plan = plan_retirement(kr, ["aaaa1111"], _config(kr))
        assert [e.reason for e in plan.unresolved] == ["ambiguous"]
        pinned = plan_retirement(kr, ["off-corpus:aaaa1111"], _config(kr))
        assert not pinned.blocked
        assert pinned.targets[0].store == STORE_OFF_CORPUS


class TestOneCommandEveryStore:
    def test_dry_run_writes_nothing(self, kr: Path, tmp_path: Path, capsys) -> None:
        head = _git(kr, "rev-parse", "HEAD").stdout
        code = _retire(kr, tmp_path, "--uids", "aaaa1111", "cccc3333", "dddd4444")
        assert code == 2
        assert _git(kr, "rev-parse", "HEAD").stdout == head
        assert (kr / "excluded" / "cccc3333.md").exists()
        assert (tmp_path / "off-corpus-store" / "dddd4444-claim.md").exists()
        out = capsys.readouterr().out
        assert "pages: 1" in out and "contact records: 1" in out
        assert "off-corpus records: 1" in out and "cannot be recovered" in out

    def test_apply_snapshots_removes_reindexes_and_records_reason(
        self, kr: Path, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[Path] = []
        monkeypatch.setattr(
            "athenaeum._cmd_retire.rebuild_recall_index",
            lambda root, cfg, args: calls.append(root),
        )
        invalidated: list[Path] = []
        import athenaeum.retire_targets as rt

        real = rt.invalidate_cached_record

        def _spy(contacts_root, cache_dir, path):
            invalidated.append(Path(path))
            return real(contacts_root, cache_dir, path)

        monkeypatch.setattr(rt, "invalidate_cached_record", _spy)
        code = _retire(kr, tmp_path, "--uids", "aaaa1111", "cccc3333", "dddd4444", "--apply")
        assert code == 0

        # Page: gone, snapshot recoverable, recall index rebuilt.
        assert not (kr / "wiki" / "aaaa1111-alpha.md").exists()
        assert calls == [kr]
        # Contact record: gone, recoverable from the snapshot, index updated.
        assert not (kr / "excluded" / "cccc3333.md").exists()
        assert invalidated == [(kr / "excluded" / "cccc3333.md").resolve()]
        log = _git(kr, "log", "--format=%H %s").stdout.splitlines()
        removal = next(line for line in log if "contact record(s)" in line and "retire 1" in line)
        sha = removal.split()[0]
        recovered = _git(kr, "show", f"{sha}~1:excluded/cccc3333.md").stdout
        assert "uid: cccc3333" in recovered
        # Reason recorded verbatim in both archive commits.
        assert sum("stale test data" in line for line in log) == 2
        # Off-corpus: erased, reason in its ledger, no git trace.
        off = tmp_path / "off-corpus-store"
        assert not (off / "dddd4444-claim.md").exists()
        rows = [
            json.loads(line)
            for line in (off / OFF_CORPUS_RETIREMENT_LEDGER).read_text().splitlines()
        ]
        assert rows[0]["uid"] == "dddd4444" and rows[0]["reason"] == "stale test data"

    def test_retire_pages_alias_still_works(self, kr: Path, tmp_path: Path, capsys) -> None:
        code = cli.main(
            ["retire-pages", "--path", str(kr), "--uids", "aaaa1111", "--reason", "x"]
        )
        assert code == 2
        assert "deprecated alias" in capsys.readouterr().err


class TestAccess:
    def test_no_read_right_reads_as_not_found(self, kr: Path, tmp_path: Path, capsys) -> None:
        off = tmp_path / "off-corpus-store"
        _declare(off, _decl(["propose"]))
        assert target_access(off / "dddd4444-claim.md", kr, _config(kr))[0] == ACCESS_HIDDEN
        code = _retire(kr, tmp_path, "--uids", "dddd4444", "--apply")
        assert code == 1
        err = capsys.readouterr().err
        assert "dddd4444: unknown" in err
        assert "off-corpus" not in err and "refused" not in err
        assert (off / "dddd4444-claim.md").exists()

    def test_hidden_match_does_not_make_a_uid_ambiguous(self, kr: Path, tmp_path: Path) -> None:
        off = tmp_path / "off-corpus-store"
        (off / "dup.md").write_text(_page("aaaa1111", "Dup", ptype="erasure-claim"))
        _declare(off, _decl(["propose"]))
        plan = plan_retirement(kr, ["aaaa1111"], _config(kr))
        assert not plan.blocked
        assert plan.targets[0].store == STORE_PAGE

    def test_read_without_write_is_refused_by_name_and_nothing_runs(
        self, kr: Path, tmp_path: Path, capsys
    ) -> None:
        _declare(kr / "excluded", _decl(["read"]))
        _git(kr, "add", "-A")
        _git(kr, "commit", "-m", "declare")
        head = _git(kr, "rev-parse", "HEAD").stdout
        assert target_access(kr / "excluded" / "cccc3333.md", kr, _config(kr))[0] == ACCESS_REFUSED
        # The page is writable, but the run names a refused contact record too.
        code = _retire(kr, tmp_path, "--uids", "bbbb2222", "cccc3333", "--apply")
        assert code == 1
        err = capsys.readouterr().err
        assert "cccc3333 (contact): refused" in err
        assert "does not grant this install compile" in err
        assert "nothing retired" in err
        assert _git(kr, "rev-parse", "HEAD").stdout == head
        assert (kr / "wiki" / "bbbb2222-beta.md").exists()

    def test_invalid_declaration_fails_closed(self, kr: Path, tmp_path: Path, capsys) -> None:
        _declare(kr / "excluded", "version: 1\nnot_a_key: true\n")
        assert target_access(kr / "excluded" / "cccc3333.md", kr, _config(kr))[0] == ACCESS_REFUSED
        code = _retire(kr, tmp_path, "--uids", "cccc3333", "--apply")
        assert code == 1
        assert "cccc3333 (contact): refused" in capsys.readouterr().err
        assert (kr / "excluded" / "cccc3333.md").exists()

    def test_full_rights_and_no_declaration_both_allow(self, kr: Path, tmp_path: Path) -> None:
        path = kr / "excluded" / "cccc3333.md"
        assert target_access(path, kr, _config(kr))[0] == ACCESS_OK
        _declare(kr / "excluded", _decl(["read", "compile"]))
        assert target_access(path, kr, _config(kr))[0] == ACCESS_OK

    def test_page_cascade_skipped_when_contacts_hidden(self, kr: Path, tmp_path: Path) -> None:
        _declare(kr / "excluded", _decl(["propose"]))
        _git(kr, "add", "-A")
        _git(kr, "commit", "-m", "declare")
        code = _retire(kr, tmp_path, "--uids", "aaaa1111", "--apply")
        assert code == 0
        assert not (kr / "wiki" / "aaaa1111-alpha.md").exists()
        # The unreadable store was never written to.
        assert (kr / "excluded" / "aaaa1111.md").exists()


class TestCascades:
    def test_retiring_a_page_retires_its_contact_record(self, kr: Path, tmp_path: Path) -> None:
        from athenaeum import pii

        code = _retire(kr, tmp_path, "--uids", "aaaa1111", "--apply")
        assert code == 0
        assert pii.resolve_contact_record_for_uid(kr / "excluded", "aaaa1111") is None

    def test_only_records_of_a_live_page_need_force(
        self, kr: Path, tmp_path: Path, capsys
    ) -> None:
        code = _retire(kr, tmp_path, "--uids", "contact:bbbb2222", "--apply")
        assert code == 1
        assert "only contact records of live page" in capsys.readouterr().err
        assert (kr / "excluded" / "bbbb2222.md").exists()

        code = _retire(kr, tmp_path, "--uids", "contact:bbbb2222", "--force-retire", "--apply")
        assert code == 0
        assert not (kr / "excluded" / "bbbb2222.md").exists()
        # The page it belongs to is never deleted.
        assert (kr / "wiki" / "bbbb2222-beta.md").exists()
        assert "Forced: --force-retire" in _git(kr, "log", "-1", "--format=%B").stdout


class TestPageless:
    def test_lists_then_retires_and_lint_reads_zero(
        self, kr: Path, tmp_path: Path, capsys
    ) -> None:
        plan = plan_pageless_retirement(kr, _config(kr))
        assert [t.uid for t in plan.targets] == ["cccc3333"]
        summary = plan.pageless[0]
        assert summary.records == 1
        assert summary.masked_values[0] == MASKED_ORPHAN
        assert summary.on_live_record == [False, True]
        assert summary.origin_commit

        assert _retire(kr, tmp_path, "--pageless") == 2
        out = capsys.readouterr().out
        assert "cccc3333: 1 record(s)" in out
        assert "values: 2" in out and "also on a live record" in out
        assert f"origin commit: {summary.origin_commit}" in out

        assert _retire(kr, tmp_path, "--pageless", "--apply") == 0
        assert not (kr / "excluded" / "cccc3333.md").exists()
        capsys.readouterr()
        assert cli.main(["contact-surface-lint", "--path", str(kr), "--json"]) in (0, 2)
        report = json.loads(capsys.readouterr().out)
        assert report["counts"]["pageless_records"] == 0

    def test_nothing_pageless_is_a_clean_no_op(self, kr: Path, tmp_path: Path, capsys) -> None:
        (kr / "excluded" / "cccc3333.md").unlink()
        assert _retire(kr, tmp_path, "--pageless", "--apply") == 0
        assert "Nothing selected" in capsys.readouterr().out


class TestNoValuesLeak:
    def test_no_address_or_phone_in_output_logs_or_commits(
        self, kr: Path, tmp_path: Path, capsys, caplog, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "athenaeum._cmd_retire.rebuild_recall_index", lambda root, cfg, args: None
        )
        caplog.set_level(logging.DEBUG)
        texts: list[str] = []
        assert _retire(kr, tmp_path, "--pageless") == 2
        texts.append("".join(capsys.readouterr()))
        assert _retire(kr, tmp_path, "--pageless", "--apply") == 0
        assert _retire(kr, tmp_path, "--uids", "aaaa1111", "contact:bbbb2222") == 1
        assert (
            _retire(kr, tmp_path, "--uids", "aaaa1111", "contact:bbbb2222", "--force-retire",
                    "--apply")
            == 0
        )
        texts.append("".join(capsys.readouterr()))
        texts.append(caplog.text)
        texts.append(_git(kr, "log", "--format=%B").stdout)
        blob = "\n".join(texts)
        assert MASKED_ORPHAN in blob
        for raw in (EMAIL_LIVE, EMAIL_ORPHAN, EMAIL_SHARED, PHONE, "orphan.only", "dana.quill"):
            assert raw not in blob, raw
