# SPDX-License-Identifier: Apache-2.0
"""Store-injection parity suite for :mod:`athenaeum.quarantine` (issue athenaeum#982,
slice S7 of the whole-store adapter design lock, issue athenaeum#911).

``tests/test_quarantine.py`` is the pre-migration suite and is left byte-for-byte
UNCHANGED (AC 3) — every one of its assertions still passes because it never
passes ``store=``, so ``quarantine.py`` falls back to a private
:class:`~athenaeum.store.FilesystemStore` scoped to the same ``wiki_root``/
``raw_root`` it always took, and reads/writes real files at exactly the same
paths as before the migration.

That suite cannot simply be re-run against
:class:`tests.store_fakes.InMemoryStore`, though: its test bodies assert
directly against the real filesystem (``raw.path.exists()``,
``moved.read_text(...)``, etc.), which is exactly the implementation detail
the store migration exists to hide from a caller. Demonstrating AC 4 ("the
same test suite also passes against the in-memory fake adapter... this is
what demonstrates the 'no caller can tell' property rather than asserting
it") therefore means the SAME logical scenarios test_quarantine.py covers —
quarantine moves the object and records it, release reverses it, pending
listing excludes released items, unknown/already-released ids raise, a
missing object at release time still releases the record, and the
ledger-before-move ordering survives a move failure — reimplemented against
the ``store=`` injection point, parametrized over both S1 implementations,
and verified by reading BACK THROUGH THE STORE rather than by peeking at the
filesystem. A caller that gets identical outcomes from both backends, using
only the ``Store`` protocol to check, is the property AC 4 asks for.

Seeding a raw object for a test uses :meth:`~athenaeum.store.Store.put` — the
one store primitive quarantine.py itself has no write site for (its own
write surface is exactly two primitives, ``append`` and ``move``; see the
issue athenaeum#982 report for the full inventory) — which is how ``put``
enters this slice's test coverage despite not appearing in production code.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from athenaeum import quarantine
from athenaeum.store import FilesystemStore, Store, StoreKey
from tests.store_fakes import InMemoryStore

_WIKI_SURFACE = quarantine._WIKI_SURFACE
_RAW_SURFACE = quarantine._RAW_SURFACE


# ---------------------------------------------------------------------------
# Implementations under test (mirrors tests/test_store_conformance.py's shape)
# ---------------------------------------------------------------------------


def _make_filesystem_store(tmp_path: Path) -> Store:
    wiki_root = tmp_path / "wiki"
    raw_root = tmp_path / "raw"
    wiki_root.mkdir()
    raw_root.mkdir()
    return FilesystemStore(tmp_path, roots={_WIKI_SURFACE: wiki_root, _RAW_SURFACE: raw_root})


def _make_in_memory_store(tmp_path: Path) -> Store:
    return InMemoryStore()


_IMPLEMENTATIONS: dict[str, Callable[[Path], Store]] = {
    "filesystem": _make_filesystem_store,
    "in-memory": _make_in_memory_store,
}


@pytest.fixture(params=sorted(_IMPLEMENTATIONS))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    """One ``Store``, parametrized over every S1 implementation."""
    factory = _IMPLEMENTATIONS[request.param]
    return factory(tmp_path)


class _FakeRaw:
    """Minimal RawFile double — path/source/ref, no content needed here.

    Mirrors ``tests/test_quarantine.py::_FakeRaw`` exactly; duplicated rather
    than imported so this module has no dependency on another test module's
    internals.
    """

    def __init__(self, path: Any, source: str) -> None:
        self.path = path
        self.source = source

    @property
    def ref(self) -> str:
        return f"{self.source}/{Path(self.path).name}"


def _seed_raw(
    store: Store, wiki_root: Path, raw_root: Path, *, rel_key: str, content: bytes
) -> _FakeRaw:
    """Put *content* under the raw surface at *rel_key* and hand back a
    ``_FakeRaw`` whose ``.path`` resolves to that same key relative to
    *raw_root* — the one seeding step every scenario below shares."""
    store.put(StoreKey(surface=_RAW_SURFACE, key=rel_key), content, expect=None)
    return _FakeRaw(path=raw_root / rel_key, source=rel_key.split("/", 1)[0])


# ---------------------------------------------------------------------------
# quarantine_file — move raw -> wiki, ledger record appended
# ---------------------------------------------------------------------------


class TestQuarantineFileParity:
    def test_moves_object_out_of_raw_surface(self, store: Store, tmp_path: Path) -> None:
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        raw = _seed_raw(
            store, wiki_root, raw_root, rel_key="sessions/x.md", content=b"Some content.\n"
        )

        record = quarantine.quarantine_file(
            raw,
            wiki_root=wiki_root,
            raw_root=raw_root,
            bound="bytes",
            detail="d",
            violations=2,
            store=store,
        )

        # Source key is gone ...
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(StoreKey(surface=_RAW_SURFACE, key="sessions/x.md"))
        # ... and the destination key holds the original bytes, addressed by
        # the same relative key the ledger record names.
        dest = StoreKey(surface=_WIKI_SURFACE, key=record["quarantine_path"])
        assert store.read(dest) == b"Some content.\n"
        assert record["original_path"] == "sessions/x.md"

    def test_writes_readable_ledger_record(self, store: Store, tmp_path: Path) -> None:
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        raw = _seed_raw(store, wiki_root, raw_root, rel_key="sessions/x.md", content=b"c")

        record = quarantine.quarantine_file(
            raw,
            wiki_root=wiki_root,
            raw_root=raw_root,
            bound="llm_calls",
            detail="12 call(s) > 8-call limit",
            violations=3,
            store=store,
        )

        ledger = quarantine.read_quarantine_ledger(wiki_root, store=store)
        assert ledger == [record]
        assert record["kind"] == quarantine.QUARANTINE_KIND
        assert record["bound"] == "llm_calls"
        assert record["violations"] == 3

    def test_ledger_written_before_move_survives_a_move_failure(
        self, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backend-agnostic counterpart to
        ``test_quarantine.py::test_ledger_record_written_before_the_move_survives_a_move_failure``
        — that test's ``monkeypatch.setattr("athenaeum.quarantine.shutil.move", ...)``
        can no longer intercept anything (quarantine.py imports neither
        ``pathlib`` nor ``shutil`` post-migration; see the issue athenaeum#982
        report), so this monkeypatches the STORE INSTANCE's ``move`` instead —
        the seam the ordering guarantee now actually runs through, and one
        that works identically regardless of which backend it wraps.
        """
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        raw = _seed_raw(store, wiki_root, raw_root, rel_key="sessions/x.md", content=b"c")

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated disk-full mid-move")

        monkeypatch.setattr(store, "move", _boom)

        with pytest.raises(OSError, match="simulated disk-full"):
            quarantine.quarantine_file(
                raw,
                wiki_root=wiki_root,
                raw_root=raw_root,
                bound="bytes",
                detail="d",
                violations=2,
                store=store,
            )

        # The ledger record landed anyway (detectable) ...
        ledger = quarantine.read_quarantine_ledger(wiki_root, store=store)
        assert len(ledger) == 1
        assert ledger[0]["ref"] == raw.ref
        # ... and the source object was never moved (still exactly where it
        # was — the failing ``move`` never ran to completion).
        assert store.read(StoreKey(surface=_RAW_SURFACE, key="sessions/x.md")) == b"c"


