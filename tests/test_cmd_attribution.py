# SPDX-License-Identifier: Apache-2.0
"""``athenaeum attribution show|place`` (athenaeum#2212).

Design ``docs/design/attribution-verification.md`` §8.1 names both commands;
§9.4 names ``place --as own`` as the rollback for a value the deterministic
checks withheld.

Driven through ``cli.main`` rather than the handlers directly, so the
argparse wiring, the ``--as`` parsing and the exit codes are all exercised
on the real path a human takes. No model is reachable from either command:
``place`` files a HUMAN answer, which by design carries no model id, prompt
sha or calibrated probability, so there is nothing to mock.

Cases: ``TestShow`` (the sanctioned read and its failure paths),
``TestPlaceAsOwn``/``TestPlaceAsOther``/``TestPlaceAsAlias``/
``TestPlaceAsMerge`` (one class per ``--as`` form), ``TestPlaceRejections``
(every rejection path), and ``TestPlaceProvenance`` (§8.1's "no calibrated
probability").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii, pii_attribution
from athenaeum.cli import main
from athenaeum.models import parse_frontmatter, render_frontmatter

_VALUE = "introducer@example.org"
_TARGET_UID = "uid-target"
_TARGET_NAME = "Target Person"
_OWNER_UID = "uid-owner"
_OWNER_NAME = "Owner Person"
_EXCLUDED_CONFIG = "storage:\n  mapping:\n    pii: excluded\n"


def _write_page(wiki_root: Path, *, uid: str, name: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(
        render_frontmatter({"uid": uid, "name": name, "type": "person"})
        + "\nSynthetic page.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    """A knowledge root with two pages, an excluded surface and one suspect."""
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    _write_page(wiki, uid=_TARGET_UID, name=_TARGET_NAME)
    _write_page(wiki, uid=_OWNER_UID, name=_OWNER_NAME)
    (root / "athenaeum.yaml").write_text(_EXCLUDED_CONFIG, encoding="utf-8")
    context = pii_attribution.build_attribution_context(
        root, {"storage": {"mapping": {"pii": "excluded"}}}
    )
    verdict = pii_attribution.AttributionVerdict(
        disposition=pii_attribution.DISPOSITION_SUSPECT,
        checks=(
            pii_attribution.CheckEvidence(
                check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE, uids=(_OWNER_UID,)
            ),
        ),
        candidates=(_OWNER_UID,),
        value_key=pii_attribution.value_key_for(context, _VALUE),
    )
    pii_attribution.park_suspect(
        context,
        _VALUE,
        verdict,
        target_uid=_TARGET_UID,
        target_name=_TARGET_NAME,
        writer=pii_attribution.WRITER_OBSERVED_CONTACT,
        source_ref="raw/intake/note.md",
        shadow=True,
    )
    return root


def _suspect_id(knowledge_root: Path) -> str:
    context = pii_attribution.build_attribution_context(
        knowledge_root, {"storage": {"mapping": {"pii": "excluded"}}}
    )
    return pii_attribution.suspect_id_for(
        pii_attribution.value_key_for(context, _VALUE), _TARGET_UID
    )


def _contacts_root(knowledge_root: Path) -> Path:
    return pii.contacts_surface_root(
        knowledge_root, {"storage": {"mapping": {"pii": "excluded"}}}
    )


def _record_for(knowledge_root: Path, uid: str) -> dict[str, Any]:
    index = pii.ExcludedRecordIndex(_contacts_root(knowledge_root))
    path = index.by_uid(uid)
    if path is None:
        return {}
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta if isinstance(meta, dict) else {}


def _place(knowledge_root: Path, suspect_id: str, answer: str, *extra: str) -> int:
    return main(
        [
            "attribution",
            "place",
            suspect_id,
            "--path",
            str(knowledge_root),
            "--as",
            answer,
            *extra,
        ]
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShow:
    def test_prints_the_raw_value(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["attribution", "show", _suspect_id(knowledge), "--path", str(knowledge)])
        out = capsys.readouterr().out
        assert rc == 0
        assert _VALUE in out
        assert _TARGET_UID in out
        assert _TARGET_NAME in out
        assert pii_attribution.CHECK_D2_HELD_ELSEWHERE in out

    def test_json_mode(self, knowledge: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "attribution",
                "show",
                _suspect_id(knowledge),
                "--path",
                str(knowledge),
                "--json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["value"] == _VALUE
        assert payload["target_uid"] == _TARGET_UID
        assert payload["candidates"] == [_OWNER_UID]
        assert payload["shadow"] is True

    def test_unknown_suspect_exits_one(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "attribution",
                "show",
                pii_attribution.suspect_id_for("a" * 64, "uid-nobody"),
                "--path",
                str(knowledge),
            ]
        )
        assert rc == 1
        assert "no parked suspect" in capsys.readouterr().err

    def test_malformed_suspect_id_exits_one(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["attribution", "show", "../../etc/passwd", "--path", str(knowledge)])
        assert rc == 1
        assert "is not a suspect id" in capsys.readouterr().err

    def test_unreadable_surface_exits_one_rather_than_reporting_absence(
        self, knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "attribution",
                "show",
                _suspect_id(knowledge),
                "--path",
                str(knowledge),
                "--contacts-root",
                str(tmp_path / "not-a-surface"),
            ]
        )
        assert rc == 1
        assert "not readable" in capsys.readouterr().err

    def test_no_subcommand_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["attribution"])
        assert rc == 2
        assert "usage: athenaeum attribution" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# place -- one class per --as form
# ---------------------------------------------------------------------------


class TestPlaceAsOwn:
    def test_files_on_the_target_as_unclassified(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), "own")
        out = capsys.readouterr().out
        assert rc == 0
        assert f"own -> {pii.PLACEMENT_FILED}" in out
        meta = _record_for(knowledge, _TARGET_UID)
        assert _VALUE in meta["emails"]
        assert (
            pii.classification_for_value(meta, _VALUE).usage_class
            == pii.USAGE_CLASS_UNCLASSIFIED
        )

    def test_this_is_the_documented_rollback_for_a_withheld_value(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Design §9.4: a value the deterministic checks withheld is filed on
        the target it was withheld from by this exact command.

        A relay local part is a D1-narrow value — the one class enforced in
        every mode — so an injected re-check would refuse it forever. The
        human answer must still land, and the command says so on stderr
        rather than silently.
        """
        context = pii_attribution.build_attribution_context(
            knowledge, {"storage": {"mapping": {"pii": "excluded"}}}
        )
        relay = "noreply@example.org"
        verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_NON_PERSONAL,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_D1_NARROW, label="noreply"
                ),
            ),
            value_key=pii_attribution.value_key_for(context, relay),
        )
        pii_attribution.park_suspect(
            context,
            relay,
            verdict,
            target_uid=_TARGET_UID,
            target_name=_TARGET_NAME,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=False,
        )
        suspect_id = pii_attribution.suspect_id_for(verdict.value_key, _TARGET_UID)
        rc = _place(knowledge, suspect_id, "own")
        captured = capsys.readouterr()
        assert rc == 0
        assert "still say 'non-personal'" in captured.err
        assert relay in _record_for(knowledge, _TARGET_UID)["emails"]


