# SPDX-License-Identifier: Apache-2.0
"""Persisted EntityIndex cache and the uid page fast path for handle recall.

- ``TestCacheEquivalence``: a cached index is identical to a fresh
  ``EntityIndex`` build, including last-write-wins name/alias collisions.
- ``TestCacheInvalidation``: hit, add, edit (including a same-size edit with
  the mtime put back), delete.
- ``TestCacheRobustness``: corrupt, wrong-shape and wrong-version cache files
  rebuild; file modes; two concurrent cold rebuilds.
- ``TestHandleResolutionFastPath``: ``resolve_handle_query(cache_dir=...)``
  answers exactly what the uncached call answers, locates a page by filename
  without any full index build, falls back when the filename does not carry
  the uid, and scans the contact surface once per call.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import athenaeum
from athenaeum import entity_index_cache as eic
from athenaeum import identity_resolution, pii
from athenaeum.models import EntityIndex

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def _page(wiki: Path, filename: str, frontmatter: str, body: str = "Body.\n") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return path


def _shape(index: EntityIndex) -> tuple[object, ...]:
    types = {"person", "company", "project", ""}
    return (
        dict(index._by_name),
        dict(index._by_uid),
        set(index._entity_format_paths),
        {t: sorted(index.pages_of_type(t)) for t in types if t},
        len(index),
    )


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "knowledge" / "wiki"
    _page(
        wiki,
        "aaaa1111-ada-quill.md",
        "uid: aaaa1111\nname: Ada Quill\ntype: person\naliases:\n  - Ada\n  - AQ\n",
    )
    _page(
        wiki,
        "bbbb2222-bram-quill.md",
        "uid: bbbb2222\nname: Bram Quill\ntype: person\naliases:\n  - AQ\n",
    )
    _page(
        wiki,
        "cccc3333-widgetworks.md",
        "uid: cccc3333\nname: Widgetworks\nmetadata:\n  type: company\n",
    )
    _page(wiki, "dora-nouid.md", "name: Dora Nouid\ntype: person\n")
    _page(wiki, "eeee5555-noname.md", "uid: eeee5555\ntype: person\n")
    _page(wiki, "_index.md", "uid: ffff6666\nname: Should Not Index\n")
    _page(wiki, "gggg7777.md", "uid: gggg7777\nname: Gale Exact\ntype: project\n")
    (wiki / "broken.md").write_bytes(b"---\nname: \xff\xfe\n---\n")
    (wiki / "notes.txt").write_text("not a page", encoding="utf-8")
    return wiki


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


class TestCacheEquivalence:
    def test_cold_and_warm_match_full_build(self, wiki: Path, cache_dir: Path) -> None:
        full = EntityIndex(wiki)
        cold, cold_stats = eic.load_with_stats(wiki, cache_dir)
        warm, warm_stats = eic.load_with_stats(wiki, cache_dir)
        assert _shape(cold) == _shape(full)
        assert _shape(warm) == _shape(full)
        assert cold_stats.cache_was_valid is False and cold_stats.wrote is True
        assert warm_stats.cache_was_valid is True and warm_stats.reparsed == 0
        assert warm_stats.wrote is False

    def test_last_write_wins_collision_survives_an_edit_to_the_earlier_page(
        self, wiki: Path, cache_dir: Path
    ) -> None:
        # "aq" is an alias on both Ada (sorts first) and Bram (sorts second):
        # the full scan keeps Bram's. Re-parsing only Ada must not move it.
        eic.load_entity_index(wiki, cache_dir)
        _page(
            wiki,
            "aaaa1111-ada-quill.md",
            "uid: aaaa1111\nname: Ada Quill\ntype: person\naliases:\n  - Ada\n  - AQ\n  - Q\n",
        )
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.reparsed == 1
        assert cached.lookup("AQ").uid == "bbbb2222"
        assert _shape(cached) == _shape(EntityIndex(wiki))

    def test_lookup_methods_agree(self, wiki: Path, cache_dir: Path) -> None:
        full = EntityIndex(wiki)
        cached = eic.load_entity_index(wiki, cache_dir)
        for key in ("ada", "aq", "widgetworks", "dora nouid", "gale exact", "missing"):
            assert cached.lookup(key) == full.lookup(key)
        for uid in ("aaaa1111", "cccc3333", "eeee5555", "ffff6666", "gggg7777"):
            path = full.get_by_uid(uid)
            assert cached.get_by_uid(uid) == path
            if path is not None:
                assert cached.has_entity_format(path) is True


class TestCacheInvalidation:
    def test_added_page_is_indexed(self, wiki: Path, cache_dir: Path) -> None:
        eic.load_entity_index(wiki, cache_dir)
        _page(wiki, "hhhh8888-hal-new.md", "uid: hhhh8888\nname: Hal New\ntype: person\n")
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.reparsed == 1
        assert cached.get_by_uid("hhhh8888") == wiki / "hhhh8888-hal-new.md"
        assert _shape(cached) == _shape(EntityIndex(wiki))

    def test_edited_page_is_reparsed(self, wiki: Path, cache_dir: Path) -> None:
        eic.load_entity_index(wiki, cache_dir)
        _page(
            wiki,
            "cccc3333-widgetworks.md",
            "uid: cccc3333\nname: Gadgetworks Incorporated\ntype: company\n",
        )
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.reparsed == 1
        assert cached.lookup("widgetworks") is None
        assert cached.lookup("gadgetworks incorporated").uid == "cccc3333"
        assert _shape(cached) == _shape(EntityIndex(wiki))

    def test_same_size_edit_with_mtime_restored_is_still_seen(
        self, wiki: Path, cache_dir: Path
    ) -> None:
        target = wiki / "cccc3333-widgetworks.md"
        before = target.stat()
        eic.load_entity_index(wiki, cache_dir)
        replacement = target.with_name("tmp-replacement")
        replacement.write_text(
            target.read_text(encoding="utf-8").replace("Widgetworks", "Widgetwerks"),
            encoding="utf-8",
        )
        os.replace(replacement, target)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert target.stat().st_size == before.st_size
        assert target.stat().st_mtime_ns == before.st_mtime_ns
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.reparsed == 1
        assert cached.lookup("widgetwerks").uid == "cccc3333"

    def test_deleted_page_is_dropped(self, wiki: Path, cache_dir: Path) -> None:
        eic.load_entity_index(wiki, cache_dir)
        (wiki / "bbbb2222-bram-quill.md").unlink()
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.removed == 1 and stats.wrote is True
        assert cached.get_by_uid("bbbb2222") is None
        assert cached.lookup("aq").uid == "aaaa1111"
        assert _shape(cached) == _shape(EntityIndex(wiki))
        names = json.loads(eic.cache_path(wiki, cache_dir).read_text(encoding="utf-8"))["pages"]
        assert "bbbb2222-bram-quill.md" not in names


class TestCacheRobustness:
    @pytest.mark.parametrize(
        "content",
        [
            "not json at all {",
            json.dumps([1, 2, 3]),
            json.dumps({"format": eic.CACHE_FORMAT, "wiki_root": "x", "pages": {}}),
            json.dumps({"format": eic.CACHE_FORMAT + 1, "pages": {}}),
            "WRONG_ROOT",
            "BAD_ENTRY",
        ],
    )
    def test_corrupt_cache_rebuilds(self, wiki: Path, cache_dir: Path, content: str) -> None:
        path = eic.cache_path(wiki, cache_dir)
        path.parent.mkdir(parents=True)
        root = str(wiki.resolve())
        if content == "WRONG_ROOT":
            content = json.dumps({"format": eic.CACHE_FORMAT, "wiki_root": root + "x", "pages": {}})
        elif content == "BAD_ENTRY":
            content = json.dumps(
                {
                    "format": eic.CACHE_FORMAT,
                    "wiki_root": root,
                    "pages": {
                        "aaaa1111-ada-quill.md": [[1, 2, 3, 4], ["aaaa1111", "", "person", []]]
                    },
                }
            )
        path.write_text(content, encoding="utf-8")
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.cache_was_valid is False
        assert _shape(cached) == _shape(EntityIndex(wiki))
        # ...and the rebuilt file is a valid cache for the next call.
        assert eic.load_with_stats(wiki, cache_dir)[1].reparsed == 0

    def test_poisoned_record_with_valid_stamps_is_trusted_only_while_stamps_match(
        self, wiki: Path, cache_dir: Path
    ) -> None:
        # A cache entry is a claim about a specific file version; once the
        # file changes, the claim is discarded however plausible it looks.
        eic.load_entity_index(wiki, cache_dir)
        path = eic.cache_path(wiki, cache_dir)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["pages"]["cccc3333-widgetworks.md"][0][0] -= 1
        raw["pages"]["cccc3333-widgetworks.md"][1][1] = "Stale Name"
        path.write_text(json.dumps(raw), encoding="utf-8")
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.reparsed == 1
        assert cached.lookup("stale name") is None

    def test_modes(self, wiki: Path, cache_dir: Path) -> None:
        eic.load_entity_index(wiki, cache_dir)
        path = eic.cache_path(wiki, cache_dir)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        os.chmod(path, 0o644)
        _page(wiki, "hhhh8888-hal-new.md", "uid: hhhh8888\nname: Hal New\n")
        eic.load_entity_index(wiki, cache_dir)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_unwritable_cache_dir_still_answers(self, wiki: Path, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the cache dir should be", encoding="utf-8")
        cached, stats = eic.load_with_stats(wiki, blocker)
        assert stats.wrote is False
        assert _shape(cached) == _shape(EntityIndex(wiki))

    def test_concurrent_cold_rebuilds(self, wiki: Path, cache_dir: Path) -> None:
        for i in range(200):
            _page(wiki, f"p{i:04d}-person.md", f"uid: p{i:04d}\nname: Person {i}\ntype: person\n")
        src = str(Path(athenaeum.__file__).resolve().parents[1])
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from athenaeum import entity_index_cache as eic\n"
            "ix = eic.load_entity_index(Path(sys.argv[1]), Path(sys.argv[2]))\n"
            "print(json.dumps(sorted((k, str(v)) for k, v in ix._by_uid.items())))\n"
        )
        env = dict(os.environ, PYTHONPATH=src)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(wiki), str(cache_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outputs.append(json.loads(out))
        expected = sorted((k, str(v)) for k, v in EntityIndex(wiki)._by_uid.items())
        assert all(o == [list(pair) for pair in expected] for o in outputs)
        cached, stats = eic.load_with_stats(wiki, cache_dir)
        assert stats.cache_was_valid is True and stats.reparsed == 0
        assert _shape(cached) == _shape(EntityIndex(wiki))
        leftovers = [
            p for p in eic.cache_path(wiki, cache_dir).parent.iterdir() if p.suffix == ".tmp"
        ]
        assert leftovers == []


def _record(root: Path, filename: str, uid: str, email: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_text(
        f"---\nuid: {uid}\npii: true\nemails:\n  - {email}\n---\n\nArchival data.\n",
        encoding="utf-8",
    )


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    _page(wiki, "aaaa1111-ada-quill.md", "uid: aaaa1111\nname: Ada Quill\ntype: person\n")
    _record(contacts, "aaaa1111-ada.md", "aaaa1111", "ada@example.org")
    # Filename does not carry the uid: the fast path must fall back.
    _page(wiki, "bram-quill.md", "uid: bbbb2222\nname: Bram Quill\ntype: person\n")
    _record(contacts, "bbbb2222-bram.md", "bbbb2222", "bram@example.org")
    # Address shared by two people: ambiguous.
    _record(contacts, "cccc3333-cleo.md", "cccc3333", "shared@example.org")
    _record(contacts, "dddd4444-dev.md", "dddd4444", "shared@example.org")
    # Record whose uid has no page: orphan.
    _record(contacts, "eeee5555-eve.md", "eeee5555", "eve@example.org")
    # Prefixed file that carries a DIFFERENT uid: not a fast-path hit.
    _page(wiki, "ffff6666-old-name.md", "uid: 99999999\nname: Fern Moved\ntype: person\n")
    _page(wiki, "ffff6666-fern.md", "uid: ffff6666\nname: Fern Real\ntype: person\n")
    _record(contacts, "ffff6666-fern.md", "ffff6666", "fern@example.org")
    return knowledge


QUERIES = [
    "ada@example.org",
    "bram@example.org",
    "shared@example.org",
    "eve@example.org",
    "fern@example.org",
    "nobody@example.org",
]


class TestHandleResolutionFastPath:
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

    def test_expected_dispositions(self, knowledge: Path, cache_dir: Path) -> None:
        got = {
            q: identity_resolution.resolve_handle_query(
                knowledge, knowledge / "wiki", q, config=EXCLUDED_CONFIG, cache_dir=cache_dir
            )
            for q in QUERIES
        }
        assert got["ada@example.org"].uid == "aaaa1111"
        assert got["bram@example.org"].resolved is True  # not orphan-uid
        assert got["bram@example.org"].display_name == "Bram Quill"
        assert got["shared@example.org"].reason == "ambiguous"
        assert got["eve@example.org"].reason == "orphan-uid"
        assert got["fern@example.org"].display_name == "Fern Real"
        assert got["nobody@example.org"].reason == "no-match"

    def test_filename_hit_builds_no_index(
        self, knowledge: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("full index built on the fast path")

        monkeypatch.setattr(EntityIndex, "_load", _explode)
        monkeypatch.setattr(eic, "load_with_stats", _explode)
        result = identity_resolution.resolve_handle_query(
            knowledge,
            knowledge / "wiki",
            "ada@example.org",
            config=EXCLUDED_CONFIG,
            cache_dir=cache_dir,
        )
        assert result is not None and result.uid == "aaaa1111"
        assert not eic.cache_path(knowledge / "wiki", cache_dir).exists()

    def test_unprefixed_page_falls_back_to_the_cache(
        self, knowledge: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("fresh full scan instead of the persisted cache")

        result = identity_resolution.resolve_handle_query(
            knowledge,
            knowledge / "wiki",
            "bram@example.org",
            config=EXCLUDED_CONFIG,
            cache_dir=cache_dir,
        )
        assert result is not None and result.uid == "bbbb2222"
        assert eic.cache_path(knowledge / "wiki", cache_dir).exists()
        # Warm: served from the cache file, no page re-parsed.
        monkeypatch.setattr(EntityIndex, "_load", _explode)
        result = identity_resolution.resolve_handle_query(
            knowledge,
            knowledge / "wiki",
            "bram@example.org",
            config=EXCLUDED_CONFIG,
            cache_dir=cache_dir,
        )
        assert result is not None and result.uid == "bbbb2222"

    def test_two_prefixed_pages_with_the_uid_fall_back(
        self, knowledge: Path, cache_dir: Path
    ) -> None:
        wiki = knowledge / "wiki"
        _page(wiki, "aaaa1111-ada-quill-copy.md", "uid: aaaa1111\nname: Ada Copy\ntype: person\n")
        assert identity_resolution._page_by_uid_filename(wiki, "aaaa1111") is None
        uncached = identity_resolution.resolve_handle_query(
            knowledge, wiki, "ada@example.org", config=EXCLUDED_CONFIG
        )
        cached = identity_resolution.resolve_handle_query(
            knowledge, wiki, "ada@example.org", config=EXCLUDED_CONFIG, cache_dir=cache_dir
        )
        assert cached.to_dict() == uncached.to_dict()

    @pytest.mark.parametrize("uid", ["../aaaa1111", "aaaa*", "a/b", "", "[a]"])
    def test_unsafe_uid_skips_the_filename_glob(self, knowledge: Path, uid: str) -> None:
        assert identity_resolution._page_by_uid_filename(knowledge / "wiki", uid) is None

    def test_contact_surface_scanned_once_per_call(
        self, knowledge: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0
        original = pii.iter_contact_records

        def _counting(root: Path) -> list[Path]:
            nonlocal calls
            calls += 1
            return original(root)

        monkeypatch.setattr(pii, "iter_contact_records", _counting)
        result = identity_resolution.resolve_handle_query(
            knowledge, knowledge / "wiki", "ada@example.org", config=EXCLUDED_CONFIG, with_pii=True
        )
        assert result is not None and result.contact_values
        assert calls == 1
