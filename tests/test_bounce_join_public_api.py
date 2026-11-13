# SPDX-License-Identifier: Apache-2.0
"""Contract test for the public deliverability reader surface (issue athenaeum#2470).

`tests/test_bounce_join.py` already exercises `bounce_join`'s behaviour through
the MODULE import path (`from athenaeum.bounce_join import ...`); that module
was finished but unused outside its own tests. This file exercises the three
symbols athenaeum#2470 promoted — `deliverability_for_page`, `join_identifier`
and `Deliverability` — through the PUBLIC package import path
(`from athenaeum import deliverability_for_page`), which is the contract
`docs/extending/authorized-reader-contract.md` §6 documents and
`tests/test_public_surface_guard.py` / `tests/fixtures/public_surface_baseline.json`
guard going forward.

Three cases, matching the issue's acceptance criterion exactly:

- a page whose address bounced -> not deliverable;
- a page with no bounce -> deliverable;
- an unknown ``uid`` -> a clear "not found" result (``[]``).

All fixtures are synthetic, invented people — never a real name or address
(the issue's own suggestion, "Ada Example", is used below).
"""

from __future__ import annotations

from pathlib import Path

from athenaeum import Deliverability, deliverability_for_page, join_identifier
from athenaeum.pii import mark_bounced


def _person_record(contacts_root: Path, *, uid: str = "77001") -> Path:
    """A synthetic person record in the athenaeum#427/#437 migrator's shape."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"uid: '{uid}'",
        "name: Ada Example — contact record",
        "contact_of: Ada Example",
        "pii: true",
        "emails:",
        "  - ada@example.org",
        "---",
        "",
        "Archival contact data.",
        "",
    ]
    path = contacts_root / f"{uid}-ada-example.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _wiki_page(wiki_root: Path, *, uid: str = "77001") -> Path:
    """A synthetic entity page carrying *uid* -- the page a consumer holds."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"uid: '{uid}'",
        "type: person",
        "name: Ada Example",
        "---",
        "",
        "An entity page.",
        "",
    ]
    path = wiki_root / "ada-example.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestPublicImportPath:
    """The three promoted names resolve through `athenaeum` itself."""

    def test_names_are_importable_from_the_package_root(self) -> None:
        import athenaeum

        assert athenaeum.deliverability_for_page is deliverability_for_page
        assert athenaeum.join_identifier is join_identifier
        assert athenaeum.Deliverability is Deliverability

    def test_names_are_in_all(self) -> None:
        import athenaeum

        assert {"deliverability_for_page", "join_identifier", "Deliverability"} <= set(
            athenaeum.__all__
        )


class TestDeliverabilityContract:
    """The three cases athenaeum#2470's acceptance criterion names."""

    def test_bounced_address_is_not_deliverable(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root)
        mark_bounced(
            contacts_root,
            "ada@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-09-01",
            source="script:voltaire-bounce-relay",
        )

        results = deliverability_for_page(page, contacts_root)

        assert len(results) == 1
        result = results[0]
        assert isinstance(result, Deliverability)
        assert result.identifier == "ada@example.org"
        assert result.hard_bounced is True
        assert result.any_evidence is True

    def test_no_bounce_is_deliverable(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        _person_record(contacts_root)
        page = _wiki_page(wiki_root)

        results = deliverability_for_page(page, contacts_root)

        assert len(results) == 1
        result = results[0]
        assert result.identifier == "ada@example.org"
        assert result.hard_bounced is False
        assert result.wiki_verdict is None
        assert result.any_evidence is False

    def test_unknown_uid_reports_not_found(self, tmp_path: Path) -> None:
        contacts_root, wiki_root = tmp_path / "contacts", tmp_path / "wiki"
        contacts_root.mkdir(parents=True)
        # A page whose uid no contacts-surface record carries at all.
        page = _wiki_page(wiki_root, uid="99999-unknown")

        results = deliverability_for_page(page, contacts_root)

        # The documented "not found" result: nothing recorded, never silently
        # read as "deliverable" by a caller who only checks for a non-empty list.
        assert results == []
