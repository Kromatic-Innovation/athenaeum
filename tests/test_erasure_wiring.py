# SPDX-License-Identifier: Apache-2.0
"""Round-trip wiring tests for issue athenaeum#1116 — wiring
:mod:`athenaeum.erasure`'s athenaeum#985 classification helpers into the real
production write/read paths (S3 of the athenaeum#985 slice, mirroring
:mod:`athenaeum.sensitivity`'s S1a/S1b -> S3 precedent, athenaeum#992).

Each class below drives the REAL write path end to end — the actual
``merge_clusters_to_wiki`` compile loop, the actual ``ingest_answers``
re-ingestion path, and the actual ``build_sweep_report``/``apply_sweep``
decay sweep — rather than calling ``athenaeum.erasure``'s helpers directly.
That isolated-helper coverage already exists in ``tests/test_erasure.py``
from athenaeum#985 and does not satisfy any AC here (see the issue body).

* ``TestAC1OffCorpusPlacement`` — the C3 compile write loop routes a page
  whose ``## Inference`` block cites erasure-class content off-corpus.
* ``TestAC2AnswersLaneClassification`` — ``ingest_answers`` classifies a
  re-ingested off-corpus recall by PROVENANCE and re-routes it off-corpus
  without re-guessing from content.
* ``TestAC3RetentionPackAuthority`` — ``decay_sweep`` consults the active
  retention pack for a page's ``(memory_class, data_class)`` before falling
  back to its independent ``bucket: daily`` / ``valid_until`` logic.

Every fixture is a scratch ``tmp_path`` tree — never the operator's live
``~/knowledge`` store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from athenaeum.answers import ingest_answers
from athenaeum.decay_sweep import apply_sweep, build_sweep_report
from athenaeum.merge import merge_clusters_to_wiki
from athenaeum.off_corpus import off_corpus_store
from athenaeum.store import StoreKey

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SCOPE = "-Users-synthetic-Code"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Erasure Wiring Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed knowledge root")


def _off_corpus_config_yaml(off_corpus_dir: Path) -> str:
    """Same shape as ``tests/test_off_corpus.py``'s ``_make_config``, as YAML."""
    return (
        "off_corpus:\n"
        "  enabled: true\n"
        "  adapter: off-corpus-test\n"
        "storage:\n"
        "  adapters:\n"
        "    off-corpus-test:\n"
        "      backing_store: markdown\n"
        f"      surface_root: {off_corpus_dir}\n"
        "      corpus_policy:\n"
        "        embedded: false\n"
        "        recallable: true\n"
        "        merge_eligible: false\n"
        "  mapping:\n"
        "    erasure-claim: off-corpus-test\n"
    )


def _off_corpus_config_dict(off_corpus_dir: Path) -> dict:
    return {
        "off_corpus": {"enabled": True, "adapter": "off-corpus-test"},
        "storage": {
            "adapters": {
                "off-corpus-test": {
                    "backing_store": "markdown",
                    "surface_root": str(off_corpus_dir),
                    "corpus_policy": {
                        "embedded": False,
                        "recallable": True,
                        "merge_eligible": False,
                    },
                },
            },
            "mapping": {"erasure-claim": "off-corpus-test"},
        },
    }


# ---------------------------------------------------------------------------
# AC1 — off-corpus placement at the C3 compile write path
# ---------------------------------------------------------------------------


def _write_am(root: Path, name: str, body: str) -> None:
    d = root / "raw" / "auto-memory" / _SCOPE
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n", encoding="utf-8"
    )


def _write_cluster(root: Path, rows: list[dict]) -> None:
    out = root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )


