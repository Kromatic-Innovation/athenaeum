# SPDX-License-Identifier: Apache-2.0
"""Tier-0 email-handle → uid resolution via the PII surface (issue athenaeum#884).

Implements the answers recorded on athenaeum#858/#859 (operator, 2026-08-13): a
``{"type": "person", "handle": {"email": …}}`` correction target resolves at
tier 0 by reverse-lookup through the PII contacts surface —
``email -> contact record -> record uid -> wiki page`` — inside the correction
applier, so no external system ever needs to read the excluded surface to
correlate an address to a person.

Cases (the four the issue enumerates, plus the orphan-uid branch its
acceptance-criteria amendment adds):

- ``TestMatched`` — resolves at tier 0, with no LLM call, through ``pii``.
- ``TestAmbiguous`` — several DISTINCT persons on one address raises a tier.
- ``TestZeroMatch`` — raises a tier and, load-bearingly, NEVER creates.
- ``TestOrphanUid`` — the amendment: the address is known and its person page
  is missing. Raises a tier, never creates, never crashes, and is
  DISTINGUISHABLE in the ledger from an ordinary zero-match.
- ``TestCrossTypeGuard`` — a ``person`` target never resolves onto a
  non-person page.
- ``TestNoEmailWrittenToWikiFrontmatter`` — the address stays resolvable
  without becoming a registry handle key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum.corrections import (
    EMAIL_HANDLE_KEY,
    resolve_target,
    resolve_target_for_apply,
)
from athenaeum.models import EntityIndex
from athenaeum.registry import SOURCE_HANDLE_KEYS

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_page(
    wiki_root: Path, uid: str, *, name: str, entity_type: str = "person"
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: {entity_type}\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_record(knowledge: Path, filename: str, *, uid: str, emails: list[str]) -> Path:
    root = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    root.mkdir(parents=True, exist_ok=True)
    listed = "".join(f"  - {address}\n" for address in emails)
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\nemails:\n{listed}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


def _resolve(knowledge: Path, target: dict, **kwargs: object):
    return resolve_target(
        target,
        index=EntityIndex(knowledge / "wiki"),
        registry_entities={},
        knowledge_root=knowledge,
        config=EXCLUDED_CONFIG,
        **kwargs,
    )


def _resolve_for_apply(knowledge: Path, target: dict, **kwargs: object):
    return resolve_target_for_apply(
        target,
        index=EntityIndex(knowledge / "wiki"),
        registry_entities={},
        knowledge_root=knowledge,
        config=EXCLUDED_CONFIG,
        **kwargs,
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    _write_page(knowledge / "wiki", "alex", name="Alex Widget")
    _write_record(knowledge, "alex-contact.md", uid="alex", emails=["alex@example.org"])
    return knowledge


class TestMatched:
    def test_resolves_to_the_owning_person_page(self, corpus: Path) -> None:
        path = _resolve(
            corpus, {"type": "person", "handle": {"email": "alex@example.org"}}
        )

        assert path is not None
        assert path.name == "alex.md"

    def test_resolution_is_case_insensitive(self, corpus: Path) -> None:
        """`pii.normalize_identifier`'s comparison, reused rather than redone."""
        path = _resolve(
            corpus, {"type": "person", "handle": {"email": "Alex@Example.ORG"}}
        )

        assert path is not None and path.name == "alex.md"

    def test_resolves_through_pii_and_never_builds_a_surface_path_itself(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/design/one-way-in-one-way-out.md §3 — the applier asks `pii`, always."""
        called: list[str] = []
        original_root = pii.contacts_surface_root
        original_records = pii.resolve_contact_records
        original_uid = pii.uid_on_record

        def _root(*args: object, **kwargs: object) -> Path:
            called.append("contacts_surface_root")
            return original_root(*args, **kwargs)  # type: ignore[arg-type]

        def _records(*args: object, **kwargs: object) -> list[Path]:
            called.append("resolve_contact_records")
            return original_records(*args, **kwargs)  # type: ignore[arg-type]

        def _uid(*args: object, **kwargs: object) -> str | None:
            called.append("uid_on_record")
            return original_uid(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pii, "contacts_surface_root", _root)
        monkeypatch.setattr(pii, "resolve_contact_records", _records)
        monkeypatch.setattr(pii, "uid_on_record", _uid)

        _resolve(corpus, {"type": "person", "handle": {"email": "alex@example.org"}})

        assert called == [
            "contacts_surface_root",
            "resolve_contact_records",
            "uid_on_record",
        ]

    def test_is_tier_zero_no_llm_call(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deterministic reverse-lookup — nothing here may reach a model."""
        import athenaeum.provider as provider_mod

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("email-handle resolution must make no LLM call")

        monkeypatch.setattr(provider_mod, "build_llm_client", _explode)

        assert (
            _resolve(corpus, {"type": "person", "handle": {"email": "alex@example.org"}})
            is not None
        )

    def test_a_shared_index_answers_without_a_second_scan(self, corpus: Path) -> None:
        """A batch of corrections pays the surface scan once (athenaeum#883)."""
        index = pii.ExcludedRecordIndex(
            pii.contacts_surface_root(corpus, EXCLUDED_CONFIG)
        )

        path = _resolve(
            corpus,
            {"type": "person", "handle": {"email": "alex@example.org"}},
            excluded_index=index,
        )

        assert path is not None and path.name == "alex.md"

    def test_resolution_matches_with_and_without_an_index(self, corpus: Path) -> None:
        index = pii.ExcludedRecordIndex(
            pii.contacts_surface_root(corpus, EXCLUDED_CONFIG)
        )
        target = {"type": "person", "handle": {"email": "alex@example.org"}}

        assert _resolve(corpus, target) == _resolve(
            corpus, target, excluded_index=index
        )


class TestAmbiguous:
    def test_several_distinct_persons_raises_a_tier(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_page(knowledge / "wiki", "sam", name="Sam Widget")
        _write_record(knowledge, "a.md", uid="alex", emails=["shared@example.org"])
        _write_record(knowledge, "b.md", uid="sam", emails=["shared@example.org"])

        outcome = _resolve_for_apply(
            knowledge, {"type": "person", "handle": {"email": "shared@example.org"}}
        )

        assert outcome.kind == "unresolvable"
        assert outcome.reason == "email-handle-ambiguous"

    def test_several_records_for_ONE_person_is_not_ambiguous(
        self, tmp_path: Path
    ) -> None:
        """Deduped by uid: one person described twice is not two candidates."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(knowledge, "a.md", uid="alex", emails=["alex@example.org"])
        _write_record(knowledge, "b.md", uid="alex", emails=["alex@example.org"])

        path = _resolve(
            knowledge, {"type": "person", "handle": {"email": "alex@example.org"}}
        )

        assert path is not None and path.name == "alex.md"


class TestZeroMatch:
    """Raises a tier and NEVER creates — notwithstanding athenaeum#865."""

    def test_unmatched_email_handle_is_unresolvable_not_creatable(
        self, corpus: Path
    ) -> None:
        outcome = _resolve_for_apply(
            corpus, {"type": "person", "handle": {"email": "stranger@example.org"}}
        )

        assert outcome.kind == "unresolvable"
        assert outcome.kind != "creatable"
        assert outcome.reason == "email-handle-no-match"

    def test_no_page_is_created(self, corpus: Path) -> None:
        before = {p.name for p in (corpus / "wiki").glob("*.md")}

        _resolve_for_apply(
            corpus, {"type": "person", "handle": {"email": "stranger@example.org"}}
        )

        assert {p.name for p in (corpus / "wiki").glob("*.md")} == before

    def test_carve_out_holds_even_if_email_were_added_to_source_handle_keys(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard is explicit, not a side effect of an allowlist omission.

        A future widening of ``SOURCE_HANDLE_KEYS`` must not silently open the
        create branch to every address voltaire has ever seen — which is the
        firehose the operator rejected on 2026-08-12.
        """
        import athenaeum.corrections as corrections_mod

        monkeypatch.setattr(
            corrections_mod, "SOURCE_HANDLE_KEYS", (*SOURCE_HANDLE_KEYS, "email")
        )

        outcome = _resolve_for_apply(
            corpus, {"type": "person", "handle": {"email": "stranger@example.org"}}
        )

        assert outcome.kind == "unresolvable"

    def test_a_non_email_handle_still_creates_as_before(self, corpus: Path) -> None:
        """athenaeum#865's create branch is untouched for every other handle key."""
        outcome = _resolve_for_apply(
            corpus, {"type": "org", "handle": {"domains": "example.net"}}
        )

        assert outcome.kind == "creatable"


class TestOrphanUid:
    """The amendment: the address is KNOWN and its person page is MISSING."""

    def test_orphan_uid_raises_a_tier_and_never_creates(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        # A record whose uid has no wiki page — the ~47-record population the
        # issue measured and did not otherwise handle.
        _write_record(knowledge, "ghost.md", uid="ghost", emails=["ghost@example.org"])

        outcome = _resolve_for_apply(
            knowledge, {"type": "person", "handle": {"email": "ghost@example.org"}}
        )

        assert outcome.kind == "unresolvable"
        assert list((knowledge / "wiki").glob("*.md")) == []

    def test_orphan_uid_is_distinguishable_from_an_ordinary_zero_match(
        self, tmp_path: Path
    ) -> None:
        """Zero-match = the address is unknown. Orphan-uid = its page is gone.

        The second is a store-consistency signal worth surfacing rather than
        swallowing, and both are ``raised-tier`` — only the reason tells them
        apart.
        """
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        _write_record(knowledge, "ghost.md", uid="ghost", emails=["ghost@example.org"])

        orphan = _resolve_for_apply(
            knowledge, {"type": "person", "handle": {"email": "ghost@example.org"}}
        )
        unknown = _resolve_for_apply(
            knowledge, {"type": "person", "handle": {"email": "nobody@example.org"}}
        )

        assert orphan.kind == unknown.kind == "unresolvable"
        assert orphan.reason == "email-handle-orphan-uid"
        assert unknown.reason == "email-handle-no-match"
        assert orphan.reason != unknown.reason

    def test_orphan_uid_never_crashes(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        _write_record(knowledge, "ghost.md", uid="ghost", emails=["ghost@example.org"])

        assert (
            _resolve(
                knowledge, {"type": "person", "handle": {"email": "ghost@example.org"}}
            )
            is None
        )

    def test_a_record_with_no_uid_is_its_own_reason(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        root = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        root.mkdir(parents=True, exist_ok=True)
        (root / "no-uid.md").write_text(
            "---\npii: true\nemails:\n  - nouid@example.org\n---\n\nData.\n",
            encoding="utf-8",
        )

        outcome = _resolve_for_apply(
            knowledge, {"type": "person", "handle": {"email": "nouid@example.org"}}
        )

        assert outcome.kind == "unresolvable"
        assert outcome.reason == "email-handle-record-without-uid"


class TestCrossTypeGuard:
    def test_person_target_never_resolves_onto_a_non_person_page(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "acme", name="Acme Ltd", entity_type="org")
        _write_record(knowledge, "acme.md", uid="acme", emails=["ap@example.org"])

        outcome = _resolve_for_apply(
            knowledge, {"type": "person", "handle": {"email": "ap@example.org"}}
        )

        assert outcome.kind == "unresolvable"
        assert outcome.reason == "email-handle-cross-type"

    def test_matching_type_resolves(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "acme", name="Acme Ltd", entity_type="org")
        _write_record(knowledge, "acme.md", uid="acme", emails=["ap@example.org"])

        path = _resolve(
            knowledge, {"type": "org", "handle": {"email": "ap@example.org"}}
        )

        assert path is not None and path.name == "acme.md"


class TestNoEmailWrittenToWikiFrontmatter:
    def test_email_is_not_a_registry_handle_key(self) -> None:
        """It must never become one — the athenaeum#502/#507 migrator would fold
        it off the page on the next `storage migrate-pii` run, and the registry
        entry would evaporate."""
        assert EMAIL_HANDLE_KEY not in SOURCE_HANDLE_KEYS

    def test_resolution_writes_nothing_to_the_wiki_page(self, corpus: Path) -> None:
        page = corpus / "wiki" / "alex.md"
        before = page.read_text(encoding="utf-8")

        _resolve(corpus, {"type": "person", "handle": {"email": "alex@example.org"}})

        assert page.read_text(encoding="utf-8") == before
        assert "alex@example.org" not in before

    def test_resolution_writes_nothing_to_the_contacts_surface(
        self, corpus: Path
    ) -> None:
        root = pii.contacts_surface_root(corpus, EXCLUDED_CONFIG)
        before = {p: p.read_text(encoding="utf-8") for p in root.rglob("*.md")}

        _resolve(corpus, {"type": "person", "handle": {"email": "alex@example.org"}})

        assert {p: p.read_text(encoding="utf-8") for p in root.rglob("*.md")} == before

    def test_registry_is_not_consulted_for_an_email_handle(self, corpus: Path) -> None:
        """The address lives on the PII surface by design (athenaeum#427/#437);
        a registry entry for it could not survive a migrate-pii run."""
        registry = corpus / "registry.json"
        registry.write_text(
            json.dumps(
                {"entities": {"wrong": {"handles": {"email": "alex@example.org"}}}}
            ),
            encoding="utf-8",
        )

        path = _resolve(
            corpus, {"type": "person", "handle": {"email": "alex@example.org"}}
        )

        # Resolved from the PII surface (uid `alex`), not from the registry
        # entry that claims `wrong`.
        assert path is not None and path.name == "alex.md"


class TestExistingBehaviourUnchanged:
    def test_absent_a_knowledge_root_an_email_handle_simply_does_not_resolve(
        self, corpus: Path
    ) -> None:
        """Every pre-athenaeum#884 caller is untouched: no knowledge_root, no
        email resolution, exactly as before the branch existed."""
        outcome = resolve_target(
            {"type": "person", "handle": {"email": "alex@example.org"}},
            index=EntityIndex(corpus / "wiki"),
            registry_entities={},
        )

        assert outcome is None

    def test_other_handle_shapes_are_unaffected(self, corpus: Path) -> None:
        registry = {"alex": {"handles": {"domains": ["example.org"]}}}

        path = resolve_target(
            {"type": "person", "handle": {"domains": "example.org"}},
            index=EntityIndex(corpus / "wiki"),
            registry_entities=registry,
        )

        assert path is not None and path.name == "alex.md"

    def test_uid_and_name_shapes_are_unaffected(self, corpus: Path) -> None:
        index = EntityIndex(corpus / "wiki")

        assert (
            resolve_target({"uid": "alex"}, index=index, registry_entities={}) is not None
        )
        assert (
            resolve_target(
                {"type": "person", "name": "Alex Widget"},
                index=index,
                registry_entities={},
            )
            is not None
        )
