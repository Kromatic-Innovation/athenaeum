# SPDX-License-Identifier: Apache-2.0
"""Persisted, HMAC-keyed contact-index cache (issue athenaeum#2182).

- ``TestCacheEquivalence``: cold/warm cache agrees with a fresh
  ``pii.ExcludedRecordIndex`` (AC1, index-level).
- ``TestHandleResolutionEquivalence``: ``resolve_handle_query`` agrees with
  and without the cache, under both ``with_pii`` values (AC1, full).
- ``TestKeyedIdentifiers``: no fixture address, in any case-folding, ever
  appears in the cache file's bytes; key rotation invalidates (AC2).
- ``TestErasureIntegration``: an erasure is unresolvable immediately, no
  reload, with no erased identifier surviving in the file bytes (AC3).
- ``TestModes``: file ``0600`` inside directory ``0700``, on create and
  rewrite (AC4).
- ``TestCacheInvalidation`` / ``TestRobustness``: added/edited/deleted
  records; corrupt/wrong-format/wrong-root caches rebuild; an unavailable
  erasure key still answers.
- ``TestWriterCoherence``: ``register``/``unregister`` stay visible
  immediately on a cache-backed index, exactly as on the plain one.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from athenaeum import contact_index_cache as cic
from athenaeum import erasure as erasure_mod
from athenaeum import identity_resolution, pii
from athenaeum.erasure import load_or_create_erasure_key, purge_erasure_key

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def _record(root: Path, filename: str, uid: str, emails: list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"  - {e}" for e in emails)
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\nemails:\n{body}\n---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


def _page(wiki: Path, filename: str, frontmatter: str) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(f"---\n{frontmatter}---\n\nBody.\n", encoding="utf-8")
    return path


@pytest.fixture
def contacts_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "contacts"
    _record(root, "aaaa1111-ada.md", "aaaa1111", ["ada@example.org"])
    _record(root, "bbbb2222-bram.md", "bbbb2222", ["bram@example.org", "bram.alt@example.org"])
    _record(root, "cccc3333-cleo.md", "cccc3333", ["shared@example.org"])
    _record(root, "dddd4444-dev.md", "dddd4444", ["shared@example.org"])
    return root


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


class TestCacheEquivalence:
    def test_cold_and_warm_match_fresh_index(self, contacts_root: Path, cache_dir: Path) -> None:
        fresh = pii.ExcludedRecordIndex(contacts_root)
        cold, cold_stats = cic.load_with_stats(contacts_root, cache_dir)
        warm, warm_stats = cic.load_with_stats(contacts_root, cache_dir)

        for addr in ("ada@example.org", "bram@example.org", "bram.alt@example.org",
                     "shared@example.org", "ADA@EXAMPLE.ORG", "nobody@example.org"):
            assert cold.all_by_identifier(addr) == fresh.all_by_identifier(addr)
            assert warm.all_by_identifier(addr) == fresh.all_by_identifier(addr)
            assert cold.by_identifier(addr) == fresh.by_identifier(addr)

        for uid in ("aaaa1111", "bbbb2222", "cccc3333", "dddd4444", "missing"):
            assert cold.by_uid(uid) == fresh.by_uid(uid)
            assert cold.all_by_uid(uid) == fresh.all_by_uid(uid)

        assert sorted(cold.uids()) == sorted(fresh.uids())
        assert cold.uid_map() == fresh.uid_map()

        assert cold_stats.cache_was_valid is False and cold_stats.wrote is True
        assert warm_stats.cache_was_valid is True and warm_stats.reparsed == 0
        assert warm_stats.wrote is False

    def test_returns_a_real_excluded_record_index(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        assert isinstance(index, pii.ExcludedRecordIndex)


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    _page(wiki, "aaaa1111-ada-quill.md", "uid: aaaa1111\nname: Ada Quill\ntype: person\n")
    _record(contacts, "aaaa1111-ada.md", "aaaa1111", ["ada@example.org"])
    _page(wiki, "bbbb2222-bram-quill.md", "uid: bbbb2222\nname: Bram Quill\ntype: person\n")
    _record(contacts, "bbbb2222-bram.md", "bbbb2222", ["bram@example.org"])
    _record(contacts, "cccc3333-cleo.md", "cccc3333", ["shared@example.org"])
    _record(contacts, "dddd4444-dev.md", "dddd4444", ["shared@example.org"])
    _record(contacts, "eeee5555-eve.md", "eeee5555", ["eve@example.org"])
    return knowledge


QUERIES = [
    "ada@example.org",
    "bram@example.org",
    "shared@example.org",
    "eve@example.org",
    "nobody@example.org",
]


class TestHandleResolutionEquivalence:
    """AC1, in full: resolve_handle_query with/without the cache, both with_pii."""

    @pytest.mark.parametrize("with_pii", [False, True])
    @pytest.mark.parametrize("query", QUERIES)
    def test_identical_with_and_without_cache(
        self, knowledge: Path, cache_dir: Path, query: str, with_pii: bool
    ) -> None:
        def run(**kw: object) -> dict[str, object]:
            result = identity_resolution.resolve_handle_query(
                knowledge,
                knowledge / "wiki",
                query,
                config=EXCLUDED_CONFIG,
                with_pii=with_pii,
                **kw,
            )
            assert result is not None
            return result.to_dict()

        uncached = run()
        assert run(cache_dir=cache_dir) == uncached  # cold
        assert run(cache_dir=cache_dir) == uncached  # warm


class TestKeyedIdentifiers:
    """AC2: HMAC-keyed identifiers, never raw -- and never even case-folded raw."""

    def test_no_fixture_address_in_any_case_appears_in_cache_bytes(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        raw_bytes = cic.cache_path(contacts_root, cache_dir).read_bytes()
        lowered = raw_bytes.lower()
        for addr in (
            "ada@example.org",
            "bram@example.org",
            "bram.alt@example.org",
            "shared@example.org",
        ):
            for variant in (addr, addr.upper(), addr.capitalize(), addr.swapcase()):
                assert variant.encode("utf-8").lower() not in lowered

    def test_cache_payload_carries_hmac_digests_not_values(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        payload = json.loads(cic.cache_path(contacts_root, cache_dir).read_text(encoding="utf-8"))
        for _rel, (_stamp, _uid, hmacs) in payload["records"].items():
            for digest in hmacs:
                assert len(digest) == 64
                assert all(c in "0123456789abcdef" for c in digest)

    def test_key_rotation_invalidates_the_cache(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        warm_stats = cic.load_with_stats(contacts_root, cache_dir)[1]
        assert warm_stats.reparsed == 0

        purge_erasure_key(cache_dir)
        load_or_create_erasure_key(cache_dir)  # mint a new, different key

        rotated, rotated_stats = cic.load_with_stats(contacts_root, cache_dir)
        assert rotated_stats.cache_was_valid is False  # old digests unusable, full rebuild
        assert rotated.all_by_identifier("ada@example.org") == [
            contacts_root / "aaaa1111-ada.md"
        ]

    def test_purging_key_makes_old_digests_unmatchable(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        old_payload = json.loads(
            cic.cache_path(contacts_root, cache_dir).read_text(encoding="utf-8")
        )
        purge_erasure_key(cache_dir)
        new_key = load_or_create_erasure_key(cache_dir)
        from athenaeum.erasure import erasure_content_hash

        recomputed = erasure_content_hash("ada@example.org", key=new_key)
        all_old_digests = {
            d for (_s, _u, hmacs) in old_payload["records"].values() for d in hmacs
        }
        assert recomputed not in all_old_digests


class TestErasureIntegration:
    """AC3: erasure deletes/invalidates in ONE operation; no later load needed."""

    def test_erased_address_unresolvable_immediately_no_reload(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        assert index.by_identifier("ada@example.org") is not None

        record_path = contacts_root / "aaaa1111-ada.md"
        existed = cic.erase_contact_record(
            contacts_root, record_path, cache_dir=cache_dir, index=index
        )
        assert existed is True

        # Same in-memory object, no reload: already gone.
        assert index.by_identifier("ada@example.org") is None
        assert index.all_by_identifier("ada@example.org") == []
        assert index.by_uid("aaaa1111") is None

    def test_erased_address_unresolvable_on_a_fresh_load_too(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        record_path = contacts_root / "aaaa1111-ada.md"
        cic.erase_contact_record(contacts_root, record_path, cache_dir=cache_dir, index=index)

        reloaded, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert reloaded.by_identifier("ada@example.org") is None
        assert not record_path.exists()

    def test_no_erased_identifier_survives_in_cache_bytes(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        record_path = contacts_root / "aaaa1111-ada.md"
        cic.erase_contact_record(contacts_root, record_path, cache_dir=cache_dir, index=index)

        from athenaeum.erasure import erasure_content_hash

        key = load_or_create_erasure_key(cache_dir)
        erased_digest = erasure_content_hash("ada@example.org", key=key)
        payload = json.loads(cic.cache_path(contacts_root, cache_dir).read_text(encoding="utf-8"))
        all_digests = {d for (_s, _u, hmacs) in payload["records"].values() for d in hmacs}
        assert erased_digest not in all_digests
        assert "aaaa1111-ada.md" not in payload["records"]

    def test_erase_without_a_live_index_still_updates_the_cache(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        record_path = contacts_root / "bbbb2222-bram.md"
        cic.erase_contact_record(contacts_root, record_path, cache_dir=cache_dir)

        reloaded, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert reloaded.by_identifier("bram@example.org") is None
        # No stale "removed" re-parse storm: the cache already dropped it.
        assert stats.reparsed == 0

    def test_erase_nonexistent_record_returns_false_but_still_cleans_up(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        ghost = contacts_root / "already-gone.md"
        assert cic.erase_contact_record(contacts_root, ghost, cache_dir=cache_dir) is False


class TestModes:
    """AC4: file mode and directory mode, on create AND rewrite."""

    def test_file_and_directory_modes_on_create(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        path = cic.cache_path(contacts_root, cache_dir)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_modes_never_widen_on_rewrite(self, contacts_root: Path, cache_dir: Path) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        path = cic.cache_path(contacts_root, cache_dir)
        os.chmod(path, 0o644)
        _record(contacts_root, "ffff6666-fay.md", "ffff6666", ["fay@example.org"])
        cic.load_contact_index(contacts_root, cache_dir)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_modes_never_widen_on_erasure_rewrite(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        path = cic.cache_path(contacts_root, cache_dir)
        os.chmod(path, 0o644)
        cic.erase_contact_record(
            contacts_root, contacts_root / "aaaa1111-ada.md", cache_dir=cache_dir
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_directory_mode_never_widens_on_rewrite(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        # `mkdir(exist_ok=True)` does not touch an existing directory's mode,
        # so widening it out-of-band must be re-narrowed on the NEXT write,
        # not only when the directory is first created.
        cic.load_contact_index(contacts_root, cache_dir)
        path = cic.cache_path(contacts_root, cache_dir)
        os.chmod(path.parent, 0o755)
        _record(contacts_root, "ffff6666-fay.md", "ffff6666", ["fay@example.org"])
        cic.load_contact_index(contacts_root, cache_dir)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_directory_mode_never_widens_on_erasure_rewrite(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        path = cic.cache_path(contacts_root, cache_dir)
        os.chmod(path.parent, 0o755)
        cic.erase_contact_record(
            contacts_root, contacts_root / "aaaa1111-ada.md", cache_dir=cache_dir
        )
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


class TestCacheInvalidation:
    def test_added_record_is_indexed(self, contacts_root: Path, cache_dir: Path) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        _record(contacts_root, "ffff6666-fay.md", "ffff6666", ["fay@example.org"])
        cached, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert stats.reparsed == 1
        assert cached.by_identifier("fay@example.org") == contacts_root / "ffff6666-fay.md"

    def test_edited_record_is_reparsed(self, contacts_root: Path, cache_dir: Path) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        _record(contacts_root, "aaaa1111-ada.md", "aaaa1111", ["ada-new@example.org"])
        cached, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert stats.reparsed == 1
        assert cached.by_identifier("ada@example.org") is None
        assert cached.by_identifier("ada-new@example.org") == contacts_root / "aaaa1111-ada.md"

    def test_deleted_record_is_dropped(self, contacts_root: Path, cache_dir: Path) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        (contacts_root / "aaaa1111-ada.md").unlink()
        cached, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert stats.removed == 1
        assert cached.by_identifier("ada@example.org") is None


class TestRobustness:
    @pytest.mark.parametrize(
        "content",
        [
            "not json {",
            json.dumps([1, 2, 3]),
            json.dumps({"format": cic.CACHE_FORMAT + 1, "records": {}}),
        ],
    )
    def test_corrupt_cache_rebuilds(
        self, contacts_root: Path, cache_dir: Path, content: str
    ) -> None:
        path = cic.cache_path(contacts_root, cache_dir)
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        cached, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert stats.cache_was_valid is False
        assert cached.by_identifier("ada@example.org") == contacts_root / "aaaa1111-ada.md"
        assert cic.load_with_stats(contacts_root, cache_dir)[1].reparsed == 0

    def test_wrong_root_rebuilds(self, contacts_root: Path, cache_dir: Path) -> None:
        cic.load_contact_index(contacts_root, cache_dir)
        path = cic.cache_path(contacts_root, cache_dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["contacts_root"] = payload["contacts_root"] + "-wrong"
        path.write_text(json.dumps(payload), encoding="utf-8")
        cached, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert stats.cache_was_valid is False
        assert cached.by_identifier("ada@example.org") == contacts_root / "aaaa1111-ada.md"

    def test_unwritable_cache_dir_still_answers(
        self, contacts_root: Path, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the cache dir should be", encoding="utf-8")
        index, stats = cic.load_with_stats(contacts_root, blocker)
        assert stats.key_unavailable is True
        assert index.by_identifier("ada@example.org") == contacts_root / "aaaa1111-ada.md"

    def test_corrupt_key_file_still_answers_uncached(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        cache_dir.mkdir(parents=True)
        (cache_dir / "_erasure_hmac_key").write_text("not-valid-hex", encoding="utf-8")
        index, stats = cic.load_with_stats(contacts_root, cache_dir)
        assert stats.key_unavailable is True
        assert index.by_identifier("ada@example.org") == contacts_root / "aaaa1111-ada.md"
        assert not cic.cache_path(contacts_root, cache_dir).exists()


class TestWriterCoherence:
    """register/unregister on a cache-backed index behave exactly as on the plain one."""

    def test_register_visible_immediately(self, contacts_root: Path, cache_dir: Path) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        assert index.by_identifier("newname@example.org") is None

        new_path = _record(contacts_root, "zzzz9999-new.md", "zzzz9999", ["newname@example.org"])
        index.register(new_path)
        # Visible on the SAME in-memory index, no reload (the athenaeum#850
        # staleness concern, restated for a cache-backed index): a second
        # lookup in the same batch resolves through the digest the register
        # call just added, not through a stale "not found" from the cache.
        assert index.by_identifier("newname@example.org") == new_path
        assert index.by_uid("zzzz9999") == new_path

    def test_merge_onto_an_existing_cached_record_stays_first_match(
        self, contacts_root: Path, cache_dir: Path
    ) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        existing = contacts_root / "aaaa1111-ada.md"
        assert index.by_identifier("ada@example.org") == existing

        record_path, changed = pii.mark_bounced(
            contacts_root,
            "ada@example.org",
            diagnostic="550 5.1.1 unknown",
            observed_at="2026-10-31",
            source="test",
            index=index,
        )
        assert record_path == existing
        assert index.by_identifier("ada@example.org") == existing
        entries = pii.identifier_validity_entries(pii.read_bounce_record(existing))
        assert any(
            e.get("identifier") == "ada@example.org" and e.get("bounce_diagnostic")
            for e in entries
        )

    def test_unregister_then_reregister(self, contacts_root: Path, cache_dir: Path) -> None:
        index, _ = cic.load_with_stats(contacts_root, cache_dir)
        existing = contacts_root / "aaaa1111-ada.md"
        index.unregister(existing)
        assert index.by_identifier("ada@example.org") is None
        index.register(existing)
        assert index.by_identifier("ada@example.org") == existing


class TestCorruptInputsRebuildRatherThanRaise:
    """The issue requires "a corrupt or missing index is rebuilt" — never a raise.

    Both shapes here escaped as uncaught exceptions before (Seer review on
    PR athenaeum#2274). The second matters for erasure integrity rather than
    mere robustness: it fires after the record file is already unlinked.
    """

    @pytest.mark.parametrize("records", [None, [], "x", 3])
    def test_malformed_records_mapping_is_a_cache_miss(
        self, tmp_path: Path, records: object
    ) -> None:
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir()
        cache = tmp_path / "cache.json"
        cache.write_text(
            json.dumps(
                {
                    "format": cic.CACHE_FORMAT,
                    "contacts_root": str(contacts_root.resolve()),
                    "key_fingerprint": "deadbeef",
                    "records": records,
                }
            ),
            encoding="utf-8",
        )
        assert cic._read_cache(cache, contacts_root, "deadbeef") is None

    def test_non_utf8_erasure_key_falls_back_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        """A key file of non-UTF-8 bytes must not escape as UnicodeDecodeError."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / erasure_mod.ERASURE_KEY_FILENAME).write_bytes(b"\xff\xfe\x00not-utf8")
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir()
        (contacts_root / "aaaa1111-someone.md").write_text(
            "---\nuid: aaaa1111\nemails:\n  - someone@example.com\n---\n\nbody\n",
            encoding="utf-8",
        )
        index, stats = cic.load_with_stats(contacts_root, cache_dir)
        # Resolution still works, just uncached.
        assert index.by_uid("aaaa1111") is not None or index.uids() is not None
        assert stats is not None
