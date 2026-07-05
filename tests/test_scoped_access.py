# SPDX-License-Identifier: Apache-2.0
"""Scoped read access — the fail-closed security boundary (issue #312).

A secondary agent/routine is pinned at ``serve`` time to a restricted
``caller_audience`` and must be able to recall operational knowledge but
NEVER a PII / client-confidential / financial page — in NO field (title,
path, tags, snippet, body) and with NO contribution to ranking/top-k.

The suite drives the boundary through :func:`recall_search` (which exercises
all three enforcement layers: A index build, B in-query filter, C fresh
on-disk re-check) parametrized over every backend, plus backend-level
ranking-leak assertions where the ordering is deterministic.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.models import (
    audience_index_string,
    effective_audience,
    is_page_authorized,
    parse_audience,
)

# Backends to prove the filter on. ``vector`` is skipped when chromadb is
# absent (mirrors the existing suite convention).
_BACKENDS = ["keyword", "fts5", "vector"]

# Unique markers that must NEVER appear in a restricted caller's output.
_CONFIDENTIAL_SECRET = "CONFIDENTIALPRICINGXYZ"
_PERSONAL_SECRET = "HOMEADDRESSSECRETXYZ"
_UNTAGGED_SECRET = "UNTAGGEDINTERNALXYZ"
_OPS_MARKER = "OPSRUNBOOKMARKER"


def _build(backend_name: str, wiki: Path, cache: Path) -> None:
    """Build the on-disk index for indexed backends; keyword is scan-on-query."""
    from athenaeum.search import get_backend

    if backend_name != "keyword":
        get_backend(backend_name).build_index(wiki, cache)


def _recall(
    backend_name: str,
    wiki: Path,
    cache: Path,
    query: str,
    *,
    caller_audience: set[str] | None,
    top_k: int = 10,
) -> str:
    return recall_search(
        wiki,
        query,
        top_k,
        search_backend=backend_name,
        cache_dir=cache,
        caller_audience=caller_audience,
    )


@pytest.fixture
def scoped_wiki(tmp_path: Path) -> Path:
    """A wiki mixing operational, confidential, personal, open, untagged pages.

    Every page carries the query term ``project`` in its NAME so all three
    backends (fts5 indexes name; keyword/vector see name + body) match it —
    which page is RETURNED is then decided purely by the audience filter, not
    by relevance.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    (wiki / "ops-runbook.md").write_text(
        "---\n"
        "name: Ops Runbook Project\n"
        "audience: [operations]\n"
        "access: internal\n"
        "---\n\n"
        f"Deployment project runbook. {_OPS_MARKER}.\n"
    )
    (wiki / "client-secrets.md").write_text(
        "---\n"
        "name: Client Secrets Project\n"
        "access: confidential\n"
        "---\n\n"
        f"Client project pricing {_CONFIDENTIAL_SECRET}.\n"
    )
    (wiki / "home-address.md").write_text(
        "---\n"
        "name: Home Address Project\n"
        "access: personal\n"
        "---\n\n"
        f"Personal project home {_PERSONAL_SECRET}.\n"
    )
    (wiki / "public-blog.md").write_text(
        "---\n"
        "name: Public Blog Project\n"
        "access: open\n"
        "---\n\n"
        "Public project blog content, safe to publish.\n"
    )
    (wiki / "internal-notes.md").write_text(
        "---\n"
        "name: Internal Notes Project\n"
        "access: internal\n"
        "---\n\n"
        f"Internal project notes {_UNTAGGED_SECRET}.\n"
    )
    return wiki


# ---------------------------------------------------------------------------
# Unit — the fail-closed helpers
# ---------------------------------------------------------------------------