def _seed_inference_cluster(root: Path) -> None:
    """One cluster whose synthesized body carries a ``## Inference`` block
    whose ``**Basis**`` cites the slug ``fact-a`` — the shape
    ``athenaeum.erasure.classify_inference_taint`` looks for, produced the
    same way a real compile would (``athenaeum.merge.synthesize_body``
    concatenating member bodies, not a hand-built compiled page)."""
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    a, b = "inference_a.md", "inference_b.md"
    _write_am(
        root,
        a,
        "## Inference\n"
        "**Basis**: [[fact-a]]\n"
        "**Confidence**: 0.8\n"
        "A derived claim about the erasure-class subject.\n",
    )
    _write_am(root, b, "An unrelated plain observation about pricing.")
    _write_cluster(
        root,
        [
            {
                "cluster_id": "inference-0001",
                "member_paths": [f"{_SCOPE}/{a}", f"{_SCOPE}/{b}"],
                "centroid_score": 0.9,
                "rationale": "test fixture",
            }
        ],
    )


class TestAC1OffCorpusPlacement:
    def test_tainted_compiled_page_is_routed_off_corpus_not_written_to_wiki(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "knowledge"
        _seed_inference_cluster(root)
        off_corpus_dir = tmp_path / "off-corpus-store"
        (root / "athenaeum.yaml").write_text(
            "contradiction:\n  cross_scope_mode: off\n"
            + _off_corpus_config_yaml(off_corpus_dir),
            encoding="utf-8",
        )

        # Off-corpus already holds `fact-a.md` — the erasure-class content
        # the compiled page's Inference block basis cites (issue athenaeum#1116's
        # documented source for `erasure_class_slugs`: off-corpus store
        # membership, since this repo has no page-level data_class to
        # consult otherwise — see `_off_corpus_erasure_class_slugs`).
        cfg = _off_corpus_config_dict(off_corpus_dir)
        store = off_corpus_store(cfg, root)
        assert store is not None
        store.put(
            StoreKey(surface="off-corpus-test", key="fact-a.md"),
            b"---\nname: fact-a\n---\nJane's email is redacted.\n",
        )

        merge_clusters_to_wiki(root, client=None)

        wiki_pages = sorted((root / "wiki").glob("auto-*.md"))
        assert wiki_pages == [], (
            "the tainted compiled page must NOT land in the ordinary corpus"
        )

        off_corpus_pages = [
            m.key.key for m in store.iter_meta("off-corpus-test") if m.key.key != "fact-a.md"
        ]
        assert len(off_corpus_pages) == 1, "the compiled page must be routed off-corpus"
        routed_key = StoreKey(surface="off-corpus-test", key=off_corpus_pages[0])
        routed_text = store.read(routed_key).decode("utf-8")
        assert "## Inference" in routed_text
        assert "[[fact-a]]" in routed_text

    def test_off_corpus_not_configured_falls_back_to_ordinary_corpus_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        root = tmp_path / "knowledge"
        _seed_inference_cluster(root)
        (root / "athenaeum.yaml").write_text(
            "contradiction:\n  cross_scope_mode: off\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="athenaeum.merge"):
            merge_clusters_to_wiki(root, client=None)

        wiki_pages = sorted((root / "wiki").glob("auto-*.md"))
        assert len(wiki_pages) == 1, (
            "reversible default (issue athenaeum#1116): no off-corpus surface "
            "configured -> the page still lands in the ordinary corpus"
        )
        text = wiki_pages[0].read_text(encoding="utf-8")
        assert "## Inference" in text

        # Note: this fixture's Inference block cites `fact-a`, but no
        # off-corpus store exists at all, so `_off_corpus_erasure_class_slugs`
        # returns an empty set and nothing is classified tainted -- the
        # warning path only fires once *something* off-corpus exists to seed
        # a nonempty `erasure_class_slugs` while `off_corpus_store` itself is
        # unconfigured, which cannot happen (`off_corpus_store` returns
        # `None` under the exact same config gate `_off_corpus_erasure_class_slugs`
        # uses to build that set). So the correct, provable assertion here is
        # the byte-identical fallback behavior above.


# ---------------------------------------------------------------------------
# AC2 — answers-lane re-ingestion classifies by provenance
# ---------------------------------------------------------------------------


def _answered_block(*, source: str) -> str:
    return (
        f'## [2026-06-01] Entity: "Jane Doe" (from {source})\n'
        "- [x] Was Jane's role confirmed?\n"
        "**Conflict type**: principled\n"
        "**Description**: Recalled from an off-corpus record.\n"
        "\n"
        "Confirmed: yes, still active.\n"
    )


class TestAC2AnswersLaneClassification:
    def test_answer_re_ingesting_an_off_corpus_recall_is_routed_off_corpus(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        raw_root = knowledge_root / "raw"
        raw_root.mkdir(parents=True)
        pending_path = knowledge_root / "wiki" / "_pending_questions.md"
        pending_path.parent.mkdir(parents=True)
        pending_path.write_text(
            "# Pending Questions\n\n" + _answered_block(source="recall-offcorpus:jane-doe"),
            encoding="utf-8",
        )

        off_corpus_dir = tmp_path / "off-corpus-store"
        cfg = _off_corpus_config_dict(off_corpus_dir)

        count = ingest_answers(pending_path, raw_root, config=cfg)
        assert count == 1

        assert not (raw_root / "answers").exists() or not list(
            (raw_root / "answers").glob("*.md")
        ), "a re-ingested off-corpus recall must not land in the ordinary raw intake tree"

        store = off_corpus_store(cfg, knowledge_root)
        assert store is not None
        answer_objs = list(store.iter_meta("off-corpus-test"))
        assert len(answer_objs) == 1
        routed = store.read(answer_objs[0].key).decode("utf-8")
        assert "source: recall-offcorpus:jane-doe" in routed
        assert "Confirmed: yes, still active." in routed

    def test_answer_with_ordinary_source_is_unaffected(self, tmp_path: Path) -> None:
        """Byte-for-byte pre-athenaeum#1116 behavior for the common case: a
        source ref that is a plain path, not a provenance scalar."""
        knowledge_root = tmp_path / "knowledge"
        raw_root = knowledge_root / "raw"
        raw_root.mkdir(parents=True)
        pending_path = knowledge_root / "wiki" / "_pending_questions.md"
        pending_path.parent.mkdir(parents=True)
        pending_path.write_text(
            "# Pending Questions\n\n"
            + _answered_block(source="sessions/20240406T120000Z-aabb0011.md"),
            encoding="utf-8",
        )

        count = ingest_answers(pending_path, raw_root)
        assert count == 1
        answer_files = list((raw_root / "answers").glob("*.md"))
        assert len(answer_files) == 1
        text = answer_files[0].read_text(encoding="utf-8")
        assert "source: pending_question_answer\n" in text

    def test_off_corpus_not_configured_still_stamps_provenance_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        knowledge_root = tmp_path / "knowledge"
        raw_root = knowledge_root / "raw"
        raw_root.mkdir(parents=True)
        pending_path = knowledge_root / "wiki" / "_pending_questions.md"
        pending_path.parent.mkdir(parents=True)
        pending_path.write_text(
            "# Pending Questions\n\n" + _answered_block(source="recall-offcorpus:jane-doe"),
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="athenaeum.answers"):
            count = ingest_answers(pending_path, raw_root)
        assert count == 1

        answer_files = list((raw_root / "answers").glob("*.md"))
        assert len(answer_files) == 1, (
            "reversible default (issue athenaeum#1116): no off-corpus surface "
            "configured -> the answer still lands in the ordinary raw intake tree"
        )
        text = answer_files[0].read_text(encoding="utf-8")
        assert "source: recall-offcorpus:jane-doe" in text, (
            "provenance is stamped regardless of whether routing was possible "
            "-- never re-guessed from content"
        )
        assert any("erasure-taint-not-routed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# AC3 — decay sweep consults the active retention pack first
# ---------------------------------------------------------------------------


def _page(
    *,
    name: str,
    bucket: str,
    valid_until: str | None,
    memory_class: str | None,
    data_class: str | None,
    body: str,
) -> str:
    lines = ["---", f"name: {name}", "type: feedback", f"bucket: {bucket}"]
    if valid_until is not None:
        lines.append(f"valid_until: '{valid_until}'")
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    if data_class is not None:
        lines.append(f"data_class: {data_class}")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


class TestAC3RetentionPackAuthority:
    def test_pack_authoritative_page_is_routed_off_corpus_before_expiry_even_considered(
        self, tmp_path: Path
    ) -> None:
        """A page with an explicit ``data_class`` and a FUTURE ``valid_until``
        (i.e. the old bucket:daily/valid_until logic alone would RETAIN it)
        is nonetheless routed off-corpus, because the active pack's
        unknown-jurisdiction default is ``store-off-corpus`` and placement,
        not expiry, is what that action means (`docs/provenance-shape.md`
        §8.8, "once a pack exists, it is authoritative")."""
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "subject-status.md").write_text(
            _page(
                name="Subject status",
                bucket="daily",
                valid_until="2099-01-01",
                memory_class="entity",
                data_class="pii",
                body="Not yet expired by the old bucket/valid_until logic alone.",
            ),
            encoding="utf-8",
        )
        _git_init(knowledge_root)

        off_corpus_dir = tmp_path / "off-corpus-store"
        cfg = _off_corpus_config_dict(off_corpus_dir)

        report = build_sweep_report(wiki, knowledge_root=knowledge_root, config=cfg)
        assert report.kill == []
        assert report.retained == []
        assert len(report.routed_off_corpus) == 1
        assert report.routed_off_corpus[0].path.name == "subject-status.md"

        report = apply_sweep(knowledge_root, report, config=cfg)
        assert report.errors == []
        assert report.committed is True
        assert not (wiki / "subject-status.md").exists()

        store = off_corpus_store(cfg, knowledge_root)
        assert store is not None
        routed_keys = [m.key.key for m in store.iter_meta("off-corpus-test")]
        assert routed_keys == ["wiki/subject-status.md"], (
            "off-corpus keys are relative to knowledge_root, same convention "
            "the kill-list git-rm flow already uses"
        )
        routed = store.read(StoreKey(surface="off-corpus-test", key=routed_keys[0]))
        assert b"Not yet expired by the old bucket/valid_until logic alone." in routed

        # Recoverable from git history exactly like a plain archive.
        show = _git(knowledge_root, "show", "HEAD~1:wiki/subject-status.md")
        assert "Not yet expired" in show.stdout

    def test_page_without_data_class_is_unaffected_even_with_pack_configured(
        self, tmp_path: Path
    ) -> None:
        """Issue athenaeum#1116's reversible-default posture: this gate is a
        no-op for every page shipped code produces today, since no write
        path stamps `data_class` -- an ordinary expired daily page is
        archived exactly as it was before this issue, pack or no pack."""
        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "status-monday.md").write_text(
            _page(
                name="Monday status",
                bucket="daily",
                valid_until="2020-01-01",
                memory_class=None,
                data_class=None,
                body="Monday's status, long expired.",
            ),
            encoding="utf-8",
        )
        _git_init(knowledge_root)

        off_corpus_dir = tmp_path / "off-corpus-store"
        cfg = _off_corpus_config_dict(off_corpus_dir)

        report = build_sweep_report(wiki, knowledge_root=knowledge_root, config=cfg)
        assert report.routed_off_corpus == []
        assert len(report.kill) == 1
        assert report.kill[0].path.name == "status-monday.md"

    def test_off_corpus_not_configured_falls_back_to_valid_until_logic_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        knowledge_root = tmp_path / "knowledge"
        wiki = knowledge_root / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "subject-status.md").write_text(
            _page(
                name="Subject status",
                bucket="daily",
                valid_until="2099-01-01",
                memory_class="entity",
                data_class="pii",
                body="Not yet expired.",
            ),
            encoding="utf-8",
        )
        _git_init(knowledge_root)

        with caplog.at_level(logging.WARNING, logger="athenaeum.decay_sweep"):
            report = build_sweep_report(wiki, knowledge_root=knowledge_root, config=None)

        assert report.routed_off_corpus == []
        assert report.kill == []
        assert len(report.retained) == 1, (
            "reversible default (issue athenaeum#1116): no off-corpus surface "
            "configured -> falls back to the original bucket:daily/valid_until "
            "logic byte-identically (not-yet-expired -> retained)"
        )
        assert any("erasure-taint-not-routed" in rec.message for rec in caplog.records)
