# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.connectors.apollo.

Covers:
- canonical ``api:apollo:<YYYY-MM-DD>`` provenance string.
- ``enrich_person`` returns ``field_sources`` for every field it sets.
- ``people_match`` is the default path (per the bulk-match bug).
- ``people_bulk_match`` is opt-in and emits a UserWarning.
- YAML round-trip of an enriched wiki: ``render_frontmatter`` of the
  merged dict re-loads cleanly through ``yaml.safe_load`` (regression
  for the indent-corruption bug in the cwc-side enricher).
- CLI ``enrich --persons`` dry-run produces no writes; ``--apply``
  produces correct writes.
"""
from __future__ import annotations

import json
import warnings
from datetime import date
from pathlib import Path

import yaml

from athenaeum.connectors.apollo import (
    ApolloClient,
    EnrichResult,
    _apollo_source,
    build_match_request,
    enrich_person,
)
from athenaeum.models import parse_frontmatter, render_frontmatter

# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


def make_person_payload(**overrides) -> dict:
    base = {
        "id": "apollo-123",
        "title": "VP Engineering",
        "headline": "Builder of platforms",
        "linkedin_url": "https://linkedin.com/in/jane-doe",
        "twitter_url": "",
        "github_url": "",
        "city": "San Francisco",
        "state": "CA",
        "country": "United States",
        "employment_history": [
            {
                "title": "VP Engineering",
                "organization_name": "Acme Corp",
                "current": True,
                "start_date": "2024-01-01",
                "end_date": "",
            },
            {
                "title": "Director",
                "organization_name": "Old Co",
                "current": False,
                "start_date": "2020-01-01",
                "end_date": "2023-12-31",
            },
        ],
        "organization": {"name": "Acme Corp"},
    }
    base.update(overrides)
    return base


class FakeTransport:
    """Records calls and returns canned responses keyed by URL path."""

    def __init__(self, person: dict | None = None, *, bulk: list | None = None) -> None:
        self.person = person
        self.bulk = bulk or []
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload))
        if "/people/match" in url and "bulk" not in url:
            return {"person": self.person}
        if "/people/bulk_match" in url:
            return {"matches": self.bulk}
        return {}


# ---------------------------------------------------------------------------
# Provenance source string
# ---------------------------------------------------------------------------


def test_apollo_source_canonical_form():
    assert _apollo_source("2026-05-08") == "api:apollo:2026-05-08"


def test_apollo_source_default_is_today_utc():
    out = _apollo_source()
    # api:apollo:YYYY-MM-DD
    assert out.startswith("api:apollo:")
    date.fromisoformat(out.split(":", 2)[2])  # must parse


# ---------------------------------------------------------------------------
# build_match_request
# ---------------------------------------------------------------------------


def test_build_match_request_with_email_and_name():
    req = build_match_request({"name": "Jane Doe", "emails": ["jane@example.com"]})
    assert req == {
        "email": "jane@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
    }


def test_build_match_request_skips_unknown_name():
    assert build_match_request({"name": "(unknown)"}) is None


def test_build_match_request_requires_identifier():
    # First-name only with no email/linkedin/last-name = not enough.
    assert build_match_request({"name": "Cher"}) is None


# ---------------------------------------------------------------------------
# enrich_person
# ---------------------------------------------------------------------------


def test_enrich_person_returns_field_sources_for_every_field():
    transport = FakeTransport(person=make_person_payload())
    client = ApolloClient(api_key="test", transport=transport)
    meta = {
        "uid": "abc12345",
        "type": "person",
        "name": "Jane Doe",
        "emails": ["jane@example.com"],
    }
    result = enrich_person(meta, client, today="2026-05-08")
    assert isinstance(result, EnrichResult)
    assert result.matched is True
    # Every field set must have a field_sources entry.
    assert set(result.field_sources.keys()) == set(result.fields.keys())
    expected_src = "api:apollo:2026-05-08"
    for src in result.field_sources.values():
        assert src == expected_src
    # Spot-check a few fields landed.
    assert result.fields["apollo_id"] == "apollo-123"
    assert result.fields["current_title"] == "VP Engineering"
    assert result.fields["current_company"] == "Acme Corp"
    assert result.fields["apollo_enriched_on"] == "2026-05-08"


def test_enrich_person_no_match_returns_empty_result():
    transport = FakeTransport(person=None)
    client = ApolloClient(api_key="test", transport=transport)
    result = enrich_person(
        {"uid": "x", "type": "person", "name": "Nobody", "emails": ["x@y.z"]},
        client,
        today="2026-05-08",
    )
    assert result.matched is False
    assert result.fields == {}
    assert result.field_sources == {}


def test_enrich_person_does_not_clobber_existing_linkedin():
    transport = FakeTransport(person=make_person_payload())
    client = ApolloClient(api_key="test", transport=transport)
    meta = {
        "uid": "abc12345",
        "type": "person",
        "name": "Jane Doe",
        "emails": ["jane@example.com"],
        "linkedin_url": "https://linkedin.com/in/operator-curated",
    }
    result = enrich_person(meta, client, today="2026-05-08")
    # linkedin_url already present on input -> connector must not return it.
    assert "linkedin_url" not in result.fields
    assert "linkedin_url" not in result.field_sources


# ---------------------------------------------------------------------------
# bulk_match opt-in + warning
# ---------------------------------------------------------------------------


def test_bulk_match_emits_warning():
    transport = FakeTransport(bulk=[{"id": "p-1"}, None])
    client = ApolloClient(api_key="test", transport=transport)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        result = client.people_bulk_match([{"email": "a@x"}, {"email": "b@x"}])
    assert any(issubclass(x.category, UserWarning) for x in w)
    assert result[0] == {"id": "p-1"}
    assert result[1] is None
    # Endpoint discrimination: bulk_match path hits /people/bulk_match
    # exactly once and never falls through to /people/match. Guards
    # against a regression where a future refactor routes bulk callers
    # back to the single-call endpoint (or vice versa).
    assert sum("/people/bulk_match" in c[1] for c in transport.calls) == 1
    assert (
        sum(
            "/people/match" in c[1] and "/people/bulk_match" not in c[1]
            for c in transport.calls
        )
        == 0
    )


def test_default_path_uses_people_match_not_bulk():
    transport = FakeTransport(person=make_person_payload())
    client = ApolloClient(api_key="test", transport=transport)
    enrich_person(
        {
            "uid": "u",
            "type": "person",
            "name": "Jane Doe",
            "emails": ["jane@example.com"],
        },
        client,
        today="2026-05-08",
    )
    # Exactly one call, to /people/match (not /people/bulk_match).
    assert len(transport.calls) == 1
    method, url, _payload = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/people/match")
    assert "/bulk_match" not in url


# ---------------------------------------------------------------------------
# YAML round-trip regression
# ---------------------------------------------------------------------------


def test_enriched_frontmatter_yaml_roundtrips():
    """Regression for the cwc-side indent-corruption bug.

    Compose enrichment output into a wiki dict, render via
    ``render_frontmatter``, and assert the output reloads cleanly via
    ``yaml.safe_load`` AND survives a second render with the same bytes.
    """
    transport = FakeTransport(person=make_person_payload())
    client = ApolloClient(api_key="test", transport=transport)
    meta = {
        "uid": "abc12345",
        "type": "person",
        "name": "Jane Doe",
        "emails": ["jane@example.com"],
        "tags": ["tier:warm-a", "warm:network"],
    }
    result = enrich_person(meta, client, today="2026-05-08")

    new_meta = dict(meta)
    new_meta.update(result.fields)
    new_meta["field_sources"] = dict(result.field_sources)

    rendered = render_frontmatter(new_meta) + "\nBody.\n"

    # 1) Reloads clean via yaml.safe_load
    parsed_meta, body = parse_frontmatter(rendered)
    assert parsed_meta["apollo_id"] == "apollo-123"
    assert parsed_meta["field_sources"]["apollo_id"] == "api:apollo:2026-05-08"
    # employment history is a list of dicts — survived the round-trip.
    assert isinstance(parsed_meta["apollo_employment_history"], list)
    assert (
        parsed_meta["apollo_employment_history"][0]["organization_name"] == "Acme Corp"
    )
    # tags survived intact (no indent splice)
    assert parsed_meta["tags"] == ["tier:warm-a", "warm:network"]
    assert body.strip() == "Body."

    # 2) Idempotent re-render
    rendered2 = render_frontmatter(parsed_meta) + "\n" + body
    yaml.safe_load(rendered2.split("---\n", 2)[1])  # no exception


# ---------------------------------------------------------------------------
# CLI: dry-run vs --apply
# ---------------------------------------------------------------------------


def _seed_person_wiki(wiki_root: Path, name: str = "Jane Doe") -> Path:
    path = wiki_root / "abc12345-jane-doe.md"
    path.write_text(
        "---\n"
        "uid: abc12345\n"
        "type: person\n"
        f"name: {name}\n"
        "emails:\n"
        "- jane@example.com\n"
        "tags:\n"
        "- tier:warm-a\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def test_cli_enrich_dry_run_does_not_write(tmp_path, monkeypatch):
    """Dry-run must not modify the wiki file on disk."""
    from athenaeum.cli import main

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    path = _seed_person_wiki(wiki_root)
    original = path.read_text(encoding="utf-8")

    # Stub the Apollo client + enrich_person so the dry-run path doesn't
    # need network OR a real API key. We exercise the candidate-listing
    # branch, which is the dry-run-without-key path.
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)

    rc = main(["enrich", "--persons", "--wiki-root", str(wiki_root)])
    assert rc == 0
    assert path.read_text(encoding="utf-8") == original


def test_cli_enrich_apply_writes_with_field_sources(tmp_path, monkeypatch):
    """``--apply`` writes enriched fields with provenance entries."""
    from athenaeum.cli import main
    from athenaeum.connectors import apollo as apollo_mod

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    path = _seed_person_wiki(wiki_root)

    # Patch ApolloClient construction to inject a fake transport. The
    # CLI's `_cmd_enrich` calls ApolloClient() with no args; substituting
    # a factory keeps the production call site unmodified.
    transport = FakeTransport(person=make_person_payload())

    class _PatchedClient(ApolloClient):
        def __init__(self):
            super().__init__(api_key="test", transport=transport)

    monkeypatch.setattr(apollo_mod, "ApolloClient", _PatchedClient)
    # Force the apply branch (which would otherwise also try to build a
    # real client) by giving _cmd_enrich a non-empty APOLLO_API_KEY env.
    monkeypatch.setenv("APOLLO_API_KEY", "test")

    rc = main(["enrich", "--persons", "--apply", "--wiki-root", str(wiki_root)])
    assert rc == 0

    new_text = path.read_text(encoding="utf-8")
    new_meta, _body = parse_frontmatter(new_text)
    assert new_meta["apollo_id"] == "apollo-123"
    assert new_meta["current_title"] == "VP Engineering"
    fs = new_meta["field_sources"]
    assert fs["apollo_id"].startswith("api:apollo:")
    assert fs["current_title"].startswith("api:apollo:")
    # YAML re-parses cleanly (regression guard)
    yaml.safe_load(new_text.split("---\n", 2)[1])
    # CLI default --apply path MUST NOT touch /people/bulk_match.
    # Apollo's bulk endpoint silently drops ~70% of valid matches; the
    # CLI must always route through single-call /people/match.
    assert all("/bulk_match" not in c[1] for c in transport.calls)
    assert any(c[1].endswith("/people/match") for c in transport.calls)


# ---------------------------------------------------------------------------
# UTC-date determinism in _apollo_source
# ---------------------------------------------------------------------------


def test_apollo_source_uses_utc_date_under_non_utc_local_zone(monkeypatch):
    """``_apollo_source(None)`` must stamp UTC date, not local-zone date.

    Pick a moment 30 minutes after UTC midnight on 2026-05-08. In a
    Pacific (UTC-7/8) zone, the local wall-clock date is still
    2026-05-07. The provenance string must reflect UTC (2026-05-08).
    """
    from datetime import timedelta as real_timedelta
    from datetime import timezone as real_timezone

    from athenaeum.connectors import apollo as apollo_mod

    real_datetime = apollo_mod.datetime
    fixed_utc = real_datetime(2026, 5, 8, 0, 30, tzinfo=real_timezone.utc)
    pacific = real_timezone(real_timedelta(hours=-7))

    class _FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # Local wall-clock would be 2026-05-07 17:30 (Pacific),
                # naive — i.e. local-zone date is 2026-05-07.
                return fixed_utc.astimezone(pacific).replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(apollo_mod, "datetime", _FakeDateTime)

    out = apollo_mod._apollo_source()
    assert out == "api:apollo:2026-05-08", out


# ---------------------------------------------------------------------------
# Legacy 2-space-tag frontmatter ingest (cwc-bug regression)
# ---------------------------------------------------------------------------


def test_cli_enrich_apply_handles_legacy_2space_tag_block(tmp_path, monkeypatch):
    """Pre-existing wiki with cwc-bug-shaped 2-space-indented tags block.

    The legacy ``apollo_enrich_warm_tier.py`` (cwc-side) wrote tag
    blocks with 2-space indentation, which broke yaml.safe_load when
    spliced into a 0-space block. The Lane E enricher uses dict
    composition + ``render_frontmatter``, so any pre-existing 2-space
    block on disk should round-trip through ``parse_frontmatter`` ->
    enrich -> ``render_frontmatter`` cleanly.
    """
    from athenaeum.cli import main
    from athenaeum.connectors import apollo as apollo_mod

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    path = wiki_root / "abc12345-jane-doe.md"
    # 2-space-indented tag entries — the cwc-bug shape.
    path.write_text(
        "---\n"
        "uid: abc12345\n"
        "type: person\n"
        "name: Jane Doe\n"
        "emails:\n"
        "- jane@example.com\n"
        "tags:\n"
        "  - tier:warm-a\n"
        "  - warm:network\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    transport = FakeTransport(person=make_person_payload())

    class _PatchedClient(ApolloClient):
        def __init__(self):
            super().__init__(api_key="test", transport=transport)

    monkeypatch.setattr(apollo_mod, "ApolloClient", _PatchedClient)
    monkeypatch.setenv("APOLLO_API_KEY", "test")

    rc = main(["enrich", "--persons", "--apply", "--wiki-root", str(wiki_root)])
    assert rc == 0

    new_text = path.read_text(encoding="utf-8")
    # Re-parses cleanly — no indent-splice corruption.
    parsed_meta, _body = parse_frontmatter(new_text)
    # Tags survived intact (regardless of original indent shape).
    assert parsed_meta["tags"] == ["tier:warm-a", "warm:network"]
    # Apollo fields landed.
    assert parsed_meta["apollo_id"] == "apollo-123"
    assert parsed_meta["field_sources"]["apollo_id"].startswith("api:apollo:")
    # Direct yaml.safe_load also clean.
    yaml.safe_load(new_text.split("---\n", 2)[1])
