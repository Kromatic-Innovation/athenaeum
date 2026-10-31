# SPDX-License-Identifier: Apache-2.0
"""Tests for the attribution context checks C1-C3 (issue athenaeum#2215).

Child C of epic athenaeum#2192, layered onto athenaeum#2210's seam
(``pii_attribution.check_contact_attribution``). Covers the two child-C
indexes (inline-elsewhere, person-name) and the three checks they feed, in
isolation from the D1-D4 checks already covered by
``tests/test_pii_attribution.py``.

Synthetic fixtures only: every address uses ``example.org``/``example.com``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import pii, pii_attribution
from athenaeum.models import render_frontmatter

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_page(
    wiki_root: Path,
    *,
    uid: str,
    name: str,
    type_: str = "person",
    aliases: list[str] | None = None,
    body: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"uid": uid, "name": name, "type": type_}
    if aliases:
        meta["aliases"] = aliases
    slug = name.lower().replace(" ", "-")
    path = wiki_root / f"{uid}-{slug}.md"
    path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return path


def _write_contact_record(
    contacts_root: Path, filename: str, *, uid: str | None, emails: list[str]
) -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    listed = "".join(f"  - {address}\n" for address in emails)
    uid_line = f"uid: {uid}\n" if uid is not None else ""
    path = contacts_root / filename
    path.write_text(
        f"---\n{uid_line}pii: true\nemails:\n{listed}---\n\nContact record.\n",
        encoding="utf-8",
    )
    return path


def _context(
    tmp_path: Path, *, config: dict[str, object] | None = None
) -> pii_attribution.AttributionContext:
    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True, exist_ok=True)
    cfg = config if config is not None else EXCLUDED_CONFIG
    return pii_attribution.build_attribution_context(knowledge, cfg)


def _write_source(tmp_path: Path, text: str, name: str = "raw-source.md") -> str:
    """A plain raw-text source file (never a wiki page) for C3 fixtures —
    deliberately NOT a compiled entity page, since a page's own frontmatter
    co-locating several of its own values is a different scenario than C3's
    "cited source" (see this issue's final report for the storage_migrate.py
    self-citation finding)."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestC1InlineElsewhere:
    def test_fires_when_value_inline_on_another_uids_page_body(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(
            wiki_root,
            uid="uid-holder",
            name="Other Holder",
            body="Reach out via inline-c1@example.org for questions.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "inline-c1@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        c1 = [
            c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C1_INLINE_ELSEWHERE
        ]
        assert c1 and c1[0].uids == ("uid-holder",)
        assert "uid-holder" in verdict.candidates

    def test_does_not_fire_on_the_targets_own_page(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(
            wiki_root,
            uid="uid-target",
            name="Target Person",
            body="Reach out via inline-own@example.org for questions.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "inline-own@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id == pii_attribution.CHECK_C1_INLINE_ELSEWHERE for c in verdict.checks
        )

    def test_both_indexes_are_built_once_per_context_not_once_per_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Design §5.1: the per-run index is "built once ... never a
        per-token search". Counts actual invocations of the builder rather
        than inferring it from behavior, per this issue's dispatch brief."""
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-holder", name="Other Holder", body="x2@example.org\n")

        calls: list[int] = []
        real_builder = pii_attribution._build_child_c_indexes

        def counting_builder(entity_index: object, secret: bytes) -> object:
            calls.append(1)
            return real_builder(entity_index, secret)  # type: ignore[arg-type]

        monkeypatch.setattr(pii_attribution, "_build_child_c_indexes", counting_builder)

        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        assert len(calls) == 1

        for value in ("x2@example.org", "unrelated-1@example.org", "unrelated-2@example.org"):
            pii_attribution.check_contact_attribution(
                value,
                target_uid="uid-target",
                target_name="Target",
                target_type="person",
                source_ref=None,
                writer=pii_attribution.WRITER_OBSERVED_CONTACT,
                context=context,
            )
        assert len(calls) == 1


class TestC2LocalPartNamesSomeoneElse:
    @pytest.mark.parametrize(
        "local_part",
        ["nperson", "namesake.person", "namesakeperson"],
        ids=["flast", "first.last", "firstlast"],
    )
    def test_fires_on_each_abbreviated_form(self, tmp_path: Path, local_part: str) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Namesake Person")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            f"{local_part}@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        c2 = [
            c
            for c in verdict.checks
            if c.check_id == pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER
        ]
        assert c2 and c2[0].uids == ("uid-namesake",)

    def test_fires_on_full_name_form(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Namesake Quincy Person")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "namesakequincyperson@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert any(
            c.check_id == pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER for c in verdict.checks
        )

    def test_first_name_only_never_fires(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Namesake Person")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "namesake@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id == pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER for c in verdict.checks
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_local_part_with_no_name_signal_passes(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Namesake Person")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "random-token-9@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_matches_target_and_someone_else_passes(self, tmp_path: Path) -> None:
        """Issue AC: "a local part matching BOTH the target and someone
        else" — two different ``uid``s whose name forms collide on the same
        local part — passes, because the target's own match is among them."""
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-target", name="Jordan Smith")
        _write_page(wiki_root, uid="uid-other", name="Jordan Smith")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "jsmith@example.org",
            target_uid="uid-target",
            target_name="Jordan Smith",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id == pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER for c in verdict.checks
        )

    def test_inert_for_a_phone_value(self, tmp_path: Path) -> None:
        """No ``@`` means no local part — C2 must stay inert rather than
        treating the whole phone digit string as a "local part"."""
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Namesake Person")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "+1-555-0199",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id == pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER for c in verdict.checks
        )


class TestC3SourceContext:
    def test_label_hit(self, tmp_path: Path) -> None:
        source = _write_source(
            tmp_path,
            "Referred by Alex Example. Reach them at suspect-c3a@example.org for details.\n",
        )
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "suspect-c3a@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=source,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        c3 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT]
        assert c3 and c3[0].label == "label:referred by"

    def test_name_proximity_hit(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-colleague", name="Robin Colleague")
        source = _write_source(
            tmp_path,
            "Chatting with Robin Colleague earlier, reach suspect-c3b@example.org for the file.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "suspect-c3b@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=source,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        c3 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT]
        assert c3 and c3[0].label == "name-proximity"
        assert c3[0].uids == ("uid-colleague",)

    def test_name_proximity_excludes_the_targets_own_name(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-target", name="Target Person")
        source = _write_source(
            tmp_path,
            "Target Person asked us to note suspect-c3e@example.org for the file.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "suspect-c3e@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=source,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT for c in verdict.checks
        )

    def test_value_absent_fires(self, tmp_path: Path) -> None:
        source = _write_source(tmp_path, "Nothing relevant to contacts in this note.\n")
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "missing-c3c@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=source,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        c3 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT]
        assert c3 and c3[0].label == "value-absent"

    def test_source_missing_does_not_fire(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "nosource-c3d@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=str(tmp_path / "does-not-exist.md"),
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS
        assert not any(
            c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT for c in verdict.checks
        )

    def test_no_source_ref_at_all_does_not_fire(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "nosourceref@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_quoted_block_from_label_fires(self, tmp_path: Path) -> None:
        source = _write_source(
            tmp_path,
            "See note below.\n\n> From: suspect-c3f@example.org\n> Subject: intro\n",
        )
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "suspect-c3f@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=source,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        c3 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT]
        assert c3 and c3[0].label == f"label:{pii_attribution.QUOTED_BLOCK_LABEL}"

    def test_bare_from_outside_a_quoted_block_is_not_a_label_hit(self, tmp_path: Path) -> None:
        source = _write_source(
            tmp_path,
            "Notes from: an onboarding call. Contact plainvalue-c3g@example.org directly.\n",
        )
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "plainvalue-c3g@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=source,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_source_ref_as_dict_with_path_key(self, tmp_path: Path) -> None:
        source = _write_source(
            tmp_path, "cc suspect-c3h@example.org on all replies please.\n"
        )
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "suspect-c3h@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref={"path": source},
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        c3 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT]
        assert c3 and c3[0].label == "label:cc"

    def test_self_citation_guard_suppresses_a_hit_on_the_targets_own_page(
        self, tmp_path: Path
    ) -> None:
        """Positive control: a label sits well within range of the value,
        but *source_ref* is the TARGET's own entity page — C3 must not
        treat a page as context evidence about itself (athenaeum#2215)."""
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        own_page = _write_page(
            wiki_root,
            uid="uid-target",
            name="Target Person",
            body="Referred by Alex Example. Reach them at selfcite-c3i@example.org directly.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "selfcite-c3i@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=str(own_page),
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT for c in verdict.checks
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_guard_is_narrow_a_different_uids_page_still_fires(self, tmp_path: Path) -> None:
        """Negative control: the SAME shape (a label in range, source_ref a
        wiki page), but the page belongs to a DIFFERENT uid than the
        target — this must still fire normally, proving the guard above is
        not just "C3 never fires on a page" (athenaeum#2215)."""
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        other_page = _write_page(
            wiki_root,
            uid="uid-other",
            name="Other Person",
            body="Referred by Alex Example. Reach them at othercite-c3j@example.org directly.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "othercite-c3j@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=str(other_page),
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        c3 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_C3_SOURCE_CONTEXT]
        assert c3 and c3[0].label == "label:referred by"


class TestOrderingAndCompleteness:
    def test_c_checks_run_after_d_checks_and_evidence_is_complete(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Namesake Person")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts, "uid-namesake.md", uid="uid-namesake", emails=["nperson@example.org"]
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "nperson@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        fired_ids = [c.check_id for c in verdict.checks]
        assert pii_attribution.CHECK_D2_HELD_ELSEWHERE in fired_ids
        assert pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER in fired_ids
        # D-checks precede C-checks in the ordered tuple (design §5.1: "the
        # C-checks ... run after the D-checks").
        d_positions = [i for i, cid in enumerate(fired_ids) if cid.startswith("D")]
        c_positions = [i for i, cid in enumerate(fired_ids) if cid.startswith("C")]
        assert d_positions and c_positions
        assert max(d_positions) < min(c_positions)
        assert "uid-namesake" in verdict.candidates

    def test_evidence_never_carries_the_raw_value_for_c_checks(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(
            wiki_root,
            uid="uid-carrier",
            name="Carrier Person",
            body="carrier-evidence@example.org appears here too.\n",
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "carrier-evidence@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert any(
            c.check_id == pii_attribution.CHECK_C1_INLINE_ELSEWHERE for c in verdict.checks
        )
        for check in verdict.checks:
            assert "carrier-evidence@example.org" not in (check.label or "")
            for uid in check.uids:
                assert "carrier-evidence@example.org" not in uid

    def test_a_c_check_row_lands_in_the_attribution_log(self, tmp_path: Path) -> None:
        """Issue AC: "one fixture run shows C-check rows in the attribution
        log" — not just evidence on the in-memory verdict."""
        import json

        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-namesake", name="Logrow Person")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        pii_attribution.check_contact_attribution(
            "lperson@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        log_path = pii_attribution.default_attribution_log_path(context.contacts_root)
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert any(
            pii_attribution.CHECK_C2_LOCAL_PART_NAMES_OTHER in row["checks"] for row in rows
        )