# ---------------------------------------------------------------------------
# list_pending_quarantine / release_quarantine
# ---------------------------------------------------------------------------


class TestReleaseQuarantineParity:
    def _quarantine_one(self, store: Store, wiki_root: Path, raw_root: Path) -> dict[str, Any]:
        raw = _seed_raw(store, wiki_root, raw_root, rel_key="sessions/x.md", content=b"c")
        return quarantine.quarantine_file(
            raw,
            wiki_root=wiki_root,
            raw_root=raw_root,
            bound="bytes",
            detail="d",
            violations=2,
            store=store,
        )

    def test_quarantine_without_release_is_pending(self, store: Store, tmp_path: Path) -> None:
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        record = self._quarantine_one(store, wiki_root, raw_root)

        pending = quarantine.list_pending_quarantine(wiki_root, store=store)
        assert len(pending) == 1
        assert pending[0]["id"] == record["id"]

    def test_moves_object_back_and_writes_release_record(
        self, store: Store, tmp_path: Path
    ) -> None:
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        record = self._quarantine_one(store, wiki_root, raw_root)

        release = quarantine.release_quarantine(
            wiki_root,
            raw_root,
            quarantine_id=record["id"],
            note="reviewed, false positive",
            store=store,
        )

        assert release["kind"] == quarantine.RELEASE_KIND
        assert release["note"] == "reviewed, false positive"
        # Back on the raw surface, at the original key ...
        assert store.read(StoreKey(surface=_RAW_SURFACE, key="sessions/x.md")) == b"c"
        # ... and gone from the wiki-surface holding key.
        with pytest.raises((FileNotFoundError, KeyError)):
            store.read(StoreKey(surface=_WIKI_SURFACE, key=record["quarantine_path"]))
        assert quarantine.list_pending_quarantine(wiki_root, store=store) == []

    def test_unknown_id_raises(self, store: Store, tmp_path: Path) -> None:
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        with pytest.raises(ValueError, match="unknown quarantine item id"):
            quarantine.release_quarantine(
                wiki_root, raw_root, quarantine_id="nope", store=store
            )

    def test_already_released_raises(self, store: Store, tmp_path: Path) -> None:
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        record = self._quarantine_one(store, wiki_root, raw_root)
        quarantine.release_quarantine(
            wiki_root, raw_root, quarantine_id=record["id"], store=store
        )
        with pytest.raises(ValueError, match="already released"):
            quarantine.release_quarantine(
                wiki_root, raw_root, quarantine_id=record["id"], store=store
            )

    def test_missing_object_at_release_time_still_releases_the_record(
        self, store: Store, tmp_path: Path
    ) -> None:
        """An operator who removed the quarantined object out-of-band must
        still be able to clear the pending decision — mirrors
        ``tests/test_quarantine.py``'s identical scenario, but removes the
        object via :meth:`~athenaeum.store.Store.delete` (works uniformly
        across backends) rather than ``Path.unlink()``."""
        wiki_root, raw_root = tmp_path / "wiki", tmp_path / "raw"
        record = self._quarantine_one(store, wiki_root, raw_root)
        store.delete(StoreKey(surface=_WIKI_SURFACE, key=record["quarantine_path"]))

        release = quarantine.release_quarantine(
            wiki_root, raw_root, quarantine_id=record["id"], store=store
        )
        assert release["kind"] == quarantine.RELEASE_KIND
        assert quarantine.list_pending_quarantine(wiki_root, store=store) == []
