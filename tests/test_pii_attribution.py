# SPDX-License-Identifier: Apache-2.0
"""Tests for the contact-value attribution seam (issue athenaeum#2210).

Covers the seam itself (``AttributionContext``/``AttributionVerdict``,
checks D1-D4, excluded-surface storage) in isolation. The four writer call
sites (``pii.record_observed_contact_value``, the corrections list-add
branch, ``storage_migrate.plan_pii_migration``, ``contact_record_fold``) each
have their own observe-mode / D1-narrow-enforcement tests in their own
module's test file — see ``tests/test_correspondent_contact_record.py``,
``tests/test_corrections.py``, ``tests/test_storage_migrate_pii.py``,
``tests/test_contact_record_fold.py``.

Synthetic fixtures only: every address uses ``example.org``/``example.com``.
"""

from __future__ import annotations

import json
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
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {"uid": uid, "name": name, "type": type_}
    if aliases:
        meta["aliases"] = aliases
    slug = name.lower().replace(" ", "-")
    path = wiki_root / f"{uid}-{slug}.md"
    path.write_text(render_frontmatter(meta) + "\n", encoding="utf-8")
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
    tmp_path: Path,
    *,
    config: dict[str, object] | None = None,
    wiki_pages: bool = True,
) -> pii_attribution.AttributionContext:
    knowledge = tmp_path / "knowledge"
    if wiki_pages:
        (knowledge / "wiki").mkdir(parents=True, exist_ok=True)
    cfg = config if config is not None else EXCLUDED_CONFIG
    return pii_attribution.build_attribution_context(knowledge, cfg)


