# SPDX-License-Identifier: Apache-2.0
"""``recall(with_pii=True)`` — excluded fields for any entity class (athenaeum#885).

The caller-facing generalization of athenaeum#883's primitives, at the Python
``recall_search`` / ``_recall_via_backend`` layer only. The MCP tool argument
and ``athenaeum query entity`` (and the now-removed, athenaeum#888,
person-shaped ``read_person`` tool / ``athenaeum query person``) are
athenaeum#886's scope and are untouched here.

Structure mirrors the issue's acceptance criteria:

- ``TestDefaultPathIsFreeAndUnchanged`` — with the flag unset, ZERO
  excluded-surface scans and byte-identical output. Asserted by making
  ``pii.iter_contact_records`` raise.
- ``TestJoinReusesTheAssemblySeam`` — the join never builds an ``EntityIndex``
  and never calls ``read_entity``/``read_entities``.
- ``TestPageClassToSurfaceClassMapping`` — identity default, the shipped
  ``person: pii`` entry, operator override, and — tested directly, not just
  the happy path — the ``is_excluded`` GATE that refuses to join a class whose
  surface is the ordinary wiki adapter.
- ``TestLayerOrdering`` — a hit dropped by the audience check or by the
  athenaeum#532 ``recallable`` drop gets NO excluded-surface lookup at all, even
  with ``with_pii=True``.
- ``TestOneIndexPerCall`` — one scan per call shared across all hits, never
  one per hit.
- ``TestNoUidHit`` — a hit with no ``uid`` performs no join, produces no
  marker, and is not an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum import mcp_server, pii
from athenaeum.mcp_server import recall_search

#: `pii` routed off-corpus; every other class stays on the default wiki
#: surface — the shape of the live `~/knowledge/athenaeum.yaml`.
EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}

RESTRICTED = {"secondary"}


def _write_page(
    wiki_root: Path,
    uid: str,
    *,
    name: str,
    entity_type: str = "person",
    extra: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: {entity_type}\n{extra}---\n\n"
        f"{name} works on widget calibration.\n",
        encoding="utf-8",
    )
    return path


def _write_record(root: Path, filename: str, *, uid: str, fields: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A knowledge root with one person page and its excluded record."""
    knowledge = tmp_path / "knowledge"
    _write_page(knowledge / "wiki", "alex", name="Alex Widget")
    _write_record(
        pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
        "alex-contact.md",
        uid="alex",
        fields="emails:\n  - alex@example.org\n",
    )
    return knowledge