class TestAudienceHelpers:
    def test_parse_audience_clean_list(self) -> None:
        assert parse_audience({"audience": ["Operations", " Voltaire "]}) == [
            "operations",
            "voltaire",
        ]

    def test_parse_audience_missing(self) -> None:
        assert parse_audience({}) == []
        assert parse_audience({"access": "internal"}) == []

    def test_parse_audience_scalar_is_fail_closed(self) -> None:
        # A scalar (not a list) is malformed -> withhold, never raise.
        assert parse_audience({"audience": "operations"}) == []

    def test_parse_audience_bad_entry_voids_list(self) -> None:
        assert parse_audience({"audience": ["ops", 5]}) == []
        assert parse_audience({"audience": ["ops", ""]}) == []

    def test_effective_audience_open_is_public(self) -> None:
        roles, public = effective_audience({"access": "open"})
        assert public is True
        assert roles == set()

    def test_effective_audience_internal_untagged_is_empty(self) -> None:
        roles, public = effective_audience({"access": "internal"})
        assert public is False
        assert roles == set()

    def test_owner_authorized_for_everything(self) -> None:
        assert is_page_authorized({}, None) is True
        assert is_page_authorized({"access": "personal"}, None) is True

    def test_restricted_withheld_from_untagged(self) -> None:
        assert is_page_authorized({"access": "internal"}, {"operations"}) is False
        assert is_page_authorized({}, {"operations"}) is False

    def test_restricted_authorized_by_role(self) -> None:
        meta = {"audience": ["operations"], "access": "internal"}
        assert is_page_authorized(meta, {"operations"}) is True
        assert is_page_authorized(meta, {"marketing"}) is False

    def test_open_authorized_for_any_restricted_caller(self) -> None:
        assert is_page_authorized({"access": "open"}, {"anyrole"}) is True

    def test_index_string_anchoring(self) -> None:
        # Delimiter anchoring: |ops| must not substring-match |opsadmin|.
        s = audience_index_string({"audience": ["opsadmin"], "access": "internal"})
        assert s == "|opsadmin|"
        assert "|ops|" not in s
        # Public marker is the internal sentinel, never the bare "open" word.
        assert audience_index_string({"access": "open"}) == "|__access_open__|"
        assert audience_index_string({"access": "internal"}) == "|"

    def test_reserved_words_dropped_as_roles(self) -> None:
        # Access-level words + the internal sentinel are not valid role ids.
        assert parse_audience({"audience": ["open"]}) == []
        assert parse_audience({"audience": ["Internal", "personal"]}) == []
        assert parse_audience({"audience": ["__access_open__"]}) == []
        # Real roles survive; a stray reserved word alongside them is dropped.
        assert parse_audience({"audience": ["operations", "open"]}) == ["operations"]

    def test_audience_open_role_is_not_public(self) -> None:
        # SHOULD-1: `audience: [open]` + non-open access must NOT be public —
        # the "open" role is dropped and the page is owner-only.
        meta = {"audience": ["open"], "access": "internal"}
        assert is_page_authorized(meta, {"operations"}) is False
        assert audience_index_string(meta) == "|"
        assert "__access_open__" not in audience_index_string(meta)


# ---------------------------------------------------------------------------
# Acceptance — the must-pass case, every backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestAcceptance:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_operations_caller_gets_ops_never_pii(
        self, backend_name: str, scoped_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, scoped_wiki, cache)

        out = _recall(
            backend_name,
            scoped_wiki,
            cache,
            "project",
            caller_audience={"operations"},
        )

        # Operational page IS served (some field of it present).
        assert _OPS_MARKER in out or "Ops Runbook" in out
        # Forbidden pages appear in NO field — name AND body secret absent.
        assert "Client Secrets" not in out
        assert _CONFIDENTIAL_SECRET not in out
        assert "Home Address" not in out
        assert _PERSONAL_SECRET not in out
        assert _UNTAGGED_SECRET not in out

    def test_open_page_served_to_restricted_caller(
        self, backend_name: str, scoped_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, scoped_wiki, cache)
        out = _recall(
            backend_name,
            scoped_wiki,
            cache,
            "project",
            caller_audience={"operations"},
        )
        assert "Public Blog" in out