class TestPlaceAsOther:
    def test_re_attributes_to_the_named_uid_and_leaves_the_target_alone(
        self, knowledge: Path
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), f"other:{_OWNER_UID}")
        assert rc == 0
        assert _VALUE in _record_for(knowledge, _OWNER_UID)["emails"]
        assert _VALUE not in (_record_for(knowledge, _TARGET_UID).get("emails") or [])

    def test_provenance_names_the_original_target_and_the_human(
        self, knowledge: Path
    ) -> None:
        _place(knowledge, _suspect_id(knowledge), f"other:{_OWNER_UID}")
        source = _record_for(knowledge, _OWNER_UID)["field_sources"]["emails"][0]["source"]
        assert source["type"] == "attribution"
        assert source["original_target"] == _TARGET_UID
        assert source["original_source"] == "raw/intake/note.md"
        assert source["verdict"] == "other-known"
        assert source["decided_by"] == "human"


class TestPlaceAsAlias:
    @pytest.mark.parametrize("relation", list(pii.IDENTIFIER_RELATIONS))
    def test_every_relation_files_into_alt_emails_with_a_relation_entry(
        self, knowledge: Path, relation: str
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), f"alias:{relation}")
        assert rc == 0
        meta = _record_for(knowledge, _TARGET_UID)
        assert _VALUE in meta["alt_emails"]
        entries = meta[pii.IDENTIFIER_RELATION_FIELD]
        assert len(entries) == 1
        assert entries[0]["relation"] == relation
        assert entries[0]["decided_by"] == "human"
        assert entries[0]["identifier"] == pii.normalize_identifier(_VALUE)


class TestPlaceAsMerge:
    def test_proposes_a_merge_and_writes_no_contact_value(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), f"merge:{_OWNER_UID}")
        out = capsys.readouterr().out
        assert rc == 0
        assert pii.PLACEMENT_MERGE_PROPOSED in out
        merges = (knowledge / "wiki" / "_pending_merges.md").read_text(encoding="utf-8")
        assert _OWNER_NAME in merges
        # No contact value was written anywhere on the surface.
        assert _VALUE not in (_record_for(knowledge, _TARGET_UID).get("emails") or [])
        assert _VALUE not in (_record_for(knowledge, _OWNER_UID).get("emails") or [])

    def test_the_merge_block_never_holds_the_raw_value(self, knowledge: Path) -> None:
        """``_pending_merges.md`` lives in the wiki (design §3)."""
        _place(knowledge, _suspect_id(knowledge), f"merge:{_OWNER_UID}")
        merges = (knowledge / "wiki" / "_pending_merges.md").read_text(encoding="utf-8")
        assert _VALUE not in merges
        assert pii.mask_contact_address(_VALUE) in merges
        assert pii.find_inline_emails(merges) == []

    def test_the_block_names_both_pages_as_sources(self, knowledge: Path) -> None:
        _place(knowledge, _suspect_id(knowledge), f"merge:{_OWNER_UID}")
        merges = (knowledge / "wiki" / "_pending_merges.md").read_text(encoding="utf-8")
        assert f"{_TARGET_UID}-target-person.md" in merges
        assert f"{_OWNER_UID}-owner-person.md" in merges