class TestDefaultPathIsFreeAndUnchanged:
    def test_default_recall_performs_zero_excluded_scans(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AC's own test: make the scan raise, assert default recall works."""

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("default recall must not scan the excluded surface")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        result = recall_search(corpus / "wiki", "widget", config=EXCLUDED_CONFIG)

        assert "Alex Widget" in result

    def test_default_output_is_byte_identical_to_with_pii_false(self, corpus: Path) -> None:
        implicit = recall_search(corpus / "wiki", "widget", config=EXCLUDED_CONFIG)
        explicit = recall_search(
            corpus / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=False
        )

        assert implicit == explicit
        assert "alex@example.org" not in implicit

    def test_default_path_never_renders_an_excluded_value(self, corpus: Path) -> None:
        result = recall_search(corpus / "wiki", "widget", config=EXCLUDED_CONFIG)

        assert "alex@example.org" not in result
        assert "redacted" not in result


class TestJoinReusesTheAssemblySeam:
    def test_with_pii_renders_the_excluded_fields(self, corpus: Path) -> None:
        result = recall_search(
            corpus / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "Alex Widget" in result
        assert "alex@example.org" in result

    def test_join_never_builds_an_entity_index(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """25.2s of the measured 28.1s per-call cost IS this construction."""

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("the with_pii join must not build an EntityIndex")

        monkeypatch.setattr(pii, "EntityIndex", _explode)
        monkeypatch.setattr(mcp_server, "EntityIndex", _explode)

        result = recall_search(
            corpus / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "alex@example.org" in result

    def test_join_never_calls_read_entity_or_read_entities(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("the join must use the assembly seam, not read_entity")

        monkeypatch.setattr(pii, "read_entity", _explode)
        monkeypatch.setattr(pii, "read_entities", _explode)

        result = recall_search(
            corpus / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "alex@example.org" in result

    def test_usage_classes_filter_is_threaded_to_the_join(self, corpus: Path) -> None:
        """`with_pii` is not usage-class-blind (security-posture.md §2.3)."""
        unfiltered = recall_search(
            corpus / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )
        # The record carries no classification, so its value is `unclassified`;
        # asking only for `observed` must drop it.
        filtered = recall_search(
            corpus / "wiki",
            "widget",
            config=EXCLUDED_CONFIG,
            with_pii=True,
            usage_classes=("observed",),
        )

        assert "alex@example.org" in unfiltered
        assert "alex@example.org" not in filtered

    def test_entity_with_no_record_joins_to_nothing_and_is_not_an_error(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "sam", name="Sam Widget")

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "Sam Widget" in result
        assert "redacted" not in result

    def test_withheld_fields_render_as_markers_not_as_absence(
        self, tmp_path: Path
    ) -> None:
        """Withheld and absent must never collapse to the same shape."""
        page = _write_page(tmp_path / "wiki", "alex", name="Alex Widget")

        block = mcp_server._excluded_block_for_hit(
            page,
            {"uid": "alex", "type": "person"},
            wiki_root=tmp_path / "wiki",
            config=EXCLUDED_CONFIG,
            indexes={},
            usage_classes=None,
        )
        # No record on this surface -> nothing to say at all.
        assert block == ""

        # A redacted assembly, by contrast, must render a marker naming the
        # field and how many values exist — never simply nothing.
        fields, redactions, _ = pii.assemble_excluded_read(
            page,
            {"uid": "alex"},
            {"uid": "alex", "emails": ["alex@example.org", "a2@example.org"]},
            surface_class="pii",
            include_excluded=False,
        )
        assert fields == {}
        assert [(m.field, m.value_count) for m in redactions] == [("emails", 2)]


class TestPageClassToSurfaceClassMapping:
    def test_person_maps_to_pii_by_default(self) -> None:
        assert pii.surface_class_for_page_class("person", None) == "pii"

    def test_every_other_class_is_identity_by_default(self) -> None:
        assert pii.surface_class_for_page_class("vendor", None) == "vendor"
        assert pii.surface_class_for_page_class("project", None) == "project"

    def test_operator_override_wins(self) -> None:
        config = {"storage": {"excluded_read_mapping": {"vendor": "supplier-pii"}}}

        assert pii.surface_class_for_page_class("vendor", config) == "supplier-pii"

    def test_operator_can_point_person_back_at_identity(self) -> None:
        config = {"storage": {"excluded_read_mapping": {"person": "person"}}}

        assert pii.surface_class_for_page_class("person", config) == "person"

    def test_blank_page_class_maps_to_nothing(self) -> None:
        assert pii.surface_class_for_page_class("", None) == ""
        assert pii.surface_class_for_page_class(None, None) == ""

    def test_gate_refuses_a_class_whose_surface_is_not_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing gate: never scan the WIKI ROOT as an excluded surface.

        A `type: project` hit maps (identity) to surface class `project`, which
        on the live config resolves to the ordinary wiki adapter. Joining there
        would read the corpus back as though it were the excluded store.
        """
        knowledge = tmp_path / "knowledge"
        page = _write_page(
            knowledge / "wiki", "atlas", name="Atlas Widget", entity_type="project"
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("a non-excluded surface class must never be scanned")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        block = mcp_server._excluded_block_for_hit(
            page,
            {"uid": "atlas", "type": "project"},
            wiki_root=knowledge / "wiki",
            config=EXCLUDED_CONFIG,
            indexes={},
            usage_classes=None,
        )

        assert block == ""

    def test_gate_refused_class_is_not_an_error_through_recall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(
            knowledge / "wiki", "atlas", name="Atlas Widget", entity_type="project"
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("a non-excluded surface class must never be scanned")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        assert "Atlas Widget" in result


class TestLayerOrdering:
    """A hit dropped by either Layer-C check gets NO excluded lookup at all."""

    def test_unauthorized_hit_gets_no_hit_and_no_excluded_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        # No `access:` -> a restricted caller is not authorized (fail-closed).
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError(
                "an unauthorized hit must never trigger an excluded-surface lookup"
            )

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        result = recall_search(
            knowledge / "wiki",
            "widget",
            config=EXCLUDED_CONFIG,
            caller_audience=RESTRICTED,
            with_pii=True,
        )

        assert "Alex Widget" not in result
        assert "alex@example.org" not in result

    def test_non_recallable_hit_gets_no_hit_and_no_excluded_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The athenaeum#532 `recallable` drop is likewise BEFORE the join."""
        knowledge = tmp_path / "knowledge"
        config: dict[str, object] = {
            "storage": {"mapping": {"pii": "excluded", "person": "unrecallable"},
                        "adapters": {
                            "unrecallable": {
                                "backing_store": "wiki-markdown",
                                "surface_root": "wiki",
                                "corpus_policy": {
                                    "embedded": True,
                                    "recallable": False,
                                    "merge_eligible": False,
                                },
                            }
                        }}
        }
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, config),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError(
                "a recallable:false hit must never trigger an excluded-surface lookup"
            )

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        result = recall_search(
            knowledge / "wiki", "widget", config=config, with_pii=True
        )

        assert "Alex Widget" not in result
        assert "alex@example.org" not in result

    def test_authorized_restricted_caller_still_receives_the_join(
        self, tmp_path: Path
    ) -> None:
        """The rule is entity_read's: survive the audience check, get values."""
        knowledge = tmp_path / "knowledge"
        _write_page(
            knowledge / "wiki", "alex", name="Alex Widget", extra="access: open\n"
        )
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        result = recall_search(
            knowledge / "wiki",
            "widget",
            config=EXCLUDED_CONFIG,
            caller_audience=RESTRICTED,
            with_pii=True,
        )

        assert "Alex Widget" in result
        assert "alex@example.org" in result


class TestOneIndexPerCall:
    def test_surface_is_scanned_once_for_many_hits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        for uid in ("alex", "sam", "kim"):
            _write_page(knowledge / "wiki", uid, name=f"{uid.title()} Widget")
            _write_record(
                contacts,
                f"{uid}-contact.md",
                uid=uid,
                fields=f"emails:\n  - {uid}@example.org\n",
            )

        scans = 0
        original = pii.iter_contact_records

        def _counting(root: Path) -> list[Path]:
            nonlocal scans
            scans += 1
            return original(root)

        monkeypatch.setattr(pii, "iter_contact_records", _counting)

        result = recall_search(
            knowledge / "wiki", "widget", config=EXCLUDED_CONFIG, with_pii=True
        )

        # One rendered `emails:` line per hit. Counting the LINES, not raw
        # address occurrences: since athenaeum#851 each hit also carries a
        # structured `athenaeum-excluded-facts` block that repeats the address
        # inside its per-value classification/validity entries, so a raw
        # substring count no longer measures "how many hits rendered".
        assert result.count("**emails:**") == 3
        for uid in ("alex", "sam", "kim"):
            assert f"{uid}@example.org" in result
        assert scans == 1


class TestNoUidHit:
    def test_hit_without_uid_joins_to_nothing_and_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "loose.md").write_text(
            "---\nname: Loose Widget\ntype: person\n---\n\nA widget note.\n",
            encoding="utf-8",
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("a hit with no uid has nothing to join on")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        result = recall_search(wiki, "widget", config=EXCLUDED_CONFIG, with_pii=True)

        assert "Loose Widget" in result
        assert "redacted" not in result


class TestHandleShapedPathFieldParity:
    """athenaeum#961 — the handle-shaped (`identity_resolution.resolve_handle_query`)
    path gains `do_not_email` and structured `validity` on every
    `contact_values` entry, matching what `read_entity(include_excluded=True)`
    already returns for the same entity via the similarity-search path
    (`recall`'s excluded-facts block). The marked case, the unmarked case, and
    the with_pii=False / audience-restricted case the field-widening AC
    forbids.
    """

    def _corpus_marked(self, tmp_path: Path) -> Path:
        knowledge = tmp_path / "knowledge"
        _write_page(
            knowledge / "wiki",
            "alex",
            name="Alex Widget",
            extra="do_not_email: true\ndo_not_email_reason: family request\n",
        )
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        return knowledge

    def test_marked_record_carries_do_not_email_and_validity(self, tmp_path: Path) -> None:
        knowledge = self._corpus_marked(tmp_path)

        payload = json.loads(
            recall_search(
                knowledge / "wiki", "alex@example.org", config=EXCLUDED_CONFIG, with_pii=True
            )
        )

        entry = payload["contact_values"][0]
        assert entry["do_not_email"]["marked"] is True
        assert entry["do_not_email"]["reason"] == "family request"
        assert entry["validity"]["closed"] is False
        assert entry["validity"]["recorded"] is False
        # The existing flat bounds are untouched — additive, not a replacement.
        assert entry["valid_from"] is None
        assert entry["valid_until"] is None

    def test_unmarked_record_reports_marked_false_with_no_provenance(
        self, corpus: Path
    ) -> None:
        payload = json.loads(
            recall_search(
                corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG, with_pii=True
            )
        )

        entry = payload["contact_values"][0]
        assert entry["do_not_email"] == {
            "marked": False,
            "source": None,
            "observed_at": None,
            "reason": None,
            "surface": None,
        }
        assert entry["validity"]["closed"] is False
        assert entry["validity"]["recorded"] is False

    def test_without_with_pii_the_new_fields_do_not_widen_the_response(
        self, tmp_path: Path
    ) -> None:
        """A handle-shaped query issued without `with_pii=True` returns the
        same shape it returns today (AC): `do_not_email`/`validity` never
        appear because `contact_values` stays empty, exactly as before this
        issue existed."""
        knowledge = self._corpus_marked(tmp_path)

        payload = json.loads(
            recall_search(knowledge / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["with_pii"] is False
        assert payload["contact_values"] == []
        assert "do_not_email" not in json.dumps(payload)

    def test_unauthorized_caller_gets_no_match_and_no_leak(self, tmp_path: Path) -> None:
        """A caller outside the page's audience never reaches the join at all
        (D6 step 2, unchanged by this issue) — the new fields cannot leak
        through a channel that was already fail-closed."""
        knowledge = self._corpus_marked(tmp_path)

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                caller_audience=RESTRICTED,
                with_pii=True,
            )
        )

        assert payload["resolved"] is False
        assert payload["contact_values"] == []

    def test_do_not_email_matches_read_entity_for_a_marked_record(
        self, tmp_path: Path
    ) -> None:
        knowledge = self._corpus_marked(tmp_path)

        handle_payload = json.loads(
            recall_search(
                knowledge / "wiki", "alex@example.org", config=EXCLUDED_CONFIG, with_pii=True
            )
        )
        entity_read = pii.read_entity(
            knowledge, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=True
        )

        assert entity_read is not None
        assert entity_read.do_not_email.marked is True
        assert (
            handle_payload["contact_values"][0]["do_not_email"]
            == entity_read.do_not_email.to_dict()
        )

    def test_do_not_email_matches_read_entity_for_an_unmarked_record(
        self, corpus: Path
    ) -> None:
        handle_payload = json.loads(
            recall_search(
                corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG, with_pii=True
            )
        )
        entity_read = pii.read_entity(
            corpus, EXCLUDED_CONFIG, "alex", surface_class="pii", include_excluded=True
        )

        assert entity_read is not None
        assert entity_read.do_not_email.marked is False
        assert (
            handle_payload["contact_values"][0]["do_not_email"]
            == entity_read.do_not_email.to_dict()
        )