class TestVerdictShape:
    def test_pass_has_no_checks_or_candidates(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "alex@example.org",
            target_uid="uid-alex",
            target_name="Alex Example",
            target_type="person",
            source_ref="raw/intake/x.md",
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS
        assert verdict.checks == ()
        assert verdict.candidates == ()
        assert verdict.value_key

    def test_verdict_is_frozen(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "alex@example.org",
            target_uid="uid-alex",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        with pytest.raises(AttributeError):
            verdict.disposition = "suspect"  # type: ignore[misc]

    def test_evidence_never_carries_the_raw_value(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "noreply@example.org",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        for check in verdict.checks:
            assert "noreply@example.org" not in (check.label or "")


class TestD1Narrow:
    def test_relay_local_prefix_fires(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "noreply@example.org",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_NON_PERSONAL
        assert pii_attribution.is_d1_narrow(verdict)
        assert verdict.checks[0].check_id == pii_attribution.CHECK_D1_NARROW

    def test_service_address_fires(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "git@github.com",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert pii_attribution.is_d1_narrow(verdict)

    def test_operator_address_on_non_owner_page_is_non_personal(self, tmp_path: Path) -> None:
        config = {
            **EXCLUDED_CONFIG,
            "owner": {"uid": "uid-owner", "emails": ["owner@example.org"]},
        }
        context = _context(tmp_path, config=config)
        verdict = pii_attribution.check_contact_attribution(
            "owner@example.org",
            target_uid="uid-someone-else",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert pii_attribution.is_d1_narrow(verdict)

    def test_operator_address_on_the_owners_own_page_passes(self, tmp_path: Path) -> None:
        config = {
            **EXCLUDED_CONFIG,
            "owner": {"uid": "uid-owner", "emails": ["owner@example.org"]},
        }
        context = _context(tmp_path, config=config)
        verdict = pii_attribution.check_contact_attribution(
            "owner@example.org",
            target_uid="uid-owner",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_operator_mailer_domain_on_non_owner_page(self, tmp_path: Path) -> None:
        config = {
            **EXCLUDED_CONFIG,
            "librarian": {
                "attribution": {"operator_mailer_domains": ["mailer.example.org"]}
            },
        }
        context = _context(tmp_path, config=config)
        verdict = pii_attribution.check_contact_attribution(
            "updates@mailer.example.org",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert pii_attribution.is_d1_narrow(verdict)

    def test_configured_relay_local_prefix_extends_the_builtins(self, tmp_path: Path) -> None:
        config = {
            **EXCLUDED_CONFIG,
            "librarian": {"attribution": {"relay_local_prefixes": ["campaign-"]}},
        }
        context = _context(tmp_path, config=config)
        verdict = pii_attribution.check_contact_attribution(
            "campaign-winter@example.org",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert pii_attribution.is_d1_narrow(verdict)

    def test_owner_configured_by_name_alone_still_matches_their_own_page(
        self, tmp_path: Path
    ) -> None:
        """Seer PR review finding (athenaeum#2210): an owner configured with
        no ``uid`` (emails/aliases only) must still be recognized as the
        owner on THEIR OWN page — identified here by name, since
        ``owner_uid`` is empty and can never equal any ``target_uid``.
        Before the fix, this fired D1-narrow on the owner's own address on
        their own page, exactly like any stranger's page."""
        config = {
            **EXCLUDED_CONFIG,
            "owner": {
                "aliases": ["Tristan Kromer"],
                "emails": ["owner@example.org"],
            },
        }
        context = _context(tmp_path, config=config)
        verdict = pii_attribution.check_contact_attribution(
            "owner@example.org",
            target_uid="uid-tristan",
            target_name="Tristan Kromer",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

        # The guard still fires for the SAME address on a DIFFERENT page.
        verdict_elsewhere = pii_attribution.check_contact_attribution(
            "owner@example.org",
            target_uid="uid-someone-else",
            target_name="Someone Else",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert pii_attribution.is_d1_narrow(verdict_elsewhere)


class TestD1Domain:
    def test_relay_domain_is_non_personal_but_not_narrow(self, tmp_path: Path) -> None:
        config = {
            **EXCLUDED_CONFIG,
            "librarian": {"attribution": {"relay_domains": ["relay.example.org"]}},
        }
        context = _context(tmp_path, config=config)
        verdict = pii_attribution.check_contact_attribution(
            "someone@relay.example.org",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_NON_PERSONAL
        assert not pii_attribution.is_d1_narrow(verdict)
        assert verdict.checks[0].check_id == pii_attribution.CHECK_D1_DOMAIN


class TestD1Role:
    def test_role_local_part_matching_own_domain_passes(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts, "uid-acme-rep.md", uid="uid-acme-rep", emails=["rep@example.org"]
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "info@example.org",
            target_uid="uid-acme-rep",
            target_name="ACME Rep",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS

    def test_role_local_part_on_unrelated_domain_is_suspect(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "info@other.example.com",
            target_uid="uid-x",
            target_name="Someone",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        assert any(c.check_id == pii_attribution.CHECK_D1_ROLE for c in verdict.checks)
        assert not pii_attribution.is_d1_narrow(verdict)

    def test_role_local_part_on_non_person_target_does_not_itself_fire_d1(
        self, tmp_path: Path
    ) -> None:
        """D1's own role-local-part branch does not flag a non-person target
        (design §5.1: "role addresses on non-person targets pass") — D3
        still independently flags ANY value on a non-person target, so the
        OVERALL verdict is ``suspect`` via D3 alone, with no D1 evidence."""
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "info@other.example.com",
            target_uid="uid-acme",
            target_name="Acme Corp",
            target_type="company",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert not any(
            c.check_id in (pii_attribution.CHECK_D1_NARROW, pii_attribution.CHECK_D1_ROLE)
            for c in verdict.checks
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        assert verdict.checks == (
            pii_attribution.CheckEvidence(
                check_id=pii_attribution.CHECK_D3_NOT_A_PERSON, label="company"
            ),
        )


class TestD2HeldElsewhere:
    def test_fires_when_another_live_uid_already_holds_it(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-holder", name="Other Holder")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts, "uid-holder.md", uid="uid-holder", emails=["shared@example.org"]
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "shared@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        d2 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_D2_HELD_ELSEWHERE]
        assert d2 and d2[0].uids == ("uid-holder",)
        assert "uid-holder" in verdict.candidates

    def test_a_record_whose_uid_has_no_live_page_does_not_count(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True, exist_ok=True)
        # No page written for uid-ghost: the record exists but the page does not.
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts, "uid-ghost.md", uid="uid-ghost", emails=["shared@example.org"]
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "shared@example.org",
            target_uid="uid-target",
            target_name="Target Person",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_PASS


class TestD3NotAPerson:
    def test_target_type_not_person_is_suspect(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.check_contact_attribution(
            "deal@example.org",
            target_uid="uid-acme",
            target_name="Acme Corp",
            target_type="company",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        assert any(c.check_id == pii_attribution.CHECK_D3_NOT_A_PERSON for c in verdict.checks)

    def test_name_matches_a_company_page_even_with_person_type(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-acme", name="Acme Corp", type_="company")
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "deal@example.org",
            target_uid="uid-mistyped",
            target_name="Acme Corp",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        assert any(c.check_id == pii_attribution.CHECK_D3_NOT_A_PERSON for c in verdict.checks)


class TestD4FanOut:
    def test_fires_at_two_or_more_holders(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-a", name="Holder A")
        _write_page(wiki_root, uid="uid-b", name="Holder B")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(contacts, "uid-a.md", uid="uid-a", emails=["fanout@example.org"])
        _write_contact_record(contacts, "uid-b.md", uid="uid-b", emails=["fanout@example.org"])
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "fanout@example.org",
            target_uid="uid-target",
            target_name="Target",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_SUSPECT
        d4 = [c for c in verdict.checks if c.check_id == pii_attribution.CHECK_D4_FAN_OUT]
        assert d4 and set(d4[0].uids) == {"uid-a", "uid-b"}


class TestOrderingAndStopping:
    def test_non_personal_stops_the_run_before_d2(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-a", name="Holder A")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        # A value that would ALSO fire D2 if D1 did not stop the run first.
        _write_contact_record(
            contacts, "uid-a.md", uid="uid-a", emails=["noreply@example.org"]
        )
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "noreply@example.org",
            target_uid="uid-target",
            target_name="Target",
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        assert verdict.disposition == pii_attribution.DISPOSITION_NON_PERSONAL
        assert len(verdict.checks) == 1

    def test_suspect_does_not_stop_the_run(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_page(wiki_root, uid="uid-a", name="Holder A")
        _write_page(wiki_root, uid="uid-b", name="Holder B")
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(contacts, "uid-a.md", uid="uid-a", emails=["multi@example.org"])
        _write_contact_record(contacts, "uid-b.md", uid="uid-b", emails=["multi@example.org"])
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        verdict = pii_attribution.check_contact_attribution(
            "multi@example.org",
            target_uid="uid-target",
            target_name="Target Corp",
            target_type="company",  # fires D3 too
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        fired = {c.check_id for c in verdict.checks}
        assert pii_attribution.CHECK_D2_HELD_ELSEWHERE in fired
        assert pii_attribution.CHECK_D3_NOT_A_PERSON in fired
        assert pii_attribution.CHECK_D4_FAN_OUT in fired


class TestExcludedSurfaceStorage:
    def test_log_row_carries_no_value(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        pii_attribution.check_contact_attribution(
            "secret-address@example.org",
            target_uid="uid-x",
            target_name=None,
            target_type="person",
            source_ref=None,
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            context=context,
        )
        log_path = pii_attribution.default_attribution_log_path(context.contacts_root)
        raw = log_path.read_text(encoding="utf-8")
        assert "secret-address@example.org" not in raw
        row = json.loads(raw.strip().splitlines()[-1])
        assert row["target_uid"] == "uid-x"
        assert row["writer"] == pii_attribution.WRITER_OBSERVED_CONTACT
        assert "value" not in row

    def test_value_key_is_a_stable_hmac(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True, exist_ok=True)
        context = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        first = pii_attribution.value_key_for(context, "alex@example.org")
        second = pii_attribution.value_key_for(context, "ALEX@Example.ORG")
        assert first == second  # normalized before hashing
        # A fresh context built from the SAME knowledge root reads back the
        # SAME minted key, so the value_key is stable across process/context
        # boundaries, not just within one AttributionContext instance.
        rebuilt = pii_attribution.build_attribution_context(knowledge, EXCLUDED_CONFIG)
        assert pii_attribution.value_key_for(rebuilt, "alex@example.org") == first

    def test_value_key_does_not_reveal_the_value(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        key = pii_attribution.value_key_for(context, "alex@example.org")
        assert "alex" not in key
        assert "example" not in key

    def test_park_suspect_is_idempotent(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_SUSPECT,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE, uids=("uid-a",)
                ),
            ),
            candidates=("uid-a",),
            value_key=pii_attribution.value_key_for(context, "alex@example.org"),
        )
        path = pii_attribution.park_suspect(
            context,
            "alex@example.org",
            verdict,
            target_uid="uid-target",
            target_name="Target",
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/x.md",
            shadow=True,
        )
        before = path.read_text(encoding="utf-8")
        before_mtime = path.stat().st_mtime_ns
        again = pii_attribution.park_suspect(
            context,
            "alex@example.org",
            verdict,
            target_uid="uid-target",
            target_name="Target",
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/x.md",
            shadow=True,
        )
        assert again == path
        assert path.read_text(encoding="utf-8") == before
        # Re-parking with identical content writes nothing new at all.
        assert path.stat().st_mtime_ns == before_mtime

    def test_reparking_merges_checks_and_ratchets_shadow_to_false(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        key = pii_attribution.value_key_for(context, "alex@example.org")
        first_verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_SUSPECT,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE, uids=("uid-a",)
                ),
            ),
            candidates=("uid-a",),
            value_key=key,
        )
        path = pii_attribution.park_suspect(
            context,
            "alex@example.org",
            first_verdict,
            target_uid="uid-target",
            target_name="Target",
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/x.md",
            shadow=True,
        )
        second_verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_NON_PERSONAL,
            checks=(pii_attribution.CheckEvidence(check_id=pii_attribution.CHECK_D1_NARROW),),
            candidates=(),
            value_key=key,
        )
        pii_attribution.park_suspect(
            context,
            "alex@example.org",
            second_verdict,
            target_uid="uid-target",
            target_name="Target",
            writer=pii_attribution.WRITER_CORRECTION_ADD,
            source_ref="raw/y.md",
            shadow=False,
        )
        meta, _ = pii.parse_frontmatter(path.read_text(encoding="utf-8"))
        assert set(meta["checks"]) == {
            pii_attribution.CHECK_D2_HELD_ELSEWHERE,
            pii_attribution.CHECK_D1_NARROW,
        }
        assert meta["shadow"] is False


class TestWriterInventoryGuard:
    def test_membership(self) -> None:
        assert set(pii_attribution.WRITER_IDS) == {
            pii_attribution.WRITER_OBSERVED_CONTACT,
            pii_attribution.WRITER_CORRECTION_ADD,
            pii_attribution.WRITER_PII_MIGRATION,
            pii_attribution.WRITER_CONTACT_FOLD,
            pii_attribution.WRITER_PROSE_COMPILE,
        }


class TestScrubCompiledContactTokens:
    """Issue athenaeum#2216: the prose-compile scrub (design §10.2)."""

    def _suspect_path(
        self,
        context: pii_attribution.AttributionContext,
        value: str,
        target_uid: str,
    ) -> Path:
        key = pii_attribution.value_key_for(context, value)
        return (
            context.contacts_root
            / pii_attribution.ATTRIBUTION_SUSPECTS_DIRNAME
            / f"{key}-{target_uid}.md"
        )

    def test_byte_identical_when_no_tokens(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        body = "No contact tokens here, just prose about a meeting."
        scrubbed, removed = pii_attribution.scrub_compiled_contact_tokens(
            body,
            subject_uid="uid-subject",
            source_ref="raw/x.md",
            context=context,
        )
        assert scrubbed == body
        assert removed == []

    def test_removes_subject_and_third_party_tokens(self, tmp_path: Path) -> None:
        context = _context(tmp_path)
        body = (
            "Reach the subject at subject@example.org, or cc "
            "third.party@example.com on anything urgent."
        )
        scrubbed, removed = pii_attribution.scrub_compiled_contact_tokens(
            body,
            subject_uid="uid-subject",
            target_name="Subject Person",
            target_type="person",
            source_ref="raw/x.md",
            context=context,
        )
        assert "subject@example.org" not in scrubbed
        assert "third.party@example.com" not in scrubbed
        assert set(removed) == {"subject@example.org", "third.party@example.com"}

    def test_name_field_carve_out_preserves_the_token(self, tmp_path: Path) -> None:
        """athenaeum#1461: a name-is-an-email target's own token survives the scrub.

        Reuses ``storage_migrate.plan_pii_migration``'s own carve-out rule
        (via ``pii.name_field_pii_values``) rather than re-deriving it — the
        heading that echoes ``target_name`` verbatim must not be mangled.
        """
        context = _context(tmp_path)
        body = (
            "# subject@example.org\n\nAlso mentions third.party@example.com "
            "in the body."
        )
        scrubbed, removed = pii_attribution.scrub_compiled_contact_tokens(
            body,
            subject_uid="uid-subject",
            target_name="subject@example.org",
            target_type="person",
            source_ref="raw/x.md",
            context=context,
        )
        assert "subject@example.org" in scrubbed
        assert "third.party@example.com" not in scrubbed
        assert removed == ["third.party@example.com"]

    def test_parks_every_removed_token_as_a_shadow_suspect_readable_through_pii(
        self, tmp_path: Path
    ) -> None:
        """Nothing is lost: a removed token is parked, not dropped.

        Reads the parked entry back through ``pii.parse_frontmatter`` — the
        same reader ``TestExcludedSurfaceStorage``-style tests in this file
        use for the other four writers' parked suspects.
        """
        context = _context(tmp_path)
        body = "No existing holder, so this is a plain pass: new@example.org."
        _scrubbed, removed = pii_attribution.scrub_compiled_contact_tokens(
            body,
            subject_uid="uid-subject",
            target_name="Subject Person",
            target_type="person",
            source_ref="raw/x.md",
            context=context,
        )
        assert removed == ["new@example.org"]
        path = self._suspect_path(context, "new@example.org", "uid-subject")
        assert path.exists()
        meta, _ = pii.parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["writer"] == pii_attribution.WRITER_PROSE_COMPILE
        assert meta["target_uid"] == "uid-subject"
        assert meta["shadow"] is True