# ---------------------------------------------------------------------------
# place -- rejections
# ---------------------------------------------------------------------------


class TestPlaceRejections:
    def _nothing_was_written(self, knowledge: Path) -> None:
        assert _record_for(knowledge, _TARGET_UID) == {}
        assert _record_for(knowledge, _OWNER_UID) == {}

    @pytest.mark.parametrize(
        "answer",
        ["nonsense", "other", "alias", "merge", "own:uid-x", "other:", "alias:"],
    )
    def test_malformed_as_form_exits_one(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str], answer: str
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), answer)
        assert rc == 1
        err = capsys.readouterr().err
        assert "--as" in err or "unknown" in err
        self._nothing_was_written(knowledge)

    def test_unknown_relation_exits_one_and_lists_the_vocabulary(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), "alias:best-friend")
        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown identifier relation" in err
        for relation in pii.IDENTIFIER_RELATIONS:
            assert relation in err
        self._nothing_was_written(knowledge)

    @pytest.mark.parametrize("prefix", ["other", "merge"])
    def test_unknown_uid_exits_one(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str], prefix: str
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), f"{prefix}:uid-does-not-exist")
        assert rc == 1
        assert "resolves to no wiki page" in capsys.readouterr().err
        self._nothing_was_written(knowledge)

    def test_unknown_suspect_exits_one(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _place(
            knowledge, pii_attribution.suspect_id_for("b" * 64, "uid-nobody"), "own"
        )
        assert rc == 1
        assert "no parked suspect" in capsys.readouterr().err
        self._nothing_was_written(knowledge)

    def test_malformed_suspect_id_exits_one(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _place(knowledge, "not-a-suspect-id", "own")
        assert rc == 1
        assert "is not a suspect id" in capsys.readouterr().err

    def test_place_refuses_when_the_run_lock_is_held(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``place`` WRITES, so it takes the run lock the way every other
        writing ``_cmd_*`` module does (``docs/reference/exit-codes.md``)."""
        from athenaeum._cli_shared import EXIT_LOCK_HELD
        from athenaeum.runlock import RunLock

        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        try:
            rc = _place(knowledge, _suspect_id(knowledge), "own")
        finally:
            lock.release()
        assert rc == EXIT_LOCK_HELD
        assert "holds the lock" in capsys.readouterr().err
        self._nothing_was_written(knowledge)

    def test_show_does_not_require_the_lock(self, knowledge: Path) -> None:
        """A read must not be blocked by a concurrent writer."""
        from athenaeum.runlock import RunLock

        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        try:
            rc = main(
                ["attribution", "show", _suspect_id(knowledge), "--path", str(knowledge)]
            )
        finally:
            lock.release()
        assert rc == 0

    def test_as_is_required(self, knowledge: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "attribution",
                    "place",
                    _suspect_id(knowledge),
                    "--path",
                    str(knowledge),
                ]
            )
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# place -- provenance (design §8.1)
# ---------------------------------------------------------------------------


class TestPlaceProvenance:
    def test_no_calibrated_probability_no_model_and_no_prompt_sha(
        self, knowledge: Path
    ) -> None:
        """Design §8.1: a human answer is filed "with ``decided_by: human``
        in the provenance and no calibrated probability" -- and by the same
        reasoning no model id and no prompt sha, since there was no model
        call to attribute it to. Omitted, not written as nulls: a
        ``calibrated_p: null`` would read as a measured-and-zero confidence.
        """
        _place(knowledge, _suspect_id(knowledge), "own")
        source = _record_for(knowledge, _TARGET_UID)["field_sources"]["emails"][0][
            "source"
        ]
        assert source["decided_by"] == "human"
        assert "calibrated_p" not in source
        assert "model" not in source
        assert "prompt_sha256" not in source

    def test_the_writer_is_the_re_attribution_id(self, knowledge: Path) -> None:
        _place(knowledge, _suspect_id(knowledge), "own")
        source = _record_for(knowledge, _TARGET_UID)["field_sources"]["emails"][0][
            "source"
        ]
        assert source["writer"] == pii_attribution.WRITER_RE_ATTRIBUTION

    def test_a_human_placement_never_writes_observed(self, knowledge: Path) -> None:
        _place(knowledge, _suspect_id(knowledge), "own")
        meta = _record_for(knowledge, _TARGET_UID)
        for entry in meta.get(pii.CONTACT_CLASSIFICATION_FIELD) or []:
            assert entry.get("usage_class") != pii.USAGE_CLASS_OBSERVED

    def test_json_mode_reports_the_outcome(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = _place(knowledge, _suspect_id(knowledge), "own", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["verdict"] == "own"
        assert payload["outcome"] == pii.PLACEMENT_FILED
        assert payload["placed_uid"] == _TARGET_UID
        assert payload["field"] == "emails"
        assert payload["usage_class"] == pii.USAGE_CLASS_UNCLASSIFIED