# ---------------------------------------------------------------------------
# Fail-closed — untagged hidden from restricted, visible to owner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestFailClosed:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_untagged_hidden_from_restricted_shown_to_owner(
        self, backend_name: str, scoped_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, scoped_wiki, cache)

        restricted = _recall(
            backend_name, scoped_wiki, cache, "project", caller_audience={"operations"}
        )
        assert _UNTAGGED_SECRET not in restricted

        owner = _recall(
            backend_name, scoped_wiki, cache, "project", caller_audience=None
        )
        assert _UNTAGGED_SECRET in owner  # owner sees the untagged internal page

    def test_malformed_audience_fails_closed(
        self, backend_name: str, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Scalar audience (malformed) + corrupt-frontmatter page.
        (wiki / "scalar-widget.md").write_text(
            "---\n"
            "name: Scalar Widget\n"
            "audience: operations\n"  # scalar, not a list -> fail closed
            "access: internal\n"
            "---\n\n"
            "Widget scalar SCALARSECRETXYZ.\n"
        )
        (wiki / "corrupt-widget.md").write_text(
            "---\n"
            "name: Corrupt Widget\n"
            "audience: [unclosed\n"  # broken YAML
            "access: internal\n"
            "Widget corrupt CORRUPTSECRETXYZ.\n"
        )
        cache = tmp_path / "cache"
        _build(backend_name, wiki, cache)

        restricted = _recall(
            backend_name, wiki, cache, "widget", caller_audience={"operations"}
        )
        # No exception; both malformed pages withheld from the restricted caller.
        assert "SCALARSECRETXYZ" not in restricted
        assert "CORRUPTSECRETXYZ" not in restricted


# ---------------------------------------------------------------------------
# No-ranking-leak — a forbidden top-ranked page cannot steal a slot
# ---------------------------------------------------------------------------


@pytest.fixture
def ranking_wiki(tmp_path: Path) -> Path:
    """A forbidden page engineered to out-rank the one permitted page."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # Forbidden: query term saturates every indexed frontmatter field + body.
    (wiki / "forbidden-widget.md").write_text(
        "---\n"
        "name: Widget Widget Widget\n"
        "tags: [widget, widget]\n"
        "aliases: [widget]\n"
        "description: widget widget widget widget\n"
        "access: personal\n"
        "---\n\n"
        "widget widget widget widget FORBIDDENRANKXYZ.\n"
    )
    # Permitted: a single, weaker mention.
    (wiki / "allowed-widget.md").write_text(
        "---\n"
        "name: Widget Alpha\n"
        "audience: [operations]\n"
        "access: internal\n"
        "---\n\n"
        "widget ALLOWEDRANKXYZ.\n"
    )
    return wiki


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestNoRankingLeak:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_permitted_page_reclaims_the_only_slot(
        self, backend_name: str, ranking_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, ranking_wiki, cache)

        # top_k=1: without in-query filtering the forbidden page would occupy
        # the single slot. With Layer B the permitted page must surface here.
        out = _recall(
            backend_name,
            ranking_wiki,
            cache,
            "widget",
            caller_audience={"operations"},
            top_k=1,
        )
        assert "ALLOWEDRANKXYZ" in out
        assert "FORBIDDENRANKXYZ" not in out
        assert "Found 1 matching pages" in out


class TestRankingLeakDeterministic:
    """fts5/keyword ranking is deterministic — prove the forbidden page really
    out-ranks the permitted one, so the slot-reclaim above is meaningful."""

    @pytest.mark.parametrize("backend_name", ["keyword", "fts5"])
    def test_forbidden_outranks_for_owner(
        self, backend_name: str, ranking_wiki: Path, tmp_path: Path
    ) -> None:
        from athenaeum.search import get_backend

        cache = tmp_path / "cache"
        _build(backend_name, ranking_wiki, cache)
        hits = get_backend(backend_name).query(
            "widget", cache, n=1, wiki_root=ranking_wiki, caller_audience=None
        )
        # Owner, top_k=1: the forbidden page is the single strongest hit.
        assert len(hits) == 1
        assert "forbidden-widget.md" in hits[0][0]


# ---------------------------------------------------------------------------
# SHOULD-1 — an `audience: [open]` role cannot forge public + multi-permitted
# no-starvation (a restricted caller with >=2 permitted pages gets them all)
# ---------------------------------------------------------------------------


@pytest.fixture
def sneaky_open_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # A page trying to look public via an `open` ROLE (not access: open). The
    # reserved word is dropped, so this is owner-only. Fields saturated with the
    # query term so it would out-rank if it ever leaked into ranking.
    (wiki / "sneaky-open.md").write_text(
        "---\n"
        "name: Sneaky Widget Widget Widget\n"
        "tags: [widget, widget]\n"
        "description: widget widget widget widget\n"
        "audience: [open]\n"
        "access: internal\n"
        "---\n\n"
        "widget widget widget SNEAKYOPENXYZ.\n"
    )
    (wiki / "permitted-one.md").write_text(
        "---\n"
        "name: Permitted One Widget\n"
        "audience: [operations]\n"
        "access: internal\n"
        "---\n\n"
        "widget PERMITTEDONEXYZ.\n"
    )
    (wiki / "permitted-two.md").write_text(
        "---\n"
        "name: Permitted Two Widget\n"
        "audience: [operations]\n"
        "access: internal\n"
        "---\n\n"
        "widget PERMITTEDTWOXYZ.\n"
    )
    return wiki


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestSneakyOpenAndNoStarvation:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_open_role_withheld_and_permitted_pages_all_surface(
        self, backend_name: str, sneaky_open_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, sneaky_open_wiki, cache)

        out = _recall(
            backend_name,
            sneaky_open_wiki,
            cache,
            "widget",
            caller_audience={"operations"},
            top_k=10,
        )
        # The `audience: [open]` page is NOT public — withheld.
        assert "SNEAKYOPENXYZ" not in out
        assert "Sneaky" not in out
        # Both permitted pages surface (no starvation).
        assert "PERMITTEDONEXYZ" in out
        assert "PERMITTEDTWOXYZ" in out

    def test_sneaky_open_does_not_steal_a_slot(
        self, backend_name: str, sneaky_open_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, sneaky_open_wiki, cache)
        # top_k=2: the field-saturated sneaky page would win slots if it leaked
        # into ranking. Both permitted pages must fill the two slots instead.
        out = _recall(
            backend_name,
            sneaky_open_wiki,
            cache,
            "widget",
            caller_audience={"operations"},
            top_k=2,
        )
        assert "SNEAKYOPENXYZ" not in out
        assert "PERMITTEDONEXYZ" in out
        assert "PERMITTEDTWOXYZ" in out
        assert "Found 2 matching pages" in out


# ---------------------------------------------------------------------------
# Layer C — an index hit whose file is gone/unreadable at render is withheld
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestUnreadableHitWithheld:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_deleted_file_after_index_is_withheld_from_restricted(
        self, backend_name: str, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = wiki / "vanishing.md"
        page.write_text(
            "---\n"
            "name: Vanishing Widget\n"
            "audience: [operations]\n"
            "access: internal\n"
            "---\n\n"
            "widget VANISHINGXYZ.\n"
        )
        cache = tmp_path / "cache"
        _build(backend_name, wiki, cache)

        # Delete the file: the (stale) fts5/vector index still lists it, but the
        # render funnel can't read fresh frontmatter -> readable=False -> the
        # restricted caller must be withheld (fail-closed).
        page.unlink()

        out = _recall(
            backend_name, wiki, cache, "widget", caller_audience={"operations"}
        )
        assert "Vanishing Widget" not in out
        assert "VANISHINGXYZ" not in out


# ---------------------------------------------------------------------------
# Stale index (Layer C) — fresh on-disk frontmatter wins over a stale index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestStaleIndex:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_frontmatter_change_after_index_is_honored(
        self, backend_name: str, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = wiki / "drifting.md"
        page.write_text(
            "---\n"
            "name: Drifting Project\n"
            "audience: [operations]\n"
            "access: internal\n"
            "---\n\n"
            "Drifting project STALESECRETXYZ.\n"
        )
        cache = tmp_path / "cache"
        _build(backend_name, wiki, cache)

        # It was authorized at index time.
        before = _recall(
            backend_name, wiki, cache, "drifting", caller_audience={"operations"}
        )
        assert "STALESECRETXYZ" in before

        # Now the page is re-classified on disk WITHOUT a rebuild.
        page.write_text(
            "---\n"
            "name: Drifting Project\n"
            "access: personal\n"
            "---\n\n"
            "Drifting project STALESECRETXYZ.\n"
        )
        after = _recall(
            backend_name, wiki, cache, "drifting", caller_audience={"operations"}
        )
        # Layer C re-reads fresh frontmatter and drops the now-forbidden page.
        assert "STALESECRETXYZ" not in after


# ---------------------------------------------------------------------------
# Shell `athenaeum recall --audience` path (cli.py) — filter + Layer C
# ---------------------------------------------------------------------------


class TestCliRecallAudience:
    def _run(
        self,
        knowledge_root: Path,
        cache: Path,
        query: str,
        audience: str | None,
        capsys: pytest.CaptureFixture[str],
    ) -> str:
        from athenaeum.cli import _cmd_recall

        args = argparse.Namespace(
            query=query,
            top_k=10,
            path=knowledge_root,
            cache_dir=cache,
            backend="fts5",
            audience=audience,
        )
        rc = _cmd_recall(args)
        assert rc == 0
        return capsys.readouterr().out

    def test_cli_restricted_filters_and_owner_sees_all(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "ops.md").write_text(
            "---\nname: Ops Widget\naudience: [operations]\naccess: internal\n"
            "---\n\nwidget CLIOPSXYZ.\n"
        )
        (wiki / "secret.md").write_text(
            "---\nname: Secret Widget\naccess: confidential\n"
            "---\n\nwidget CLISECRETXYZ.\n"
        )
        cache = tmp_path / "cache"
        _build("fts5", wiki, cache)

        restricted = self._run(knowledge, cache, "widget", "operations", capsys)
        assert "ops.md" in restricted
        assert "secret.md" not in restricted

        owner = self._run(knowledge, cache, "widget", None, capsys)
        assert "ops.md" in owner
        assert "secret.md" in owner

    def test_cli_deleted_file_withheld_from_restricted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        page = wiki / "vanishing.md"
        page.write_text(
            "---\nname: Vanishing Widget\naudience: [operations]\naccess: internal\n"
            "---\n\nwidget CLIVANISHXYZ.\n"
        )
        cache = tmp_path / "cache"
        _build("fts5", wiki, cache)
        page.unlink()  # stale index still lists it; render can't read it

        out = self._run(knowledge, cache, "widget", "operations", capsys)
        # Layer C not-readable branch in _cmd_recall withholds it.
        assert "vanishing.md" not in out


# ---------------------------------------------------------------------------
# Owner regression — no audience configured => unchanged, sees everything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", _BACKENDS)
class TestOwnerRegression:
    @pytest.fixture(autouse=True)
    def _maybe_skip_vector(self, backend_name: str) -> None:
        if backend_name == "vector":
            pytest.importorskip("chromadb")

    def test_owner_sees_all_including_untagged_and_pii(
        self, backend_name: str, scoped_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        _build(backend_name, scoped_wiki, cache)
        out = _recall(backend_name, scoped_wiki, cache, "project", caller_audience=None)
        # Every page is visible to the owner.
        assert _OPS_MARKER in out
        assert _CONFIDENTIAL_SECRET in out
        assert _PERSONAL_SECRET in out
        assert _UNTAGGED_SECRET in out
        assert "Public Blog" in out
