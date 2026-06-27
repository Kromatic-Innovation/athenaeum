# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum.owner`` — owner-namespace memory routing (#263)."""

from __future__ import annotations

import pytest

from athenaeum.owner import route_owner_memory

OWNER = {
    "uid": "a545c038",
    "google_contact": "people/c765728850212863135",
    "aliases": ["user_tristan", "tristan@kromatic.com", "Tristan Kromer"],
}


class TestRouteOwnerMemory:
    def test_family_exclusion_routes_to_reference(self) -> None:
        assert route_owner_memory("user_tristan_family_relationships", OWNER) == (
            "reference"
        )

    @pytest.mark.parametrize(
        "name",
        [
            "user_tristan_relationship_exclusions",
            "user_tristan_blocklist",
            "user_tristan_do_not_contact",
            "user_tristan_operational_notes",
        ],
    )
    def test_operational_markers_route_to_reference(self, name: str) -> None:
        assert route_owner_memory(name, OWNER) == "reference"

    def test_owner_bio_memory_routes_to_person(self) -> None:
        assert route_owner_memory("user_tristan_career", OWNER) == "person"

    def test_alias_prefixed_namespace_matches(self) -> None:
        # An alias-prefixed (non-user_) name is still owner namespace.
        assert route_owner_memory("tristan@kromatic.com_family", OWNER) == "reference"

    def test_non_owner_memory_is_none(self) -> None:
        assert route_owner_memory("acme_corp_overview", OWNER) is None

    def test_inert_when_no_owner(self) -> None:
        assert route_owner_memory("user_tristan_family_relationships", None) is None

    def test_empty_name_is_none(self) -> None:
        assert route_owner_memory("", OWNER) is None
